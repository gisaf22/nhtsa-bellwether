# nhtsa-bellwether

Emerging vehicle failure patterns from NHTSA owner complaints.

Complaints describing the same failure share almost no vocabulary — "shudders
between 30 and 40", "hesitates when accelerating", "feels like it's slipping".
NHTSA's component categories are too coarse to separate them and keyword search
groups nothing, so the pattern is invisible until something groups the
narratives by meaning.

This ingests complaints, groups them semantically, ranks each pattern against
an age-matched baseline, and checks whether it's already covered by a published
recall.

See [docs/brief.md](docs/brief.md) for the problem, workflow, and what the MVP
delivers.

## Stack

Lakebase for the operational store and embeddings · Spark for batch embedding
and rate computation · Databricks App frontend · LLM agent for recall matching

## Status

End-to-end and running on real data: 20,101 failure reports, 275 patterns,
17,207 assigned members, all 275 patterns scored and given a novelty verdict
(163 novel, 88 known, 24 partially covered).

## Limitations

Stated plainly, because each one changes how the output should be read.

**Recall matching is vehicle-scoped, not date-scoped.** The novelty agent
shortlists recalls for vehicles the pattern actually has members on, then asks
an LLM whether the failure is covered. Nothing compares dates, so a pattern can
be marked `known` on the strength of a recall issued *after* the complaints
that formed it — which inverts the finding: complaints that followed a recall
are evidence the remedy did not work, not evidence the problem was already
handled. Verdicts should be read as "a recall exists for this failure on this
vehicle", not "this was already known when it emerged."

**`patterns` is load-bearing and formation is now a one-way door.** The table
holds all 275 novelty verdicts and whatever triage state the app has written
(acknowledge / watch / hide). `form_patterns()` begins with
`truncate ... restart identity cascade` on every run, so re-running it destroys
every verdict and every state, and reassigns pattern IDs so nothing can be
matched back. **Do not re-run `form_patterns()`.** `score_patterns()` and the
novelty batch `assess_all()` are both idempotent and safe to re-run — the
novelty batch skips patterns that already have a verdict unless asked not to.
The real fix is one-shot formation plus a separate incremental-assignment path;
that is not built.

**Rate maths runs on `date_of_incident`, not `date_complaint_filed`.** Owners
file well after the incident, so the most recent window is filled in by
complaints that have not arrived yet. That biases the recent window downward
and understates exactly the emerging patterns this project exists to surface.
The size of the effect has not been measured and no correction is applied.

**76 of 275 patterns have no ratio.** Their baseline window contains no
incidents, so there is no rate to divide by — unrankable rather than zero. The
app labels these "insufficient history" and sorts them last.

**Change data capture (Lakebase → Delta) was scoped out.** Not deferred for
time: it is not in the capstone requirements brief, confirmed by reading the
brief rather than inferred. The app writes triage state as a plain column
update on `patterns`, with no append-only event log, because the event log only
ever existed to feed CDF analytics.