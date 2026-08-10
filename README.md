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

Lakebase for the operational store and embeddings · change data capture into
Delta · Spark for batch embedding and rate computation · Databricks App
frontend · LLM agent with read and write tools

## Status

Build in progress.