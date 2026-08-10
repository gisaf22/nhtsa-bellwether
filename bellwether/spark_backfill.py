"""Spark batch backfill from NHTSA's daily flat complaints file.

The whole file goes into Spark — all 2.2M rows, every make and model. Filtering
to the configured models happens in Spark, not before it, so the batch job sees
the full population and widening the model list never means re-parsing outside
the engine.

Shape of the source (see CMPL.txt): tab-delimited, 51 fields, one row per
complaint *component*, so ODINO repeats. Dedupe collapses those to one row per
complaint with the components aggregated into an array.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from pyspark.sql import DataFrame, SparkSession, functions as F, types as T

from . import config, lakebase

FLAT_CMPL_URL = "https://static.nhtsa.gov/odi/ffdd/cmpl/FLAT_CMPL.zip"

# The flat file is UTF-8. This must be pinned explicitly: latin-1 and cp1252
# both decode every byte without raising, so a wrong choice produces no error
# at all — it silently turns every curly apostrophe and dash into mojibake
# ("Honda's" -> "Hondaâs"). Measured: UTF-8 matches the API on 99.6% of
# summaries, latin-1 on 76.3%.
FLAT_ENCODING = "utf-8"

# Column order from CMPL.txt. Only the fields we keep are named; the rest are
# read and discarded, because the file is positional and skipping is not an
# option.
FIELDS = [
    "CMPLID", "ODINO", "MFR_NAME", "MAKETXT", "MODELTXT", "YEARTXT",
    "CRASH", "FAILDATE", "FIRE", "INJURED", "DEATHS", "COMPDESC",
    "CITY", "STATE", "VIN", "DATEA", "LDATE", "MILES", "OCCURENCES",
    "CDESCR", "CMPL_TYPE", "POLICE_RPT_YN", "PURCH_DT", "ORIG_OWNER_YN",
    "ANTI_BRAKES_YN", "CRUISE_CONT_YN", "NUM_CYLS", "DRIVE_TRAIN",
    "FUEL_SYS", "FUEL_TYPE", "TRANS_TYPE", "VEH_SPEED", "DOT", "TIRE_SIZE",
    "LOC_OF_TIRE", "TIRE_FAIL_TYPE", "ORIG_EQUIP_YN", "MANUF_DT",
    "SEAT_TYPE", "RESTRAINT_TYPE", "DEALER_NAME", "DEALER_TEL",
    "DEALER_CITY", "DEALER_STATE", "DEALER_ZIP", "PROD_TYPE", "REPAIRED_YN",
    "MEDICAL_ATTN", "VEHICLES_TOWED_YN", "STATE_OF_INCIDENT",
    "VEHICLE_OPERATOR",
]

SCHEMA = T.StructType([T.StructField(name, T.StringType(), True) for name in FIELDS])


def download(dest_dir: Path) -> Path:
    """Fetch and unzip the daily flat file. ~352MB compressed, ~1.5GB out."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    txt = dest_dir / "FLAT_CMPL.txt"
    if txt.exists():
        return txt

    with urlopen(FLAT_CMPL_URL, timeout=900) as response:
        payload = response.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as src, open(txt, "wb") as out:
            while chunk := src.read(1 << 22):
                out.write(chunk)
    return txt


def session(app_name: str = "bellwether-backfill") -> SparkSession:
    return (
        SparkSession.builder.appName(app_name)
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.driver.memory", "4g")
        .getOrCreate()
    )


def load(spark: SparkSession, path: Path) -> DataFrame:
    """Read the whole file. No filtering here — that is the job's point."""
    return (
        spark.read.option("sep", "\t")
        .option("header", "false")
        .option("encoding", FLAT_ENCODING)
        .option("quote", "")  # the file is not quoted; a quote char eats tabs
        .option("mode", "PERMISSIVE")
        .schema(SCHEMA)
        .csv(str(path))
    )


def _model_filter(column):
    """Match configured models and their powertrain variants.

    The flat file carries no body styles — 'F-150', not 'F-150 SUPER CREW' —
    so a prefix at a token boundary picks up 'F-150 HYBRID' and
    'F-150 LIGHTNING BEV' without reaching 'F-250'.
    """
    clauses = []
    for make, model in config.MODELS:
        upper = model.upper()
        clauses.append(
            (F.upper(F.col("MAKETXT")) == make.upper())
            & (
                (F.upper(column) == upper)
                | F.upper(column).startswith(upper + " ")
            )
        )
    combined = clauses[0]
    for clause in clauses[1:]:
        combined = combined | clause
    return combined


def transform(raw: DataFrame) -> DataFrame:
    """Filter to configured vehicles, then collapse ODINO to one row.

    Runs entirely in Spark so the full population is visible to the engine.
    """
    vehicles = raw.filter(F.col("PROD_TYPE") == "V")

    scoped = vehicles.filter(
        _model_filter(F.col("MODELTXT"))
        & F.col("YEARTXT").cast("int").isin(list(config.MODEL_YEARS))
    )

    # One row per complaint. Components aggregate; everything else is constant
    # within an ODINO, so first() is safe and cheaper than a window.
    deduped = scoped.groupBy("ODINO").agg(
        F.array_sort(F.collect_set(F.trim(F.col("COMPDESC")))).alias("components"),
        F.first("MAKETXT", ignorenulls=True).alias("make"),
        F.first("MODELTXT", ignorenulls=True).alias("model"),
        F.first("YEARTXT", ignorenulls=True).alias("model_year"),
        F.first("CDESCR", ignorenulls=True).alias("summary"),
        F.first("FAILDATE", ignorenulls=True).alias("faildate"),
        F.first("LDATE", ignorenulls=True).alias("ldate"),
        F.first("CRASH", ignorenulls=True).alias("crash"),
        F.first("FIRE", ignorenulls=True).alias("fire"),
        F.first("INJURED", ignorenulls=True).alias("injured"),
        F.first("DEATHS", ignorenulls=True).alias("deaths"),
    )

    return deduped.select(
        F.col("ODINO").cast("long").alias("odi_number"),
        F.col("make"),
        F.col("model"),
        F.col("model_year").cast("int").alias("model_year"),
        # Full strings kept; the coarse form is derived, never substituted.
        F.col("components"),
        F.array_sort(
            F.array_distinct(
                F.transform(
                    F.col("components"),
                    lambda c: F.trim(F.split(c, ":").getItem(0)),
                )
            )
        ).alias("components_coarse"),
        F.col("summary"),
        F.to_date(F.col("faildate"), "yyyyMMdd").alias("date_of_incident"),
        F.to_date(F.col("ldate"), "yyyyMMdd").alias("date_complaint_filed"),
        (F.col("crash") == "Y").alias("crash"),
        (F.col("fire") == "Y").alias("fire"),
        F.col("injured").cast("int").alias("injuries"),
        F.col("deaths").cast("int").alias("deaths"),
        # VIN is deliberately absent. The flat file carries a fuller VIN than
        # the API does; it is still not wanted, so it is dropped here and
        # never leaves Spark.
    ).filter(F.col("summary").isNotNull())


_UPSERT = """
    insert into complaints (
        odi_number, make, model, model_year, components, components_coarse,
        summary, date_of_incident, date_complaint_filed, crash, fire,
        injuries, deaths, source
    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'flat')
    on conflict (odi_number) do update set
        make = excluded.make, model = excluded.model,
        model_year = excluded.model_year, components = excluded.components,
        components_coarse = excluded.components_coarse,
        summary = excluded.summary,
        date_of_incident = excluded.date_of_incident,
        date_complaint_filed = excluded.date_complaint_filed,
        crash = excluded.crash, fire = excluded.fire,
        injuries = excluded.injuries, deaths = excluded.deaths,
        source = 'flat', ingested_at = now()
"""


@dataclass
class BackfillReport:
    rows_loaded: int = 0
    rows_vehicles: int = 0
    rows_scoped: int = 0
    complaints_deduped: int = 0
    written: int = 0


def backfill(path: Path | None = None, *, batch_size: int = 5000) -> BackfillReport:
    spark = session()
    try:
        source = path or download(Path("data"))
        raw = load(spark, source)

        report = BackfillReport()
        report.rows_loaded = raw.count()
        report.rows_vehicles = raw.filter(F.col("PROD_TYPE") == "V").count()

        scoped = raw.filter(F.col("PROD_TYPE") == "V").filter(
            _model_filter(F.col("MODELTXT"))
            & F.col("YEARTXT").cast("int").isin(list(config.MODEL_YEARS))
        )
        report.rows_scoped = scoped.count()

        final = transform(raw).cache()
        report.complaints_deduped = final.count()

        rows = [tuple(r) for r in final.collect()]
        with lakebase.connect() as conn:
            with conn.cursor() as cur:
                for start in range(0, len(rows), batch_size):
                    cur.executemany(_UPSERT, rows[start:start + batch_size])
            conn.commit()
        report.written = len(rows)
        return report
    finally:
        spark.stop()
