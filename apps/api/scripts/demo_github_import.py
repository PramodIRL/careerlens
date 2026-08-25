"""Import the fictional GitHub account for one existing user, without
contacting GitHub (Prompt 4.5).

    make demo-github EMAIL=ada.sample@example.com

WHY THIS EXISTS. The GitHub half of the product cannot be shown in a
browser without a GitHub account to import, and the only accounts
available are real ones — which puts a real person's repositories into a
dev database and a demo screenshot, and spends GitHub's 60-requests-per-
hour unauthenticated budget to do it. This runs the REAL ingestion and
the REAL evidence derivation against scripts/sample_github.py's invented
account instead. See docs/phase-4-acceptance.md.

WHAT IT DELIBERATELY IS NOT. There is no fake-client mode inside the
running API server, and this script does not add one: the server always
resolves the real client, so there is no environment variable that could
turn a deployment into a fixture server. The substitution lives here, in
a developer command, for the same reason scripts/sample_resumes.py
refuses to seed database state — a demo convenience that ships as a code
path is a backdoor. See docs/decisions.md.

IT WILL NOT CREATE A USER. The account must already exist, registered
through the normal flow — so this cannot become a way to conjure
pre-made demo users that skip the parts of the product a demo is meant
to show. It touches exactly one user's rows and prints every one of
them.

SAFE TO RE-RUN, with one honest exception. app/github/ingestion.py and
app/github/skill_evidence.py both reconcile against a freshly computed
desired set, so a second run over the same fixture creates no duplicate
repository, no duplicate evidence and no new candidate skill, and it
never writes `candidate_skills.status` — a skill confirmed or rejected
in the UI keeps that decision.

The exception: each run DOES move `github_repositories.last_seen_at`
and `updated_at`, because `_upsert_repository` records "we saw this
repository again" unconditionally (Prompt 3.2). That is a real fact
about the import, not churn — but it means "writes nothing" is not
literally true, and this file should not claim it is. README rows are
genuinely untouched: their sha is a content hash (see
scripts/sample_github.py), so the write is skipped.
"""

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.db import build_session_factory
from app.github.ingestion import run_ingestion
from app.github.skill_evidence import extract_github_skill_evidence
from app.models.github_connection import GitHubConnection
from app.models.github_ingestion_run import GitHubIngestionRun
from app.models.user import User
from app.schemas.github_ingestion import IngestionStatus
from app.settings import get_settings
from scripts.sample_github import SAMPLE_USER, SAMPLE_USERNAME, SampleGitHubClient

logger = logging.getLogger(__name__)


class DemoImportError(RuntimeError):
    """Something the operator needs to fix, reported without a stack
    trace — a missing user is a normal mistake, not a crash."""


async def import_sample_github_for(email: str) -> str:
    """Connect and import the fictional account for one user.

    Returns a human-readable summary. Raises DemoImportError when the
    user does not exist.
    """
    factory = build_session_factory(get_settings().database_url)
    client = SampleGitHubClient()

    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if user is None:
            raise DemoImportError(
                f"no user with email {email!r}. Register through the app first "
                "(http://localhost:3000/register) — this script deliberately "
                "will not create one."
            )
        user_id = user.id

        # The same row the PUT /github-connection endpoint writes, with
        # the same values taken from the client's answer rather than
        # from a literal here, so the two paths cannot disagree about
        # what "connected" means.
        connection = await db.get(GitHubConnection, user_id)
        if connection is None:
            connection = GitHubConnection(user_id=user_id)
            db.add(connection)
        connection.github_user_id = SAMPLE_USER.id
        connection.username = SAMPLE_USER.login
        connection.public_repo_count = SAMPLE_USER.public_repos
        connection.last_verified_at = datetime.now(UTC)

        # A run row, exactly as POST /ingestions creates one — so the
        # dashboard's import history shows this the same way it shows a
        # real import.
        run = GitHubIngestionRun(user_id=user_id, status=IngestionStatus.QUEUED.value)
        db.add(run)
        await db.commit()
        run_id = run.id

    # The two calls app/worker.py's ingest_github_repositories makes, in
    # the same order, against the fixture client instead of the network.
    async with factory() as db:
        outcome = await run_ingestion(db, client, run_id, attempt=1, max_attempts=1)
        if outcome.should_retry:  # pragma: no cover - the fixture never rate-limits
            raise DemoImportError("the sample client asked for a retry, which should be impossible")
        summary = await extract_github_skill_evidence(db, user_id)
        refreshed = await db.get(GitHubIngestionRun, run_id)

    status = refreshed.status if refreshed else "unknown"
    completed = refreshed.repositories_completed if refreshed else 0
    forks = refreshed.repositories_forks_excluded if refreshed else 0

    return "\n".join(
        (
            f"  Connected {SAMPLE_USERNAME} (id {SAMPLE_USER.id}) to {email}",
            f"  Ingestion run {run_id} finished: {status}",
            f"  Ingested {completed} repositories ({forks} fork(s) excluded)",
            f"  Derived {summary.evidence_written} new github evidence row(s) "
            f"across {summary.skills_matched} skill(s)",
            f"  Created {summary.candidate_skills_created} new candidate skill(s); "
            f"removed {summary.evidence_removed} stale evidence row(s)",
            "  Wrote nothing outside this user. No network request was made.",
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        required=True,
        help="the existing user to import the fictional account for",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    try:
        summary = asyncio.run(import_sample_github_for(args.email))
    except DemoImportError as exc:
        print(f"error: {exc}", file=sys.stderr)  # noqa: T201 - CLI output
        raise SystemExit(1) from exc

    print(summary)  # noqa: T201 - a CLI script; this is its output


if __name__ == "__main__":
    main()
