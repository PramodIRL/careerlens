"""A fictional public GitHub account, and a client that serves it
(Prompt 4.5).

Run nothing here directly; this module is a FIXTURE. Two callers share
it:

    tests/test_mvp_acceptance.py   the automated Phase 4 acceptance run
    scripts/demo_github_import.py  `make demo-github`, for a live demo

WHY THIS EXISTS. Prompt 3.1-3.3 built the GitHub path against the real,
unauthenticated GitHub API, and every test of it so far has defined its
own private `_FakeClient` inline. That is fine for testing one slice,
but it left no way to SHOW the GitHub half of the product without
pointing it at somebody's real account — which puts a real person's
repositories into a dev database and a screenshot, and spends a 60-per-
hour rate limit to do it. This module is the GitHub equivalent of
scripts/sample_resumes.py: invented data, good enough to demo, that
never touches the network.

EVERY ACCOUNT, REPOSITORY AND README BELOW IS INVENTED. The login is
not a real GitHub account, the numeric id is outside the range GitHub
has issued, and every README states in its first line that it is
fictional. tests/test_mvp_acceptance.py asserts those rules
mechanically, so a real account pasted in here fails the suite rather
than reaching a demo.

WHAT THE FIXTURE IS SHAPED TO PROVE. The repository set is not
arbitrary — each entry exists to make one property visible in a demo:

  * `ledger-service`  reinforces resume skills from a SECOND source, so
    one candidate skill visibly carries both resume and GitHub evidence.
  * `deploy-notes`    is the ONLY source of Linux anywhere in the
    fixture. Ada Sample's resume never mentions it, so a job that
    prefers Linux is satisfied purely by repository evidence — the
    "GitHub closed this gap" beat.
  * `borrowed-toolkit` is a FORK whose description and topics both name
    Kubernetes. It must contribute nothing at all. This is what keeps
    Kubernetes genuinely missing for the acceptance run while the word
    is demonstrably present in ingested data — a much stronger check
    than a skill that simply never appears.
  * `sample-notes`    is ARCHIVED and has no README. Archived
    repositories ARE still credited (app/github/skill_evidence.py says
    why), so this proves archived is not quietly treated like a fork.

NO NETWORK, NO TOKEN, NO SECRET. `SampleGitHubClient` satisfies the
same app/github/base.py Protocol the real client does and answers from
the constants below.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.github.base import GitHubReadme, GitHubRepository, GitHubUser, RepositoryListing

# Stated inside each README, not only in this file's comments: README
# text is stored in the database and rendered as evidence excerpts, so
# it should say what it is wherever it surfaces.
FICTION_NOTICE = "Fictional sample repository - not a real GitHub account."

# Outside the id range GitHub has issued, so this cannot collide with a
# real account even by accident. The login is equally invented; the
# `octo-` prefix is a nod to GitHub's own mascot placeholder, and
# "fictional" says the rest.
SAMPLE_USER_ID = 9_000_001
SAMPLE_USERNAME = "octofictional"

# Fixed, not `now()`. The demo and the acceptance test must produce
# byte-identical ingested rows on every run — a moving timestamp would
# make repository ordering, and therefore evidence ordering, drift.
_PUSHED_AT = datetime(2026, 5, 14, 9, 30, tzinfo=UTC)


@dataclass(frozen=True)
class SampleRepository:
    """One fictional repository, plus the two detail responses GitHub
    would answer separately for it.

    `languages` and `readme` live here rather than in the listing
    because that is how the real API is shaped — app/github/http.py
    fetches them per repository — and SampleGitHubClient answers the
    same three calls in the same order.
    """

    repository: GitHubRepository
    languages: dict[str, int] = field(default_factory=dict)
    readme: GitHubReadme | None = None


def _readme(body: str) -> GitHubReadme:
    """Build a README whose first line is the fiction notice.

    The sha is a CONTENT hash, not `hash(text)`: Python randomises string
    hashing per process, which would hand the ingestion a different sha
    on every run. app/github/ingestion.py skips the README write only
    when the sha is unchanged, so a per-process sha would silently
    rewrite `readme_text` on every `make demo-github` — quietly breaking
    the "safe to re-run, writes nothing" property this fixture exists to
    demonstrate. SHA-1 also happens to be the shape of a real GitHub
    blob sha; it is an identifier here, never a security primitive.
    """
    text = f"{FICTION_NOTICE}\n\n{body}"
    digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    return GitHubReadme(text=text, sha=digest, size_bytes=len(text))


SAMPLE_REPOSITORIES: tuple[SampleRepository, ...] = (
    SampleRepository(
        repository=GitHubRepository(
            id=8_100_001,
            name="ledger-service",
            full_name=f"{SAMPLE_USERNAME}/ledger-service",
            description="An invented internal ledger API.",
            language="Python",
            topics=["postgresql", "docker"],
            stargazers_count=3,
            forks_count=0,
            pushed_at=_PUSHED_AT,
        ),
        languages={"Python": 24_100, "SQL": 3_400},
        readme=_readme(
            "# ledger-service\n\n"
            "A made-up service used only to demonstrate CareerLens.\n"
            "Built with FastAPI and PostgreSQL. Tested with pytest.\n"
        ),
    ),
    SampleRepository(
        repository=GitHubRepository(
            id=8_100_002,
            name="deploy-notes",
            full_name=f"{SAMPLE_USERNAME}/deploy-notes",
            description="Personal notes on running services.",
            language="Shell",
            topics=["linux", "git"],
            stargazers_count=1,
            forks_count=0,
            pushed_at=datetime(2026, 4, 2, 17, 5, tzinfo=UTC),
        ),
        languages={"Shell": 5_200},
        readme=_readme(
            "# deploy-notes\n\nNotes I keep for myself about Linux servers and shell tooling.\n"
        ),
    ),
    # A FORK. Its description and topics both say Kubernetes, and it must
    # still contribute nothing — see this module's docstring.
    SampleRepository(
        repository=GitHubRepository(
            id=8_100_003,
            name="borrowed-toolkit",
            full_name=f"{SAMPLE_USERNAME}/borrowed-toolkit",
            description="A Kubernetes deployment toolkit.",
            language="Go",
            topics=["kubernetes", "go"],
            fork=True,
            stargazers_count=0,
            forks_count=0,
            pushed_at=datetime(2026, 3, 20, 11, 0, tzinfo=UTC),
        ),
        # No languages or README: Prompt 3.2 skips detail fetching for
        # forks, so the real pipeline would never have these either.
    ),
    SampleRepository(
        repository=GitHubRepository(
            id=8_100_004,
            name="sample-notes",
            full_name=f"{SAMPLE_USERNAME}/sample-notes",
            description="Old scratch repository. Some Git experiments.",
            language=None,
            topics=[],
            archived=True,
            stargazers_count=0,
            forks_count=0,
            pushed_at=datetime(2025, 11, 8, 8, 15, tzinfo=UTC),
        ),
        languages={},
        readme=None,
    ),
)

SAMPLE_USER = GitHubUser(
    id=SAMPLE_USER_ID,
    login=SAMPLE_USERNAME,
    type="User",
    public_repos=len(SAMPLE_REPOSITORIES),
)

_BY_FULL_NAME = {sample.repository.full_name: sample for sample in SAMPLE_REPOSITORIES}


class SampleGitHubClient:
    """Serves SAMPLE_USER and SAMPLE_REPOSITORIES, and nothing else.

    Satisfies app/github/base.py's GitHubClient Protocol, so it drops
    into either of the two seams the real client is resolved through —
    `app.dependency_overrides[get_github_client]` for the connection
    endpoint, or `app.worker.get_github_client` for the ingestion task.

    Deliberately NOT a subclass of HttpGitHubClient and deliberately
    importing no HTTP library: there is no inherited code path here that
    could reach the network if a method were left unimplemented.

    `calls` records every method invoked, so a caller can assert the
    real pipeline actually asked for detail rather than silently
    skipping it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_user(self, username: str) -> GitHubUser:
        self.calls.append(("get_user", username))
        return SAMPLE_USER

    async def list_repositories(self, username: str) -> RepositoryListing:
        self.calls.append(("list_repositories", username))
        return RepositoryListing(
            repositories=tuple(sample.repository for sample in SAMPLE_REPOSITORIES),
            # The fixture IS the whole account, so pagination genuinely
            # reached the end — anything else would be a lie the
            # ingestion is entitled to act on.
            complete=True,
        )

    async def get_languages(self, full_name: str) -> dict[str, int]:
        self.calls.append(("get_languages", full_name))
        sample = _BY_FULL_NAME.get(full_name)
        return dict(sample.languages) if sample else {}

    async def get_readme(self, full_name: str) -> GitHubReadme | None:
        self.calls.append(("get_readme", full_name))
        sample = _BY_FULL_NAME.get(full_name)
        return sample.readme if sample else None
