"""is_failure_report: does this complaint describe a vehicle failure?

Checked in order:

1. CMPL_TYPE (flat-file field 21, the complaint's submission channel — VOQ,
   EWR, RC, ...). Checked and rejected as a signal here: only 290 of 20,848
   complaints (1.4%) are still attributed to a flat-sourced row after the
   full API refresh overwrote `source`, and even among those, CMPL_TYPE is
   'IVOQ'/'EVOQ' (channel: web vs phone) for all of them — it does not
   distinguish a failure report from a parts-availability complaint. The
   flat file's own recall-specific codes (RC, RP) never appeared.

2. Marker phrases. NHTSA's ODI system emits one fixed sentence for a specific
   non-failure case: a complaint filed solely because a recall's parts were
   not yet available, where the owner explicitly has not experienced the
   underlying failure. That sentence is exact and unvarying —
   "The contact had not experienced a failure." Checked five near-miss
   phrasings; four had zero matches and the fifth had one, confirming this is
   the sole template NHTSA's system uses for the case, not one of several.
   Every complaint carrying it was hand-checked and is pure template with no
   failure narrative (see docs/rejected-approaches.md sibling findings for
   the verification method).

This does not flag short or low-content summaries ("Unknown", "Oil leak") —
that is a content-quality question, already handled by MIN_USEFUL_CHARS
downstream, not a signal that the complaint isn't about a failure.
"""

from __future__ import annotations

import re

# Exact template sentence, plus the one near-variant seen in the corpus.
# Anchored loosely (not to sentence boundaries) since it is a strong enough
# phrase that no other complaint content produces it by coincidence.
_NON_FAILURE_MARKERS = (
    re.compile(r"\bhad not experienced a failure\b", re.I),
    re.compile(r"\bdid not experience a failure\b", re.I),
)


def is_failure_report(summary: str) -> bool:
    if not summary:
        return False
    return not any(rx.search(summary) for rx in _NON_FAILURE_MARKERS)


def classify_all() -> dict[str, int]:
    """Set complaints.is_failure_report for every unclassified row."""
    from . import lakebase

    rows = lakebase.execute(
        "select odi_number, summary from complaints where is_failure_report is null",
        fetch="all",
    )
    updates = [(is_failure_report(s), odi) for odi, s in rows]

    with lakebase.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "update complaints set is_failure_report=%s where odi_number=%s",
                updates,
            )
        conn.commit()

    return {
        "classified": len(updates),
        "failure_report": sum(1 for v, _ in updates if v),
        "non_failure_report": sum(1 for v, _ in updates if not v),
    }
