"""Derives skill evidence from ALREADY-INGESTED public GitHub
repository data (Prompt 3.3).

Third module in the same split app/github/http.py and
app/github/ingestion.py established:

    http.py       what GitHub SAID
    ingestion.py  what to STORE      (network-bound, pausable)
    this module   what it MEANS      (database-only, atomic, no network)

NO NETWORK, NO LLM, NO EMBEDDINGS, NO FUZZY MATCHING. Every attribution
here is a literal string match against the SAME curated taxonomy and the
SAME matcher (app/skill_matching.py) that Prompt 2.4 runs over resume
text — reused unchanged, never reimplemented. Popularity is never an
input: stars and forks are stored as facts by Prompt 3.2 and are
deliberately not read here.

WHY THIS IS SEPARATE FROM INGESTION. Unauthenticated GitHub allows 60
requests/hour and one import spends about 42 of them. Fusing the two
would mean that adding a single entry to app/seeds/skill_taxonomy.py
could only be reflected by re-spending that entire budget re-fetching
data already sitting in Postgres. Separated, re-deriving costs zero
requests (scripts/extract_github_skills.py). Ingestion also pauses and
resumes for an hour at a time under rate limiting; this is one fast
transaction and has no business living inside that loop.

FOUR INVARIANTS THIS MODULE EXISTS TO UPHOLD:

1. IT NEVER WRITES `candidate_skills.status`. Same guarantee, same
   mechanism, as app/skill_extraction.py — it shares that module's
   `ensure_candidate_skills`, whose ON CONFLICT DO NOTHING is what makes
   "confirmed survives, rejected is never resurrected" a property rather
   than a hope.

2. IT ONLY EVER TOUCHES `source_type='github'` EVIDENCE. Resume evidence
   and manual evidence are outside its scope entirely, in both the write
   path and the reconciliation sweep.

3. SIGNALS ARE MATCHED INDIVIDUALLY, NEVER CONCATENATED. Joining a
   repository's topics into one string for a single matcher pass is the
   obvious implementation and it fabricates skills: the matcher tolerates
   whitespace between a term's parts, so topics ["unit", "testing"]
   joined by a newline match the skill "Unit Testing" — which neither
   topic asserts. Same hazard for languages. Each topic and each language
   therefore gets its own matcher pass, which also yields an honest
   per-signal excerpt.

4. FORKS ARE NEVER CREDITED. A fork's README, description, topics and
   languages are overwhelmingly the upstream author's work. Note this
   filter is NOT redundant with Prompt 3.2's: that one skips forks during
   DETAIL fetching, so a fork has no README or languages — but it still
   carries `description` and `topics` from the listing payload, which
   would otherwise become evidence here.
"""

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.candidate_skill import CandidateSkill
from app.models.github_repository import (
    GitHubRepository,
    GitHubRepositoryLanguage,
    GitHubRepositoryTopic,
)
from app.models.skill_evidence import SkillEvidence
from app.schemas.skill import EvidenceSourceType, ExtractionMethod
from app.skill_extraction import (
    delete_orphaned_suggestions,
    ensure_candidate_skills,
    load_taxonomy_terms,
)
from app.skill_matching import SkillTerm, find_skill_matches

logger = logging.getLogger(__name__)

# Selection order when one repository supports one skill through several
# strings of the SAME signal kind — e.g. topics "rest-api" and "restful"
# both meaning REST APIs. The evidence natural key collapses them to one
# row, so a deterministic winner is required or reruns would churn.
# Strongest match kind wins; ties break on the source string in sorted
# order. Mirrors app/skill_matching.py's own selection philosophy.
_KIND_PRIORITY: dict[str, int] = {
    "canonical": 0,
    "alias": 1,
    "ambiguous_list_context": 2,
}


@dataclass(frozen=True)
class _EvidenceKey:
    """The part of Prompt 2.3's evidence natural key that this module
    varies. `source_type` is always 'github' and `candidate_skill_id` is
    derived from `skill_id`, so those are not carried here."""

    skill_id: uuid.UUID
    source_identifier: str
    extraction_method: str


@dataclass(frozen=True)
class _EvidenceValue:
    """What gets written for one key. `excerpt` is None ONLY for language
    evidence — see ExtractionMethod.GITHUB_LANGUAGE_MATCH."""

    excerpt: str | None
    confidence: Decimal
    kind: str
    # The source string this came from, used only to break ties
    # deterministically between two signals of the same kind.
    source_text: str


@dataclass
class GitHubSkillEvidenceSummary:
    """Per-run counts, returned for logging and asserted on in tests.

    A rerun over unchanged repositories must report zero for every field
    except `repositories_considered` and `skills_matched` — that is the
    idempotency guarantee, and tests/test_github_skill_evidence.py pins it
    on row ids and timestamps rather than on these counts alone.
    """

    repositories_considered: int = 0
    skills_matched: int = 0
    candidate_skills_created: int = 0
    evidence_written: int = 0
    evidence_removed: int = 0
    suggestions_removed: int = 0


@dataclass
class _RepositorySignals:
    """One eligible repository's matchable strings, already separated by
    signal kind so nothing is ever concatenated across kinds."""

    full_name: str
    readme_text: str | None
    description: str | None
    topics: list[str] = field(default_factory=list)
    # The union of github_repository_languages.language and the
    # repository's primary_language — see _load_repository_signals.
    languages: list[str] = field(default_factory=list)


async def _load_repository_signals(
    db: AsyncSession, user_id: uuid.UUID
) -> list[_RepositorySignals]:
    """Every repository of this user's that may contribute evidence,
    with its signals loaded.

    ELIGIBILITY:
      * `deleted_at IS NULL` — a repository that a COMPLETE listing no
        longer contains is not current evidence. The row itself is kept
        (Prompt 3.2 soft-deletes precisely so this stays explicable),
        but it stops supporting skills.
      * `is_fork = False` — invariant 4 in the module docstring.
      * ARCHIVED REPOSITORIES ARE INCLUDED, deliberately. Archiving says
        "I am no longer maintaining this", not "this is not my work" —
        categorically unlike a fork. Excluding them would silently
        discard real evidence and would smuggle a recency judgment into
        extraction; recency is Prompt 4.x's to weigh, and `is_archived`
        and `pushed_at` are both stored for it.
      * Private repositories need no filter because none exist: Prompt
        3.2 fails closed and never stores one (app/github/ingestion.py).

    Ordered by github_repo_id — the stable identity (a rename changes
    full_name, that never does) — so a rerun processes repositories in
    the same order every time.
    """
    repositories = (
        await db.scalars(
            select(GitHubRepository)
            .where(
                GitHubRepository.user_id == user_id,
                GitHubRepository.deleted_at.is_(None),
                GitHubRepository.is_fork.is_(False),
            )
            .order_by(GitHubRepository.github_repo_id)
        )
    ).all()
    if not repositories:
        return []

    repository_ids = [row.id for row in repositories]
    topic_rows = (
        await db.execute(
            select(GitHubRepositoryTopic.repository_id, GitHubRepositoryTopic.topic)
            .where(GitHubRepositoryTopic.repository_id.in_(repository_ids))
            .order_by(GitHubRepositoryTopic.topic)
        )
    ).all()
    language_rows = (
        await db.execute(
            select(GitHubRepositoryLanguage.repository_id, GitHubRepositoryLanguage.language)
            .where(GitHubRepositoryLanguage.repository_id.in_(repository_ids))
            .order_by(GitHubRepositoryLanguage.language)
        )
    ).all()

    topics_by_repository: dict[uuid.UUID, list[str]] = {}
    for repository_id, topic in topic_rows:
        topics_by_repository.setdefault(repository_id, []).append(topic)
    languages_by_repository: dict[uuid.UUID, list[str]] = {}
    for repository_id, language in language_rows:
        languages_by_repository.setdefault(repository_id, []).append(language)

    signals: list[_RepositorySignals] = []
    for row in repositories:
        # `primary_language` is GitHub's own top entry from the very same
        # breakdown, so it is a MEMBER of the language set, not a
        # separate signal — treating it as one would double-count every
        # detail-fetched repository. It still earns its place: it arrives
        # in the LISTING payload, so it is the only language signal
        # available for repositories beyond Prompt 3.2's 20-repo detail
        # cap. Union, then sort, so the set is deterministic.
        #
        # Note the dedup here is belt-and-braces: even without it the
        # evidence natural key would collapse both to one row, because
        # source_identifier and extraction_method are identical.
        languages = set(languages_by_repository.get(row.id, []))
        if row.primary_language:
            languages.add(row.primary_language)
        signals.append(
            _RepositorySignals(
                full_name=row.full_name,
                readme_text=row.readme_text,
                description=row.description,
                topics=topics_by_repository.get(row.id, []),
                languages=sorted(languages),
            )
        )
    return signals


def _record(
    desired: dict[_EvidenceKey, _EvidenceValue],
    key: _EvidenceKey,
    candidate: _EvidenceValue,
) -> None:
    """Keep the winning value for a key: strongest match kind, then the
    lexicographically first source string.

    Only ever matters when one repository supports one skill through
    several strings of the same signal kind (two topics, two languages) —
    the natural key permits exactly one row, so the choice must be
    deterministic or reruns would rewrite the row every time.
    """
    current = desired.get(key)
    if current is None or (
        _KIND_PRIORITY[candidate.kind],
        candidate.source_text,
    ) < (_KIND_PRIORITY[current.kind], current.source_text):
        desired[key] = candidate


def _collect_from_text(
    desired: dict[_EvidenceKey, _EvidenceValue],
    text: str | None,
    terms: list[SkillTerm],
    full_name: str,
    method: ExtractionMethod,
    *,
    store_excerpt: bool,
) -> None:
    """Run the shared matcher over one source string and record what it
    found.

    `store_excerpt` is the single place the language exception lives:
    every other signal keeps the matcher's verbatim source slice, while
    a language match stores NULL rather than an authored restatement.
    """
    if not text or not text.strip():
        return
    for match in find_skill_matches(text, terms):
        _record(
            desired,
            _EvidenceKey(
                skill_id=match.skill_id,
                source_identifier=full_name,
                extraction_method=method.value,
            ),
            _EvidenceValue(
                excerpt=match.excerpt if store_excerpt else None,
                confidence=match.confidence,
                kind=match.kind.value,
                source_text=text,
            ),
        )


def collect_desired_evidence(
    repositories: list[_RepositorySignals], terms: list[SkillTerm]
) -> dict[_EvidenceKey, _EvidenceValue]:
    """The complete set of GitHub evidence these repositories justify.

    Pure — no database access — so the signal-decomposition rules are
    testable on their own, the same separation app/skill_matching.py has
    from app/skill_extraction.py.

    Note the two loops at the bottom: topics and languages are matched
    ONE STRING AT A TIME. See invariant 3 in the module docstring; this
    is the part that must not be "optimized" into a single pass over
    joined text.
    """
    desired: dict[_EvidenceKey, _EvidenceValue] = {}
    for repository in repositories:
        _collect_from_text(
            desired,
            repository.readme_text,
            terms,
            repository.full_name,
            ExtractionMethod.GITHUB_README_MATCH,
            store_excerpt=True,
        )
        _collect_from_text(
            desired,
            repository.description,
            terms,
            repository.full_name,
            ExtractionMethod.GITHUB_DESCRIPTION_MATCH,
            store_excerpt=True,
        )
        for topic in repository.topics:
            # A topic IS authored text — the repository owner typed it —
            # so quoting it verbatim is a genuine quotation.
            _collect_from_text(
                desired,
                topic,
                terms,
                repository.full_name,
                ExtractionMethod.GITHUB_TOPIC_MATCH,
                store_excerpt=True,
            )
        for language in repository.languages:
            _collect_from_text(
                desired,
                language,
                terms,
                repository.full_name,
                ExtractionMethod.GITHUB_LANGUAGE_MATCH,
                store_excerpt=False,
            )
    return desired


async def _write_evidence(
    db: AsyncSession,
    candidate_skill_id: uuid.UUID,
    key: _EvidenceKey,
    value: _EvidenceValue,
) -> bool:
    """Upsert one evidence row on Prompt 2.3's natural key. Returns
    whether anything was actually written.

    The `where` clause on the conflict branch is what makes a rerun a
    true no-op rather than merely duplicate-free: without it every run
    would bump `updated_at` on byte-identical rows. `IS DISTINCT FROM`
    rather than `<>` matters here specifically — language evidence stores
    a NULL excerpt, and `NULL <> NULL` is NULL, which would make the
    WHERE clause never fire and silently skip legitimate updates.
    """
    values = {
        "id": uuid.uuid4(),
        "candidate_skill_id": candidate_skill_id,
        "source_type": EvidenceSourceType.GITHUB.value,
        "source_identifier": key.source_identifier,
        "excerpt": value.excerpt,
        "extraction_method": key.extraction_method,
        "confidence": value.confidence,
    }
    statement = pg_insert(SkillEvidence).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_skill_evidence_natural_key",
        set_={
            "excerpt": statement.excluded.excerpt,
            "confidence": statement.excluded.confidence,
            "updated_at": func.now(),
        },
        where=(
            SkillEvidence.excerpt.is_distinct_from(statement.excluded.excerpt)
            | SkillEvidence.confidence.is_distinct_from(statement.excluded.confidence)
        ),
    )
    # An INSERT always yields a CursorResult at runtime (it has
    # .rowcount); AsyncSession.execute() is only typed as Result[Any].
    # Same cast, same reason, as app/skill_extraction.py's _write_evidence.
    result = cast("CursorResult[Any]", await db.execute(statement))
    return bool(result.rowcount)


async def _sweep_stale_evidence(
    db: AsyncSession, user_id: uuid.UUID, live_keys: set[tuple[uuid.UUID, str, str]]
) -> int:
    """Delete this user's GitHub evidence that the current repository
    data no longer justifies. Returns how many rows were removed.

    A FULL USER-SCOPED SWEEP, unlike app/skill_extraction.py's narrow
    per-resume delete — and the difference is justified rather than
    incidental. Resume extraction runs for ONE resume and can only speak
    for that document, so it must scope its deletes tightly. This runs
    over the user's ENTIRE account at once, so it can compute the
    complete desired set and reconcile authoritatively against it.

    One rule then covers every case, with no special-casing:

        topic removed from a repo   -> key absent -> swept
        README edited               -> key absent -> swept
        repository soft-deleted     -> not eligible -> swept
        REPOSITORY RENAMED          -> old full_name absent -> swept,
                                       new one written
        taxonomy entry removed      -> unmatchable -> swept

    The rename case is why this matters. Repository IDENTITY is
    `github_repo_id` (Prompt 3.2's invariant 1) but evidence cites
    `full_name`, per app/models/skill_evidence.py's contract for
    source_type='github'. Without a full sweep a rename would strand the
    old evidence AND write a duplicate under the new name.

    Scoped three ways so it can never reach anything that is not its
    business: source_type='github', this user's candidate skills only,
    and only keys absent from the freshly computed desired set. Resume
    and manual evidence are untouched by construction.

    `live_keys` MUST be keyed on candidate_skill_id as well as the
    source and method. Dropping it — comparing only
    (source_identifier, extraction_method) — silently breaks
    reconciliation: two skills matched from the same repository through
    the same signal share that pair, so as long as ANY skill still has,
    say, topic evidence for "ada/project", every stale topic row for
    that repository survives the sweep. Pinned by
    test_a_removed_topic_is_reconciled_away.
    """
    rows = (
        await db.execute(
            select(
                SkillEvidence.id,
                SkillEvidence.candidate_skill_id,
                SkillEvidence.source_identifier,
                SkillEvidence.extraction_method,
            )
            .join(CandidateSkill, CandidateSkill.id == SkillEvidence.candidate_skill_id)
            .where(
                CandidateSkill.user_id == user_id,
                SkillEvidence.source_type == EvidenceSourceType.GITHUB.value,
            )
        )
    ).all()

    # The diff is computed in Python rather than as a SQL tuple NOT IN:
    # the set is small and bounded by construction (Prompt 3.2 caps a
    # user at 20 detail-fetched repositories, times four methods, times a
    # ~33-entry taxonomy), and an explicit id list is obviously correct
    # to read — which matters more than cleverness for a DELETE.
    stale = [
        evidence_id
        for evidence_id, candidate_skill_id, source_identifier, extraction_method in rows
        if (candidate_skill_id, source_identifier, extraction_method) not in live_keys
    ]
    if not stale:
        return 0
    await db.execute(delete(SkillEvidence).where(SkillEvidence.id.in_(stale)))
    return len(stale)


async def extract_github_skill_evidence(
    db: AsyncSession, user_id: uuid.UUID
) -> GitHubSkillEvidenceSummary:
    """Derive one user's GitHub-backed skill evidence from stored
    repository data. Commits once, at the end.

    ONE TRANSACTION, unlike app/github/ingestion.py's commit-per-
    repository. That module commits incrementally because it makes
    network calls and can be suspended for an hour mid-run, so partial
    progress must survive. This one touches only the database and
    finishes in milliseconds, making full atomicity both cheap and
    correct: reconciliation never observes a half-written desired set.

    `user_id` is supplied by the caller from the ingestion run's owner or
    the GitHub connection's owner — never from a request parameter — and
    every query below is filtered by it, so no cross-user write is
    reachable.
    """
    summary = GitHubSkillEvidenceSummary()

    repositories = await _load_repository_signals(db, user_id)
    summary.repositories_considered = len(repositories)

    # Loaded from app/skill_extraction.py unchanged: restricted to
    # `skills.category IS NOT NULL`, so a row coined by a user through
    # Prompt 1.3's target skills is never matchable. Prompt 3.3 adds no
    # taxonomy entries and creates no `skills` or `skill_aliases` rows.
    terms = await load_taxonomy_terms(db)
    desired = collect_desired_evidence(repositories, terms)
    summary.skills_matched = len({key.skill_id for key in desired})

    by_skill, created = await ensure_candidate_skills(
        db, user_id, {key.skill_id for key in desired}
    )
    summary.candidate_skills_created += created

    live_keys: set[tuple[uuid.UUID, str, str]] = set()
    for key, value in desired.items():
        candidate_skill = by_skill.get(key.skill_id)
        if candidate_skill is None:  # pragma: no cover - defensive
            continue
        live_keys.add((candidate_skill.id, key.source_identifier, key.extraction_method))
        if await _write_evidence(db, candidate_skill.id, key, value):
            summary.evidence_written += 1

    # Order is load-bearing: write, then sweep, then drop orphans. Doing
    # it the other way round would briefly leave a skill with no evidence
    # and delete a suggestion this very run is about to re-justify.
    summary.evidence_removed = await _sweep_stale_evidence(db, user_id, live_keys)
    summary.suggestions_removed = await delete_orphaned_suggestions(db, user_id)

    await db.commit()
    return summary


async def purge_github_skill_evidence(
    db: AsyncSession, user_id: uuid.UUID
) -> GitHubSkillEvidenceSummary:
    """Remove all of one user's GitHub-derived evidence. Does NOT commit —
    the caller owns the transaction.

    Called when a user disconnects their GitHub account
    (app/api/v1/github_connection.py). Prompt 3.1 deferred this decision
    to Prompt 3.3, and the answer is to purge, for a reason that does not
    apply to the dangling-resume reference docs/decisions.md accepts:
    deleted-resume evidence is still RECONCILABLE, because the resume
    extractor may run again — but after a disconnect there is no
    connection, so GitHub reconciliation will never run again. Leaving
    the evidence would strand the user with skills citing repositories
    that no longer exist and no mechanism to remove them.

    Deliberately narrow: it is the same sweep as above with an empty
    desired set, so resume evidence, manual evidence, and every confirmed
    or rejected candidate skill survive untouched. Only UNREVIEWED
    suggestions left with no evidence at all are removed.
    """
    summary = GitHubSkillEvidenceSummary()
    summary.evidence_removed = await _sweep_stale_evidence(db, user_id, set())
    summary.suggestions_removed = await delete_orphaned_suggestions(db, user_id)
    return summary
