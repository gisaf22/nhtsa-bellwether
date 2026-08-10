"""Recall ingest from the NHTSA API — the basis of the novelty check.

There is no flat file for recalls: RCL.txt documents the format, but
FLAT_RCL.zip and every naming variant return S3 NoSuchKey while the complaints
and investigations archives resolve. So this path is API-only.

Unlike complaints, the recalls endpoint accepts the plain model name — 'F-150'
returns results rather than the 400 the complaints endpoint gives — so the
configured names are used directly and no search-key expansion is needed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from . import config, lakebase
from . import nhtsa_client as api

# Recalls use DD/MM/YYYY where complaints use MM/DD/YYYY. Pinned per feed and
# never inferred: 16/02/2022 and 02/16/2022 are the same day written two ways,
# and a parser that guesses gets it wrong for the first twelve days of a month.
RECALL_DATE_FORMAT = "%d/%m/%Y"


def _parse_date(value: Any) -> tuple[date | None, bool]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, True
    if not isinstance(value, str):
        return None, False
    try:
        return datetime.strptime(value.strip(), RECALL_DATE_FORMAT).date(), True
    except ValueError:
        return None, False


_UPSERT = """
    insert into recalls (
        campaign_number, make, model, model_year, manufacturer, component,
        summary, consequence, remedy, notes, report_received_date,
        park_it, park_outside, over_the_air_update
    ) values (
        %(campaign_number)s, %(make)s, %(model)s, %(model_year)s,
        %(manufacturer)s, %(component)s, %(summary)s, %(consequence)s,
        %(remedy)s, %(notes)s, %(report_received_date)s,
        %(park_it)s, %(park_outside)s, %(over_the_air_update)s
    )
    on conflict (campaign_number, make, model, model_year) do update set
        manufacturer = excluded.manufacturer,
        component = excluded.component,
        summary = excluded.summary,
        consequence = excluded.consequence,
        remedy = excluded.remedy,
        notes = excluded.notes,
        report_received_date = excluded.report_received_date,
        park_it = excluded.park_it,
        park_outside = excluded.park_outside,
        over_the_air_update = excluded.over_the_air_update,
        ingested_at = now()
"""


@dataclass
class RecallReport:
    combos_attempted: int = 0
    combos_failed: list[tuple[str, str, int, str]] = field(default_factory=list)
    fetched: int = 0
    distinct: int = 0
    written: int = 0
    dropped: Counter = field(default_factory=Counter)


def normalise(record: dict, make: str, model: str, model_year: int):
    """Recalls use a capitalised envelope — Count/Message/NHTSACampaignNumber."""
    campaign = (record.get("NHTSACampaignNumber") or "").strip()
    if not campaign:
        return None, "missing_campaign_number"

    received, ok = _parse_date(record.get("ReportReceivedDate"))
    if not ok:
        return None, "report_date_unparseable"

    return (
        {
            "campaign_number": campaign,
            # Prefer what the record says over what we searched for.
            "make": (record.get("Make") or make).strip().upper(),
            "model": (record.get("Model") or model).strip().upper(),
            "model_year": int(record.get("ModelYear") or model_year),
            "manufacturer": record.get("Manufacturer"),
            "component": record.get("Component"),
            "summary": record.get("Summary"),
            "consequence": record.get("Consequence"),
            "remedy": record.get("Remedy"),
            "notes": record.get("Notes"),
            "report_received_date": received,
            "park_it": record.get("parkIt"),
            "park_outside": record.get("parkOutSide"),
            "over_the_air_update": record.get("overTheAirUpdate"),
        },
        None,
    )


def ingest_recalls(
    models: Iterable[tuple[str, str]] | None = None,
    years: Iterable[int] | None = None,
) -> RecallReport:
    models = list(models if models is not None else config.MODELS)
    years = list(years if years is not None else config.MODEL_YEARS)

    report = RecallReport()
    rows: dict[tuple, dict] = {}

    for make, model in models:
        for year in years:
            report.combos_attempted += 1
            try:
                records = api.get_recalls(make, model, year)
            except api.NHTSAError as exc:
                report.combos_failed.append((make, model, year, str(exc)[:120]))
                continue

            report.fetched += len(records)
            for record in records:
                row, reason = normalise(record, make, model, year)
                if reason is not None:
                    report.dropped[reason] += 1
                    continue
                key = (
                    row["campaign_number"], row["make"],
                    row["model"], row["model_year"],
                )
                rows[key] = row

    report.distinct = len(rows)
    if rows:
        with lakebase.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(_UPSERT, list(rows.values()))
            conn.commit()
        report.written = len(rows)

    return report
