"""All DDL for the Lakebase store. Idempotent — safe to run on every deploy.

Adding a table is one function plus one line in OBJECTS. Each function takes
an open connection, issues only create-if-not-exists DDL, and commits nothing;
create_all() owns the transaction so a partial schema is never left behind.
"""

from __future__ import annotations

from typing import Callable

import psycopg

from . import config, lakebase

# Extensions required before any table that depends on their types.
EXTENSIONS: tuple[str, ...] = ("vector",)


def create_extensions(conn: psycopg.Connection) -> None:
    for extension in EXTENSIONS:
        # Identifiers cannot be parameterised; EXTENSIONS is a fixed literal.
        conn.execute(f"create extension if not exists {extension}")


def create_complaints(conn: psycopg.Connection) -> None:
    """Owner complaints, one row per NHTSA ODI number.

    There is deliberately no VIN column. NHTSA exposes only a partial VIN and
    it has no use here, so it is dropped at ingest rather than stored.
    """
    conn.execute(
        """
        create table if not exists complaints (
            odi_number           bigint      primary key,
            make                 text        not null,
            -- Canonical model: spelling drift across years folded together,
            -- body style preserved. Nullable because ingest does not fold —
            -- the transform step populates this, and patterns group on it.
            -- The model NHTSA attributes the complaint to: MODELTXT in the
            -- flat file, products[].productModel from the API. NOT the string
            -- we searched with — that is a search key, and using it as
            -- attribution is what produced 47% phantom duplication.
            model                text        not null,
            model_year           smallint    not null,
            -- Full component strings, subcategory included, exactly as the
            -- source gave them: 'FORWARD COLLISION AVOIDANCE: ADAPTIVE
            -- CRUISE CONTROL'. Never truncated at ingest.
            components           text[],
            -- The coarse form the API returns, derived from the above by
            -- cutting at the colon. Stored so both granularities are
            -- queryable; the full value stays authoritative.
            components_coarse    text[],
            -- 'flat' (backfill) or 'api' (incremental refresh).
            source               text        not null,
            summary              text        not null,
            date_of_incident     date,
            date_complaint_filed date,
            crash                boolean,
            fire                 boolean,
            injuries             smallint,
            deaths               smallint,
            ingested_at          timestamptz not null default now()
        )
        """
    )

    conn.execute(
        "comment on table complaints is "
        "'NHTSA owner complaints. Partial VIN deliberately not ingested.'"
    )

    # Added after the base table existed; ALTER ... IF NOT EXISTS keeps
    # create_complaints idempotent on a table created before this column was.
    conn.execute(
        """
        alter table complaints
            add column if not exists summary_stripped text,
            add column if not exists is_failure_report boolean
        """
    )
    conn.execute(
        "comment on column complaints.is_failure_report is "
        "'NULL = not yet classified. FALSE excludes from pattern formation "
        "(parts-availability status, recall-repair-wait, explicit non-failure) "
        "without deleting the row.'"
    )

    # Ingest refreshes a whole combination at a time.
    conn.execute(
        """
        create index if not exists complaints_combo_idx
            on complaints (make, model, model_year)
        """
    )
    # dateOfIncident drives the rate maths.
    conn.execute(
        """
        create index if not exists complaints_incident_idx
            on complaints (date_of_incident)
        """
    )
    # dateComplaintFiled drives the ingest watermark.
    conn.execute(
        """
        create index if not exists complaints_filed_idx
            on complaints (date_complaint_filed)
        """
    )
    # Array containment: "which complaints mention this component".
    conn.execute(
        """
        create index if not exists complaints_components_idx
            on complaints using gin (components)
        """
    )


def create_recalls(conn: psycopg.Connection) -> None:
    """Published recalls, the basis of the novelty check.

    A campaign covers many vehicles, so the key is the campaign plus the
    vehicle it names — one row per (campaign, make, model, year).
    """
    conn.execute(
        """
        create table if not exists recalls (
            campaign_number      text        not null,
            make                 text        not null,
            model                text        not null,
            model_year           smallint    not null,
            manufacturer         text,
            component            text,
            summary              text,
            consequence          text,
            remedy               text,
            notes                text,
            report_received_date date,
            park_it              boolean,
            park_outside         boolean,
            over_the_air_update  boolean,
            ingested_at          timestamptz not null default now(),
            primary key (campaign_number, make, model, model_year)
        )
        """
    )
    conn.execute(
        """
        create index if not exists recalls_vehicle_idx
            on recalls (make, model, model_year)
        """
    )


def create_complaint_embeddings(conn: psycopg.Connection) -> None:
    """Narrative vectors, one per complaint.

    Kept in its own table so re-embedding with a different model never
    rewrites the complaint rows, and so the model that produced a vector is
    recorded alongside it.
    """
    conn.execute(
        f"""
        create table if not exists complaint_embeddings (
            odi_number  bigint      primary key
                        references complaints (odi_number) on delete cascade,
            embedding   vector({config.EMBEDDING_DIM}),
            model       text        not null,
            embedded_at timestamptz not null default now()
        )
        """
    )
    # Vector of the boilerplate-stripped text, kept separate from `embedding`
    # (the raw-text vector) so both remain queryable — see
    # docs/rejected-approaches.md and the stripping experiment for why the
    # stripped version is the one pattern formation retrieves against.
    conn.execute(
        f"""
        alter table complaint_embeddings
            add column if not exists embedding_stripped vector({config.EMBEDDING_DIM})
        """
    )
    # Both vectors are nullable: the raw and stripped embedding passes run
    # independently, and a row upserted by one must not be rejected for
    # lacking a value only the other pass supplies. Only odi_number and model
    # are guaranteed at insert time.
    conn.execute(
        "alter table complaint_embeddings alter column embedding drop not null"
    )


def create_recall_embeddings(conn: psycopg.Connection) -> None:
    """Recall vectors, one per (campaign, make, model, year).

    Embedded text is `summary || ' ' || consequence` rather than summary
    alone: consequence is written in owner-facing terms ("the vehicle may
    stall, increasing the risk of a crash") and sits closer in register to
    complaint prose, which is what these vectors are matched against.
    """
    conn.execute(
        f"""
        create table if not exists recall_embeddings (
            campaign_number text     not null,
            make            text     not null,
            model           text     not null,
            model_year      smallint not null,
            embedding       vector({config.EMBEDDING_DIM}),
            model_name      text     not null,
            embedded_at     timestamptz not null default now(),
            primary key (campaign_number, make, model, model_year),
            foreign key (campaign_number, make, model, model_year)
                references recalls (campaign_number, make, model, model_year)
                on delete cascade
        )
        """
    )
    conn.execute(
        """
        create index if not exists recall_embeddings_vehicle_idx
            on recall_embeddings (make, model, model_year)
        """
    )


def create_patterns(conn: psycopg.Connection) -> None:
    """One row per formed failure pattern.

    Deliberately carries no make/model: a pattern is about a shared failure,
    not a shared vehicle, and a real pattern can span manufacturers (phantom
    braking on adaptive cruise showed up across Tesla, Chevrolet, Jeep and
    Honda members at similarity ≥0.85 — see docs/rejected-approaches.md).
    Vehicle attribution lives on complaints and is reachable via
    pattern_members; pinning make/model here would have made that grouping
    structurally impossible to represent.

    `ratio` is stored, not a threshold verdict — see the brief: the ranking
    directs attention, it does not assert a defect. `novelty_verdict` and
    `novelty_recall_ref` are populated by the (not yet built) recall-matching
    step; both are nullable until that runs.
    """
    conn.execute(
        """
        create table if not exists patterns (
            id                 bigint generated always as identity primary key,
            name               text        not null,
            member_count       integer     not null default 0,
            recent_count       integer     not null default 0,
            baseline_count     integer     not null default 0,
            -- recent rate vs baseline rate. NULL when baseline_count is 0 --
            -- no rate to divide by, not 0.
            ratio              double precision,
            -- Deliberately free text pending the scoring step's own scale;
            -- not constrained to an enum yet.
            severity           text,
            state              text        not null default 'new',
            novelty_verdict    text,
            novelty_recall_ref text,
            first_seen_at      timestamptz not null default now(),
            updated_at         timestamptz not null default now()
        )
        """
    )
    conn.execute(
        """
        create index if not exists patterns_ratio_idx
            on patterns (ratio desc nulls last)
        """
    )


def create_novelty_history(conn: psycopg.Connection) -> None:
    """Prior novelty verdicts, written before each overwrite.

    `patterns.novelty_verdict` holds only the current answer. That was
    tolerable while the only writer was a controlled batch job, but the
    verdict is now reachable from an MCP tool that anyone chatting with an
    agent can invoke — a casual conversation could flip a verified verdict
    with no trace of what it was. One row is written here for every
    overwrite of a non-null verdict, so the previous value and its source
    survive.

    Deliberately append-only and never read by the pipeline: this is an audit
    trail, not state. Note the cascade — `form_patterns()` truncates
    `patterns` with `restart identity cascade`, which discards this history
    along with the patterns it describes. That is the same one-way door
    documented in the README, not an additional one.
    """
    conn.execute(
        """
        create table if not exists novelty_history (
            id              bigint generated always as identity primary key,
            pattern_id      bigint not null
                            references patterns (id) on delete cascade,
            old_verdict     text,
            old_recall_ref  text,
            new_verdict     text,
            new_recall_ref  text,
            -- Which path wrote it: 'batch' (assess_all), 'mcp_tool'
            -- (check_novelty), or whatever a future caller passes.
            source          text        not null,
            changed_at      timestamptz not null default now()
        )
        """
    )
    conn.execute(
        """
        create index if not exists novelty_history_pattern_idx
            on novelty_history (pattern_id, changed_at desc)
        """
    )


def create_pattern_members(conn: psycopg.Connection) -> None:
    """Which complaints belong to which pattern, and at what similarity.

    `similarity` is the score against the pattern at the moment of joining —
    the pattern's membership shifts as more complaints join, so this is a
    historical join-time value, not a live recomputation.
    """
    conn.execute(
        """
        create table if not exists pattern_members (
            pattern_id bigint not null references patterns (id) on delete cascade,
            odi_number bigint not null references complaints (odi_number) on delete cascade,
            similarity double precision not null,
            primary key (pattern_id, odi_number)
        )
        """
    )
    # A complaint belongs to at most one pattern; enforced here rather than
    # left to formation logic, since two patterns silently sharing a member
    # would break the ratio maths.
    conn.execute(
        """
        create unique index if not exists pattern_members_complaint_idx
            on pattern_members (odi_number)
        """
    )


# Registry, in dependency order. One line per object.
OBJECTS: tuple[tuple[str, Callable[[psycopg.Connection], None]], ...] = (
    ("extensions", create_extensions),
    ("complaints", create_complaints),
    ("recalls", create_recalls),
    ("complaint_embeddings", create_complaint_embeddings),
    ("recall_embeddings", create_recall_embeddings),
    ("patterns", create_patterns),
    ("pattern_members", create_pattern_members),
    ("novelty_history", create_novelty_history),
)


def create_all(conn: psycopg.Connection | None = None) -> list[str]:
    """Create every object, in order. Returns the names applied.

    Runs in a single transaction. Pass an open connection to enrol in a caller's
    transaction, or omit to open and commit one.
    """
    if conn is not None:
        for _, create in OBJECTS:
            create(conn)
        return [name for name, _ in OBJECTS]

    with lakebase.connect() as owned:
        for _, create in OBJECTS:
            create(owned)
        owned.commit()
    return [name for name, _ in OBJECTS]
