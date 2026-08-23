"""Closed vocabularies for the skill taxonomy and evidence model
(Prompt 2.3).

Each is a Python StrEnum validated at the application boundary, with the
underlying column stored as plain text — the same choice, for the same
reason, as `profiles.experience_level` and `resumes.status`: adding a
category or a source type later is a code change, not an `ALTER TYPE`
migration. `SkillEvidence.confidence` is the deliberate exception that
*does* get a database CHECK constraint (see app/models/skill_evidence.py)
— a numeric range is a permanent invariant, whereas a vocabulary is
expected to grow.

No Pydantic request/response models live here yet: Prompt 2.3 adds no
endpoints (see docs/decisions.md), so anything beyond these vocabularies
would be speculative.
"""

from enum import StrEnum


class SkillCategory(StrEnum):
    """Coarse grouping for a canonical skill, used to keep the seeded
    taxonomy reviewable and to group skills in a later UI.

    Intentionally coarse — this is a small curated taxonomy for
    early-career software roles, not an attempt to classify every skill
    in technology (see app/seeds/skill_taxonomy.py).

    Nullable on the column: a skill coined by a user through their
    profile's target skills (app/api/v1/profile.py) genuinely has no
    taxonomy category, and defaulting it to "other" would invent
    information we do not have.
    """

    LANGUAGE = "language"
    FRAMEWORK = "framework"
    DATABASE = "database"
    INFRASTRUCTURE = "infrastructure"
    TOOL = "tool"
    TESTING = "testing"
    CONCEPT = "concept"


class EvidenceSourceType(StrEnum):
    """Where a piece of skill evidence came from.

    Each value implies a different convention for
    `SkillEvidence.source_identifier` — see that model's docstring for
    the per-type contract.
    """

    RESUME = "resume"
    GITHUB = "github"
    MANUAL = "manual"


class ExtractionMethod(StrEnum):
    """How a piece of evidence was derived.

    Only MANUAL_ENTRY exists in Prompt 2.3, because nothing in this
    prompt extracts anything — the vocabulary starts here so that Prompt
    2.4's deterministic resume matching (and Prompt 3.x's GitHub
    ingestion) add members rather than inventing a parallel field.

    Every member must stay a *deterministic, inspectable* method, per
    docs/project-brief.md's Evidence-First rule: an LLM may later explain
    a persisted result, but must never be the thing that invented it.
    """

    MANUAL_ENTRY = "manual_entry"
