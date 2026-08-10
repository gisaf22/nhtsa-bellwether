"""Thin client for the NHTSA API. Returns raw dicts — no parsing, no
normalisation, no field renaming. Callers deal with NHTSA's shapes as they are.

Three behaviours of this API are worth knowing before changing anything here.

1. HTTP 400 comes back with a SUCCESS MESSAGE IN THE BODY. Querying an
   unrecognised model returns status 400 with
   {"count": 0, "message": "Results returned successfully", "results": []}.
   The status code is the only truth. Never trust `message`, and never retry
   a 400 — the request is wrong, not unlucky. This is the failure mode most
   likely to fool the next reader.

2. Unrecognised query parameters are SILENTLY IGNORED. A deliberately
   nonsensical parameter returns the same row count as a real one, and
   /investigations ignores make/model/modelYear entirely. There is no usable
   server-side date filter — do not add one, it will appear to work.

3. Two envelope conventions coexist. Complaints and the discovery endpoints
   use lowercase {"count", "message", "results"}; recalls uses capitalised
   {"Count", "Message", "results"} with capitalised record fields;
   /investigations uses {"meta": {"pagination": ...}, "results": [...]}.
   Handled explicitly below rather than normalised away, because this client
   returns raw dicts.
"""

from __future__ import annotations

import random
import time
from typing import Any, Iterator

import requests

BASE_URL = "https://api.nhtsa.gov"

TIMEOUT = 60
MAX_ATTEMPTS = 5
BACKOFF_BASE = 1.5

# /investigations is slow and 504s intermittently, so it needs the retry path
# more than the others do.
INVESTIGATIONS_PAGE_SIZE = 100


class NHTSAError(RuntimeError):
    """Any non-success response from NHTSA."""


class NHTSAClientError(NHTSAError):
    """4xx — the request is wrong. Never retried."""

    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url


class NHTSAServerError(NHTSAError):
    """5xx or transport failure, after retries were exhausted."""


_session = requests.Session()


def _sleep_for(attempt: int) -> float:
    """Exponential backoff with jitter, so parallel callers don't sync up."""
    return BACKOFF_BASE**attempt + random.uniform(0, 0.5)


def _get(path: str, params: dict[str, Any] | None = None) -> dict:
    """GET one URL, retrying 5xx and transport errors only.

    4xx is never retried: NHTSA returns 400 for an unrecognised make or model,
    and no amount of retrying will make an unrecognised model recognised.
    """
    url = f"{BASE_URL}/{path.lstrip('/')}"
    last_error: Exception | None = None

    for attempt in range(MAX_ATTEMPTS):
        try:
            response = _session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(_sleep_for(attempt))
            continue

        # Status first. The body claims success even when the status does not.
        if response.status_code >= 500:
            last_error = NHTSAServerError(
                f"HTTP {response.status_code} for {response.url}"
            )
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(_sleep_for(attempt))
            continue

        if response.status_code >= 400:
            raise NHTSAClientError(response.status_code, response.url, response.text)

        try:
            return response.json()
        except ValueError as exc:
            raise NHTSAError(f"non-JSON response from {response.url}") from exc

    raise NHTSAServerError(
        f"giving up on {url} after {MAX_ATTEMPTS} attempts: {last_error}"
    )


def _results(payload: dict) -> list[dict]:
    """Pull the record list out of any of the three envelopes.

    All three happen to use a lowercase "results" key even when their count
    and message keys are capitalised.
    """
    results = payload.get("results")
    if results is None:
        raise NHTSAError(f"no results key in response; keys were {sorted(payload)}")
    return results


# --- Discovery -------------------------------------------------------------

# issueType='c' restricts discovery to vehicles that actually have complaints.
_COMPLAINT_ISSUE_TYPE = "c"


def get_makes(model_year: int) -> list[dict]:
    """Makes with complaints for a model year. Records: modelYear, make."""
    payload = _get(
        "products/vehicle/makes",
        {"modelYear": model_year, "issueType": _COMPLAINT_ISSUE_TYPE},
    )
    return _results(payload)


def get_models(model_year: int, make: str) -> list[dict]:
    """Models with complaints for a make and model year.

    NHTSA returns duplicates here — 84 rows collapsing to 52 distinct models
    for FORD 2022 — so identical records are dropped. Order is preserved and
    the surviving dicts are untouched.
    """
    payload = _get(
        "products/vehicle/models",
        {
            "modelYear": model_year,
            "make": make,
            "issueType": _COMPLAINT_ISSUE_TYPE,
        },
    )

    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict] = []
    for record in _results(payload):
        key = (
            str(record.get("modelYear", "")),
            str(record.get("make", "")),
            str(record.get("model", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


# --- Per-vehicle feeds -----------------------------------------------------


def get_complaints(make: str, model: str, model_year: int) -> list[dict]:
    """Every complaint for one combination, in one response — no pagination.

    Raises NHTSAClientError (400) if NHTSA does not recognise the model. That
    is a real signal worth surfacing, not an empty result: it means the model
    string does not match what NHTSA calls the vehicle.
    """
    payload = _get(
        "complaints/complaintsByVehicle",
        {"make": make, "model": model, "modelYear": model_year},
    )
    return _results(payload)


def get_recalls(make: str, model: str, model_year: int) -> list[dict]:
    """Every recall for one combination, in one response — no pagination.

    Note the capitalised envelope: Count/Message, and record fields like
    NHTSACampaignNumber and Manufacturer. Returned as-is.
    """
    payload = _get(
        "recalls/recallsByVehicle",
        {"make": make, "model": model, "modelYear": model_year},
    )
    return _results(payload)


# --- Investigations --------------------------------------------------------


def iter_investigations(page_size: int = INVESTIGATIONS_PAGE_SIZE) -> Iterator[dict]:
    """Page through the whole investigations feed, yielding raw records.

    Unlike complaints and recalls this endpoint paginates, and it silently
    ignores make/model/modelYear — passing them changes nothing, the total
    stays the same. There is no server-side vehicle filter, so the whole feed
    comes down and callers filter locally.
    """
    offset = 0
    total: int | None = None

    while True:
        payload = _get("investigations", {"offset": offset, "max": page_size})
        records = _results(payload)
        pagination = payload.get("meta", {}).get("pagination", {})

        if total is None:
            total = pagination.get("total")

        if not records:
            return

        yield from records

        offset += len(records)
        if total is not None and offset >= total:
            return


def get_investigations(page_size: int = INVESTIGATIONS_PAGE_SIZE) -> list[dict]:
    """The entire investigations feed as raw dicts.

    Takes no make/model/modelYear because NHTSA ignores them — accepting them
    would imply a filter that does not exist. Filter the returned records.
    """
    return list(iter_investigations(page_size=page_size))
