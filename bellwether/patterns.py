"""Pattern formation and scoring.

Formation is single-linkage growth, in ascending odi_number order: for each
unassigned failure report, retrieve its neighbours (floor SIMILARITY_FLOOR,
cap NEIGHBOUR_CAP, on the boilerplate-stripped embedding), and join the
existing pattern its neighbours support most, or seed a new one if none of
its neighbours belong to a pattern yet. A pattern below MIN_PATTERN_MEMBERS
is discarded rather than persisted — its complaints stay unassigned.

Deliberately no make/model filter here: patterns.make/model do not exist —
see schema.py's create_patterns for why — so a pattern is free to span
vehicles if the failure does. Nothing about retrieval below scopes by
vehicle; docs/rejected-approaches.md is the record of why that was tried
and rejected for retrieval specifically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from . import config, lakebase

MIN_PATTERN_MEMBERS = 5

# Scoring windows. Deliberately distinct from config.RECENT_WINDOW_DAYS/
# BASELINE_WINDOW_DAYS (90/365) — this step's spec calls for a 60-day recent
# window specifically, over a trailing baseline. "Same model years" is
# automatic here: baseline and recent both come from the pattern's own
# members, whose vehicles don't change between the two windows.
RECENT_DAYS = 60
BASELINE_DAYS = 365


@dataclass
class Neighbour:
    odi_number: int
    similarity: float


@dataclass
class FormationReport:
    pool_size: int = 0
    patterns_formed: int = 0
    patterns_discarded_below_min: int = 0
    members_assigned: int = 0
    members_unassigned: int = 0


def _load_pool() -> tuple[list[int], np.ndarray]:
    """Failure-report complaints with a stripped embedding, odi_number order."""
    rows = lakebase.execute(
        """
        select c.odi_number, e.embedding_stripped
        from complaints c
        join complaint_embeddings e on e.odi_number = c.odi_number
        where c.is_failure_report = true and e.embedding_stripped is not null
        order by c.odi_number
        """,
        fetch="all",
    )
    odis = [r[0] for r in rows]
    # pgvector returns a string like '[0.1,0.2,...]'; parsed directly rather
    # than pulling in the pgvector Python adapter for a one-off batch load.
    vectors = np.array(
        [
            [float(x) for x in r[1].strip("[]").split(",")]
            for r in rows
        ],
        dtype=np.float32,
    )
    # Cosine similarity via dot product on L2-normalised rows.
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors /= norms
    return odis, vectors


def _neighbours_by_index(
    vectors: np.ndarray, floor: float, cap: int, block: int = 500
) -> list[list[tuple[int, float]]]:
    """For every row, up to `cap` neighbours (index, similarity) >= floor.

    Computed once, up front, in blocks — neighbour identity depends only on
    the static embeddings, not on cluster assignment, so this is independent
    of the sequential formation pass that follows.
    """
    n = vectors.shape[0]
    out: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for start in range(0, n, block):
        end = min(start + block, n)
        sims = vectors[start:end] @ vectors.T  # (block, n)
        for local_i, global_i in enumerate(range(start, end)):
            row = sims[local_i]
            row[global_i] = -1.0  # exclude self
            candidates = np.where(row >= floor)[0]
            if candidates.size == 0:
                out[global_i] = []
                continue
            ranked = candidates[np.argsort(-row[candidates])][:cap]
            out[global_i] = [(int(j), float(row[j])) for j in ranked]
    return out


def form_patterns(
    *,
    floor: float = config.SIMILARITY_FLOOR,
    cap: int = config.NEIGHBOUR_CAP,
    min_members: int = MIN_PATTERN_MEMBERS,
) -> FormationReport:
    """Cluster failure-report complaints into patterns and persist them.

    Replaces any existing patterns — this is formation from scratch, not an
    incremental update; there is no online/re-run merge logic yet.
    """
    odis, vectors = _load_pool()
    n = len(odis)
    report = FormationReport(pool_size=n)
    if n == 0:
        return report

    neighbours = _neighbours_by_index(vectors, floor, cap)

    # assigned[i] = cluster index, or -1. clusters[c] = list of member
    # indices in join order; join_sim[i] = similarity that justified i's join
    # (1.0 for a pattern's founding member, by convention: maximally similar
    # to itself, and there is no "existing pattern" to score it against yet).
    assigned = np.full(n, -1, dtype=np.int64)
    clusters: list[list[int]] = []
    join_sim: list[float] = [0.0] * n

    for i in range(n):
        candidate_patterns: dict[int, list[float]] = {}
        for j, sim in neighbours[i]:
            c = assigned[j]
            if c != -1:
                candidate_patterns.setdefault(int(c), []).append(sim)

        if candidate_patterns:
            # Most-supported pattern wins; ties broken by strongest single match.
            best_cluster = max(
                candidate_patterns,
                key=lambda c: (len(candidate_patterns[c]), max(candidate_patterns[c])),
            )
            assigned[i] = best_cluster
            clusters[best_cluster].append(i)
            join_sim[i] = max(candidate_patterns[best_cluster])
        else:
            new_cluster = len(clusters)
            clusters.append([i])
            assigned[i] = new_cluster
            join_sim[i] = 1.0

    kept_clusters = [c for c in clusters if len(c) >= min_members]
    report.patterns_discarded_below_min = len(clusters) - len(kept_clusters)
    report.members_assigned = sum(len(c) for c in kept_clusters)
    report.members_unassigned = n - report.members_assigned

    with lakebase.connect() as conn:
        conn.execute("truncate table pattern_members")
        conn.execute("truncate table patterns restart identity cascade")

        for member_indices in kept_clusters:
            member_odis = [odis[idx] for idx in member_indices]
            name = _provisional_name(conn, member_odis)
            pattern_id = conn.execute(
                "insert into patterns (name, member_count) values (%s, %s) returning id",
                (name, len(member_indices)),
            ).fetchone()[0]
            with conn.cursor() as cur:
                cur.executemany(
                    "insert into pattern_members (pattern_id, odi_number, similarity) "
                    "values (%s, %s, %s)",
                    [
                        (pattern_id, odis[idx], join_sim[idx])
                        for idx in member_indices
                    ],
                )
        conn.commit()

    report.patterns_formed = len(kept_clusters)
    return report


def _provisional_name(conn, member_odis: list[int]) -> str:
    """A deterministic placeholder, not the semantic name the brief assigns to
    an LLM agent step (not yet built). Built from the dominant vehicle and
    the dominant full component string among members, so the list is
    scannable before that step exists.
    """
    rows = conn.execute(
        "select make, model, components from complaints where odi_number = any(%s)",
        (member_odis,),
    ).fetchall()

    from collections import Counter

    vehicle_counts = Counter((mk, md) for mk, md, _ in rows)
    top_vehicle, vehicle_n = vehicle_counts.most_common(1)[0]
    vehicle_label = (
        f"{top_vehicle[0]} {top_vehicle[1]}"
        if vehicle_n == len(rows)
        else f"{top_vehicle[0]} {top_vehicle[1]} +{len(vehicle_counts) - 1} other"
    )

    comp_counts = Counter()
    for _, _, comps in rows:
        for c in comps or []:
            comp_counts[c] += 1
    top_component = comp_counts.most_common(1)[0][0] if comp_counts else "uncategorised"

    return f"{vehicle_label} — {top_component}"


# --- Scoring ----------------------------------------------------------------


# Severity is a deterministic bucket off `ratio`, not an LLM judgement of harm.
# It says "this pattern's recent rate is N times its own baseline", nothing
# about injury or crash risk — the name is the brief's, the meaning is narrow.
SEVERITY_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (4.0, "high"),
    (2.0, "medium"),
    (1.0, "low"),
)


def severity_for(ratio: float | None) -> str | None:
    if ratio is None:
        return None
    for threshold, label in SEVERITY_THRESHOLDS:
        if ratio >= threshold:
            return label
    return "none"


@dataclass
class ScoringReport:
    patterns_scored: int = 0
    patterns_with_ratio: int = 0
    patterns_baseline_zero: int = 0


def score_patterns(*, today: date | None = None) -> ScoringReport:
    """Recent (last RECENT_DAYS) rate vs the pattern's own trailing baseline.

    Ratio, not a verdict: (recent_count / RECENT_DAYS) / (baseline_count /
    BASELINE_DAYS), both windows measured over date_of_incident. NULL when
    the baseline window has zero incidents — nothing to divide by, not a
    ratio of 0.

    This is not exposure-normalised (no vehicles-in-service data exists in
    this project yet), so it does not yet distinguish "rate is rising" from
    "this is a popular vehicle whose complaint count rises with everything
    else" — the brief names that failure mode explicitly. Treat the ratio as
    a ranking signal, not a defect signal.
    """
    today = today or date.today()
    recent_start = today - timedelta(days=RECENT_DAYS)
    baseline_start = recent_start - timedelta(days=BASELINE_DAYS)

    pattern_ids = [
        r[0] for r in lakebase.execute("select id from patterns", fetch="all")
    ]
    report = ScoringReport(patterns_scored=len(pattern_ids))

    updates = []
    for pattern_id in pattern_ids:
        recent, baseline = lakebase.execute(
            """
            select
                count(*) filter (where c.date_of_incident >= %s and c.date_of_incident <= %s),
                count(*) filter (where c.date_of_incident >= %s and c.date_of_incident < %s)
            from pattern_members pm
            join complaints c on c.odi_number = pm.odi_number
            where pm.pattern_id = %s and c.date_of_incident is not null
            """,
            (recent_start, today, baseline_start, recent_start, pattern_id),
            fetch="one",
        )
        if baseline == 0:
            ratio = None
            report.patterns_baseline_zero += 1
        else:
            ratio = (recent / RECENT_DAYS) / (baseline / BASELINE_DAYS)
            report.patterns_with_ratio += 1
        updates.append((recent, baseline, ratio, severity_for(ratio), pattern_id))

    with lakebase.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                update patterns
                set recent_count = %s, baseline_count = %s, ratio = %s,
                    severity = %s, updated_at = now()
                where id = %s
                """,
                updates,
            )
        conn.commit()

    return report
