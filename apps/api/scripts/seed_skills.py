"""Repeatable seed for the canonical skill taxonomy (Prompt 2.3).

Run it with:
    make seed-skills
(or: cd apps/api && uv run python -m scripts.seed_skills)

Converges the database to exactly what app/seeds/skill_taxonomy.py
declares, for the skills that file names — and touches nothing else. The
seed file is the editable source of truth: to change the taxonomy, edit
it and re-run this.

Safe and deterministic to run repeatedly:

  * skills are matched by `slug` (`name.strip().casefold()`, the same
    rule app/api/v1/profile.py's `_get_or_create_skill` uses), so a row a
    user already coined through their profile is ADOPTED and enriched
    with its category rather than duplicated;
  * a row is only written when a value actually differs, so a no-op
    re-run leaves `updated_at` untouched;
  * aliases and relations are reconciled by difference (delete only what
    the file no longer declares, insert only what is missing), so
    unchanged rows keep their original `created_at`;
  * rows whose slug is absent from the seed file — anything a user
    coined — are never read, written, or deleted.

Everything happens in one transaction: a validation failure or a
constraint violation leaves the database exactly as it was.

Not designed for concurrent execution (two operators seeding at the same
instant). The unique constraints make that fail loudly rather than
corrupt anything, and the fix is to re-run.

This script writes NO candidate skills and NO evidence — Prompt 2.3
seeds vocabulary only.
"""

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import build_session_factory
from app.models.skill import Skill, SkillAlias, SkillRelation
from app.seeds.skill_taxonomy import SKILL_TAXONOMY, SeedSkill
from app.settings import get_settings


@dataclass
class SeedResult:
    """Per-run counts, printed for the operator and asserted on in
    tests/test_seed_skills.py — a second run must report only
    `skills_unchanged`."""

    skills_created: int = 0
    skills_updated: int = 0
    skills_unchanged: int = 0
    aliases_created: int = 0
    aliases_removed: int = 0
    relations_created: int = 0
    relations_removed: int = 0

    @property
    def changed_anything(self) -> bool:
        return bool(
            self.skills_created
            or self.skills_updated
            or self.aliases_created
            or self.aliases_removed
            or self.relations_created
            or self.relations_removed
        )


def slugify(name: str) -> str:
    """The one canonical slug rule. Must stay identical to
    app/api/v1/profile.py's `_get_or_create_skill`, or a user-coined
    "Python" and the seeded "Python" would become two rows — regression
    tested in tests/test_seed_skills.py."""
    return name.strip().casefold()


def validate_taxonomy(taxonomy: tuple[SeedSkill, ...]) -> None:
    """Fail loudly, before touching the database, on a taxonomy file that
    could not produce a coherent lookup: duplicate skills, an alias
    claimed by two skills, an alias shadowing a canonical slug (which
    would make that token ambiguous — the cross-table invariant no single
    database constraint can express), a self-relation, or a `related`
    entry naming a skill that is not in the file."""
    slugs: dict[str, str] = {}
    for skill in taxonomy:
        slug = slugify(skill.name)
        if slug in slugs:
            raise ValueError(
                f"duplicate skill in taxonomy: {skill.name!r} collides with {slugs[slug]!r}"
            )
        slugs[slug] = skill.name

    alias_owners: dict[str, str] = {}
    for skill in taxonomy:
        for alias in skill.aliases:
            alias_slug = slugify(alias)
            if alias_slug in slugs:
                raise ValueError(
                    f"alias {alias!r} on {skill.name!r} shadows the canonical skill "
                    f"{slugs[alias_slug]!r} — an alias must never equal a skill's own slug"
                )
            if alias_slug in alias_owners:
                raise ValueError(
                    f"alias {alias!r} is claimed by both {alias_owners[alias_slug]!r} "
                    f"and {skill.name!r} — an alias must resolve to exactly one skill"
                )
            alias_owners[alias_slug] = skill.name

    for skill in taxonomy:
        for related_name in skill.related:
            related_slug = slugify(related_name)
            if related_slug == slugify(skill.name):
                raise ValueError(f"{skill.name!r} lists itself as a related skill")
            if related_slug not in slugs:
                raise ValueError(
                    f"{skill.name!r} lists unknown related skill {related_name!r} — "
                    "related entries must name another skill in this file"
                )


async def _sync_skills(
    db: AsyncSession, taxonomy: tuple[SeedSkill, ...], result: SeedResult
) -> dict[str, Skill]:
    """Insert missing skills and update only those whose name or category
    actually differs. Returns every seeded skill keyed by slug."""
    desired = {slugify(skill.name): skill for skill in taxonomy}
    existing_rows = await db.scalars(select(Skill).where(Skill.slug.in_(desired.keys())))
    by_slug = {row.slug: row for row in existing_rows.all()}

    for slug, seed_skill in desired.items():
        row = by_slug.get(slug)
        if row is None:
            row = Skill(name=seed_skill.name, slug=slug, category=seed_skill.category.value)
            db.add(row)
            by_slug[slug] = row
            result.skills_created += 1
        elif row.name != seed_skill.name or row.category != seed_skill.category.value:
            # Adopts a row a user coined with no category, and repairs a
            # seeded row someone edited in the database by hand.
            row.name = seed_skill.name
            row.category = seed_skill.category.value
            result.skills_updated += 1
        else:
            result.skills_unchanged += 1

    # Assigns ids to the newly added rows so aliases/relations below can
    # reference them within this same transaction.
    await db.flush()
    return by_slug


async def _sync_aliases(
    db: AsyncSession,
    taxonomy: tuple[SeedSkill, ...],
    skills_by_slug: dict[str, Skill],
    result: SeedResult,
) -> None:
    desired: dict[str, tuple[str, Skill]] = {}
    for seed_skill in taxonomy:
        owner = skills_by_slug[slugify(seed_skill.name)]
        for alias in seed_skill.aliases:
            desired[slugify(alias)] = (alias, owner)

    seeded_ids = [skill.id for skill in skills_by_slug.values()]
    existing_rows = (
        await db.scalars(select(SkillAlias).where(SkillAlias.skill_id.in_(seeded_ids)))
    ).all()

    # Delete before inserting: an alias moved from one skill to another
    # would otherwise collide on the global alias_slug unique constraint.
    for row in existing_rows:
        wanted = desired.get(row.alias_slug)
        if wanted is None or wanted[1].id != row.skill_id or wanted[0] != row.alias:
            await db.delete(row)
            result.aliases_removed += 1
    await db.flush()

    surviving = {
        row.alias_slug
        for row in existing_rows
        if (w := desired.get(row.alias_slug)) is not None
        and w[1].id == row.skill_id
        and w[0] == row.alias
    }
    for alias_slug, (alias, owner) in desired.items():
        if alias_slug not in surviving:
            db.add(SkillAlias(skill_id=owner.id, alias=alias, alias_slug=alias_slug))
            result.aliases_created += 1


async def _sync_relations(
    db: AsyncSession,
    taxonomy: tuple[SeedSkill, ...],
    skills_by_slug: dict[str, Skill],
    result: SeedResult,
) -> None:
    """Materializes both ordered rows for every declared pair, so a read
    is a single `WHERE from_skill_id = ?`. A pair need only be declared
    once in the seed file, on either side."""
    desired: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for seed_skill in taxonomy:
        left = skills_by_slug[slugify(seed_skill.name)].id
        for related_name in seed_skill.related:
            right = skills_by_slug[slugify(related_name)].id
            desired.add((left, right))
            desired.add((right, left))

    seeded_ids = [skill.id for skill in skills_by_slug.values()]
    existing_rows = (
        await db.scalars(
            select(SkillRelation).where(
                SkillRelation.from_skill_id.in_(seeded_ids)
                | SkillRelation.to_skill_id.in_(seeded_ids)
            )
        )
    ).all()

    existing_pairs = {(row.from_skill_id, row.to_skill_id) for row in existing_rows}
    for row in existing_rows:
        if (row.from_skill_id, row.to_skill_id) not in desired:
            await db.delete(row)
            result.relations_removed += 1

    for from_id, to_id in desired - existing_pairs:
        db.add(SkillRelation(from_skill_id=from_id, to_skill_id=to_id))
        result.relations_created += 1


async def seed_skill_taxonomy(
    db: AsyncSession, taxonomy: tuple[SeedSkill, ...] = SKILL_TAXONOMY
) -> SeedResult:
    """Seed (or re-seed) the taxonomy into `db`. Takes a session rather
    than building its own so tests can run it against their isolated
    schema without monkeypatching. Commits once, at the end."""
    validate_taxonomy(taxonomy)

    result = SeedResult()
    skills_by_slug = await _sync_skills(db, taxonomy, result)
    await _sync_aliases(db, taxonomy, skills_by_slug, result)
    await _sync_relations(db, taxonomy, skills_by_slug, result)
    await db.commit()
    return result


async def _main() -> None:
    factory = build_session_factory(get_settings().database_url)
    async with factory() as db:
        result = await seed_skill_taxonomy(db)

    print(
        f"skills:    {result.skills_created} created, "
        f"{result.skills_updated} updated, {result.skills_unchanged} unchanged"
    )
    print(f"aliases:   {result.aliases_created} created, {result.aliases_removed} removed")
    print(f"relations: {result.relations_created} created, {result.relations_removed} removed")
    if not result.changed_anything:
        print("Taxonomy already up to date — nothing changed.")


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
