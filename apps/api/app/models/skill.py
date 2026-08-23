import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Skill(Base):
    """A canonical, deduplicated skill name.

    Shared vocabulary, not owned by any one profile: Prompt 1.3 links it
    from `profile_target_skills` (see app/models/profile.py), Prompt 2.3
    gives it a taxonomy (category, aliases, related skills) and hangs
    per-candidate claims off it via `candidate_skills`, and later prompts
    (resume-extracted skills, GitHub evidence, job requirements) match
    against this same table rather than each inventing their own
    free-text skill spelling — see docs/decisions.md for the full
    normalization rationale.

    `slug` is the dedup key (lowercased, trimmed) so "Python", "python",
    and " Python " all resolve to one row; `name` keeps a display form.
    The slug rule is exactly `name.casefold()` and is shared with
    app/api/v1/profile.py's `_get_or_create_skill` and with the taxonomy
    seed (scripts/seed_skills.py) — all three must agree, or a
    user-coined "Python" and a seeded "Python" would become two rows.
    That agreement is regression-tested in tests/test_seed_skills.py.

    Rows come from two places and are deliberately not distinguished by a
    column: the curated seed, and users coining a skill through their
    profile's target skills. The seed only ever touches rows whose slug
    appears in the seed file, which is what keeps a user-coined skill
    safe across reruns — no `is_seeded` flag needed to express that.
    """

    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    # Nullable on purpose — a user-coined skill has no taxonomy category,
    # and inventing one ("other") would be information we do not have.
    # A closed set validated by app.schemas.skill.SkillCategory rather
    # than a Postgres enum, matching this repo's existing convention.
    category: Mapped[str | None] = mapped_column(String(40), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SkillAlias(Base):
    """An alternative spelling that resolves to exactly one canonical
    Skill — "js" -> JavaScript, "k8s" -> Kubernetes.

    A normalized table rather than a JSON/array column on `skills`
    because the entire purpose of an alias is *reverse lookup*: Prompt
    2.4's deterministic extractor asks "what skill, if any, does this
    token mean?", which needs an indexed unique key. A JSON column could
    not enforce the guarantee that actually matters here — `alias_slug`
    is unique **globally**, not just per skill, so one alias can never
    resolve to two different skills.

    An alias must never collide with a canonical `skills.slug` either
    (e.g. an alias "postgresql" when that is already PostgreSQL's own
    slug). That is a cross-table invariant no single constraint can
    express, so the seed checks it explicitly and
    tests/test_seed_skills.py asserts it.
    """

    __tablename__ = "skill_aliases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    alias: Mapped[str] = mapped_column(String(80), nullable=False)
    alias_slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SkillRelation(Base):
    """A general association between two canonical skills.

    "Related" means ONLY that the two skills commonly appear together and
    that surfacing one alongside the other is useful. It carries no
    further semantics, and nothing in the codebase may read any into it.
    Specifically, a relation does NOT mean:

      - prerequisite ("you must know X before Y")
      - parent/child, or any hierarchy or taxonomy nesting
      - substitute or equivalence (that is what SkillAlias is for)
      - dependency, implication, or transitivity

    So Python <-> Django asserts only "these two go together", never
    "Django requires Python" — even where that happens to be true in the
    real world. Encoding directional meaning would need a `relation_type`
    column and a defined vocabulary; that is deliberately out of scope
    for Prompt 2.3, and adding it later is an additive migration.

    Because the association is symmetric and untyped, the seed writes
    **both** ordered rows for each declared pair (A->B and B->A). Reads
    are then a single `WHERE from_skill_id = ?` instead of an OR/UNION
    across both columns; the cost is two rows per pair, which the seed
    maintains automatically (scripts/seed_skills.py).
    """

    __tablename__ = "skill_relations"
    __table_args__ = (
        CheckConstraint(
            "from_skill_id <> to_skill_id", name="ck_skill_relations_no_self_reference"
        ),
    )

    from_skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )
    to_skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
