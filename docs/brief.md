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
checks whether it is already covered by a published recall, and surfaces
what's left.

## Why AI is required

The grouping is the reason. Complaints describing the same failure are written
in each owner's own words, so deciding that two of them describe the same thing
means comparing meaning rather than wording.

Two further steps need language comprehension:

- **Naming a pattern** — reading a group of complaints and writing the single
  failure they describe, so the list is readable rather than numbered.
- **Novelty checking** — comparing a pattern against published recalls to see
  whether it is already known. Recall text is written in regulatory language
  and complaints in owners' language, so the comparison is about meaning
  rather than shared wording.

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
5. Agent names the pattern and checks it against published recalls.
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
actually match the cited recall.

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
- Agent with tools — retrieves recalls, names the pattern, writes its verdict
  and the pattern's state back to the database
- Databricks App — ranked pattern list, pattern detail with member narratives
  and rate curve, and acknowledge / watch / hide

## Cut list, in order

1. Performance charts
2. Cohort comparison across model-years
3. Filing-lag display

## Data notes

- **Investigations were evaluated and excluded.** `/investigations` is the only
  working path, and its records carry no vehicle identifiers at all — no make,
  model or model year, only a subject line and an HTML description. There is
  therefore no structured way to tie an investigation to a combination, and
  text-only matching against prose is too weak to decide suppression, which is
  a call about whether to hide a signal from the user. Novelty is built on
  recalls alone, which do carry `Make`, `Model`, `ModelYear`, `Component`,
  `Summary` and `Consequence`.
- NHTSA silently ignores unrecognised parameters — a deliberately nonsensical
  parameter returned the same row count as a real one. Incremental ingest
  therefore diffs client-side rather than trusting a server-side filter.
  Confirmed a second time on `/investigations`, which ignores `make`, `model`
  and `modelYear` outright: the total is identical with and without them.
- No pagination on complaints or recalls — a combination's entire set arrives
  in one response. (`/investigations` does paginate via `meta.pagination`, but
  it is out of scope; see above.)
- HTTP 400 carries a success message in the body. An unrecognised model
  returns status 400 alongside `"message": "Results returned successfully"`
  and an empty result set. The status code is the only truth; 400 is never
  retried, and a 400 means the model string is wrong rather than the vehicle
  having no complaints.
- **Complaint records do not carry body style.** The discovery endpoint's
  vocabulary has `F-150  SUPER CREW`, `F-150  REGULAR CAB` and so on, but no
  complaint is attributed to any of them: querying all three returns
  byte-identical result sets, and every record's `products[].productModel`
  reads plain `F-150` — matching `MODELTXT` in the flat file, which likewise
  has only `F-150`, `F-150 HYBRID` and `F-150 LIGHTNING BEV`.
  Those strings are therefore **search keys, not vehicles**: the endpoint
  rejects `F-150` with a 400, so one is needed to reach the records, but
  attribution comes from `productModel`. Using the query string as the model
  produced 47% phantom duplication. Powertrain variants are real and disjoint
  (`RAV4` / `RAV4 HYBRID` / `RAV4 PRIME` share no complaints), so every
  accepted key is still queried and results deduped on `odiNumber`.
- **Backfill runs from the daily flat file, incremental from the API.**
  `FLAT_CMPL.zip` is 352 MB compressed, 1.48 GB and 2.23M rows uncompressed,
  refreshed daily, and covers complaints from 1995. One fetch replaces ~130
  API calls, and a join on `odiNumber` agreed with the API on 100% of
  coverage and on every structured field.
- The flat file is **UTF-8, pinned explicitly**. latin-1 and cp1252 both decode
  it without raising, so a wrong choice fails silently rather than loudly —
  it turns every curly apostrophe into mojibake. Measured against the API:
  UTF-8 matches 99.6% of summaries, latin-1 76.3%.
- The flat file carries components at a finer grain than the API
  (`FORWARD COLLISION AVOIDANCE: ADAPTIVE CRUISE CONTROL` vs
  `FORWARD COLLISION AVOIDANCE`). The full string is stored and the coarse
  form derived from it, never the reverse — an API refresh must not degrade a
  row the backfill wrote.
- **NHTSA's own caveat, unresolved:** a notice in `CMPL.txt` dated June 2021
  warns of discrepancies between the flat file and the NHTSA website following
  a May 2021 system update, still open. Our join on 2022–23 data found no
  discrepancies, but the caveat is theirs and remains uncleared for older data.
- The recalls flat file does not exist. `RCL.txt` documents it, but
  `FLAT_RCL.zip` and every naming variant return S3 `NoSuchKey`, while the
  complaints and investigations equivalents return valid archives. Recalls are
  therefore fetched from the API.
- Envelope conventions differ between feeds: lowercase `count`/`message`/
  `odiNumber` on complaints and discovery, capitalised `Count`/`Message`/
  `NHTSACampaignNumber` on recalls. The client returns raw dicts and leaves
  these differences intact.
- Two date fields: `dateOfIncident` drives the rate maths, `dateComplaintFiled`
  drives the ingest watermark.
- The two feeds use **different date formats**: complaints are `MM/DD/YYYY`
  (`07/23/2026`), recalls are `DD/MM/YYYY` (`16/02/2022`). Each feed is parsed
  with its format pinned explicitly — no inference, because an inferring parser
  silently transposes day and month for the first twelve days of any month.
- Partial VIN excluded at ingest — no use for it.
- Incident dates are self-reported. Impossible dates are rejected and counted.