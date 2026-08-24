"""Human-readable names for the things skill evidence cites (Prompt 3.4).

`skill_evidence.source_identifier` is a polymorphic string, not a foreign
key — no single FK can span a resume, a repository and a user (see
app/models/skill_evidence.py). That keeps the evidence table simple, but
it means the stored value is not always something a person can read: a
resume is cited by UUID.

The cost of leaving that unresolved is a real product failure, not a
cosmetic one. Prompt 3.3 can write evidence from up to twenty
repositories, and the dashboard rendered every one of them as the same
four words, "From GitHub" — which tells a candidate nothing about which
of their projects demonstrates the skill. Evidence-First means naming
the source, not just its category.

Split into an async loader (one query, user-scoped) and a PURE mapper,
so every labelling rule is testable without a database — the same
separation app/skill_matching.py has from app/skill_extraction.py.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.resume import Resume
from app.schemas.skill import EvidenceSourceType


async def load_resume_labels(db: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    """Map this user's resume ids (as strings) to their original filenames.

    SCOPED TO ONE USER, deliberately. The lookup is keyed by a value
    taken from evidence rows, and evidence is reachable only through the
    caller's own candidate skills — but filtering here too means a label
    can never be resolved from another user's resume even if a
    source_identifier were somehow wrong. Fail closed rather than rely on
    an upstream join being correct.

    Keys are `str(id)` because that is exactly how the extractor stores
    them (app/skill_extraction.py writes `str(resume_id)`), so no
    parsing or UUID coercion is needed at the comparison point — and a
    malformed identifier simply fails to match instead of raising.
    """
    rows = (
        await db.execute(
            select(Resume.id, Resume.original_filename).where(Resume.user_id == user_id)
        )
    ).all()
    return {str(resume_id): filename for resume_id, filename in rows}


def label_for(
    source_type: str, source_identifier: str, resume_labels: dict[str, str]
) -> str | None:
    """The display name for one piece of evidence, or None.

    Pure. Per source type:

        resume  -> the resume's own `original_filename`
        github  -> `source_identifier` unchanged; it is already
                   "owner/repo", which is the readable form
        manual  -> None. The identifier is the user's own id, and
                   echoing "you asserted this because you are you" adds
                   nothing the source type has not already said.

    NONE IS A LEGITIMATE ANSWER, NOT A FAILURE. A resume deleted after
    its evidence was written leaves an identifier that no longer
    resolves — the dangling polymorphic reference docs/decisions.md
    accepts by design. Returning None keeps the evidence as the
    historical record it is; inventing "(deleted resume)" would be
    authored prose in a field reserved for real names, which is the same
    line Prompt 3.3 drew when it stored a NULL excerpt for language
    evidence rather than a generated one.
    """
    if source_type == EvidenceSourceType.RESUME.value:
        return resume_labels.get(source_identifier)
    if source_type == EvidenceSourceType.GITHUB.value:
        return source_identifier or None
    # manual, and any source type added later without a labelling rule.
    return None
