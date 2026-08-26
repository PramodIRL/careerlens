"""What text gets embedded, and how it is cut up.

PURE: rows in, documents out. No database session, no network, no
provider — the ORM classes it accepts are plain Python objects, so every
selection rule and every chunk boundary is testable without a fixture.
Same split as app/skill_matching.py (pure) from app/skill_extraction.py
(persistence).

THIS MODULE IS THE PRIVACY BOUNDARY. Nothing reaches a vector unless a
function here returns it, so the exclusions below are the enforcement
point, not a convention:

  * NEVER the full extracted resume text. Resume-derived content is
    embedded only as `skill_evidence` excerpts — short spans that are
    already shown to the user in the UI.
  * NEVER a storage key, a filesystem path, a URL the user typed, an
    email address, a password hash, a token, or any auth or session
    field.
  * NEVER a whole database row serialized wholesale. Every document
    below is an explicit, named set of fields.
  * NEVER a README body. It is bulk third-party prose, and the
    repository summary carries the candidate's own framing instead.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.models.candidate_skill import CandidateSkill
from app.models.github_repository import GitHubRepository
from app.models.saved_job import SavedJob
from app.models.skill_evidence import SkillEvidence
from app.schemas.embedding import EmbeddingSourceType
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType

# Chunk size for a job description, in characters. Module constants
# rather than settings: these are content-shaping decisions that belong
# with the algorithm and its tests, and making them configurable would
# mean the same description chunks differently on two machines — so the
# same content would hash differently and re-embed for no reason.
#
# 1000 characters is a few paragraphs of a posting: small enough that a
# chunk is about one topic, large enough that a requirements list is not
# shredded across chunks.
JOB_CHUNK_MAX_CHARS = 1000

# Hard ceiling on chunks per description. A posting is capped at 60,000
# characters at the API boundary, which at 1000 characters a chunk is 60
# — so this bound only bites on pathological input, and it bounds the
# rows (and provider calls) one saved job can ever cost.
JOB_CHUNK_MAX_COUNT = 80

# A "blank line": a newline, optional non-newline whitespace, a newline.
_PARAGRAPH_BREAK = re.compile(r"\n[^\S\n]*\n")


@dataclass(frozen=True)
class TextChunk:
    """A contiguous slice of a source text.

    `text` is ALWAYS exactly `source[char_start:char_end]`. Keeping that
    literally true is what lets `embeddings.char_start`/`char_end`
    explain a chunk without the table storing a second copy of the
    prose — a reader can go back to the source row and see the same
    characters.
    """

    index: int
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class EmbeddingDocument:
    """One unit of text to embed, with the metadata it will be stored
    under. Produced only by this module."""

    user_id: uuid.UUID
    source_type: str
    source_id: str
    chunk_index: int
    text: str
    char_start: int | None = None
    char_end: int | None = None


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Shrink [start, end) past leading and trailing whitespace, or None
    if nothing but whitespace is left."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Split on blank lines, returning trimmed spans.

    A blank line is the one boundary a job posting reliably has: it
    separates the intro from the requirements from the benefits. Splitting
    on sentences instead would cut mid-list, and splitting on a fixed
    width would cut mid-word.

    Matched by regex rather than by splitting on a literal "\n\n" so a
    line containing only spaces, tabs or a stray carriage return still
    counts as blank — a posting pasted out of a browser is full of them,
    and treating one as ordinary text would glue two sections together.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        span = _trimmed_span(text, cursor, match.start())
        if span is not None:
            spans.append(span)
        cursor = match.end()
    span = _trimmed_span(text, cursor, len(text))
    if span is not None:
        spans.append(span)
    return spans


def _split_oversized(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Break one over-long span into pieces no longer than `max_chars`,
    preferring a whitespace boundary so words stay whole.

    Falls back to a hard cut when a span has no whitespace at all in the
    window (a pasted base64 blob, a very long URL) — a chunk that is one
    character over the bound is not worth an unbounded search backwards.
    """
    pieces: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > max_chars:
        window_end = cursor + max_chars
        cut = window_end
        while cut > cursor and not text[cut - 1].isspace():
            cut -= 1
        if cut <= cursor:
            cut = window_end
        piece = _trimmed_span(text, cursor, cut)
        if piece is not None:
            pieces.append(piece)
        cursor = cut
    tail = _trimmed_span(text, cursor, end)
    if tail is not None:
        pieces.append(tail)
    return pieces


def chunk_text(text: str, *, max_chars: int, max_chunks: int) -> list[TextChunk]:
    """Cut `text` into bounded, contiguous, deterministic chunks.

    Deterministic in the strict sense: a pure function of (text,
    max_chars, max_chunks), with no randomness, no dictionary-ordering
    dependence and no clock. The same posting always produces the same
    chunks, which is what makes `content_hash` a usable change detector
    — a chunking rule that drifted would re-embed everything.

    Paragraphs are packed greedily rather than emitted one per chunk, so
    a posting written as many short lines does not become many tiny
    vectors.
    """
    spans: list[tuple[int, int]] = []
    for start, end in _paragraph_spans(text):
        if end - start > max_chars:
            spans.extend(_split_oversized(text, start, end, max_chars))
        else:
            spans.append((start, end))

    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and end - merged[-1][0] <= max_chars:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    return [
        TextChunk(index=index, text=text[start:end], char_start=start, char_end=end)
        for index, (start, end) in enumerate(merged[:max_chunks])
    ]


def job_description_documents(saved_job: SavedJob) -> list[EmbeddingDocument]:
    """Chunk one saved job's description.

    Only `description`. NOT `source_url` — that is a URL the user typed,
    which app/models/saved_job.py is explicit is stored and never
    fetched; putting it into embedded text would invite exactly the
    treatment that model refuses it. Company and title are identity, not
    prose, and are already on the row a reader lands on via `source_id`.
    """
    return [
        EmbeddingDocument(
            user_id=saved_job.user_id,
            source_type=EmbeddingSourceType.SAVED_JOB_DESCRIPTION.value,
            source_id=str(saved_job.id),
            chunk_index=chunk.index,
            text=chunk.text,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
        )
        for chunk in chunk_text(
            saved_job.description,
            max_chars=JOB_CHUNK_MAX_CHARS,
            max_chunks=JOB_CHUNK_MAX_COUNT,
        )
    ]


def evidence_documents(
    pairs: Sequence[tuple[CandidateSkill, SkillEvidence]],
) -> list[EmbeddingDocument]:
    """Select the skill evidence worth embedding, as (parent, evidence).

    THREE EXCLUSIONS, each for its own reason:

      * No excerpt -> nothing to embed. `excerpt` is nullable precisely
        because some evidence has no quotable text (a language
        statistic), and embedding a placeholder would be inventing
        content.
      * `manual` source -> a user asserting "I know Python" is a claim,
        not descriptive prose. Its excerpt, when present, is the
        assertion itself; retrieving it semantically would surface the
        user's own input as if it were evidence about their work.
      * `rejected` parent skill -> the user has said this attribution is
        wrong. app/models/candidate_skill.py keeps the row as a
        tombstone rather than deleting it; embedding its evidence would
        quietly re-admit through a second door what the user shut.

    The parent is passed in rather than looked up so this stays pure;
    the caller is responsible for pairing each evidence row with ITS
    candidate skill.
    """
    documents: list[EmbeddingDocument] = []
    for candidate_skill, evidence in pairs:
        if candidate_skill.status == CandidateSkillStatus.REJECTED.value:
            continue
        if evidence.source_type == EvidenceSourceType.MANUAL.value:
            continue
        excerpt = (evidence.excerpt or "").strip()
        if not excerpt:
            continue
        documents.append(
            EmbeddingDocument(
                user_id=candidate_skill.user_id,
                source_type=EmbeddingSourceType.SKILL_EVIDENCE.value,
                source_id=str(evidence.id),
                chunk_index=0,
                text=excerpt,
            )
        )
    return documents


def repository_documents(repositories: Sequence[GitHubRepository]) -> list[EmbeddingDocument]:
    """Summarize a candidate's own public projects.

    Three fields, joined: the repository name, its description, and its
    primary language. That is the candidate's own framing of a project,
    which is what a semantic search over "who has built something like
    this" should be reading.

    EXCLUDED: forks (not the candidate's work — the same judgement
    app/models/github_repository.py already encodes), soft-deleted rows,
    and repositories with neither a description nor a language. A bare
    slug like "test-repo-2" is not a project summary, and embedding it
    would add a vector that can only ever be noise.

    NOT INCLUDED: the README body, stargazer and fork counts, or any
    timestamp. The README is bulk third-party prose; the counts are
    numbers this product deliberately stores without deciding what they
    are worth.
    """
    documents: list[EmbeddingDocument] = []
    for repository in repositories:
        if repository.is_fork or repository.deleted_at is not None:
            continue
        description = (repository.description or "").strip()
        language = (repository.primary_language or "").strip()
        if not description and not language:
            continue
        parts = [repository.name.strip(), description]
        if language:
            parts.append(f"Primary language: {language}")
        documents.append(
            EmbeddingDocument(
                user_id=repository.user_id,
                source_type=EmbeddingSourceType.GITHUB_REPOSITORY.value,
                source_id=str(repository.id),
                chunk_index=0,
                text="\n".join(part for part in parts if part),
            )
        )
    return documents
