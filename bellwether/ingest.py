"""Incremental refresh from the NHTSA API. Backfill lives in spark_backfill.py.

Attribution comes from each record's own `products[].productModel`, never from
the string we searched with. Querying 'F-150 SUPER CREW' returns records whose
productModel is plain 'F-150' — the same records 'F-150 SUPER CAB' returns.
Storing the query string as the model is what produced 47% phantom duplication.

Body style is therefore not a dimension of this data. It exists only in the
discovery endpoint's vocabulary; no complaint record carries it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from . import config, lakebase
from . import nhtsa_client as api


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", text)).strip().upper()


def _is_variant(configured: str, actual: str) -> bool:
    want, got = _norm(configured), _norm(actual)
    return got == want or (
        got.startswith(want) and got[len(want):len(want) + 1] == " "
    )


# --- Discovery -------------------------------------------------------------


@dataclass(frozen=True)
class SearchKey:
    """A string the complaints endpoint accepts, and the year to pair it with.

    NOT a vehicle. These exist only because the endpoint rejects the real model
    name — querying 'F-150' returns HTTP 400 — so a string from the discovery
    vocabulary is needed to reach the records. What comes back is attributed by
    productModel, not by this.
    """

    make: str
    query_model: str
    model_year: int


@dataclass
class DiscoveryReport:
    keys: list[SearchKey] = field(default_factory=list)
    gaps: list[tuple[str, str, int]] = field(default_factory=list)


def discover(
    models: Iterable[tuple[str, str]] | None = None,
    years: Iterable[int] | None = None,
) -> DiscoveryReport:
    """Resolve configured models to strings the complaints endpoint accepts.

    Every accepted string is kept rather than one per model. Body-style
    variants are redundant (they return identical sets, deduped downstream on
    odi_number), but powertrain variants are not: RAV4, RAV4 HYBRID and
    RAV4 PRIME return disjoint sets, so dropping to a single key per configured
    model would silently lose whole vehicles.
    """
    models = list(models if models is not None else config.MODELS)
    years = list(years if years is not None else config.MODEL_YEARS)

    report = DiscoveryReport()
    cache: dict[tuple[int, str], list[dict]] = {}

    for make, configured in models:
        for year in years:
            key = (year, make)
            if key not in cache:
                cache[key] = api.get_models(year, make)
            found = sorted(
                {
                    rec["model"]
                    for rec in cache[key]
                    if _is_variant(configured, rec.get("model", ""))
                }
            )
            if not found:
                report.gaps.append((make, configured, year))
                continue
            report.keys.extend(SearchKey(make, m, year) for m in found)

    return report


# --- Normalisation ---------------------------------------------------------

# Pinned. The API uses MM/DD/YYYY; the flat file uses YYYYMMDD. Neither is
# inferred, because an inferring parser transposes day and month for the first
# twelve days of every month and the damage is invisible downstream.
API_DATE_FORMAT = "%m/%d/%Y"


def _parse_date(value: Any) -> tuple[date | None, bool]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, True
    if not isinstance(value, str):
        return None, False
    try:
        return datetime.strptime(value.strip(), API_DATE_FORMAT).date(), True
    except ValueError:
        return None, False


def attributed_model(record: dict, key: SearchKey) -> tuple[str, int] | None:
    """The vehicle NHTSA attributes this complaint to.

    A record can carry several products — a tyre, a child seat, the vehicle.
    Only the Vehicle entry matching the searched make is attribution.
    """
    for product in record.get("products") or []:
        if product.get("type") != "Vehicle":
            continue
        if (product.get("productMake") or "").upper() != key.make.upper():
            continue
        model = (product.get("productModel") or "").strip()
        year = (product.get("productYear") or "").strip()
        if not model or not year.isdigit():
            continue
        return model, int(year)
    return None


@dataclass
class Normalised:
    odi_number: int
    make: str
    model: str
    model_year: int
    components_coarse: list[str]
    summary: str
    date_of_incident: date | None
    date_complaint_filed: date | None
    crash: bool | None
    fire: bool | None
    injuries: int | None
    deaths: int | None


def normalise(record: dict, key: SearchKey) -> tuple[Normalised | None, str | None]:
    """Drops only what cannot be stored. The partial VIN is discarded here."""
    odi = record.get("odiNumber")
    if not isinstance(odi, int):
        return None, "missing_odi_number"

    summary = record.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, "summary_missing"

    attribution = attributed_model(record, key)
    if attribution is None:
        return None, "no_vehicle_attribution"
    model, model_year = attribution

    incident, ok = _parse_date(record.get("dateOfIncident"))
    if not ok:
        return None, "incident_date_unparseable"
    filed, ok = _parse_date(record.get("dateComplaintFiled"))
    if not ok:
        return None, "filed_date_unparseable"

    # The API only ever returns the coarse component form; the subcategory is
    # available solely in the flat file, so this feeds components_coarse and
    # must never overwrite the richer value a backfill wrote.
    coarse = [
        c.strip()
        for c in (record.get("components") or "").split(",")
        if c.strip()
    ]

    return (
        Normalised(
            odi_number=odi,
            make=key.make,
            model=model,
            model_year=model_year,
            components_coarse=sorted(set(coarse)),
            summary=summary.strip(),
            date_of_incident=incident,
            date_complaint_filed=filed,
            crash=record.get("crash"),
            fire=record.get("fire"),
            injuries=record.get("numberOfInjuries"),
            deaths=record.get("numberOfDeaths"),
        ),
        None,
    )


# --- Ingest ----------------------------------------------------------------

# `components` is coalesced, not overwritten: the API cannot supply the
# subcategory, so refreshing a flat-backfilled row must not degrade it.
_UPSERT = """
    insert into complaints (
        odi_number, make, model, model_year, components_coarse, summary,
        date_of_incident, date_complaint_filed, crash, fire, injuries,
        deaths, source
    ) values (
        %(odi_number)s, %(make)s, %(model)s, %(model_year)s,
        %(components_coarse)s, %(summary)s, %(date_of_incident)s,
        %(date_complaint_filed)s, %(crash)s, %(fire)s, %(injuries)s,
        %(deaths)s, 'api'
    )
    on conflict (odi_number) do update set
        make = excluded.make,
        model = excluded.model,
        model_year = excluded.model_year,
        components = coalesce(complaints.components, excluded.components_coarse),
        components_coarse = excluded.components_coarse,
        summary = excluded.summary,
        date_of_incident = excluded.date_of_incident,
        date_complaint_filed = excluded.date_complaint_filed,
        crash = excluded.crash,
        fire = excluded.fire,
        injuries = excluded.injuries,
        deaths = excluded.deaths,
        source = 'api',
        ingested_at = now()
"""


@dataclass
class IngestReport:
    keys_attempted: int = 0
    keys_failed: list[tuple[str, str, int, str]] = field(default_factory=list)
    fetched: int = 0
    distinct_complaints: int = 0
    inserted: int = 0
    refreshed: int = 0
    dropped: Counter = field(default_factory=Counter)
    models_seen: Counter = field(default_factory=Counter)

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())


def ingest(keys: Iterable[SearchKey] | None = None) -> IngestReport:
    """Full refresh per search key, diffed client-side on odi_number.

    NHTSA ignores unrecognised parameters silently, so there is no server-side
    date filter to lean on; the whole set comes down and the diff happens here.
    """
    if keys is None:
        keys = discover().keys
    keys = list(keys)

    report = IngestReport()
    rows: dict[int, Normalised] = {}

    for key in keys:
        report.keys_attempted += 1
        try:
            records = api.get_complaints(key.make, key.query_model, key.model_year)
        except api.NHTSAError as exc:
            report.keys_failed.append(
                (key.make, key.query_model, key.model_year, str(exc)[:120])
            )
            continue

        report.fetched += len(records)
        for record in records:
            row, reason = normalise(record, key)
            if reason is not None:
                report.dropped[reason] += 1
                continue
            assert row is not None
            # Body-style keys return the same records; the second sighting is
            # the same complaint, not a duplicate to count.
            rows[row.odi_number] = row

    report.distinct_complaints = len(rows)
    for row in rows.values():
        report.models_seen[(row.make, row.model)] += 1

    with lakebase.connect() as conn:
        existing = {
            r[0] for r in conn.execute("select odi_number from complaints").fetchall()
        }
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, [vars(r) for r in rows.values()])
        conn.commit()

    for odi in rows:
        if odi in existing:
            report.refreshed += 1
        else:
            report.inserted += 1

    return report
