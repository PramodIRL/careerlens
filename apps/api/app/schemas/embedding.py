"""Closed vocabulary for embedding storage (embedding infrastructure
slice).

Deliberately holds NO request/response models. This slice adds no HTTP
endpoint at all — there is nothing for a client to send or receive — so
the only thing that belongs here is the source-type vocabulary, kept in
`app/schemas/` for the same reason `EvidenceSourceType` is: closed sets
live at the application boundary, and the underlying column stays plain
text so adding a source type later is a code change rather than an
`ALTER TYPE` migration.
"""

from enum import StrEnum


class EmbeddingSourceType(StrEnum):
    """What kind of row an embedding was derived from.

    Each value fixes the meaning of `embeddings.source_id`, exactly the
    way `app/models/skill_evidence.py` fixes `source_identifier`:

        skill_evidence        -> the `skill_evidence.id` UUID, as a string
        github_repository     -> the `github_repositories.id` UUID
        saved_job_description -> the `saved_jobs.id` UUID

    Every value points at OUR row, never at a third party's identifier.
    That is what lets a chunk be explained back to its source with an
    ordinary join, and it keeps the provenance the source row already
    records (which resume, which "owner/repo") reachable without copying
    it here.

    WHAT IS ABSENT IS THE POINT. There is no `resume` member: the full
    extracted resume text is private prose, and embedding it wholesale
    would put a copy of a candidate's document into a second table with
    a different access path. Resume-derived content reaches this table
    only as `skill_evidence` excerpts — short, already-quotable spans a
    user can see in the UI today.
    """

    SKILL_EVIDENCE = "skill_evidence"
    GITHUB_REPOSITORY = "github_repository"
    SAVED_JOB_DESCRIPTION = "saved_job_description"
