"""Provider-agnostic embedding interface and pgvector-backed storage.

INFRASTRUCTURE ONLY. Nothing in this package is wired into matching,
gaps, eligibility, extraction, ranking or the API. `skill_match_v1` and
`skill_gap_v1` neither import it nor know it exists; there is no
endpoint, no similarity function and no retrieval path. The schema is
shaped so a later slice can add semantic retrieval without a migration,
and that slice is where any of it becomes visible to a user.
"""
