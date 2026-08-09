# nhtsa-bellwether — project brief

## The problem

Vehicle owners file complaints with NHTSA when something goes wrong. The
complaints are public and free. Each carries structured fields — make, model,
model year, component category, dates, and crash and injury flags — alongside a
few hundred characters of the owner's own description of what happened.

The structured fields tell you which vehicle and which broad component. The
description is where the specific failure is, and it is written in the owner's
own words rather than to any standard.

So finding a particular failure across many complaints means working from the
descriptions, and grouping them means comparing what they mean rather than how
they are worded.

## What this builds

A pipeline that ingests complaints, groups them into failure patterns by
semantic similarity, scores each pattern against an age-matched baseline,
checks whether anyone is already investigating it, and surfaces what's left.

## Why AI is required

The grouping is the reason. Complaints describing the same failure are written
in each owner's own words, so deciding that two of them describe the same thing
means comparing meaning rather than wording.

Two further steps need language comprehension:

- **Naming a pattern** — reading a group of complaints and writing the single
  failure they describe, so the list is readable rather than numbered.
- **Novelty checking** — comparing a pattern against published recalls and open
  investigations to see whether it is already known. Recall text is written in
  regulatory language and complaints in owners' language, so the comparison is
  about meaning rather than shared wording.

Everything else — rates, baselines, ranking — is deterministic by design.

## User and value

**User:** someone tracking vehicle reliability signals — a quality or
reliability engineer at a manufacturer or supplier.

**Value:** the complaints are public, but a specific failure pattern only
becomes visible once the narratives are grouped by meaning. This builds that
grouping and ranks the results by how far a pattern's recent rate sits above
its age-matched baseline.

The ranking directs attention. It does not assert that anything is a defect.

## Workflow

1. Discover which make / model / model-year combinations have complaints.
2. Ingest complaints on a schedule. Full refresh per combination, diff
   client-side on `odiNumber`.
3. Embed narratives. Match each new complaint to an existing pattern above a
   similarity threshold, or seed a new one.
4. Recompute each affected pattern: recent count, rate per vehicles in service,
   ratio against the age-matched baseline.
5. Agent names the pattern and checks it against published recalls and open
   investigations.
6. Patterns that are elevated and not already known are surfaced. The user
   acknowledges, watches, or hides.
7. Pattern state changes and user decisions flow through change data capture
   into the analytical layer.

## How success is measured

**Pattern coherence** — read the groups. Do the complaints in a pattern
describe the same failure? Judged by eye on a sample, and it decides whether
anything else matters.

**Ranking sanity** — does the top of the list contain genuine signals, or just
the best-selling vehicles? Without exposure normalisation it will be the
latter.

**Suppression accuracy** — of patterns the agent marked already known, how many
actually match the cited recall or investigation.

**Threshold tradeoff** — at several similarity thresholds, how many coherent
patterns versus incoherent ones. Choose one deliberately and show the curve.

### Failure modes to watch

- Groups cohering around shared vocabulary rather than shared failure.
- Ranking that tracks sales volume — exposure normalisation is broken.
- Rates rising because the fleet is ageing — the baseline is not age-matched.

## Scope

50 combinations: 10 models across 5 model-years. Roughly 25,000 complaints,
history from 2022 onward.

Backfill discards the most recent 30 days, then the scheduled job ingests them
normally — so patterns visibly grow and change state, and the change feed
carries real events rather than one bulk load.

## MVP

- Scheduled ingest from NHTSA into Lakebase, with client-side change detection
- Change data capture from Lakebase into the analytical layer
- Spark: batch embedding of the backfill, and per-pattern rate and baseline
  computation each run
- Vector search over complaint narratives
- Agent with tools — retrieves recalls and open investigations, names the
  pattern, writes its verdict and the pattern's state back to the database
- Databricks App — ranked pattern list, pattern detail with member narratives
  and rate curve, and acknowledge / watch / hide

## Cut list, in order

1. Performance charts
2. Cohort comparison across model-years
3. Filing-lag display

## Data notes

- NHTSA silently ignores unrecognised parameters — a deliberately nonsensical
  parameter returned the same row count as a real one. Incremental ingest
  therefore diffs client-side rather than trusting a server-side filter.
- No pagination. A combination's entire complaint set arrives in one response.
- Two date fields: `dateOfIncident` drives the rate maths, `dateComplaintFiled`
  drives the ingest watermark.
- Partial VIN excluded at ingest — no use for it.
- Incident dates are self-reported. Impossible dates are rejected and counted.