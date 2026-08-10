"""Batch embedding of complaint narratives in Spark.

Vectors come from the workspace's `databricks-gte-large-en` serving endpoint,
1024 dimensions, matching config.EMBEDDING_DIM.

The endpoint caps inputs per request: 8 succeeds, 16 returns
REQUEST_LIMIT_EXCEEDED immediately. That is a per-request size limit rather
than a rate limit, so the work parallelises across Spark partitions provided
each request stays within the cap.
"""

from __future__ import annotations

import os
import random
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterator

import requests
from pyspark.sql import SparkSession, types as T

from . import config, lakebase

EMBEDDING_ENDPOINT = "databricks-gte-large-en"
WORKSPACE_HOST = os.getenv(
    "DATABRICKS_HOST", "https://dbc-d547a350-6525.cloud.databricks.com"
).rstrip("/")

# Measured ceiling: 8 inputs succeed, 16 is rejected outright.
MAX_INPUTS_PER_REQUEST = 8
# The endpoint also throttles by concurrency, not just request size: 8 Spark
# partitions firing batches of 8 exhausted six attempts and failed the job.
# Measured throughput — 1 worker 14 texts/s clean, 4 workers 51 texts/s with
# ~25% 429s that backoff absorbs. Hence deep retries and modest parallelism.
MAX_ATTEMPTS = 10
BACKOFF_CAP = 60
# Small gap after each success so partitions drift out of lockstep.
PACING_SECONDS = 0.1
REQUEST_TIMEOUT = 120

# gte-large-en truncates beyond its context anyway; cutting here keeps request
# bodies predictable. Complaint summaries top out around 2,048 characters.
MAX_CHARS = 4000


def _endpoint_url() -> str:
    return f"{WORKSPACE_HOST}/serving-endpoints/{EMBEDDING_ENDPOINT}/invocations"


def embed_batch(
    texts: list[str],
    token: str | Callable[..., str],
    session: requests.Session,
) -> list[list[float]]:
    """Embed up to MAX_INPUTS_PER_REQUEST texts, retrying on 429 and 5xx.

    `token` may be a callable so the credential can be re-minted mid-run. The
    OAuth token lives an hour and embedding the corpus takes longer than that
    from a cached one, so a full pass will hit 403 Invalid Token part way
    through unless it can refresh.
    """
    payload = {"input": [t[:MAX_CHARS] for t in texts]}
    get = token if callable(token) else (lambda **_: token)
    headers = {"Authorization": f"Bearer {get()}"}

    last = ""
    for attempt in range(MAX_ATTEMPTS):
        response = session.post(
            _endpoint_url(), headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        )
        last = f"{response.status_code}: {response.text[:200]}"

        if response.status_code in (401, 403) and callable(token):
            headers = {"Authorization": f"Bearer {get(force=True)}"}
            continue
        if response.status_code == 200:
            data = response.json()["data"]
            # The endpoint echoes an index; order is not promised, so sort.
            vectors = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
            time.sleep(PACING_SECONDS)
            return vectors
        if response.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(2**attempt + random.uniform(0, 1.0), BACKOFF_CAP))
            continue
        raise RuntimeError(
            f"embedding endpoint returned {response.status_code}: {response.text[:200]}"
        )

    raise RuntimeError(
        f"embedding endpoint still failing after {MAX_ATTEMPTS} attempts; last {last}"
    )


def _insert_sql(vector_column: str) -> str:
    return f"""
        insert into complaint_embeddings (odi_number, {vector_column}, model)
        values (%s, %s, %s)
        on conflict (odi_number) do update set
            {vector_column} = excluded.{vector_column},
            model = excluded.model,
            embedded_at = now()
    """


def _embed_partition(rows: Iterator, vector_column: str) -> Iterator:
    """Runs on the executor: batch the partition, embed, and persist.

    Writes as it goes rather than returning vectors to the driver. Embedding
    the corpus takes tens of minutes, and a failure most of the way through
    must not discard that work — with rows persisted, a rerun picks up only
    what is still missing.

    A DB connection is opened fresh for each write rather than held for the
    partition's lifetime. embed_batch() can legitimately take minutes under
    endpoint throttling (up to MAX_ATTEMPTS retries, each up to BACKOFF_CAP
    seconds), and a Postgres connection left idle that long gets closed
    server-side — observed as "the connection is closed" on the very next
    write. The fix is to not hold a connection across a call that can block
    that long, not to retry harder once it has already been dropped.
    """
    def token(*, force: bool = False) -> str:
        return lakebase.mint_token(force=force)

    session = requests.Session()
    buffer: list[tuple[int, str]] = []
    written = 0
    insert_sql = _insert_sql(vector_column)

    def flush() -> int:
        if not buffer:
            return 0
        vectors = embed_batch([t for _, t in buffer], token, session)
        rows_to_write = [
            (odi, str(list(vec)), EMBEDDING_ENDPOINT)
            for (odi, _), vec in zip(buffer, vectors)
        ]
        # A bare TLS blip on the write ("bad record mac") is unrelated to the
        # idle-timeout issue above — the connection here is freshly opened,
        # not held — so this retries with a fresh connection rather than
        # reusing the broken one, distinct from and in addition to that fix.
        import psycopg

        for attempt in range(3):
            try:
                with lakebase.connect() as conn:
                    with conn.cursor() as cur:
                        cur.executemany(insert_sql, rows_to_write)
                    conn.commit()
                break
            except psycopg.OperationalError:
                if attempt == 2:
                    raise
                time.sleep(2**attempt)
        n = len(buffer)
        buffer.clear()
        return n

    for row in rows:
        buffer.append((row.odi_number, row.text))
        if len(buffer) >= MAX_INPUTS_PER_REQUEST:
            written += flush()
    written += flush()

    yield written


_RECALL_UPSERT = """
    insert into recall_embeddings
        (campaign_number, make, model, model_year, embedding, model_name)
    values (%s, %s, %s, %s, %s, %s)
    on conflict (campaign_number, make, model, model_year) do update set
        embedding = excluded.embedding,
        model_name = excluded.model_name,
        embedded_at = now()
"""


def embed_recalls(*, only_missing: bool = True) -> "EmbedReport":
    """Embed recall summary+consequence. Driver-local — a few hundred rows."""
    conditions = ["coalesce(r.summary, r.consequence) is not null"]
    if only_missing:
        conditions.append(
            "not exists (select 1 from recall_embeddings e "
            "where e.campaign_number = r.campaign_number and e.make = r.make "
            "and e.model = r.model and e.model_year = r.model_year "
            "and e.embedding is not null)"
        )
    pending = lakebase.execute(
        f"""
        select r.campaign_number, r.make, r.model, r.model_year,
               trim(concat_ws(' ', r.summary, r.consequence))
        from recalls r where {' and '.join(conditions)}
        """,
        fetch="all",
    )

    report = EmbedReport(to_embed=len(pending))
    if not pending:
        return report

    session = requests.Session()
    token = lambda *, force=False: lakebase.mint_token(force=force)

    for start in range(0, len(pending), MAX_INPUTS_PER_REQUEST):
        batch = pending[start : start + MAX_INPUTS_PER_REQUEST]
        vectors = embed_batch([row[4] for row in batch], token, session)
        rows = [
            (row[0], row[1], row[2], row[3], str(list(vec)), EMBEDDING_ENDPOINT)
            for row, vec in zip(batch, vectors)
        ]
        with lakebase.connect() as conn:
            with conn.cursor() as cur:
                cur.executemany(_RECALL_UPSERT, rows)
            conn.commit()
        report.embedded += len(rows)

    report.written = report.embedded
    return report


@dataclass
class EmbedReport:
    to_embed: int = 0
    embedded: int = 0
    written: int = 0
    dim: int = config.EMBEDDING_DIM


def embed_all(
    *,
    text_column: str = "summary",
    vector_column: str = "embedding",
    partitions: int = 1,
    only_missing: bool = True,
) -> EmbedReport:
    """Embed a text column into its matching vector column.

    `partitions` defaults to 1: a prior run at 8 partitions exhausted retries
    under concurrent load and failed outright, where 1 worker ran 40/40
    batches clean at 14 texts/s. Raise it only with the concurrency behaviour
    in mind — see embed_batch's docstring and MAX_ATTEMPTS/BACKOFF_CAP above.
    """
    conditions = [f"c.{text_column} is not null"]
    if only_missing:
        conditions.append(
            f"not exists (select 1 from complaint_embeddings e "
            f"where e.odi_number = c.odi_number and e.{vector_column} is not null)"
        )
    pending = lakebase.execute(
        f"select c.odi_number, c.{text_column} from complaints c "
        f"where {' and '.join(conditions)}",
        fetch="all",
    )

    report = EmbedReport(to_embed=len(pending))
    if not pending:
        return report

    # Python workers must run the same interpreter as the driver: Spark
    # otherwise picks whatever `python3` resolves to on PATH and fails with a
    # version mismatch once a Python UDF is involved.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    spark = (
        SparkSession.builder.appName("bellwether-embed")
        .config("spark.driver.memory", "4g")
        .getOrCreate()
    )
    try:
        schema = T.StructType([
            T.StructField("odi_number", T.LongType()),
            T.StructField("text", T.StringType()),
        ])
        df = spark.createDataFrame(
            [(int(o), t) for o, t in pending], schema=schema
        ).repartition(partitions)

        per_partition = df.rdd.mapPartitions(
            lambda rows: _embed_partition(rows, vector_column)
        ).collect()
        report.embedded = sum(per_partition)
        report.written = report.embedded
        return report
    finally:
        spark.stop()
