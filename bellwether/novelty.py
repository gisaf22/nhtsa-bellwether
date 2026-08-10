"""Is a pattern already covered by a published recall?

Thin v1 of the agent step: structured filter (recalls for a vehicle the
pattern actually has members on), embedding shortlist against
recall_embeddings, one LLM call for the verdict. Writes novelty_verdict and
novelty_recall_ref onto patterns, preserving any replaced verdict in
novelty_history.

`assess_one()` is the single-pattern unit; `assess_all()` loops it and the
MCP tool calls it directly, so there is one implementation of
retrieve → judge → write rather than one per caller.

Two known limitations, deliberate for now:
  - No recall-date scoping. A pattern can be called 'known' on the strength
    of a recall issued after the complaints that formed it.
  - model_year is not required to match. Patterns span 2020-2024 and the
    same defect usually spans the same range, so requiring equality would
    reject the true match more often than it would prevent a false one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

import numpy as np
import requests

from . import lakebase
from .embed import WORKSPACE_HOST

LLM_ENDPOINT = os.getenv("BELLWETHER_LLM_ENDPOINT", "databricks-llama-4-maverick")
SHORTLIST_SIZE = 5
SAMPLE_SUMMARIES = 4
MAX_SUMMARY_CHARS = 900
REQUEST_TIMEOUT = 120

VERDICTS = ("novel", "known", "partially_covered")

_PROMPT = """You are triaging emerging vehicle failure patterns against \
published NHTSA recalls.

PATTERN: {name}
Vehicles affected: {vehicles}
Representative owner complaints:
{complaints}

CANDIDATE RECALLS:
{recalls}

Decide whether this pattern's failure is already covered by one of the \
candidate recalls. Judge on the failure mechanism described, not on the \
vehicle matching alone — a recall for a different component on the same \
vehicle does NOT cover this pattern.

Answer with JSON only:
{{"verdict": "novel" | "known" | "partially_covered", "recall": "<campaign \
number, or null>", "reason": "<one sentence>"}}

Use "known" when a candidate recall covers this failure for the affected \
vehicles, "partially_covered" when it covers the failure for only some of \
them or only part of the failure, and "novel" when none of the candidates \
describes this failure."""


def _parse_vector(raw: str) -> np.ndarray:
    return np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)


@dataclass
class NoveltyReport:
    patterns: int = 0
    no_candidates: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    failed: list[tuple[int, str]] = field(default_factory=list)


def _load_patterns() -> list[dict]:
    rows = lakebase.execute(
        """
        select p.id, p.name,
               array_agg(distinct c.make || '|' || c.model) as vehicles
        from patterns p
        join pattern_members pm on pm.pattern_id = p.id
        join complaints c on c.odi_number = pm.odi_number
        group by p.id, p.name
        order by p.ratio desc nulls last, p.id
        """,
        fetch="all",
    )
    return [
        {
            "id": r[0],
            "name": r[1],
            "vehicles": [tuple(v.split("|", 1)) for v in r[2]],
        }
        for r in rows
    ]


def _centroid(pattern_id: int) -> np.ndarray | None:
    rows = lakebase.execute(
        """
        select e.embedding_stripped
        from pattern_members pm
        join complaint_embeddings e on e.odi_number = pm.odi_number
        where pm.pattern_id = %s and e.embedding_stripped is not null
        """,
        (pattern_id,),
        fetch="all",
    )
    if not rows:
        return None
    vectors = np.array([_parse_vector(r[0]) for r in rows])
    centroid = vectors.mean(axis=0)
    norm = np.linalg.norm(centroid)
    return centroid / norm if norm else centroid


def _shortlist(pattern_id: int, vehicles: list[tuple[str, str]]) -> list[dict]:
    centroid = _centroid(pattern_id)
    if centroid is None:
        return []
    makes = [v[0] for v in vehicles]
    models = [v[1] for v in vehicles]
    rows = lakebase.execute(
        """
        select * from (
        -- One row per campaign: a campaign covers many (model, year) rows and
        -- without this the shortlist fills with repeats of one recall.
        select distinct on (r.campaign_number)
               r.campaign_number, r.make, r.model, r.model_year, r.component,
               r.summary, r.consequence,
               1 - (e.embedding <=> %s::vector) as similarity
        from recall_embeddings e
        join recalls r
          on r.campaign_number = e.campaign_number and r.make = e.make
         and r.model = e.model and r.model_year = e.model_year
        where (r.make, r.model) in (
            select * from unnest(%s::text[], %s::text[])
        )
        order by r.campaign_number, e.embedding <=> %s::vector
        ) ranked order by similarity desc limit %s
        """,
        (str(list(map(float, centroid))), makes, models,
         str(list(map(float, centroid))), SHORTLIST_SIZE),
        fetch="all",
    )
    return [
        {
            "campaign_number": r[0],
            "vehicle": f"{r[1]} {r[2]} {r[3]}",
            "component": r[4],
            "summary": r[5],
            "consequence": r[6],
            "similarity": r[7],
        }
        for r in rows
    ]


def _load_pattern(pattern_id: int) -> dict | None:
    """One pattern's id, name and distinct member vehicles."""
    row = lakebase.execute(
        """
        select p.id, p.name,
               array_agg(distinct c.make || '|' || c.model) as vehicles
        from patterns p
        join pattern_members pm on pm.pattern_id = p.id
        join complaints c on c.odi_number = pm.odi_number
        where p.id = %s
        group by p.id, p.name
        """,
        (pattern_id,),
        fetch="one",
    )
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "vehicles": [tuple(v.split("|", 1)) for v in row[2]],
    }


def shortlist_recalls(pattern_id: int) -> dict:
    """Candidate recalls for one pattern — retrieval only, no LLM, no write.

    The structured filter plus embedding shortlist half of the novelty check,
    exposed on its own so it can be called without triggering a judgement.
    `assess_one()` calls this too, so there is one implementation of the
    retrieval step rather than one per caller.

    Raises LookupError if the pattern does not exist, which is distinct from
    it existing with no candidate recalls (empty `candidates`).
    """
    pattern = _load_pattern(pattern_id)
    if pattern is None:
        raise LookupError(f"no pattern with id {pattern_id}")
    return {**pattern, "candidates": _shortlist(pattern_id, pattern["vehicles"])}


def _samples(pattern_id: int) -> list[str]:
    rows = lakebase.execute(
        """
        select coalesce(c.summary_stripped, c.summary)
        from pattern_members pm
        join complaints c on c.odi_number = pm.odi_number
        where pm.pattern_id = %s
        order by pm.similarity desc
        limit %s
        """,
        (pattern_id, SAMPLE_SUMMARIES),
        fetch="all",
    )
    return [r[0][:MAX_SUMMARY_CHARS] for r in rows]


def _ask_llm(prompt: str, session: requests.Session) -> dict:
    url = f"{WORKSPACE_HOST}/serving-endpoints/{LLM_ENDPOINT}/invocations"
    for attempt in (0, 1):
        response = session.post(
            url,
            headers={"Authorization": f"Bearer {lakebase.mint_token(force=attempt == 1)}"},
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                # Generous: the model often writes a paragraph of reasoning
                # before the JSON, and a tight cap truncates it mid-preamble.
                "max_tokens": 900,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code in (401, 403) and attempt == 0:
            continue
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"]
        # raw_decode rather than json.loads on a greedy brace match: the model
        # sometimes emits prose, then the object, then more prose, which
        # loads() rejects as trailing "Extra data".
        start = text.find("{")
        if start == -1:
            raise ValueError(f"no JSON in response: {text[:200]}")
        return json.JSONDecoder().raw_decode(text[start:])[0]
    raise RuntimeError("LLM endpoint rejected the credential twice")


def _write_verdict(
    pattern_id: int,
    verdict: str,
    recall_ref: str | None,
    *,
    source: str,
) -> tuple[str | None, str | None]:
    """Persist a verdict, preserving any prior one in novelty_history.

    Returns the values that were replaced, so callers can report what
    changed. History is only written when a prior verdict existed — a
    first-ever assessment overwrites nothing and has nothing to preserve.

    The read and both writes share one transaction: a verdict must never be
    replaced without its predecessor being recorded, and `for update` holds
    the row so two concurrent callers cannot interleave and lose one.
    """
    with lakebase.connect() as conn:
        row = conn.execute(
            "select novelty_verdict, novelty_recall_ref from patterns "
            "where id = %s for update",
            (pattern_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"no pattern with id {pattern_id}")
        old_verdict, old_recall_ref = row

        if old_verdict is not None:
            conn.execute(
                """
                insert into novelty_history (
                    pattern_id, old_verdict, old_recall_ref,
                    new_verdict, new_recall_ref, source
                ) values (%s, %s, %s, %s, %s, %s)
                """,
                (pattern_id, old_verdict, old_recall_ref, verdict,
                 recall_ref, source),
            )

        conn.execute(
            "update patterns set novelty_verdict = %s, novelty_recall_ref = %s, "
            "updated_at = now() where id = %s",
            (verdict, recall_ref, pattern_id),
        )
        conn.commit()

    return old_verdict, old_recall_ref


def assess_one(
    pattern_id: int,
    *,
    source: str = "batch",
    session: requests.Session | None = None,
) -> dict:
    """Retrieve, judge and persist a novelty verdict for one pattern.

    The single-pattern unit that `assess_all()` runs in a loop and the MCP
    tool calls directly, so both paths share one implementation of
    retrieve → judge → write.

    Overwrites any existing verdict, preserving the previous value in
    novelty_history tagged with `source`.

    Raises LookupError for an unknown pattern, ValueError if the model
    returns something outside VERDICTS, and whatever _ask_llm raises when the
    endpoint fails — callers decide whether one bad pattern is fatal.
    """
    found = shortlist_recalls(pattern_id)
    candidates = found["candidates"]

    if not candidates:
        # No recall exists for any vehicle this pattern covers, so there is
        # nothing for a model to weigh — novel by construction, and asking
        # anyway would just invite an answer with no evidence behind it.
        verdict, recall_ref = "novel", None
        reason = "no published recall for any vehicle in this pattern"
    else:
        prompt = _PROMPT.format(
            name=found["name"],
            vehicles=", ".join(f"{mk} {md}" for mk, md in found["vehicles"]),
            complaints="\n".join(f"- {s}" for s in _samples(pattern_id)),
            recalls="\n".join(
                f"- {c['campaign_number']} ({c['vehicle']}, {c['component']}): "
                f"{(c['summary'] or '')[:600]} {(c['consequence'] or '')[:300]}"
                for c in candidates
            ),
        )
        answer = _ask_llm(prompt, session or requests.Session())
        verdict = answer.get("verdict")
        if verdict not in VERDICTS:
            raise ValueError(f"model returned an unknown verdict: {verdict!r}")
        recall_ref = answer.get("recall") or None
        if verdict == "novel":
            recall_ref = None
        reason = answer.get("reason")

    previous_verdict, previous_recall_ref = _write_verdict(
        pattern_id, verdict, recall_ref, source=source
    )

    return {
        "pattern_id": pattern_id,
        "name": found["name"],
        "verdict": verdict,
        "recall_ref": recall_ref,
        "reason": reason,
        "candidates_considered": len(candidates),
        "previous_verdict": previous_verdict,
        "previous_recall_ref": previous_recall_ref,
        "changed": previous_verdict is not None
        and (previous_verdict, previous_recall_ref) != (verdict, recall_ref),
    }


def assess_all(*, only_missing: bool = True, limit: int | None = None) -> NoveltyReport:
    patterns = _load_patterns()
    if only_missing:
        done = {
            r[0]
            for r in lakebase.execute(
                "select id from patterns where novelty_verdict is not null",
                fetch="all",
            )
        }
        patterns = [p for p in patterns if p["id"] not in done]
    if limit is not None:
        patterns = patterns[:limit]

    report = NoveltyReport(patterns=len(patterns))
    session = requests.Session()

    # Writes are now per-pattern rather than one executemany at the end. That
    # is more round trips, but a run that dies partway keeps the verdicts it
    # already earned instead of discarding all of them — and only_missing
    # makes the rerun cheap.
    for pattern in patterns:
        try:
            result = assess_one(pattern["id"], source="batch", session=session)
        except Exception as exc:  # noqa: BLE001 — one bad pattern must not stop the batch
            report.failed.append((pattern["id"], str(exc)[:160]))
            continue

        if result["candidates_considered"] == 0:
            report.no_candidates += 1
        verdict = result["verdict"]
        report.verdicts[verdict] = report.verdicts.get(verdict, 0) + 1

    return report
