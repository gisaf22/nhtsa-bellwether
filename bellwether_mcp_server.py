"""MCP server exposing Bellwether's novelty check as agent-callable tools.

Two tools, deliberately non-overlapping: one retrieves candidate recalls
without judging them, one judges and persists a verdict. Both delegate
straight into bellwether.novelty — no retrieval, scoring or matching logic
is reimplemented here.

Intended for Databricks Playground / Agent Bricks. Run locally with:

    python bellwether_mcp_server.py

That serves HTTP on PORT (default 8000) with the MCP endpoint at /mcp,
which is also how it runs as a Databricks App. Set MCP_TRANSPORT=stdio to
run it over stdio instead, for a local client that expects a subprocess.

Requires the same Lakebase credentials as the rest of the project (see
.env.example): DATABRICKS_PROFILE locally, or the app's own service
principal when deployed.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from bellwether import novelty

mcp = FastMCP("bellwether")


@mcp.tool
def search_recalls_for_pattern(pattern_id: int) -> dict:
    """Find published NHTSA recalls that might cover a failure pattern.

    READ-ONLY. Retrieves candidates and nothing else — it does not judge
    whether any of them actually covers the pattern, and it writes nothing.
    Use this to show a user what recalls exist near a pattern, or to inspect
    the evidence before deciding whether to run check_novelty. If the user
    wants an actual answer on coverage, use check_novelty instead.

    How candidates are chosen: recalls are first restricted to vehicles the
    pattern actually has complaints on (make and model; model year is
    deliberately NOT required to match, since patterns span 2020-2024 and
    the same defect usually does too), then ranked by cosine similarity
    between the recall text and the pattern's average complaint embedding.
    Only the closest few are returned, one row per recall campaign.

    On success returns a dict with status="ok" and these fields:
      - pattern_id: the id that was searched.
      - pattern_name: human-readable name, e.g.
        "HONDA CIVIC +10 other — STEERING".
      - vehicles: list of "MAKE MODEL" strings the pattern has members on.
      - candidate_count: how many candidates were found. Zero is a valid
        answer, not an error — it means no recall exists for any of this
        pattern's vehicles, which is itself strong evidence the pattern is
        novel.
      - candidates: list, most similar first. Each has campaign_number (the
        NHTSA campaign id, e.g. "24V744000"), vehicle ("MAKE MODEL YEAR" the
        recall names), component (NHTSA component string), summary (the
        recall's description), consequence (what the defect can cause), and
        similarity (0-1 cosine similarity to the pattern). Treat similarity
        as a ranking aid only: a high score means "reads similarly", NOT
        "covers this failure". Only check_novelty decides coverage.

    On failure returns status="error" and a message, without raising. That
    happens when the pattern id does not exist ("no pattern with id ...") or
    the database is unreachable.

    Args:
        pattern_id: The pattern's integer id, as shown in the Bellwether app.
    """
    try:
        found = novelty.shortlist_recalls(pattern_id)
    except Exception as exc:  # noqa: BLE001 — a tool must return, never raise
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok",
        "pattern_id": pattern_id,
        "pattern_name": found["name"],
        "vehicles": [f"{make} {model}" for make, model in found["vehicles"]],
        "candidate_count": len(found["candidates"]),
        "candidates": found["candidates"],
    }


@mcp.tool
def check_novelty(pattern_id: int) -> dict:
    """Decide whether a failure pattern is already covered by a recall, and
    SAVE that verdict to the database.

    THIS TOOL WRITES. It overwrites `patterns.novelty_verdict` and
    `patterns.novelty_recall_ref` for this pattern, and the new verdict is
    what the Bellwether app will show from then on. The previous verdict is
    preserved in the novelty_history table (tagged source="mcp_tool"), so a
    change is auditable — but the app itself only ever displays the current
    value, and there is no in-app undo. Do not call this speculatively or in
    a loop over many patterns; call it when a user actually wants the verdict
    (re)computed. To look at the evidence without changing anything, use
    search_recalls_for_pattern instead.

    What it does: retrieves candidate recalls exactly as
    search_recalls_for_pattern does, then makes one LLM call asking whether
    the pattern's failure is covered, judging on the failure mechanism rather
    than on vehicle overlap — a recall for a different component on the same
    vehicle does not count as coverage. If no candidate recalls exist at all,
    the verdict is "novel" without an LLM call.

    IMPORTANT CAVEAT, worth passing on to the user with any "known" verdict:
    coverage is vehicle-scoped, not date-scoped. Nothing compares dates, so a
    pattern can come back "known" on the strength of a recall issued AFTER
    the complaints that formed it — which would actually suggest the recall's
    remedy did not work, rather than that the problem was already handled.
    Read "known" as "a recall exists for this failure on this vehicle", not
    "this was already known when it emerged".

    On success returns a dict with status="ok" and these fields:
      - pattern_id / pattern_name: which pattern was assessed.
      - verdict: "novel" (no candidate recall describes this failure),
        "known" (a recall covers this failure for these vehicles), or
        "partially_covered" (a recall covers it for only some of the
        vehicles, or only part of the failure).
      - recall_ref: the NHTSA campaign number the verdict rests on, or null
        when the verdict is "novel".
      - reason: one sentence from the model explaining the verdict.
      - candidates_considered: how many recalls were weighed. Zero means the
        "novel" verdict was reached with no LLM call at all.
      - previous_verdict / previous_recall_ref: what this call replaced, or
        null if the pattern had never been assessed before.
      - changed: true when the verdict or its recall reference actually
        differs from what was stored before. False means the reassessment
        confirmed the existing verdict.

    On failure returns status="error" and a message, without raising. That
    happens when the pattern id does not exist, the LLM endpoint is
    unreachable or returns an unparseable answer, the model returns a verdict
    outside the three allowed values, or the database is unreachable. On any
    error nothing is written and the stored verdict is left exactly as it was.

    Args:
        pattern_id: The pattern's integer id, as shown in the Bellwether app.
    """
    try:
        result = novelty.assess_one(pattern_id, source="mcp_tool")
    except Exception as exc:  # noqa: BLE001 — a tool must return, never raise
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok",
        "pattern_id": result["pattern_id"],
        "pattern_name": result["name"],
        "verdict": result["verdict"],
        "recall_ref": result["recall_ref"],
        "reason": result["reason"],
        "candidates_considered": result["candidates_considered"],
        "previous_verdict": result["previous_verdict"],
        "previous_recall_ref": result["previous_recall_ref"],
        "changed": result["changed"],
    }


if __name__ == "__main__":
    # Databricks Apps injects PORT and expects the process to bind it on
    # 0.0.0.0; the agent then registers the /mcp path on the app's URL.
    if os.getenv("MCP_TRANSPORT", "http") == "stdio":
        mcp.run()
    else:
        mcp.run(
            transport="http",
            host="0.0.0.0",
            port=int(os.getenv("PORT", "8000")),
            path="/mcp",
        )
