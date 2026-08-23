"""Tests for the repeatable skill-taxonomy seed (scripts/seed_skills.py).

The properties that matter, and that these tests pin down:

  * it creates the declared taxonomy;
  * re-running it is a genuine no-op — not merely "does not crash", but
    leaves every row byte-identical, including `created_at`/`updated_at`;
  * it ADOPTS a skill a user already coined rather than duplicating it,
    which is the whole reason Prompt 2.3 extends `skills` instead of
    creating a second canonical table;
  * it never touches a skill absent from the seed file;
  * it repairs a seeded row that was edited in the database by hand;
  * it reconciles aliases and relations when the seed file changes;
  * the taxonomy file itself is coherent (no shadowed slugs, no alias
    owned by two skills, no self-relations, no dangling `related`).
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import get_db
from app.main import app
from app.models.skill import Skill, SkillAlias, SkillRelation
from app.rate_limit import _request_log
from app.schemas.skill import SkillCategory
from app.seeds.skill_taxonomy import SKILL_TAXONOMY, SeedSkill
from app.settings import get_settings
from scripts.seed_skills import seed_skill_taxonomy, slugify, validate_taxonomy
from tests.conftest import TEST_SCHEMA, isolated_schema_override

_SEARCH_PATH_CONNECT_ARGS = {"server_settings": {"search_path": TEST_SCHEMA}}


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


def _expected_relation_rows(taxonomy: tuple[SeedSkill, ...]) -> int:
    """Both directions per declared pair, de-duplicated — computed from
    the file rather than hard-coded so editing the taxonomy does not
    break this test for the wrong reason."""
    pairs: set[tuple[str, str]] = set()
    for skill in taxonomy:
        left = slugify(skill.name)
        for related in skill.related:
            right = slugify(related)
            pairs.add((left, right))
            pairs.add((right, left))
    return len(pairs)


# --- the taxonomy file itself -----------------------------------------


def test_the_shipped_taxonomy_is_valid() -> None:
    validate_taxonomy(SKILL_TAXONOMY)


def test_the_shipped_taxonomy_stays_intentionally_small() -> None:
    """A guard rail, not a spec: the brief asks for a deliberately
    limited taxonomy, and a sudden jump to hundreds of skills should
    fail review rather than silently make Prompt 2.4 noisier."""
    assert 20 <= len(SKILL_TAXONOMY) <= 60


def test_every_seeded_skill_has_a_category() -> None:
    for skill in SKILL_TAXONOMY:
        assert isinstance(skill.category, SkillCategory)


def test_validate_rejects_an_alias_that_shadows_another_skills_slug() -> None:
    taxonomy = (
        SeedSkill(name="Go", category=SkillCategory.LANGUAGE),
        SeedSkill(name="Golang", category=SkillCategory.LANGUAGE, aliases=("go",)),
    )

    with pytest.raises(ValueError, match="shadows the canonical skill"):
        validate_taxonomy(taxonomy)


def test_validate_rejects_an_alias_claimed_by_two_skills() -> None:
    taxonomy = (
        SeedSkill(name="JavaScript", category=SkillCategory.LANGUAGE, aliases=("js",)),
        SeedSkill(name="Java", category=SkillCategory.LANGUAGE, aliases=("JS",)),
    )

    with pytest.raises(ValueError, match="must resolve to exactly one skill"):
        validate_taxonomy(taxonomy)


def test_validate_rejects_a_duplicate_skill() -> None:
    taxonomy = (
        SeedSkill(name="Python", category=SkillCategory.LANGUAGE),
        SeedSkill(name="python", category=SkillCategory.LANGUAGE),
    )

    with pytest.raises(ValueError, match="duplicate skill"):
        validate_taxonomy(taxonomy)


def test_validate_rejects_a_self_relation() -> None:
    taxonomy = (SeedSkill(name="Python", category=SkillCategory.LANGUAGE, related=("Python",)),)

    with pytest.raises(ValueError, match="lists itself"):
        validate_taxonomy(taxonomy)


def test_validate_rejects_an_unknown_related_skill() -> None:
    taxonomy = (SeedSkill(name="Python", category=SkillCategory.LANGUAGE, related=("Cobol",)),)

    with pytest.raises(ValueError, match="unknown related skill"):
        validate_taxonomy(taxonomy)


# --- seeding behavior -------------------------------------------------


@pytest.mark.anyio
async def test_seed_creates_the_declared_taxonomy(db: AsyncSession) -> None:
    result = await seed_skill_taxonomy(db)

    assert result.skills_created == len(SKILL_TAXONOMY)
    assert result.skills_updated == 0
    assert len((await db.scalars(select(Skill))).all()) == len(SKILL_TAXONOMY)
    assert len((await db.scalars(select(SkillAlias))).all()) == sum(
        len(s.aliases) for s in SKILL_TAXONOMY
    )
    assert len((await db.scalars(select(SkillRelation))).all()) == _expected_relation_rows(
        SKILL_TAXONOMY
    )


@pytest.mark.anyio
async def test_rerunning_the_seed_changes_absolutely_nothing(db: AsyncSession) -> None:
    """The idempotency guarantee, asserted strictly: not just equal row
    counts, but the same ids and the same timestamps — proving the seed
    diffs rather than delete-and-recreates."""
    await seed_skill_taxonomy(db)
    before = {
        (row.id, row.slug, row.name, row.category, row.created_at, row.updated_at)
        for row in (await db.scalars(select(Skill))).all()
    }
    aliases_before = {
        (row.id, row.alias_slug, row.skill_id, row.created_at)
        for row in (await db.scalars(select(SkillAlias))).all()
    }
    relations_before = {
        (row.from_skill_id, row.to_skill_id, row.created_at)
        for row in (await db.scalars(select(SkillRelation))).all()
    }

    result = await seed_skill_taxonomy(db)

    assert result.skills_created == 0
    assert result.skills_updated == 0
    assert result.skills_unchanged == len(SKILL_TAXONOMY)
    assert result.aliases_created == result.aliases_removed == 0
    assert result.relations_created == result.relations_removed == 0
    assert not result.changed_anything

    db.expire_all()
    assert {
        (row.id, row.slug, row.name, row.category, row.created_at, row.updated_at)
        for row in (await db.scalars(select(Skill))).all()
    } == before
    assert {
        (row.id, row.alias_slug, row.skill_id, row.created_at)
        for row in (await db.scalars(select(SkillAlias))).all()
    } == aliases_before
    assert {
        (row.from_skill_id, row.to_skill_id, row.created_at)
        for row in (await db.scalars(select(SkillRelation))).all()
    } == relations_before


@pytest.mark.anyio
async def test_seed_adopts_a_user_coined_skill_instead_of_duplicating_it(
    db: AsyncSession,
) -> None:
    """The reason Prompt 2.3 extends `skills` rather than creating a
    second canonical table: a row a user coined pre-seed must become THE
    seeded row, not a rival to it."""
    coined = Skill(name="python", slug="python", category=None)
    db.add(coined)
    await db.commit()

    await seed_skill_taxonomy(db)

    rows = (await db.scalars(select(Skill).where(Skill.slug == "python"))).all()
    assert len(rows) == 1
    assert rows[0].id == coined.id  # same row, adopted
    assert rows[0].name == "Python"  # display casing repaired
    assert rows[0].category == SkillCategory.LANGUAGE.value  # enriched


@pytest.mark.anyio
async def test_seed_never_touches_a_skill_absent_from_the_seed_file(db: AsyncSession) -> None:
    elixir = Skill(name="Elixir", slug="elixir", category=None)
    db.add(elixir)
    await db.commit()
    original = (elixir.id, elixir.name, elixir.category, elixir.created_at, elixir.updated_at)

    await seed_skill_taxonomy(db)

    db.expire_all()
    survivor = (await db.scalars(select(Skill).where(Skill.slug == "elixir"))).one()
    assert (
        survivor.id,
        survivor.name,
        survivor.category,
        survivor.created_at,
        survivor.updated_at,
    ) == original


@pytest.mark.anyio
async def test_seed_repairs_a_seeded_row_edited_by_hand(db: AsyncSession) -> None:
    """The documented consequence of "the seed file is the source of
    truth": a direct database edit to a seeded row is reverted on the
    next run."""
    await seed_skill_taxonomy(db)
    python = (await db.scalars(select(Skill).where(Skill.slug == "python"))).one()
    python.category = SkillCategory.CONCEPT.value
    python.name = "PYTHON!!"
    await db.commit()

    result = await seed_skill_taxonomy(db)

    assert result.skills_updated == 1
    db.expire_all()
    repaired = (await db.scalars(select(Skill).where(Skill.slug == "python"))).one()
    assert repaired.name == "Python"
    assert repaired.category == SkillCategory.LANGUAGE.value


@pytest.mark.anyio
async def test_relations_are_stored_in_both_directions(db: AsyncSession) -> None:
    await seed_skill_taxonomy(db)
    python = (await db.scalars(select(Skill).where(Skill.slug == "python"))).one()
    django = (await db.scalars(select(Skill).where(Skill.slug == "django"))).one()

    forward = await db.scalar(
        select(SkillRelation).where(
            SkillRelation.from_skill_id == python.id, SkillRelation.to_skill_id == django.id
        )
    )
    backward = await db.scalar(
        select(SkillRelation).where(
            SkillRelation.from_skill_id == django.id, SkillRelation.to_skill_id == python.id
        )
    )

    # Declared once (on Python) in the seed file, materialized both ways.
    assert forward is not None
    assert backward is not None


@pytest.mark.anyio
async def test_no_seeded_alias_shadows_a_canonical_slug_in_the_database(
    db: AsyncSession,
) -> None:
    """The cross-table invariant no single constraint can express."""
    await seed_skill_taxonomy(db)

    slugs = {row for row in (await db.scalars(select(Skill.slug))).all()}
    alias_slugs = {row for row in (await db.scalars(select(SkillAlias.alias_slug))).all()}

    assert slugs & alias_slugs == set()


@pytest.mark.anyio
async def test_seed_reconciles_aliases_and_relations_when_the_file_shrinks(
    db: AsyncSession,
) -> None:
    """Editing the seed file removes what it no longer declares — the
    database converges to the file, it does not accumulate."""
    full = (
        SeedSkill(
            name="Python",
            category=SkillCategory.LANGUAGE,
            aliases=("py", "python3"),
            related=("Django",),
        ),
        SeedSkill(name="Django", category=SkillCategory.FRAMEWORK),
    )
    await seed_skill_taxonomy(db, full)
    assert len((await db.scalars(select(SkillAlias))).all()) == 2
    assert len((await db.scalars(select(SkillRelation))).all()) == 2

    shrunk = (
        SeedSkill(name="Python", category=SkillCategory.LANGUAGE, aliases=("py",)),
        SeedSkill(name="Django", category=SkillCategory.FRAMEWORK),
    )
    result = await seed_skill_taxonomy(db, shrunk)

    assert result.aliases_removed == 1
    assert result.relations_removed == 2
    remaining = (await db.scalars(select(SkillAlias))).all()
    assert [row.alias_slug for row in remaining] == ["py"]
    assert (await db.scalars(select(SkillRelation))).all() == []


@pytest.mark.anyio
async def test_seed_moves_an_alias_between_skills_without_colliding(db: AsyncSession) -> None:
    """Deleting before inserting is what makes this work — otherwise the
    global alias_slug unique constraint would reject the move."""
    before = (
        SeedSkill(name="Python", category=SkillCategory.LANGUAGE, aliases=("snake",)),
        SeedSkill(name="Go", category=SkillCategory.LANGUAGE),
    )
    await seed_skill_taxonomy(db, before)

    after = (
        SeedSkill(name="Python", category=SkillCategory.LANGUAGE),
        SeedSkill(name="Go", category=SkillCategory.LANGUAGE, aliases=("snake",)),
    )
    await seed_skill_taxonomy(db, after)

    go = (await db.scalars(select(Skill).where(Skill.slug == "go"))).one()
    alias = (await db.scalars(select(SkillAlias))).one()
    assert alias.alias_slug == "snake"
    assert alias.skill_id == go.id


# --- cross-prompt integration -----------------------------------------


@pytest.fixture
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


def test_a_profile_target_skill_reuses_the_seeded_canonical_row(
    client: TestClient, _use_isolated_schema: None
) -> None:
    """The regression guard for slug-convention drift between the seed
    (scripts/seed_skills.py) and Prompt 1.3's `_get_or_create_skill`
    (app/api/v1/profile.py). If those two ever disagree, a user typing
    "python" would coin a SECOND row alongside the seeded "Python" and
    the whole canonical-identity guarantee would quietly break.

    A sync test using TestClient: seeding runs via asyncio.run before the
    client's own event loop is involved.
    """
    _request_log.clear()

    async def _seed() -> None:
        engine = create_async_engine(
            get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                await seed_skill_taxonomy(session)
        finally:
            await engine.dispose()

    asyncio.run(_seed())

    email = f"{uuid.uuid4()}@example.com"
    register = client.post(
        "/api/v1/auth/register", json={"email": email, "password": "correct-horse-battery"}
    )
    assert register.status_code == 201
    user_id = register.json()["id"]
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "correct-horse-battery"}
    )
    token = login.json()["access_token"]

    # Lowercase on purpose — the casing a user would actually type.
    response = client.patch(
        f"/api/v1/profiles/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"target_skills": ["python"]},
    )

    assert response.status_code == 200
    # Resolved to the seeded row, so the display name is the canonical
    # one — not the user's lowercase spelling.
    assert response.json()["target_skills"] == ["Python"]

    async def _count_pythons() -> int:
        engine = create_async_engine(
            get_settings().database_url, connect_args=_SEARCH_PATH_CONNECT_ARGS
        )
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                rows = (await session.scalars(select(Skill).where(Skill.slug == "python"))).all()
                assert rows[0].category == SkillCategory.LANGUAGE.value
                return len(rows)
        finally:
            await engine.dispose()

    assert asyncio.run(_count_pythons()) == 1
