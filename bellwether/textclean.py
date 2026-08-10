"""Strip complaint boilerplate, keeping the failure description.

Measured against the corpus (20,848 complaints), the recurring material falls
into three groups:

1. ODI hotline transcription. 18.1% of complaints are typed by a NHTSA agent
   to a fixed template — "The contact owns a 2021 Ford F-150", "The
   manufacturer was made aware of the failure", "The failure mileage was
   approximately 40,000". None of it describes the failure.
2. Owner-written complaint genre. "I am writing to report", "I request that
   NHTSA investigate", "Thank you for your attention to this matter".
3. Administrative artefacts. The FOIA redaction notice (4.5%), VIN and case
   number recitals, "*TR" transcription markers.

Stripping is sentence-level rather than inline: a sentence either is
boilerplate or is not, and deleting fragments mid-sentence leaves text that
reads as neither. The original is never modified — the stripped form is stored
alongside it.
"""

from __future__ import annotations

import re

# Sentence is dropped entirely when it matches. Ordered loosely by frequency.
BOILERPLATE = [
    # --- ODI hotline template ---
    r"^the contact (owns|leases|purchased)\b",
    r"\bthe (local )?dealer was (contacted|notified|made aware)\b",
    r"\bthe manufacturer was (not )?(made aware|notified|contacted)\b",
    r"\b(failure|approximate) mileage was\b",
    r"^the vehicle was not (repaired|diagnosed)",
    r"^the vehicle was (taken|towed) to (the|a) (local )?dealer",
    r"\bthe contact had not experienced a failure\b",
    r"\bthe contact (was|has been) (referred|advised)\b",
    r"\bparts distribution disconnect\b",
    r"\bvin tool confirms parts not available\b",
    r"\bthe manufacturer had exceeded a reasonable amount of time\b",
    r"\bthe vin was not available\b",
    r"^the contact (stated|reached out|received)\b.*\b(recall|remedy|parts)\b",

    # --- Administrative artefacts ---
    r"information redacted pursuant to the freedom of information act",
    r"^\W*\*?tr\W*$",
    r"^see attached document",
    r"\b(case|claim|reference) (number|#)\s*[:\-]?\s*\w+",
    r"\bvin\b\s*[:#]?\s*[a-hj-npr-z0-9]{9,}",

    # --- Owner-written complaint genre ---
    r"^i am (writing|reporting|filing|submitting) (this|a|to)\b",
    r"\b(i|we) (request|urge|ask|petition)\w*\s+(that\s+)?(the\s+)?nhtsa\b",
    r"\bnhtsa (to )?(investigate|look into|review)\b",
    r"\bplease (investigate|review|look into) (this|the)\b",
    r"\bthank you for your (attention|time|consideration)\b",
    r"^thank you\W*$",
    r"\bplease (feel free to )?contact me\b",
    r"\bi (would like to )?(formally )?report(ing)? (this|a|an)\b.*\b(defect|incident|issue)\b",
    r"\b(warrant|justif)\w*\s+(a\s+)?(recall|investigation|review)\b",
    r"\bi (believe|feel) this (should|warrants|merits)\b",
    r"\bi look forward to\b",
    r"^sincerely\W*",
    r"^(hello|dear)\b.*\bnhtsa\b",
]

_COMPILED = [re.compile(p, re.I) for p in BOILERPLATE]
_SENTENCE = re.compile(r"(?<=[.!?])\s+")

# Below this, the stripped text carries too little to embed meaningfully.
MIN_USEFUL_CHARS = 40


def is_boilerplate(sentence: str) -> bool:
    s = sentence.strip()
    if not s:
        return True
    return any(rx.search(s) for rx in _COMPILED)


def strip_with_flag(summary: str) -> tuple[str, bool]:
    """Return (text, fell_back).

    `fell_back` is True when stripping left too little to embed and the
    original was kept instead: a near-empty string embeds to a vector that
    clusters with every other near-empty string, which is worse than keeping
    the boilerplate. Those rows are reported rather than silently embedded.
    """
    if not summary:
        return summary or "", False

    sentences = [s for s in _SENTENCE.split(summary) if s.strip()]
    kept = [s.strip() for s in sentences if not is_boilerplate(s)]
    stripped = re.sub(r"\s+", " ", " ".join(kept)).strip()

    if len(stripped) < MIN_USEFUL_CHARS:
        return summary.strip(), True
    return stripped, False


def strip_boilerplate(summary: str) -> str:
    return strip_with_flag(summary)[0]
