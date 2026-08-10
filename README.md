# nhtsa-bellwether

Turns 20K+ NHTSA owner complaints into ranked emerging failure patterns,
checks each one against existing recalls with an LLM agent, and surfaces the
problems that haven't been recalled yet — before a keyword search or a
component-code filter would ever group them together.

## How it works

```
NHTSA API (complaints + recalls)
        │  ingest, classify is_failure_report
        ▼
Embeddings (databricks-gte-large-en)          — complaint narratives + recall text
        │
        ▼
Pattern formation (Spark)                     — single-linkage clustering on similarity
        │
        ▼
Severity scoring                              — recent rate vs. each pattern's own baseline
        │
        ▼
Novelty agent (LLM)                           — shortlist candidate recalls, verdict: novel / known / partially covered
        │
        ▼
Streamlit app on Databricks Apps              — ranked triage inbox, backed by Lakebase
```

Lakebase (Postgres + pgvector) is the operational store end to end — complaints,
recalls, embeddings, patterns, and app-written triage state all live there.

## The numbers

| | |
|---|---|
| Failure reports | 20,101 |
| Recalls | 397 |
| Patterns formed | 275 |
| Novel | 163 |
| Known (recall exists) | 88 |
| Partially covered | 24 |

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Set `DATABRICKS_PROFILE` (or the `LAKEBASE_*` variables in `.env.example`) so
`bellwether/lakebase.py` can mint a Postgres credential, then:

```bash
python -c "from bellwether import schema; schema.create_all()"   # DDL
streamlit run app.py                                             # local UI
```

Deployed as a Databricks App (`app.yaml`) backed by the same Lakebase
instance: **https://nhtsa-bellwether-7474645136578041.aws.databricksapps.com/**

## MCP server

`bellwether_mcp_server.py` exposes the novelty check as two agent-callable
tools, for use from Databricks Playground / Agent Bricks:

| Tool | | |
|---|---|---|
| `search_recalls_for_pattern(pattern_id)` | read-only | Vehicle-scoped filter plus embedding shortlist against `recall_embeddings`. Returns candidate recalls with campaign numbers and similarity scores. Judges nothing, writes nothing. |
| `check_novelty(pattern_id)` | **writes** | Same retrieval, then one LLM call for the verdict, then persists `novelty_verdict` / `novelty_recall_ref` to `patterns`. |

```bash
python bellwether_mcp_server.py
```

Both tools delegate straight into `bellwether/novelty.py` — the retrieval and
matching logic has one implementation, shared with the batch job.

`check_novelty` overwrites the stored verdict. Because it is reachable by
anyone chatting with an agent, every overwrite of a non-null verdict first
writes the previous value to `novelty_history` with a `source` tag (`batch`
vs `mcp_tool`), so a conversational reassessment that flips a verdict leaves
a trail. The app itself always shows the current value; there is no in-app
undo.

This is the conversational, tool-choosing interface. It is independent of the
Streamlit app and the two coexist — the app is the one-click version, the MCP
server is for asking questions and having an agent decide which tool to call.

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
(reviewed / dismissed). `form_patterns()` begins with
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

## More detail

- [docs/brief.md](docs/brief.md) — the problem statement, workflow, and what
  the MVP delivers.
- [docs/rejected-approaches.md](docs/rejected-approaches.md) — retrieval and
  scoring approaches that were tried and rejected, with the evidence.
