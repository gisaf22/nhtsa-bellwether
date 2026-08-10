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
    # `severity is null` is deliberately included: a null severity means the
    # trailing baseline window was empty, so no rate could be computed — not
    # that the pattern is unimportant. Those are the youngest patterns in the
    # set, which is the signal this tool exists to surface. Excluding them
    # filtered out 45 novel-or-partly-covered patterns.
    "Needs attention": (
        "novelty_verdict in ('novel', 'partially_covered') "
        "and (severity in ('medium', 'high') or severity is null) "
        "and state <> 'hidden'"
    ),
    "All active": "state <> 'hidden'",
    "Everything (incl. hidden)": "true",
}


@st.cache_data(ttl=60)
def count_patterns(view: str) -> int:
    """How many patterns the view actually matches, before the page limit.

    Kept separate from load_patterns so the header can say "showing 50 of
    275" — the page size is a display bound, and reporting it as the total
    misstates the size of the backlog.
    """
    return lakebase.execute(
        f"select count(*) from patterns where {VIEWS[view]}", fetch="one"
    )[0]


@st.cache_data(ttl=60)
def load_patterns(view: str, limit: int = PAGE_SIZE) -> list[dict]:
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
        limit %s
        """,
        (limit,),
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


# Layout primitives that arrived in specific Streamlit releases. The deployed
# build is older than this development one (it is what surfaced the
# width="stretch" TypeError), and its exact version is not knowable from here,
# so each is feature-detected and degraded rather than assumed: without the
# scroll pane the list simply grows the page, which is the old behaviour.
try:
    _ST_VERSION = tuple(int(part) for part in st.__version__.split(".")[:2])
except (AttributeError, ValueError):
    _ST_VERSION = (0, 0)
SUPPORTS_SCROLL_PANE = _ST_VERSION >= (1, 31)  # st.container(height=...)
SUPPORTS_BORDER = _ST_VERSION >= (1, 29)  # st.container(border=...)

LIST_PANE_HEIGHT = 720

# Hairline row separator. Colour is a neutral alpha rather than a fixed grey so
# it reads the same in Streamlit's light and dark themes.
ROW_RULE = (
    "<hr style='margin:0.4rem 0 0.6rem 0;border:none;"
    "border-top:1px solid rgba(128,128,128,0.25)'>"
)


def pane(*, height: int | None = None, border: bool = False):
    """A container with whatever of height/border this Streamlit supports."""
    kwargs: dict = {}
    if border and SUPPORTS_BORDER:
        kwargs["border"] = True
    if height is not None and SUPPORTS_SCROLL_PANE:
        kwargs["height"] = height
    return st.container(**kwargs)


st.set_page_config(
    page_title="Bellwether",
    layout="wide",
    # The rail is empty now that filters sit in the main content area; leaving
    # it open would just steal width from the two panes.
    initial_sidebar_state="collapsed",
)
st.title("Bellwether")
st.caption(
    "Emerging failure patterns in NHTSA owner complaints, ranked by recent "
    "rate against each pattern's own trailing baseline. A ratio directs "
    "attention; it does not assert a defect."
)

# --- Filter bar -------------------------------------------------------------

filter_bar = st.columns([3.0, 2.0])
view = filter_bar[0].radio(
    "View", list(VIEWS), index=0, horizontal=True, label_visibility="collapsed"
)
filter_bar[1].caption(
    "**Needs attention**: novel or partly-covered, at medium/high severity or "
    "too new to score."
)

if st.session_state.get("view") != view:
    # A new view starts at one page again, rather than inheriting however far
    # the reader had scrolled through the previous one.
    st.session_state["view"] = view
    st.session_state["limit"] = PAGE_SIZE

limit = st.session_state.setdefault("limit", PAGE_SIZE)
total = count_patterns(view)
patterns = load_patterns(view, limit)

if not patterns:
    st.info("Nothing in this view. Try **All active** above.")
    st.stop()

if st.session_state.get("selected_id") not in {p["id"] for p in patterns}:
    st.session_state["selected_id"] = patterns[0]["id"]


def render_row(p: dict) -> None:
    """One inbox row, laid out for a half-width pane.

    Three columns rather than six: the pane is roughly half a laptop screen,
    and six columns of chips wrapped into unreadable stacks at that width. The
    chips and the open control share the right-hand column instead.
    """
    severity_icon, severity_colour = SEVERITY_STYLE.get(
        p["severity"], SEVERITY_STYLE[None]
    )
    novelty_icon, novelty_colour, novelty_label = NOVELTY_STYLE.get(
        p["novelty_verdict"], NOVELTY_STYLE[None]
    )
    urgent = p["severity"] in ("high", "medium")
    selected = p["id"] == st.session_state["selected_id"]

    cols = st.columns([1.1, 3.0, 1.8])

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

    # The vehicle name is the click target rather than a separate Open button:
    # a button per row on top of a title line made rows ~110px tall, so only
    # three fitted on screen. This is one control instead of two, and the
    # highlight marks the selection without grey-disabling the row's title.
    if cols[1].button(
        label,
        key=f"open_{p['id']}",
        type="primary" if selected else "secondary",
    ):
        st.session_state["selected_id"] = p["id"]
        st.rerun()
    cols[1].markdown(
        f":gray[{fleet} · {p['member_count']} reports · "
        f"`{p['category'] or 'UNCATEGORISED'}`]"
    )

    if p["severity"] is None:
        # Not a severity value — these patterns are unscored, not low-risk,
        # and inventing a bucket for them would assert a rate that was never
        # computed. Flagged as emerging instead, in its own colour.
        severity_chip = ":blue[🆕 **emerging**]"
    else:
        severity_chip = (
            f"{severity_icon} :{severity_colour}[{format_severity(p['severity'])}]"
        )
    state_chip = "" if p["state"] == "new" else f"  \n:gray[{p['state']}]"
    cols[2].markdown(
        f"{severity_chip}  \n{novelty_icon} :{novelty_colour}[{novelty_label}]"
        f"{state_chip}"
    )

    # st.divider()'s vertical margins add ~60px of dead space per row, which
    # at 50 rows is most of the pane. A hairline rule separates them just as
    # well in a fraction of the height.
    st.markdown(ROW_RULE, unsafe_allow_html=True)


def render_detail(pattern: dict) -> None:
    """The detail panel. Same content and same writes as before — it just
    renders inside the right-hand pane instead of below the list.
    """
    st.subheader(pattern["name"])

    cols = st.columns(3)
    cols[0].metric("Ratio", format_ratio(pattern["ratio"]))
    cols[1].metric("Severity", format_severity(pattern["severity"]))
    cols[2].metric("Novelty", pattern["novelty_verdict"] or "—")
    cols = st.columns(3)
    cols[0].metric("Members", pattern["member_count"])
    cols[1].metric("Recent", pattern["recent_count"])
    cols[2].metric("Baseline", pattern["baseline_count"])
    if pattern["ratio"] is None:
        st.caption(
            "No incidents in this pattern's baseline window, so there is no "
            "rate to compare the recent window against — unrankable, not zero."
        )

    # Uneven split: at 1280px an even quarter is too narrow for "Acknowledge"
    # and it wraps mid-word.
    action = st.columns([1.5, 1, 1, 1])
    if action[0].button("Acknowledge", use_container_width=True):
        set_state(pattern["id"], "acknowledged")
        st.rerun()
    if action[1].button("Watch", use_container_width=True):
        set_state(pattern["id"], "watching")
        st.rerun()
    if action[2].button("Hide", use_container_width=True):
        set_state(pattern["id"], "hidden")
        st.rerun()
    if action[3].button("Reset", use_container_width=True):
        set_state(pattern["id"], "new")
        st.rerun()
    st.caption(f"Current state: **{pattern['state']}**")

    if pattern["novelty_recall_ref"]:
        recall = load_recall(pattern["novelty_recall_ref"])
        with st.expander(
            f"Recall {pattern['novelty_recall_ref']} ({pattern['novelty_verdict']})"
        ):
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
        # deployed build is older, where `width` takes an int and a string
        # raises TypeError. use_container_width works on both.
        use_container_width=True,
        hide_index=True,
    )

    st.write("**Sample narratives**")
    for m in members[:5]:
        with st.expander(
            f"{m['odi_number']} · {m['make']} {m['model']} {m['model_year']}"
        ):
            st.write(m["summary"])


# --- Two panes: list on the left, detail on the right -----------------------

pattern = load_pattern(st.session_state["selected_id"])
list_pane, detail_pane = st.columns([1.25, 1], gap="large")

with list_pane:
    st.subheader(view)
    st.caption(
        f"Showing all {total} patterns" if len(patterns) >= total
        else f"Showing {len(patterns)} of {total} patterns"
    )
    # The list scrolls inside its own pane where Streamlit supports it, so
    # selecting a pattern never scrolls the detail panel out of view.
    with pane(height=LIST_PANE_HEIGHT):
        for p in patterns:
            render_row(p)

        if len(patterns) < total:
            remaining = total - len(patterns)
            if st.button(
                f"Load {min(PAGE_SIZE, remaining)} more ({remaining} left)"
            ):
                st.session_state["limit"] = limit + PAGE_SIZE
                st.rerun()

with detail_pane:
    with pane(height=LIST_PANE_HEIGHT, border=True):
        if pattern is None:
            st.info("Select a pattern from the list.")
        else:
            render_detail(pattern)
