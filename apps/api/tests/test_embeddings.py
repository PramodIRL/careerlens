"""Pure tests for the embedding provider, hashing and content selection.

No database and no event loop where one is avoidable — these pin down
the properties everything else rests on: that the same input always
produces the same vector and the same hash, that chunking is a function
of its arguments alone, and that the content helpers refuse to embed
what they are not allowed to embed.

tests/test_embedding_store.py covers the persistence side.
"""

import json
import struct
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest

from app.embeddings.content import (
    JOB_CHUNK_MAX_CHARS,
    EmbeddingDocument,
    chunk_text,
    evidence_documents,
    job_description_documents,
    repository_documents,
)
from app.embeddings.hashing import content_hash, normalize_text
from app.embeddings.provider import (
    MOCK_MODEL_IDENTIFIER,
    MockEmbeddingProvider,
    get_embedding_provider,
)
from app.models.candidate_skill import CandidateSkill
from app.models.github_repository import GitHubRepository
from app.models.saved_job import SavedJob
from app.models.skill_evidence import SkillEvidence
from app.schemas.embedding import EmbeddingSourceType
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType, ExtractionMethod
from app.settings import Settings

_DIMENSION = 384


def _provider(model_identifier: str = MOCK_MODEL_IDENTIFIER) -> MockEmbeddingProvider:
    return MockEmbeddingProvider(dimension=_DIMENSION, model_identifier=model_identifier)


# --- the deterministic mock ------------------------------------------


def test_same_text_and_model_give_an_identical_vector() -> None:
    """The load-bearing property. Every dedup guarantee downstream is
    meaningless if this is not exactly true."""
    first = _provider().embed_text_sync("Built a REST API with FastAPI")
    second = _provider().embed_text_sync("Built a REST API with FastAPI")

    assert first == second
    assert len(first) == _DIMENSION


def test_vector_is_stable_across_processes() -> None:
    """Runs the provider in a FRESH interpreter under a different
    PYTHONHASHSEED and compares byte for byte.

    This is the test that would fail if anyone reached for Python's
    built-in hash(): it is randomly salted per process, so a vector
    derived from it would differ here while looking perfectly
    deterministic inside one run.
    """
    in_process = _provider().embed_text_sync("Deterministic across processes")

    program = (
        "from app.embeddings.provider import MockEmbeddingProvider;"
        "import json;"
        f"print(json.dumps(MockEmbeddingProvider(dimension={_DIMENSION})"
        '.embed_text_sync("Deterministic across processes")))'
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ".", "PYTHONHASHSEED": "1"},
    )

    assert json.loads(completed.stdout) == in_process


def test_different_text_gives_a_different_vector() -> None:
    assert _provider().embed_text_sync("Python") != _provider().embed_text_sync("PostgreSQL")


def test_different_model_identifier_gives_a_different_vector() -> None:
    """A model id is not a label stuck on an unchanged vector — two
    models must produce genuinely different output, or the fake would
    let a broken migration between models pass unnoticed."""
    assert _provider("mock-deterministic-v1").embed_text_sync("Python") != _provider(
        "mock-deterministic-v2"
    ).embed_text_sync("Python")


def test_vector_respects_the_configured_dimension() -> None:
    assert len(MockEmbeddingProvider(dimension=8).embed_text_sync("x")) == 8
    assert len(MockEmbeddingProvider(dimension=384).embed_text_sync("x")) == 384


def test_provider_rejects_a_non_positive_dimension() -> None:
    with pytest.raises(ValueError, match="dimension must be positive"):
        MockEmbeddingProvider(dimension=0)


def test_vector_components_are_float32_and_unit_length() -> None:
    """float32 because pgvector's column is `real`: a float64 component
    would not survive the round trip unchanged, and the store's equality
    assertions depend on it doing so."""
    vector = _provider().embed_text_sync("Docker and Kubernetes")

    for component in vector:
        assert struct.unpack("<f", struct.pack("<f", component))[0] == component
    assert sum(component * component for component in vector) == pytest.approx(1.0, abs=1e-4)


def test_factory_builds_the_mock_and_rejects_an_unknown_provider() -> None:
    settings = Settings(embedding_provider="mock", embedding_dimension=_DIMENSION)
    provider = get_embedding_provider(settings)
    assert provider.model_identifier == MOCK_MODEL_IDENTIFIER
    assert provider.dimension == _DIMENSION

    with pytest.raises(ValueError, match="unknown embedding provider"):
        get_embedding_provider(Settings(embedding_provider="openai"))


# --- content hashing --------------------------------------------------


def test_hash_is_deterministic_and_sha256_shaped() -> None:
    digest = content_hash("FastAPI and PostgreSQL")

    assert digest == content_hash("FastAPI and PostgreSQL")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_hash_ignores_whitespace_reflowing_but_not_wording() -> None:
    """Re-wrapping a paragraph is not a content change; changing a word
    is. Getting the first wrong re-embeds for nothing; getting the
    second wrong serves a stale vector forever."""
    assert content_hash("Python  and\n\tDocker") == content_hash("Python and Docker")
    assert content_hash("Python and Docker") != content_hash("Python and Podman")


def test_hash_ignores_unicode_composition() -> None:
    assert content_hash("Café") == content_hash("Café")


def test_normalize_preserves_case_and_punctuation() -> None:
    """Both carry meaning to a model — "C" is not "c", and "C++" is not
    "C". Normalization collapses whitespace only."""
    assert normalize_text("  C++   and\n C#  ") == "C++ and C#"


def test_hash_is_stable_across_processes() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.embeddings.hashing import content_hash;print(content_hash('stable'))",
        ],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ".", "PYTHONHASHSEED": "0"},
    )
    assert completed.stdout.strip() == content_hash("stable")


# --- job description chunking ----------------------------------------


_JOB_DESCRIPTION = """We are hiring a backend engineer.

You will build REST APIs in Python and FastAPI, and work with
PostgreSQL every day.

Requirements:
- Python
- SQL

Benefits include a learning budget."""


def test_chunking_is_deterministic() -> None:
    assert chunk_text(_JOB_DESCRIPTION, max_chars=80, max_chunks=10) == chunk_text(
        _JOB_DESCRIPTION, max_chars=80, max_chunks=10
    )


def test_chunk_text_is_exactly_the_source_slice() -> None:
    """`char_start`/`char_end` are the whole provenance story — the table
    stores no copy of the text — so they must point at the same
    characters the chunk holds."""
    for chunk in chunk_text(_JOB_DESCRIPTION, max_chars=80, max_chunks=10):
        assert chunk.text == _JOB_DESCRIPTION[chunk.char_start : chunk.char_end]
        assert chunk.text == chunk.text.strip()


def test_chunks_are_bounded_ordered_and_cover_the_text() -> None:
    chunks = chunk_text(_JOB_DESCRIPTION, max_chars=80, max_chunks=10)

    assert len(chunks) > 1, "the fixture should not fit in a single 80-char chunk"
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert all(len(chunk.text) <= 80 for chunk in chunks)
    assert [chunk.char_start for chunk in chunks] == sorted(chunk.char_start for chunk in chunks), (
        "chunks must stay in source order"
    )
    assert "Benefits" in chunks[-1].text


def test_chunk_count_is_capped() -> None:
    assert len(chunk_text("word\n\n" * 200, max_chars=10, max_chunks=3)) == 3


def test_a_paragraph_longer_than_the_bound_is_split() -> None:
    chunks = chunk_text("alpha beta " * 50, max_chars=40, max_chunks=20)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 40 for chunk in chunks)


def test_a_run_with_no_whitespace_is_still_bounded() -> None:
    """A pasted blob has no word boundary to prefer, so the cut is hard
    rather than unbounded."""
    assert all(len(chunk.text) <= 20 for chunk in chunk_text("x" * 100, max_chars=20, max_chunks=9))


def test_empty_description_produces_no_chunks() -> None:
    assert chunk_text("   \n\n  ", max_chars=100, max_chunks=5) == []


def test_job_description_documents_carry_job_identity_and_offsets() -> None:
    job = SavedJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        company="Example Ltd",
        title="Backend Engineer",
        description=_JOB_DESCRIPTION,
        source_url="https://example.com/jobs/1",
    )

    documents = job_description_documents(job)

    assert documents
    assert all(document.user_id == job.user_id for document in documents)
    assert all(document.source_id == str(job.id) for document in documents)
    assert all(
        document.source_type == EmbeddingSourceType.SAVED_JOB_DESCRIPTION.value
        for document in documents
    )
    assert [document.chunk_index for document in documents] == list(range(len(documents)))
    assert all(document.char_start is not None for document in documents)
    # The URL the user typed is stored and never fetched; it is not
    # content, and it must not travel into an embedded document either.
    assert all("example.com" not in document.text for document in documents)


def test_a_short_description_is_one_chunk() -> None:
    job = SavedJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        company="Example Ltd",
        title="Backend Engineer",
        description="We need a Python developer.",
    )

    documents = job_description_documents(job)

    assert len(documents) == 1
    assert documents[0].chunk_index == 0
    assert documents[0].text == "We need a Python developer."
    assert len("We need a Python developer.") < JOB_CHUNK_MAX_CHARS


# --- candidate-side content selection ---------------------------------


def _candidate_skill(user_id: uuid.UUID, status: CandidateSkillStatus) -> CandidateSkill:
    return CandidateSkill(
        id=uuid.uuid4(), user_id=user_id, skill_id=uuid.uuid4(), status=status.value
    )


def _evidence(source_type: EvidenceSourceType, excerpt: str | None) -> SkillEvidence:
    return SkillEvidence(
        id=uuid.uuid4(),
        candidate_skill_id=uuid.uuid4(),
        source_type=source_type.value,
        source_identifier=str(uuid.uuid4()),
        excerpt=excerpt,
        extraction_method=ExtractionMethod.RESUME_ALIAS_MATCH.value,
        confidence=1,
    )


def test_evidence_documents_keep_the_excerpt_and_the_owner() -> None:
    user_id = uuid.uuid4()
    skill = _candidate_skill(user_id, CandidateSkillStatus.CONFIRMED)
    evidence = _evidence(EvidenceSourceType.RESUME, "Built a REST API with FastAPI")

    documents = evidence_documents([(skill, evidence)])

    assert documents == [
        EmbeddingDocument(
            user_id=user_id,
            source_type=EmbeddingSourceType.SKILL_EVIDENCE.value,
            source_id=str(evidence.id),
            chunk_index=0,
            text="Built a REST API with FastAPI",
        )
    ]


def test_evidence_without_an_excerpt_is_not_embedded() -> None:
    skill = _candidate_skill(uuid.uuid4(), CandidateSkillStatus.CONFIRMED)

    assert evidence_documents([(skill, _evidence(EvidenceSourceType.GITHUB, None))]) == []
    assert evidence_documents([(skill, _evidence(EvidenceSourceType.GITHUB, "   "))]) == []


def test_manual_evidence_is_not_embedded() -> None:
    skill = _candidate_skill(uuid.uuid4(), CandidateSkillStatus.CONFIRMED)

    assert (
        evidence_documents([(skill, _evidence(EvidenceSourceType.MANUAL, "I know Python"))]) == []
    )


def test_rejected_skills_evidence_is_not_embedded() -> None:
    """A rejection is a decision the user made. Embedding its evidence
    would let it back in through a door the user cannot see."""
    skill = _candidate_skill(uuid.uuid4(), CandidateSkillStatus.REJECTED)

    assert (
        evidence_documents([(skill, _evidence(EvidenceSourceType.RESUME, "Used Go daily"))]) == []
    )


def _repository(**overrides: object) -> GitHubRepository:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "github_repo_id": 1,
        "name": "portfolio-api",
        "full_name": "candidate/portfolio-api",
        "description": "A FastAPI service for tracking job applications",
        "primary_language": "Python",
        "is_fork": False,
        "readme_text": "SECRET-README-BODY",
    }
    values.update(overrides)
    return GitHubRepository(**values)


def test_repository_document_summarizes_name_description_and_language() -> None:
    repository = _repository()

    documents = repository_documents([repository])

    assert len(documents) == 1
    assert documents[0].user_id == repository.user_id
    assert documents[0].source_id == str(repository.id)
    assert documents[0].source_type == EmbeddingSourceType.GITHUB_REPOSITORY.value
    assert documents[0].text == (
        "portfolio-api\nA FastAPI service for tracking job applications\nPrimary language: Python"
    )
    # The README is bulk third-party prose and stays out of embeddings.
    assert "SECRET-README-BODY" not in documents[0].text


def test_forks_and_deleted_repositories_are_not_embedded() -> None:
    assert repository_documents([_repository(is_fork=True)]) == []
    assert repository_documents([_repository(deleted_at=datetime.now(UTC))]) == []


def test_a_repository_with_nothing_to_say_is_not_embedded() -> None:
    assert repository_documents([_repository(description=None, primary_language=None)]) == []
