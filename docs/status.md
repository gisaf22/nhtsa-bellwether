# Status — handoff for new chat

## Progress: step 3.5 of 7

1. Ingest — done (20,848 complaints, 397 recalls)
2. Embed — done (raw); stripped re-embed in progress
3. Retrieval rule — settled (floor 0.78, cap 25)
4. Pattern formation — built, smoke-tested, not yet run on real data
5. Scoring — built, not yet run
6. Agent (name + novelty) — not started
7. App — not started

## Repo layout (bellwether/)

config, lakebase (OAuth token mint/refresh), schema (DDL), nhtsa_client,
spark_backfill (flat file, 2.2M rows via Spark), ingest (API incremental,
attributes via products[].productModel not search string), recalls,
textclean (boilerplate strip), embed (Spark-orchestrated), classify
(is_failure_report), patterns (formation + scoring)

## What's running now

Background job re-embedding stripped summaries, ~35–40 min left. On
completion, automatically: form_patterns() → score_patterns() → print top 20
by ratio with names/sizes/makes/5 sample summaries, then STOP for review.

## Retrieval — 3 approaches tested and rejected, evidence in docs/rejected-approaches.md

- Component scoping: UNKNOWN OR OTHER is 14% of corpus, unreachable island,
  kept a bad match and dropped the best one
- Global threshold: 9 vs 899 neighbours at same cutoff across seeds
- Global top-k: optimal k ranges 3–20+, non-monotonic, small k kills the
  cross-manufacturer matches that matter most
- Landed on loose floor + loose cap; precision deferred to pattern
  formation and human review, not retrieval

## Data quirks found (verify-don't-trust theme, useful for presentation)

- NHTSA silently ignores unrecognised query params — confirmed 3x
- HTTP 400 returns a "success" message body — status code is the only truth
- Body style isn't in the complaint records at all; fixed a 47%
  duplication bug caused by storing search string instead of
  products[].productModel
- Two date formats (complaints MM/DD/YYYY, recalls DD/MM/YYYY); flat file
  YYYYMMDD, must read as UTF-8
- ~1,600–2,300 complaints aren't failure reports (recall parts-wait
  grievances); excluded via is_failure_report, not deleted
- Investigations dropped — no vehicle-identifying fields; novelty runs on
  recalls only

## Working agreements

- Ingest raw, filter/transform downstream — cheap to revisit in SQL
- Verify every filter against a control before trusting it
- Terminal output only — no unrequested artifacts/documents
- Report what a fix costs, not just what it fixes
- Ship thin end-to-end path before polishing any stage

## Next after patterns run

Read the top-20 output first. Then: agent (name pattern, 3-stage novelty
check against recalls — structured filter → embedding shortlist → LLM
judgement), CDF Lakebase→Delta, Databricks App (ranked list, pattern
detail, acknowledge/watch/hide).

## Environment

Python 3.12.13, venv `.venv`. DATABRICKS_PROFILE=bellwether. Lakebase
Postgres 17.10 + pgvector, db databricks_postgres. Password is a
short-lived OAuth token — generate-database-credential does NOT work here;
lakebase.py mints from profile at call time, retries once on auth failure.