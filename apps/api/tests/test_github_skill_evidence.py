"""Tests for GitHub-derived skill evidence (app/github/skill_evidence.py,
Prompt 3.3).

NO NETWORK and no fake GitHub client: this module reads repository rows
that Prompt 3.2 already stored, so the fixtures write those rows directly.
That is the separation being tested as much as it is a convenience — if
these tests ever needed a GitHub client, the split would have leaked.

Runs against the real seeded taxonomy rather than a hand-built one (same
choice as tests/test_skill_extraction.py), so these also catch a seed
change that would break GitHub matching.

The load-bearing tests here, which should not be softened:

  * a byte-identical rerun changes NOTHING — asserted on row ids and
    timestamps, not merely on the absence of duplicates
  * topics are never concatenated before matching (the false-positive
    this design exists to prevent)
  * confirmed survives, rejected is never resurrected
  * resume and manual evidence are never touched
  * a rename reconciles instead of duplicating
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.github.skill_evidence import (
    extract_github_skill_evidence,
    purge_github_skill_evidence,
)
from app.models.candidate_skill import CandidateSkill
from app.models.github_repository import (
    GitHubRepository,
    GitHubRepositoryLanguage,
    GitHubRepositoryTopic,
)
from app.models.skill import Skill, SkillAlias
from app.models.skill_evidence import SkillEvidence
from app.models.user import User
from app.schemas.skill import CandidateSkillStatus, EvidenceSourceType, ExtractionMethod
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy
from tests.conftest import TEST_SCHEMA

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}

_README = ExtractionMethod.GITHUB_README_MATCH.value
_DESCRIPTION = ExtractionMethod.GITHUB_DESCRIPTION_MATCH.value
_TOPIC = ExtractionMethod.GITHUB_TOPIC_MATCH.value
_LANGUAGE = ExtractionMethod.GITHUB_LANGUAGE_MATCH.value


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(
        get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


# --------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------


async def _seeded_user(db: AsyncSession) -> uuid.UUID:
    await seed_skill_taxonomy(db)
    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    return user_id


_NEXT_REPO_ID = [1000]


async def _add_repository(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    full_name: str = "ada/project",
    description: str | None = None,
    readme_text: str | None = None,
    topics: tuple[str, ...] = (),
    languages: dict[str, int] | None = None,
    primary_language: str | None = None,
    is_fork: bool = False,
    is_archived: bool = False,
    deleted_at: datetime | None = None,
    github_repo_id: int | None = None,
) -> GitHubRepository:
    """Write the repository rows Prompt 3.2's ingestion would have
    written, directly — this module reads them, it never fetches."""
    _NEXT_REPO_ID[0] += 1
    repository = GitHubRepository(
        id=uuid.uuid4(),
        user_id=user_id,
        github_repo_id=github_repo_id if github_repo_id is not None else _NEXT_REPO_ID[0],
        name=full_name.split("/")[-1],
        full_name=full_name,
        description=description,
        readme_text=readme_text,
        readme_sha="deadbeef" if readme_text else None,
        is_fork=is_fork,
        is_archived=is_archived,
        primary_language=primary_language,
        deleted_at=deleted_at,
    )
    db.add(repository)
    await db.flush()
    for topic in topics:
        db.add(GitHubRepositoryTopic(repository_id=repository.id, topic=topic))
    for language, byte_count in (languages or {}).items():
        db.add(
            GitHubRepositoryLanguage(
                repository_id=repository.id, language=language, byte_count=byte_count
            )
        )
    await db.commit()
    return repository


async def _skill_named(db: AsyncSession, name: str) -> Skill:
    skill = await db.scalar(select(Skill).where(Skill.slug == name.casefold()))
    assert skill is not None, f"expected {name!r} in the seeded taxonomy"
    return skill


async def _candidate_skill_for(
    db: AsyncSession, user_id: uuid.UUID, name: str
) -> CandidateSkill | None:
    skill = await _skill_named(db, name)
    return await db.scalar(
        select(CandidateSkill).where(
            CandidateSkill.user_id == user_id, CandidateSkill.skill_id == skill.id
        )
    )


async def _names_for(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    rows = (
        await db.scalars(
            select(Skill.name)
            .join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
            .where(CandidateSkill.user_id == user_id)
        )
    ).all()
    return set(rows)


async def _evidence_for(db: AsyncSession, user_id: uuid.UUID, name: str) -> list[SkillEvidence]:
    candidate_skill = await _candidate_skill_for(db, user_id, name)
    if candidate_skill is None:
        return []
    return list(
        (
            await db.scalars(
                select(SkillEvidence)
                .where(SkillEvidence.candidate_skill_id == candidate_skill.id)
                .order_by(SkillEvidence.extraction_method, SkillEvidence.source_identifier)
            )
        ).all()
    )


async def _snapshot(db: AsyncSession, user_id: uuid.UUID) -> set[tuple]:
    """Ids AND timestamps — the strict form of the idempotency claim."""
    db.expire_all()
    candidate_skills = (
        await db.scalars(select(CandidateSkill).where(CandidateSkill.user_id == user_id))
    ).all()
    rows: set[tuple] = {
        (cs.id, cs.skill_id, cs.status, cs.created_at, cs.updated_at) for cs in candidate_skills
    }
    evidence = (
        await db.scalars(
            select(SkillEvidence).where(
                SkillEvidence.candidate_skill_id.in_([cs.id for cs in candidate_skills])
            )
        )
    ).all()
    rows |= {
        (
            e.id,
            e.candidate_skill_id,
            e.source_type,
            e.source_identifier,
            e.extraction_method,
            e.excerpt,
            e.confidence,
            e.created_at,
            e.updated_at,
        )
        for e in evidence
    }
    return rows


async def _add_manual_evidence(
    db: AsyncSession, candidate_skill_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    db.add(
        SkillEvidence(
            candidate_skill_id=candidate_skill_id,
            source_type=EvidenceSourceType.MANUAL.value,
            source_identifier=str(user_id),
            excerpt=None,
            extraction_method=ExtractionMethod.MANUAL_ENTRY.value,
            confidence=Decimal("1.00"),
        )
    )
    await db.commit()


# --------------------------------------------------------------------
# Per-signal matching
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_readme_canonical_match(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(
        db,
        user_id,
        full_name="ada/scheduler",
        readme_text="# Scheduler\n\nBuilt with FastAPI and deployed with Docker.\n",
    )

    await extract_github_skill_evidence(db, user_id)

    # Docker has no aliases, so this is an unambiguous canonical match.
    # (FastAPI deliberately is NOT used here: its alias "fast api" is a
    # LONGER term string than the canonical "FastAPI", so Prompt 2.4's
    # longest-first ordering claims the span as an alias before the
    # canonical spelling is ever tried. That is pre-existing matcher
    # behaviour shared with resume extraction, not something Prompt 3.3
    # introduces — see test_a_longer_alias_outranks_its_own_canonical_name.)
    evidence = await _evidence_for(db, user_id, "Docker")
    assert len(evidence) == 1
    assert evidence[0].source_type == EvidenceSourceType.GITHUB.value
    assert evidence[0].source_identifier == "ada/scheduler"
    assert evidence[0].extraction_method == _README
    assert evidence[0].confidence == Decimal("0.90")
    # A verbatim slice of the README, not a paraphrase.
    assert evidence[0].excerpt == "Built with FastAPI and deployed with Docker."
    # Both skills are found; only the confidence differs.
    assert {"Docker", "FastAPI"} <= await _names_for(db, user_id)


@pytest.mark.anyio
async def test_a_longer_alias_outranks_its_own_canonical_name(db: AsyncSession) -> None:
    """A pre-existing quirk of Prompt 2.4's matcher, pinned here so it is
    a known property rather than a surprise.

    `find_skill_matches` tries terms longest-first and each accepted match
    claims its span, so where an ALIAS string is longer than the skill's
    own canonical name — "fast api" (8) vs "FastAPI" (7) — the alias
    claims the span first and the canonical spelling can never be
    reached. The text says "FastAPI" but the evidence records alias
    confidence.

    Prompt 3.3 reuses the matcher unchanged, so it inherits this
    identically to resume extraction. Recorded, not worked around: fixing
    it belongs in the matcher, and changing matching behaviour is outside
    this slice.
    """
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, readme_text="Built with FastAPI.")

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "FastAPI")
    assert len(evidence) == 1
    assert evidence[0].confidence == Decimal("0.75")


@pytest.mark.anyio
async def test_readme_alias_match_scores_lower_than_canonical(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, readme_text="Deployed onto k8s in production.")

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Kubernetes")
    assert len(evidence) == 1
    assert evidence[0].confidence == Decimal("0.75")
    assert "k8s" in (evidence[0].excerpt or "")


@pytest.mark.anyio
async def test_description_match(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(
        db, user_id, full_name="ada/api", description="A Django service for course scheduling."
    )

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Django")
    assert len(evidence) == 1
    assert evidence[0].extraction_method == _DESCRIPTION
    assert evidence[0].excerpt == "A Django service for course scheduling."


@pytest.mark.anyio
async def test_topic_match_quotes_the_topic_verbatim(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, topics=("rest-api", "postgres"))

    await extract_github_skill_evidence(db, user_id)

    rest = await _evidence_for(db, user_id, "REST APIs")
    assert len(rest) == 1
    assert rest[0].extraction_method == _TOPIC
    # The topic slug itself — authored text, so it is quotable.
    assert rest[0].excerpt == "rest-api"
    # "rest api" is an alias, so this is alias-confidence.
    assert rest[0].confidence == Decimal("0.75")

    postgres = await _evidence_for(db, user_id, "PostgreSQL")
    assert [e.excerpt for e in postgres] == ["postgres"]


@pytest.mark.anyio
async def test_language_match_stores_a_null_excerpt(db: AsyncSession) -> None:
    """A language is a computed byte statistic, not authored text. There
    is nothing to quote, and inventing a restatement would be exactly the
    fabricated filler Prompt 2.3 made this column nullable to avoid."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"Python": 82_341})

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Python")
    assert len(evidence) == 1
    assert evidence[0].extraction_method == _LANGUAGE
    assert evidence[0].excerpt is None
    assert evidence[0].confidence == Decimal("0.90")


@pytest.mark.anyio
async def test_primary_language_alone_still_produces_evidence(db: AsyncSession) -> None:
    """A repository beyond Prompt 3.2's detail cap has no language rows,
    but its primary_language came from the listing — so it is still the
    only language signal available, and must not be dropped."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, primary_language="Java", languages=None)

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Java")
    assert len(evidence) == 1
    assert evidence[0].extraction_method == _LANGUAGE


@pytest.mark.anyio
async def test_primary_language_does_not_duplicate_a_language_row(db: AsyncSession) -> None:
    """primary_language is GitHub's top entry from the SAME breakdown, so
    it is a member of the language set, never a second signal."""
    user_id = await _seeded_user(db)
    await _add_repository(
        db, user_id, primary_language="Python", languages={"Python": 90_000, "HTML": 500}
    )

    await extract_github_skill_evidence(db, user_id)

    python = await _evidence_for(db, user_id, "Python")
    assert len(python) == 1
    assert len(await _evidence_for(db, user_id, "HTML")) == 1


@pytest.mark.anyio
async def test_a_repository_mentioning_nothing_writes_nothing(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(
        db,
        user_id,
        description="A collection of poems.",
        readme_text="Poems written between 2019 and 2021.",
        topics=("poetry", "writing"),
    )

    summary = await extract_github_skill_evidence(db, user_id)

    assert summary.skills_matched == 0
    assert await _names_for(db, user_id) == set()


# --------------------------------------------------------------------
# False positives
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_word_boundaries_prevent_false_language_matches(db: AsyncSession) -> None:
    """GitHub reports "Dockerfile", "Makefile" and "Shell" as languages.
    None of them is a taxonomy skill, and "Dockerfile" must not match
    Docker — the matcher's boundary guard is what prevents it."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"Dockerfile": 400, "Makefile": 120, "Shell": 900})

    await extract_github_skill_evidence(db, user_id)

    assert await _names_for(db, user_id) == set()


@pytest.mark.anyio
async def test_java_does_not_match_javascript(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"JavaScript": 5_000})

    await extract_github_skill_evidence(db, user_id)

    names = await _names_for(db, user_id)
    assert "JavaScript" in names
    assert "Java" not in names


@pytest.mark.anyio
async def test_topics_are_never_concatenated_before_matching(db: AsyncSession) -> None:
    """THE false positive this design exists to prevent.

    The matcher tolerates whitespace between a term's parts, so joining a
    repository's topics into one string for a single pass makes topics
    ["unit", "testing"] match the skill "Unit Testing" — which neither
    topic asserts. Each topic must get its own matcher pass.
    """
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, topics=("unit", "testing"))

    await extract_github_skill_evidence(db, user_id)

    assert "Unit Testing" not in await _names_for(db, user_id)


@pytest.mark.anyio
async def test_languages_are_never_concatenated_before_matching(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"Unit": 10, "Testing": 10})

    await extract_github_skill_evidence(db, user_id)

    assert "Unit Testing" not in await _names_for(db, user_id)


@pytest.mark.anyio
async def test_an_ambiguous_term_in_prose_is_not_matched(db: AsyncSession) -> None:
    """ "Go" in README prose is the English verb far more often than the
    language, so the existing list-context guard applies here exactly as
    it does to resume text."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, readme_text="Run the tests and go to the dashboard.")

    await extract_github_skill_evidence(db, user_id)

    assert "Go" not in await _names_for(db, user_id)


@pytest.mark.anyio
async def test_an_ambiguous_term_as_a_topic_is_matched_at_low_confidence(
    db: AsyncSession,
) -> None:
    """A topic IS an enumeration item, so a bare "go" topic satisfies the
    list-context guard — at the lowest confidence, as designed."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, topics=("go",))

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Go")
    assert len(evidence) == 1
    assert evidence[0].confidence == Decimal("0.60")


@pytest.mark.anyio
async def test_canonical_outranks_alias_within_one_signal(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, readme_text="Written in Python; see the py/ directory.")

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Python")
    assert len(evidence) == 1
    assert evidence[0].confidence == Decimal("0.90")


@pytest.mark.anyio
async def test_two_topics_for_one_skill_collapse_to_the_strongest(db: AsyncSession) -> None:
    """ "rest api" is an alias and "restful" is an alias, but the natural
    key permits one row per (skill, repo, method) — so the winner must be
    deterministic."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, topics=("rest-api", "restful"))

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "REST APIs")
    assert len(evidence) == 1


# --------------------------------------------------------------------
# Multiple sources and repositories
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_one_repository_can_support_one_skill_four_ways(db: AsyncSession) -> None:
    """README, description, topic and language are genuinely different
    observations, so they are four evidence rows — but still ONE
    candidate skill."""
    user_id = await _seeded_user(db)
    await _add_repository(
        db,
        user_id,
        full_name="ada/toolkit",
        description="A Python toolkit.",
        readme_text="Written in Python.",
        topics=("python",),
        languages={"Python": 1_000},
    )

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Python")
    assert {e.extraction_method for e in evidence} == {_README, _DESCRIPTION, _TOPIC, _LANGUAGE}
    assert all(e.source_identifier == "ada/toolkit" for e in evidence)
    assert len(await _names_for(db, user_id)) == 1


@pytest.mark.anyio
async def test_the_same_skill_from_several_repositories(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, full_name="ada/one", languages={"Python": 100})
    await _add_repository(db, user_id, full_name="ada/two", languages={"Python": 200})

    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Python")
    assert len(evidence) == 2
    assert {e.source_identifier for e in evidence} == {"ada/one", "ada/two"}
    # One candidate skill, however many repositories support it.
    candidate_skills = (
        await db.scalars(select(CandidateSkill).where(CandidateSkill.user_id == user_id))
    ).all()
    assert len(candidate_skills) == 1


# --------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_fork_is_never_credited(db: AsyncSession) -> None:
    """A fork's description and topics come from the listing payload, so
    Prompt 3.2 DOES store them even though it skips fork detail — this
    filter is load-bearing, not redundant."""
    user_id = await _seeded_user(db)
    await _add_repository(
        db,
        user_id,
        full_name="ada/forked",
        description="A Django service.",
        topics=("django", "python"),
        primary_language="Python",
        is_fork=True,
    )

    summary = await extract_github_skill_evidence(db, user_id)

    assert summary.repositories_considered == 0
    assert await _names_for(db, user_id) == set()


@pytest.mark.anyio
async def test_an_archived_repository_still_contributes(db: AsyncSession) -> None:
    """Archiving says "no longer maintained", not "not my work" — unlike
    a fork. Recency is Prompt 4.x's to weigh."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, is_archived=True, languages={"Python": 500})

    await extract_github_skill_evidence(db, user_id)

    assert "Python" in await _names_for(db, user_id)


@pytest.mark.anyio
async def test_a_soft_deleted_repository_produces_no_current_evidence(
    db: AsyncSession,
) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, deleted_at=datetime.now(UTC), languages={"Python": 500})

    summary = await extract_github_skill_evidence(db, user_id)

    assert summary.repositories_considered == 0
    assert await _names_for(db, user_id) == set()


@pytest.mark.anyio
async def test_evidence_disappears_when_a_repository_is_soft_deleted(
    db: AsyncSession,
) -> None:
    user_id = await _seeded_user(db)
    repository = await _add_repository(db, user_id, languages={"Python": 500})
    await extract_github_skill_evidence(db, user_id)
    assert "Python" in await _names_for(db, user_id)

    repository.deleted_at = datetime.now(UTC)
    await db.commit()
    summary = await extract_github_skill_evidence(db, user_id)

    assert summary.evidence_removed == 1
    assert await _names_for(db, user_id) == set()


# --------------------------------------------------------------------
# Idempotency and reconciliation
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_rerunning_changes_absolutely_nothing(db: AsyncSession) -> None:
    """The idempotency guarantee, asserted strictly: same ids, same
    timestamps — proving the upsert's WHERE clause suppresses no-op
    writes rather than merely avoiding duplicate rows.

    Includes a language row, whose excerpt is NULL: without
    IS DISTINCT FROM in that WHERE clause, NULL <> NULL would be NULL and
    this would behave differently from every other signal.
    """
    user_id = await _seeded_user(db)
    await _add_repository(
        db,
        user_id,
        description="A Python and Docker toolkit.",
        readme_text="Built with FastAPI. Tested with pytest.",
        topics=("rest-api", "postgres"),
        languages={"Python": 1_000, "HTML": 20},
    )
    await extract_github_skill_evidence(db, user_id)
    before = await _snapshot(db, user_id)

    summary = await extract_github_skill_evidence(db, user_id)

    assert summary.candidate_skills_created == 0
    assert summary.evidence_written == 0
    assert summary.evidence_removed == 0
    assert summary.suggestions_removed == 0
    assert await _snapshot(db, user_id) == before


@pytest.mark.anyio
async def test_a_removed_topic_is_reconciled_away(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    repository = await _add_repository(db, user_id, topics=("django", "redis"))
    await extract_github_skill_evidence(db, user_id)
    assert {"Django", "Redis"} <= await _names_for(db, user_id)

    await db.execute(
        GitHubRepositoryTopic.__table__.delete().where(
            GitHubRepositoryTopic.repository_id == repository.id,
            GitHubRepositoryTopic.topic == "redis",
        )
    )
    await db.commit()
    summary = await extract_github_skill_evidence(db, user_id)

    names = await _names_for(db, user_id)
    assert "Django" in names
    assert "Redis" not in names
    assert summary.evidence_removed == 1
    assert summary.suggestions_removed == 1


@pytest.mark.anyio
async def test_the_sweep_distinguishes_two_skills_from_one_signal(db: AsyncSession) -> None:
    """Regression test for a real bug this suite caught.

    Django and Redis here are two skills matched from the SAME repository
    through the SAME signal kind, so they share a
    (source_identifier, extraction_method) pair. When the reconciliation
    sweep keyed on only that pair, Django's surviving evidence kept
    Redis's stale evidence alive — reconciliation silently stopped
    working for any repository supporting more than one skill through one
    signal, which is the common case rather than an edge case.

    The sweep must key on candidate_skill_id as well.
    """
    user_id = await _seeded_user(db)
    repository = await _add_repository(
        db, user_id, full_name="ada/multi", topics=("django", "redis", "docker")
    )
    await extract_github_skill_evidence(db, user_id)
    assert {"Django", "Redis", "Docker"} <= await _names_for(db, user_id)

    await db.execute(
        GitHubRepositoryTopic.__table__.delete().where(
            GitHubRepositoryTopic.repository_id == repository.id,
            GitHubRepositoryTopic.topic.in_(["redis", "docker"]),
        )
    )
    await db.commit()
    summary = await extract_github_skill_evidence(db, user_id)

    assert await _names_for(db, user_id) == {"Django"}
    assert summary.evidence_removed == 2


@pytest.mark.anyio
async def test_a_renamed_repository_reconciles_instead_of_duplicating(
    db: AsyncSession,
) -> None:
    """Repository IDENTITY is github_repo_id, but evidence cites
    full_name. Without the full sweep a rename would strand the old
    evidence AND write a duplicate under the new name."""
    user_id = await _seeded_user(db)
    repository = await _add_repository(
        db, user_id, full_name="ada/old-name", languages={"Python": 500}
    )
    await extract_github_skill_evidence(db, user_id)
    assert [e.source_identifier for e in await _evidence_for(db, user_id, "Python")] == [
        "ada/old-name"
    ]

    repository.full_name = "ada/new-name"
    await db.commit()
    await extract_github_skill_evidence(db, user_id)

    evidence = await _evidence_for(db, user_id, "Python")
    assert len(evidence) == 1
    assert evidence[0].source_identifier == "ada/new-name"


@pytest.mark.anyio
async def test_an_edited_readme_updates_the_excerpt_in_place(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    repository = await _add_repository(db, user_id, readme_text="Built with Django.")
    await extract_github_skill_evidence(db, user_id)
    original = (await _evidence_for(db, user_id, "Django"))[0]
    original_id = original.id

    repository.readme_text = "Now rewritten, still using Django throughout."
    await db.commit()
    await extract_github_skill_evidence(db, user_id)

    db.expire_all()
    updated = (await _evidence_for(db, user_id, "Django"))[0]
    assert updated.id == original_id  # same row, not a second one
    assert updated.excerpt == "Now rewritten, still using Django throughout."


# --------------------------------------------------------------------
# Override invariants
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_confirmed_skill_survives_a_rerun(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"Python": 500})
    await extract_github_skill_evidence(db, user_id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    python.status = CandidateSkillStatus.CONFIRMED.value
    await db.commit()

    await extract_github_skill_evidence(db, user_id)

    db.expire_all()
    still = await _candidate_skill_for(db, user_id, "Python")
    assert still is not None
    assert still.status == CandidateSkillStatus.CONFIRMED.value


@pytest.mark.anyio
async def test_a_confirmed_skill_survives_losing_all_its_evidence(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    repository = await _add_repository(db, user_id, languages={"Python": 500})
    await extract_github_skill_evidence(db, user_id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    python.status = CandidateSkillStatus.CONFIRMED.value
    await db.commit()

    repository.deleted_at = datetime.now(UTC)
    await db.commit()
    await extract_github_skill_evidence(db, user_id)

    db.expire_all()
    still = await _candidate_skill_for(db, user_id, "Python")
    assert still is not None
    assert still.status == CandidateSkillStatus.CONFIRMED.value


@pytest.mark.anyio
async def test_a_rejected_skill_is_never_resurrected(db: AsyncSession) -> None:
    """The most important test in this file. If rejection were a delete,
    this rerun would recreate the skill as a fresh suggestion and the
    user's decision would silently vanish."""
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"Python": 500})
    await extract_github_skill_evidence(db, user_id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    python.status = CandidateSkillStatus.REJECTED.value
    await db.commit()

    await extract_github_skill_evidence(db, user_id)

    db.expire_all()
    still = await _candidate_skill_for(db, user_id, "Python")
    assert still is not None
    assert still.status == CandidateSkillStatus.REJECTED.value
    assert still.id == python.id  # the same tombstone, not a new row


@pytest.mark.anyio
async def test_manual_evidence_is_never_touched(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    repository = await _add_repository(db, user_id, languages={"Python": 500})
    await extract_github_skill_evidence(db, user_id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    await _add_manual_evidence(db, python.id, user_id)

    # Remove the GitHub support entirely; the manual assertion must stay.
    repository.deleted_at = datetime.now(UTC)
    await db.commit()
    await extract_github_skill_evidence(db, user_id)

    remaining = await _evidence_for(db, user_id, "Python")
    assert [e.source_type for e in remaining] == [EvidenceSourceType.MANUAL.value]


@pytest.mark.anyio
async def test_resume_evidence_is_never_touched(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    repository = await _add_repository(db, user_id, languages={"Python": 500})
    await extract_github_skill_evidence(db, user_id)
    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    resume_id = uuid.uuid4()
    db.add(
        SkillEvidence(
            candidate_skill_id=python.id,
            source_type=EvidenceSourceType.RESUME.value,
            source_identifier=str(resume_id),
            excerpt="Languages: Python",
            extraction_method=ExtractionMethod.RESUME_ALIAS_MATCH.value,
            confidence=Decimal("0.90"),
        )
    )
    await db.commit()

    repository.deleted_at = datetime.now(UTC)
    await db.commit()
    await extract_github_skill_evidence(db, user_id)

    remaining = await _evidence_for(db, user_id, "Python")
    assert [e.source_type for e in remaining] == [EvidenceSourceType.RESUME.value]
    # The suggestion survives because a source OTHER than GitHub still
    # supports it — the orphan sweep requires zero evidence from ANY source.
    assert "Python" in await _names_for(db, user_id)


# --------------------------------------------------------------------
# Scoping and taxonomy
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_cross_user_isolation(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    other_id = uuid.uuid4()
    db.add(User(id=other_id, email=f"{other_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    await _add_repository(db, other_id, full_name="grace/theirs", languages={"Python": 500})

    summary = await extract_github_skill_evidence(db, user_id)

    assert summary.repositories_considered == 0
    assert await _names_for(db, user_id) == set()
    owners = set((await db.scalars(select(CandidateSkill.user_id))).all())
    assert owners == set()


@pytest.mark.anyio
async def test_one_users_extraction_never_disturbs_another(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    other_id = uuid.uuid4()
    db.add(User(id=other_id, email=f"{other_id}@example.com", hashed_password="not-a-real-hash"))
    await db.commit()
    await _add_repository(db, other_id, full_name="grace/theirs", languages={"Python": 500})
    await _add_repository(db, user_id, full_name="ada/mine", languages={"Django": 1})
    await extract_github_skill_evidence(db, other_id)
    before = await _snapshot(db, other_id)

    await extract_github_skill_evidence(db, user_id)

    assert await _snapshot(db, other_id) == before


@pytest.mark.anyio
async def test_no_taxonomy_rows_are_ever_created(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(
        db,
        user_id,
        description="Uses Elixir, Zig and Python.",
        topics=("elixir", "zig", "haskell"),
        languages={"Elixir": 900, "Zig": 100},
    )
    skills_before = await db.scalar(select(func.count()).select_from(Skill))
    aliases_before = await db.scalar(select(func.count()).select_from(SkillAlias))

    await extract_github_skill_evidence(db, user_id)

    assert await db.scalar(select(func.count()).select_from(Skill)) == skills_before
    assert await db.scalar(select(func.count()).select_from(SkillAlias)) == aliases_before
    assert await _names_for(db, user_id) == {"Python"}


@pytest.mark.anyio
async def test_a_non_curated_skill_is_never_matched(db: AsyncSession) -> None:
    """A skill coined through Prompt 1.3's target skills has no category,
    so it must never start matching against GitHub data either."""
    user_id = await _seeded_user(db)
    db.add(Skill(name="Elixir", slug="elixir", category=None))
    await db.commit()
    await _add_repository(
        db, user_id, readme_text="Written in Elixir.", topics=("elixir",), primary_language="Elixir"
    )

    await extract_github_skill_evidence(db, user_id)

    assert await _names_for(db, user_id) == set()


# --------------------------------------------------------------------
# Disconnect
# --------------------------------------------------------------------


@pytest.mark.anyio
async def test_purge_removes_github_evidence_and_orphaned_suggestions(
    db: AsyncSession,
) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"Python": 500, "Java": 100})
    await extract_github_skill_evidence(db, user_id)
    assert {"Python", "Java"} <= await _names_for(db, user_id)

    summary = await purge_github_skill_evidence(db, user_id)
    await db.commit()

    assert summary.evidence_removed == 2
    assert summary.suggestions_removed == 2
    assert await _names_for(db, user_id) == set()


@pytest.mark.anyio
async def test_purge_preserves_reviewed_skills_and_other_sources(db: AsyncSession) -> None:
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, languages={"Python": 500, "Java": 100, "Go": 5})
    await extract_github_skill_evidence(db, user_id)

    confirmed = await _candidate_skill_for(db, user_id, "Python")
    assert confirmed is not None
    confirmed.status = CandidateSkillStatus.CONFIRMED.value
    rejected = await _candidate_skill_for(db, user_id, "Java")
    assert rejected is not None
    rejected.status = CandidateSkillStatus.REJECTED.value
    await db.commit()

    # A manually asserted skill with no GitHub involvement at all.
    kubernetes = await _skill_named(db, "Kubernetes")
    manual = CandidateSkill(
        user_id=user_id, skill_id=kubernetes.id, status=CandidateSkillStatus.CONFIRMED.value
    )
    db.add(manual)
    await db.flush()
    await _add_manual_evidence(db, manual.id, user_id)

    await purge_github_skill_evidence(db, user_id)
    await db.commit()

    db.expire_all()
    names = await _names_for(db, user_id)
    assert names == {"Python", "Java", "Kubernetes"}
    # The reviewed rows survive with their decisions intact...
    still_confirmed = await _candidate_skill_for(db, user_id, "Python")
    assert still_confirmed is not None
    assert still_confirmed.status == CandidateSkillStatus.CONFIRMED.value
    still_rejected = await _candidate_skill_for(db, user_id, "Java")
    assert still_rejected is not None
    assert still_rejected.status == CandidateSkillStatus.REJECTED.value
    # ...but their GitHub evidence is gone, while manual evidence remains.
    assert await _evidence_for(db, user_id, "Python") == []
    assert [e.source_type for e in await _evidence_for(db, user_id, "Kubernetes")] == [
        EvidenceSourceType.MANUAL.value
    ]


@pytest.mark.anyio
async def test_a_confirmed_skill_keeps_its_manual_evidence_after_a_purge(
    db: AsyncSession,
) -> None:
    """The GitHub mirror of the resume-deletion case clarified from
    browser testing.

    A skill GitHub evidenced, that the user then confirmed AND asserted
    by hand, must survive disconnecting: the repository citation
    disappears, "Added by you" remains, and the confirmed decision is
    untouched.
    """
    user_id = await _seeded_user(db)
    await _add_repository(db, user_id, full_name="ada/toolkit", languages={"Python": 500})
    await extract_github_skill_evidence(db, user_id)

    python = await _candidate_skill_for(db, user_id, "Python")
    assert python is not None
    python.status = CandidateSkillStatus.CONFIRMED.value
    await db.commit()
    await _add_manual_evidence(db, python.id, user_id)
    assert {e.source_type for e in await _evidence_for(db, user_id, "Python")} == {
        EvidenceSourceType.GITHUB.value,
        EvidenceSourceType.MANUAL.value,
    }

    await purge_github_skill_evidence(db, user_id)
    await db.commit()

    db.expire_all()
    # GitHub citation gone, the user's own assertion intact...
    assert [e.source_type for e in await _evidence_for(db, user_id, "Python")] == [
        EvidenceSourceType.MANUAL.value
    ]
    # ...and the decision is untouched.
    still = await _candidate_skill_for(db, user_id, "Python")
    assert still is not None
    assert still.status == CandidateSkillStatus.CONFIRMED.value
