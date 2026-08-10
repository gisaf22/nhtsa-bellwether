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

# A null ratio is not a missing value: the pattern's baseline window holds no
# incidents, so there is no rate to divide by. Shown as a label rather than an
# empty cell so it reads as a stated fact about the pattern, not a broken row.
NO_BASELINE = "insufficient history"


def format_ratio(ratio: float | None) -> str:
    """Every row renders as a string so the column never mixes types."""
    return NO_BASELINE if ratio is None else f"{ratio:.2f}×"


def format_severity(severity: str | None) -> str:
    return severity or NO_BASELINE


# Status coding, per the visualisation guidance: an icon AND a word, never
# colour alone — these are the triage signal, and a reader who cannot
# distinguish the hues must still be able to sort the list.
SEVERITY_STYLE = {
    "high": ("🔴", "red"),
    "medium": ("🟠", "orange"),
    "low": ("🟡", "gray"),
    "none": ("⚪", "gray"),
    None: ("◽", "gray"),
}
NOVELTY_STYLE = {
    "novel": ("✦", "violet", "novel"),
    "partially_covered": ("◐", "orange", "partly covered"),
    "known": ("✓", "gray", "known recall"),
    None: ("·", "gray", "unassessed"),
}

# Default view: what a human should look at. Everything else is one click away
# rather than in the way.
VIEWS = {
    "Needs attention": (
        "novelty_verdict in ('novel', 'partially_covered') "
        "and severity in ('medium', 'high') and state <> 'hidden'"
    ),
    "All active": "state <> 'hidden'",
    "Everything (incl. hidden)": "true",
}


@st.cache_data(ttl=60)
def load_patterns(view: str) -> list[dict]:
    """Ranked patterns, with the pieces of the name string as real columns.

    The dominant vehicle, the affected-vehicle count and the failure category
    are derived here rather than parsed back out of `patterns.name` — the name
    is a display string built by formation, and splitting it on ' — ' would
    break the moment a component contains a dash.
    """
    rows = lakebase.execute(
        f"""
        with member_vehicles as (
            select pm.pattern_id, c.make, c.model, c.components_coarse
            from pattern_members pm
            join complaints c on c.odi_number = pm.odi_number
        ),
        vehicle_rank as (
            select pattern_id, make || ' ' || model as vehicle,
                   row_number() over (
                       partition by pattern_id
                       order by count(*) desc, make || ' ' || model
                   ) as rn
            from member_vehicles group by pattern_id, make, model
        ),
        vehicle_count as (
            select pattern_id, count(distinct make || ' ' || model) as vehicles
            from member_vehicles group by pattern_id
        ),
        category_rank as (
            select pattern_id, category,
                   row_number() over (
                       partition by pattern_id order by count(*) desc, category
                   ) as rn
            from member_vehicles,
                 unnest(coalesce(components_coarse, array['UNCATEGORISED'])) as category
            group by pattern_id, category
        )
        select p.id, p.name, p.member_count, p.recent_count, p.baseline_count,
               p.ratio, p.severity, p.state, p.novelty_verdict,
               p.novelty_recall_ref,
               v.vehicle, coalesce(vc.vehicles, 0), c.category
        from patterns p
        left join vehicle_rank v on v.pattern_id = p.id and v.rn = 1
        left join vehicle_count vc on vc.pattern_id = p.id
        left join category_rank c on c.pattern_id = p.id and c.rn = 1
        where {VIEWS[view]}
        -- Unactioned first, then hardest-hitting: a triage inbox, not a report.
        order by (p.state = 'new') desc, p.ratio desc nulls last,
                 p.member_count desc
        limit {PAGE_SIZE}
        """,
        fetch="all",
    )
    keys = (
        "id name member_count recent_count baseline_count ratio severity "
        "state novelty_verdict novelty_recall_ref vehicle vehicles category"
    ).split()
    return [dict(zip(keys, r)) for r in rows]


@st.cache_data(ttl=60)
def load_pattern(pattern_id: int) -> dict | None:
    """One pattern by id, so the detail view survives its row leaving the list
    (acknowledging a pattern can filter it straight out of the current view).
    """
    row = lakebase.execute(
        """
        select id, name, member_count, recent_count, baseline_count, ratio,
               severity, state, novelty_verdict, novelty_recall_ref
        from patterns where id = %s
        """,
        (pattern_id,),
        fetch="one",
    )
    if row is None:
        return None
    keys = (
        "id name member_count recent_count baseline_count ratio severity "
        "state novelty_verdict novelty_recall_ref"
    ).split()
    return dict(zip(keys, row))


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

view = st.sidebar.radio("View", list(VIEWS), index=0)
st.sidebar.caption(
    "**Needs attention** is novel or partly-covered patterns at medium or high "
    "severity. Patterns with too little history to rank carry no severity and "
    "appear under **All active**."
)
patterns = load_patterns(view)

if not patterns:
    st.info("Nothing in this view. Try **All active** in the sidebar.")
    st.stop()

if st.session_state.get("selected_id") not in {p["id"] for p in patterns}:
    st.session_state["selected_id"] = patterns[0]["id"]


def render_row(p: dict) -> None:
    """One inbox row: ratio, vehicle, category, severity, novelty, open."""
    severity_icon, severity_colour = SEVERITY_STYLE.get(
        p["severity"], SEVERITY_STYLE[None]
    )
    novelty_icon, novelty_colour, novelty_label = NOVELTY_STYLE.get(
        p["novelty_verdict"], NOVELTY_STYLE[None]
    )
    urgent = p["severity"] in ("high", "medium")
    selected = p["id"] == st.session_state["selected_id"]

    cols = st.columns([1.4, 3.6, 2.4, 1.9, 1.9, 1.0])

    # Ratio as a numeric badge, weighted: an urgent pattern is heavier than an
    # also-ran, so the eye lands on it before reading a word.
    if p["ratio"] is None:
        cols[0].markdown(":gray[no history]")
    elif urgent:
        cols[0].markdown(f"### :{severity_colour}[{p['ratio']:.2f}×]")
    else:
        cols[0].markdown(f":gray[{p['ratio']:.2f}×]")

    vehicles = p["vehicles"] or 1
    fleet = "1 vehicle" if vehicles == 1 else f"{vehicles} vehicles"
    label = p["vehicle"] or p["name"]
    cols[1].markdown(
        f"{'**' if selected else ''}{label}{'**' if selected else ''}  \n"
        f":gray[{fleet} · {p['member_count']} reports]"
    )
    cols[2].markdown(f":gray[`{p['category'] or 'UNCATEGORISED'}`]")
    cols[3].markdown(
        f"{severity_icon} :{severity_colour}[{format_severity(p['severity'])}]"
    )
    cols[4].markdown(f"{novelty_icon} :{novelty_colour}[{novelty_label}]")
    if p["state"] != "new":
        cols[4].markdown(f":gray[{p['state']}]")
    if cols[5].button("Open", key=f"open_{p['id']}", disabled=selected):
        st.session_state["selected_id"] = p["id"]
        st.rerun()

    st.divider()


st.subheader(f"{view} · {len(patterns)}")
header = st.columns([1.4, 3.6, 2.4, 1.9, 1.9, 1.0])
for col, title in zip(
    header, ("Ratio", "Vehicle", "Category", "Severity", "Recall", "")
):
    col.markdown(f":gray[**{title}**]")
st.divider()

for p in patterns:
    render_row(p)

pattern = load_pattern(st.session_state["selected_id"])
if pattern is None:
    st.stop()

st.divider()
st.subheader(pattern["name"])

cols = st.columns(5)
cols[0].metric("Ratio", format_ratio(pattern["ratio"]))
cols[1].metric("Severity", format_severity(pattern["severity"]))
if pattern["ratio"] is None:
    st.caption(
        "No incidents in this pattern's baseline window, so there is no rate "
        "to compare the recent window against — unrankable, not zero."
    )
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
    # Not width="stretch": that spelling needs Streamlit >= 1.49 and the
    # deployed build is older, where `width` takes an int and a string raises
    # TypeError. use_container_width works on both.
    use_container_width=True,
    hide_index=True,
)

st.write("**Sample narratives**")
for m in members[:5]:
    with st.expander(f"{m['odi_number']} · {m['make']} {m['model']} {m['model_year']}"):
        st.write(m["summary"])
