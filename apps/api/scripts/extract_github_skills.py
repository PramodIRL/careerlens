"""Re-derive GitHub-backed skill evidence for every connected user,
without contacting GitHub (Prompt 3.3).

    make github-skills

THIS SCRIPT IS THE POINT OF KEEPING INGESTION AND SKILL EXTRACTION
SEPARATE. Unauthenticated GitHub allows 60 requests/hour and one import
spends about 42 of them. When the curated taxonomy changes — a skill or
alias added to app/seeds/skill_taxonomy.py, then `make seed-skills` — the
stored repository data is already correct and only the DERIVED evidence
is stale. Re-running an import to fix that would re-spend the entire
budget re-fetching README and language data we already hold. This
re-derives it from Postgres alone, at zero request cost.

SAFE TO RE-RUN BY CONSTRUCTION, for the same reason
scripts/seed_skills.py is: app/github/skill_evidence.py reconciles
against a freshly computed desired set, so a second run over unchanged
repositories writes nothing at all — no duplicate evidence, no bumped
timestamps. It never writes `candidate_skills.status`, so confirmed
skills stay confirmed and rejected ones are never resurrected, and it
only ever touches `source_type='github'` evidence — resume and manual
evidence are outside its scope.

A sync entry point doing async work via `asyncio.run`, mirroring the
split in app/worker.py and scripts/requeue_stuck_resumes.py.
"""

import asyncio
import logging

from sqlalchemy import select

from app.db import build_session_factory
from app.github.skill_evidence import extract_github_skill_evidence
from app.models.github_connection import GitHubConnection
from app.settings import get_settings

logger = logging.getLogger(__name__)


async def extract_for_all_connected_users() -> int:
    """Re-derive evidence for every user with a GitHub connection.

    Scoped to users who currently have a connection: disconnecting
    purges GitHub evidence (app/api/v1/github_connection.py), so a user
    without one has nothing to re-derive and must not have anything
    written for them here. Returns how many users were processed.

    One session per user rather than one for the whole sweep, so a
    failure for one user commits nothing for them and does not abort the
    rest — the same reasoning that gives ingestion a commit per
    repository.
    """
    factory = build_session_factory(get_settings().database_url)
    async with factory() as db:
        user_ids = list((await db.scalars(select(GitHubConnection.user_id))).all())

    for user_id in user_ids:
        async with factory() as db:
            try:
                summary = await extract_github_skill_evidence(db, user_id)
            except Exception:
                logger.exception("github skill extraction failed for user %s", user_id)
                continue
            print(  # noqa: T201 - a CLI script; this is its output
                f"{user_id}: {summary.repositories_considered} repositories, "
                f"{summary.skills_matched} skills, "
                f"+{summary.candidate_skills_created} candidate skills, "
                f"{summary.evidence_written} evidence written, "
                f"{summary.evidence_removed} removed, "
                f"{summary.suggestions_removed} suggestions removed"
            )
    return len(user_ids)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    count = asyncio.run(extract_for_all_connected_users())
    print(f"processed {count} connected user(s)")  # noqa: T201 - CLI output


if __name__ == "__main__":
    main()
