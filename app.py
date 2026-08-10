"""Bellwether — ranked emerging failure patterns, with a detail view.

Writes triage state straight to patterns.state as a column update. No service
layer and no event log: the append-only history was only ever needed for the
CDF analytics path, which is out of scope.
"""

from __future__ import annotations

import streamlit as st

from bellwether import lakebase

STATES = ("new", "acknowledged", "watching", "hidden")
PAGE_SIZE = 50


@st.cache_data(ttl=60)
def load_patterns(include_hidden: bool) -> list[dict]:
    rows = lakebase.execute(
        f"""
        select id, name, member_count, recent_count, baseline_count, ratio,
               severity, state, novelty_verdict, novelty_recall_ref
        from patterns
        {'' if include_hidden else "where state <> 'hidden'"}
        order by ratio desc nulls last, member_count desc
        limit {PAGE_SIZE}
        """,
        fetch="all",
    )
    keys = (
        "id name member_count recent_count baseline_count ratio severity "
        "state novelty_verdict novelty_recall_ref"
    ).split()
    return [dict(zip(keys, r)) for r in rows]


@st.cache_data(ttl=60)
def load_members(pattern_id: int) -> list[dict]:
    rows = lakebase.execute(
        """
        select c.odi_number, c.make, c.model, c.model_year, c.date_of_incident,
               pm.similarity, coalesce(c.summary_stripped, c.summary)
        from pattern_members pm
        join complaints c on c.odi_number = pm.odi_number
        where pm.pattern_id = %s
        order by pm.similarity desc
        """,
        (pattern_id,),
        fetch="all",
    )
    keys = "odi_number make model model_year date_of_incident similarity summary".split()
    return [dict(zip(keys, r)) for r in rows]


@st.cache_data(ttl=300)
def load_recall(campaign_number: str) -> dict | None:
    row = lakebase.execute(
        """
        select campaign_number, component, summary, consequence, remedy,
               report_received_date
        from recalls where campaign_number = %s limit 1
        """,
        (campaign_number,),
        fetch="one",
    )
    if row is None:
        return None
    keys = "campaign_number component summary consequence remedy report_received_date".split()
    return dict(zip(keys, row))


def set_state(pattern_id: int, state: str) -> None:
    lakebase.execute(
        "update patterns set state = %s, updated_at = now() where id = %s",
        (state, pattern_id),
    )
    st.cache_data.clear()


st.set_page_config(page_title="Bellwether", layout="wide")
st.title("Bellwether")
st.caption(
    "Emerging failure patterns in NHTSA owner complaints, ranked by recent "
    "rate against each pattern's own trailing baseline. A ratio directs "
    "attention; it does not assert a defect."
)

include_hidden = st.sidebar.checkbox("Show hidden", value=False)
patterns = load_patterns(include_hidden)

if not patterns:
    st.info("No patterns yet — run formation and scoring first.")
    st.stop()

labels = {
    p["id"]: f"{p['ratio']:.2f}× · {p['name']}" if p["ratio"] is not None
    else f"—    · {p['name']}"
    for p in patterns
}
selected_id = st.sidebar.radio(
    "Patterns", [p["id"] for p in patterns], format_func=lambda i: labels[i]
)

st.subheader("Ranked patterns")
st.dataframe(
    [
        {
            "ratio": p["ratio"],
            "severity": p["severity"],
            "name": p["name"],
            "members": p["member_count"],
            "recent": p["recent_count"],
            "novelty": p["novelty_verdict"],
            "recall": p["novelty_recall_ref"],
            "state": p["state"],
        }
        for p in patterns
    ],
    width="stretch",
    hide_index=True,
)

pattern = next(p for p in patterns if p["id"] == selected_id)

st.divider()
st.subheader(pattern["name"])

cols = st.columns(5)
cols[0].metric("Ratio", f"{pattern['ratio']:.2f}×" if pattern["ratio"] else "—")
cols[1].metric("Severity", pattern["severity"] or "—")
cols[2].metric("Members", pattern["member_count"])
cols[3].metric("Recent / baseline", f"{pattern['recent_count']} / {pattern['baseline_count']}")
cols[4].metric("Novelty", pattern["novelty_verdict"] or "—")

action = st.columns(4)
if action[0].button("Acknowledge"):
    set_state(pattern["id"], "acknowledged")
    st.rerun()
if action[1].button("Watch"):
    set_state(pattern["id"], "watching")
    st.rerun()
if action[2].button("Hide"):
    set_state(pattern["id"], "hidden")
    st.rerun()
if action[3].button("Reset to new"):
    set_state(pattern["id"], "new")
    st.rerun()
st.caption(f"Current state: **{pattern['state']}**")

if pattern["novelty_recall_ref"]:
    recall = load_recall(pattern["novelty_recall_ref"])
    with st.expander(f"Recall {pattern['novelty_recall_ref']} ({pattern['novelty_verdict']})"):
        if recall is None:
            st.write("Campaign not found in the recalls table.")
        else:
            st.write(f"**Component:** {recall['component']}")
            st.write(f"**Received:** {recall['report_received_date']}")
            st.write(recall["summary"])
            st.write(f"**Consequence:** {recall['consequence']}")
            st.write(f"**Remedy:** {recall['remedy']}")

members = load_members(pattern["id"])
st.write(f"**Members ({len(members)})**")
st.dataframe(
    [
        {
            "odi": m["odi_number"],
            "vehicle": f"{m['make']} {m['model']} {m['model_year']}",
            "incident": m["date_of_incident"],
            "similarity": round(m["similarity"], 3),
        }
        for m in members
    ],
    width="stretch",
    hide_index=True,
)

st.write("**Sample narratives**")
for m in members[:5]:
    with st.expander(f"{m['odi_number']} · {m['make']} {m['model']} {m['model_year']}"):
        st.write(m["summary"])
