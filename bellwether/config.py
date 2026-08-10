"""Configuration: target vehicles, Lakebase connection, and tuning constants.

Model name strings here are what get sent to NHTSA. They are asserted, not
verified — `ingest.discover()` checks each one against the live models
endpoint and reports mismatches rather than skipping them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

from dotenv import load_dotenv

load_dotenv()


# --- Target vehicles -------------------------------------------------------

MODEL_YEARS: tuple[int, ...] = (2020, 2021, 2022, 2023, 2024)

# (make, model) as NHTSA spells them. Upper case matches what the discovery
# endpoint returns; comparison in discover() is case-insensitive regardless.
MODELS: tuple[tuple[str, str], ...] = (
    ("FORD", "F-150"),
    ("CHEVROLET", "SILVERADO 1500"),
    ("RAM", "1500"),
    ("TOYOTA", "RAV4"),
    ("TOYOTA", "CAMRY"),
    ("HONDA", "CR-V"),
    ("HONDA", "CIVIC"),
    ("NISSAN", "ROGUE"),
    ("JEEP", "GRAND CHEROKEE"),
    ("TESLA", "MODEL 3"),
)


def combos() -> list[tuple[str, str, int]]:
    """Every (make, model, model_year) to ingest — 10 models x 5 years."""
    return [
        (make, model, year)
        for make, model in MODELS
        for year in MODEL_YEARS
    ]


# --- Lakebase (Postgres) ---------------------------------------------------

# Lakebase issues short-lived OAuth tokens as the Postgres password, so
# LAKEBASE_PASSWORD is expected to be a generated credential rather than a
# durable secret. DATABRICKS_PROFILE / LAKEBASE_INSTANCE identify the instance
# that credential is minted against.
DATABRICKS_PROFILE: str | None = os.getenv("DATABRICKS_PROFILE")
LAKEBASE_INSTANCE: str | None = os.getenv("LAKEBASE_INSTANCE")


@dataclass(frozen=True)
class LakebaseSettings:
    host: str | None
    port: int
    dbname: str
    user: str | None
    password: str | None
    sslmode: str

    def missing(self) -> list[str]:
        """Names of the required settings that are unset.

        Password is not required here: lakebase.resolve() prefers a freshly
        minted OAuth token and treats LAKEBASE_PASSWORD as a fallback.
        """
        required = {
            "LAKEBASE_HOST": self.host,
            "LAKEBASE_USER": self.user,
        }
        return [name for name, value in required.items() if not value]


def lakebase_settings() -> LakebaseSettings:
    return LakebaseSettings(
        host=os.getenv("LAKEBASE_HOST"),
        port=int(os.getenv("LAKEBASE_PORT", "5432")),
        dbname=os.getenv("LAKEBASE_DB", "databricks_postgres"),
        user=os.getenv("LAKEBASE_USER"),
        password=os.getenv("LAKEBASE_PASSWORD"),
        sslmode=os.getenv("LAKEBASE_SSLMODE", "require"),
    )


# --- Retrieval ---------------------------------------------------------

# Settled after three tested and rejected alternatives — see
# docs/rejected-approaches.md for the evidence:
#   - component-scoped retrieval: rejected, UNKNOWN OR OTHER (14% of the
#     corpus) becomes an unreachable island under any component filter.
#   - a single global similarity threshold: rejected, neighbour density
#     varies too much across seeds (9 vs 899 neighbours at the same cutoff)
#     for one number to mean the same thing everywhere.
#   - a single global top-k: rejected, the coherence-breaking rank varies
#     from 3 to beyond 20 across seeds and degrades non-monotonically, and
#     small k actively discards the cross-manufacturer matches this project
#     exists to surface (they cluster at rank 12-20, not rank 1-10).
#
# What is left is a floor plus a cap: admit anything above a similarity
# floor loose enough not to exclude genuine matches, capped at a neighbour
# count loose enough not to truncate a dense cluster. Neither bound is
# doing the precision work alone — pattern formation and scoring downstream
# are what separate signal from noise.
SIMILARITY_FLOOR: float = 0.78
NEIGHBOUR_CAP: int = 25

EMBEDDING_DIM: int = 1024


# --- Windows ---------------------------------------------------------------

# Complaint history floor. Anything filed before this is out of scope.
HISTORY_START: date = date(2022, 1, 1)

# Backfill stops short of the present so the scheduled job has real new rows
# to find, and the change feed carries events rather than one bulk load.
BACKFILL_HOLDBACK_DAYS: int = 30

# Rate maths, both over dateOfIncident.
RECENT_WINDOW_DAYS: int = 90
BASELINE_WINDOW_DAYS: int = 365


# --- Normalisation rules ---------------------------------------------------

# Narratives shorter than this carry no groupable content ("brakes bad").
MIN_SUMMARY_CHARS: int = 40

# Incident dates are self-reported and frequently impossible. Anything outside
# these bounds is rejected and counted rather than silently repaired.
EARLIEST_PLAUSIBLE_INCIDENT: date = date(2000, 1, 1)
