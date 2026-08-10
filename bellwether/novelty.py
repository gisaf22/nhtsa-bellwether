"""Is a pattern already covered by a published recall?

Thin v1 of the agent step: structured filter (recalls for a vehicle the
pattern actually has members on), embedding shortlist against
recall_embeddings, one LLM call for the verdict. Writes novelty_verdict and
novelty_recall_ref onto patterns.

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
    updates: list[tuple[str, str | None, int]] = []

    for pattern in patterns:
        candidates = _shortlist(pattern["id"], pattern["vehicles"])
        if not candidates:
            report.no_candidates += 1
            updates.append(("novel", None, pattern["id"]))
            report.verdicts["novel"] = report.verdicts.get("novel", 0) + 1
            continue

        prompt = _PROMPT.format(
            name=pattern["name"],
            vehicles=", ".join(f"{mk} {md}" for mk, md in pattern["vehicles"]),
            complaints="\n".join(f"- {s}" for s in _samples(pattern["id"])),
            recalls="\n".join(
                f"- {c['campaign_number']} ({c['vehicle']}, {c['component']}): "
                f"{(c['summary'] or '')[:600]} {(c['consequence'] or '')[:300]}"
                for c in candidates
            ),
        )
        try:
            answer = _ask_llm(prompt, session)
        except Exception as exc:  # noqa: BLE001 — one bad pattern must not stop the batch
            report.failed.append((pattern["id"], str(exc)[:160]))
            continue

        verdict = answer.get("verdict")
        if verdict not in VERDICTS:
            report.failed.append((pattern["id"], f"bad verdict {verdict!r}"))
            continue
        recall_ref = answer.get("recall") or None
        if verdict == "novel":
            recall_ref = None
        updates.append((verdict, recall_ref, pattern["id"]))
        report.verdicts[verdict] = report.verdicts.get(verdict, 0) + 1

    if updates:
        with lakebase.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "update patterns set novelty_verdict = %s, "
                    "novelty_recall_ref = %s, updated_at = now() where id = %s",
                    updates,
                )
            conn.commit()

    return report
