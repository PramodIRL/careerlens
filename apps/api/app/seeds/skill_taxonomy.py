"""The curated skill taxonomy seed for early-career software roles
(Prompt 2.3).

This file is the editable source of truth for the taxonomy. Change it,
then re-run `make seed-skills` (scripts/seed_skills.py) — the seed
converges the database to exactly what is written here for the skills it
names, and never touches any other row.

Kept intentionally small (see docs/project-brief.md): ~30 well-known
skills across a handful of categories, chosen to be reviewable by a
human in one sitting. This is NOT an attempt to model every skill in
technology, and it should not grow into one — a wide taxonomy would make
Prompt 2.4's matching noisier, not better.

A plain typed Python module rather than JSON/YAML because it is checked
by mypy, supports the explanatory comments below, and needs no
file-loading or parse-error handling at runtime. The audience for
"editable" here is a developer, not a spreadsheet.

ALIAS RULE — an alias must be an unambiguous *technical* identifier,
never a common English word. Prompt 2.4 will match these against raw
resume text, so a sloppy alias becomes a false positive that a candidate
then has to correct. "next" (Next.js), "rest" (REST APIs) and "node"
(Node.js) were all deliberately rejected on this basis: "next steps",
"the rest of the team" and "leaf node" would all match. An alias must
also never equal another skill's canonical slug — the seed rejects that
outright, since it would make the token ambiguous.

RELATED RULE — `related` means only a general association ("these
commonly appear together"). It does NOT encode prerequisite,
parent/child, substitute, dependency, or hierarchy; see
app/models/skill.py's SkillRelation docstring. Each pair need only be
declared once, on either side: the seed materializes both directions.
"""

from dataclasses import dataclass, field

from app.schemas.skill import SkillCategory


@dataclass(frozen=True)
class SeedSkill:
    """One canonical skill as declared in the taxonomy. `related` holds
    the canonical `name` of other seeded skills (never slugs or
    aliases), so this file stays readable; the seed resolves them to ids
    and fails loudly if a name is unknown."""

    name: str
    category: SkillCategory
    aliases: tuple[str, ...] = ()
    related: tuple[str, ...] = field(default=())


SKILL_TAXONOMY: tuple[SeedSkill, ...] = (
    # --- Languages ---
    SeedSkill(
        name="Python",
        category=SkillCategory.LANGUAGE,
        aliases=("py",),
        related=("Django", "FastAPI", "Flask", "pytest"),
    ),
    SeedSkill(
        name="JavaScript",
        category=SkillCategory.LANGUAGE,
        # "ecmascript" is safe (never plain English); bare "js" is the
        # near-universal written form and worth the small risk.
        aliases=("js", "ecmascript"),
        related=("TypeScript", "React", "Node.js", "Jest"),
    ),
    SeedSkill(
        name="TypeScript",
        category=SkillCategory.LANGUAGE,
        aliases=("ts",),
        related=("React",),
    ),
    # No alias for Java: "java" must never be reachable from JavaScript's
    # spellings, and it has no common abbreviation of its own.
    SeedSkill(name="Java", category=SkillCategory.LANGUAGE),
    SeedSkill(name="Go", category=SkillCategory.LANGUAGE, aliases=("golang",)),
    SeedSkill(
        name="SQL",
        category=SkillCategory.LANGUAGE,
        related=("PostgreSQL", "MySQL"),
    ),
    SeedSkill(name="HTML", category=SkillCategory.LANGUAGE, aliases=("html5",), related=("CSS",)),
    SeedSkill(name="CSS", category=SkillCategory.LANGUAGE, aliases=("css3",)),
    # --- Frameworks ---
    SeedSkill(
        name="React",
        category=SkillCategory.FRAMEWORK,
        aliases=("reactjs", "react.js"),
        related=("Next.js",),
    ),
    SeedSkill(name="Next.js", category=SkillCategory.FRAMEWORK, aliases=("nextjs",)),
    SeedSkill(name="Django", category=SkillCategory.FRAMEWORK),
    SeedSkill(
        name="FastAPI",
        category=SkillCategory.FRAMEWORK,
        aliases=("fast api",),
        related=("REST APIs",),
    ),
    SeedSkill(name="Flask", category=SkillCategory.FRAMEWORK),
    SeedSkill(
        name="Node.js",
        category=SkillCategory.FRAMEWORK,
        aliases=("nodejs",),
        related=("Express",),
    ),
    SeedSkill(
        name="Express",
        category=SkillCategory.FRAMEWORK,
        aliases=("expressjs", "express.js"),
        related=("REST APIs",),
    ),
    # --- Databases ---
    # Note: "postgresql" is NOT an alias — it is already this row's own
    # canonical slug, and the seed rejects an alias that shadows a slug.
    SeedSkill(
        name="PostgreSQL",
        category=SkillCategory.DATABASE,
        aliases=("postgres", "psql"),
    ),
    SeedSkill(name="MySQL", category=SkillCategory.DATABASE),
    SeedSkill(name="MongoDB", category=SkillCategory.DATABASE, aliases=("mongo",)),
    SeedSkill(name="Redis", category=SkillCategory.DATABASE),
    # --- Infrastructure ---
    SeedSkill(
        name="Docker",
        category=SkillCategory.INFRASTRUCTURE,
        related=("Kubernetes", "CI/CD"),
    ),
    SeedSkill(name="Kubernetes", category=SkillCategory.INFRASTRUCTURE, aliases=("k8s",)),
    SeedSkill(
        name="AWS",
        category=SkillCategory.INFRASTRUCTURE,
        aliases=("amazon web services",),
    ),
    SeedSkill(
        name="CI/CD",
        category=SkillCategory.INFRASTRUCTURE,
        aliases=("cicd", "continuous integration"),
    ),
    # --- Tools ---
    SeedSkill(name="Git", category=SkillCategory.TOOL),
    SeedSkill(name="Linux", category=SkillCategory.TOOL),
    # --- Testing ---
    SeedSkill(
        name="pytest",
        category=SkillCategory.TESTING,
        aliases=("py.test",),
        related=("Unit Testing",),
    ),
    SeedSkill(name="Jest", category=SkillCategory.TESTING, related=("Unit Testing",)),
    SeedSkill(name="Unit Testing", category=SkillCategory.TESTING, aliases=("unit tests",)),
    # --- Concepts ---
    SeedSkill(
        name="REST APIs",
        category=SkillCategory.CONCEPT,
        # Bare "rest" is deliberately excluded — see the ALIAS RULE above.
        aliases=("rest api", "restful"),
    ),
    SeedSkill(name="Data Structures", category=SkillCategory.CONCEPT),
    SeedSkill(name="Algorithms", category=SkillCategory.CONCEPT),
    SeedSkill(
        name="Object-Oriented Programming",
        category=SkillCategory.CONCEPT,
        aliases=("oop",),
    ),
    # No "scrum" alias: Scrum is a distinct framework, not another word
    # for Agile. Conflating them would be a taxonomy error, not a
    # convenience.
    SeedSkill(name="Agile", category=SkillCategory.CONCEPT),
)
