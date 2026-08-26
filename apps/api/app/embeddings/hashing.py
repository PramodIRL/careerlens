"""Deterministic content normalization and hashing for embeddings.

PURE: text in, text or hex digest out. No database, no configuration, no
I/O — the same split app/skill_matching.py has from
app/skill_extraction.py, so the deduplication identity can be tested
without any fixtures.

WHY NOT `hash()`. Python's built-in hash is randomly salted per process
(PYTHONHASHSEED), so it returns a different value for the same string in
the next process. A deduplication key built on it would make every
worker restart recompute every embedding, and would make "has this
content changed?" unanswerable across processes. SHA-256 is stable
across processes, machines and Python versions.

WHY THE MODEL IDENTIFIER IS NOT IN THE HASH. It is its own column and
its own part of the natural key (app/models/embedding.py). Hashing
content ALONE keeps "the same text, embedded by two models" visible as
two rows sharing one hash, which is exactly the question a future
migration between models needs to ask.
"""

import hashlib
import re
import unicodedata

# Any run of whitespace — including newlines and tabs — collapses to a
# single space. Re-flowing a paragraph or converting line endings does
# not change what the text SAYS, and a hash that treated those as new
# content would recompute embeddings for nothing.
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Canonical form of a source text, for hashing and for embedding.

    NFC first, so two byte sequences that render identically (a composed
    "é" and an "e" plus a combining accent) normalize to one — otherwise
    the same visible text pasted from two editors hashes differently.
    Then whitespace runs collapse and the ends are trimmed.

    Deliberately does NOT casefold or strip punctuation. Case and
    punctuation carry meaning to an embedding model ("C" vs "c", "C++"),
    and discarding them here would silently change what gets embedded,
    not just what gets hashed.
    """
    return _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFC", text)).strip()


def content_hash(text: str) -> str:
    """SHA-256 of the normalized text, as 64 lowercase hex characters.

    Deterministic across processes and runs. This is the value stored in
    `embeddings.content_hash` and compared on every write to decide
    whether the provider needs to be called at all.
    """
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()
