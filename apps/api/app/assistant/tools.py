"""Read-only tools the assistant can call, over the CALLER's own rows.

OWNERSHIP IS INHERITED, NOT RE-IMPLEMENTED. Every tool calls the same
read handler or query a client reaches through the API, passing the
authenticated user, so a tool scopes exactly as its route does: lists
filter on `current_user.id`, and a job id that belongs to someone else
hits `_get_owned_job`'s 403 and comes back to the model as an error
string with no data in it. There is no tool argument that names a user.

STRICTLY READ-ONLY. No tool inserts, updates, deletes or commits. The
profile tool reads the row directly rather than through GET /profiles,
because that handler creates an empty profile on first read.

RESULTS ARE COMPACT. The model gets names, levels, states and short
excerpts, not every evidence row: tokens cost the user money, and a
smaller context grounds better than a larger one.
"""

import json
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.profile import _load_target_roles, _load_target_skills, _to_response
from app.api.v1.resume import _get_owned_resume
from app.api.v1.roadmap import read_roadmap
from app.api.v1.saved_job import (
    list_saved_jobs,
    read_saved_job,
    read_saved_job_eligibility,
    read_saved_job_gaps,
    read_saved_job_match,
)
from app.api.v1.skill_profile import read_skill_profile
from app.models.profile import Profile
from app.models.resume import Resume
from app.models.user import User
from app.schemas.resume import ResumeStatus

# Character budgets for the two free-text sources, so one long posting
# or resume cannot crowd the rest of the context out.
RESUME_TEXT_MAX_CHARS = 12_000
JOB_DESCRIPTION_MAX_CHARS = 4_000
_EXCERPT_MAX_CHARS = 200
_EVIDENCE_PER_SKILL = 2

GET_PROFILE = "get_profile"
GET_SKILL_PROFILE = "get_skill_profile"
LIST_SAVED_JOBS = "list_saved_jobs"
GET_JOB_FIT = "get_job_fit"
GET_ROADMAP = "get_roadmap"
GET_RESUME_TEXT = "get_resume_text"


def _spec(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(properties or {}),
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: list[dict[str, Any]] = [
    _spec(GET_PROFILE, "The user's profile: name, headline, location, level, target roles/skills."),
    _spec(
        GET_SKILL_PROFILE,
        "The user's skills with status (confirmed/suggested), sources (resume, github, "
        "manual) and short evidence excerpts.",
    ),
    _spec(
        LIST_SAVED_JOBS,
        "The user's saved jobs in their priority order, each with its id and match score "
        "(0-100) and how many required skills are matched.",
    ),
    _spec(
        GET_JOB_FIT,
        "For one saved job: the posting, matched and missing skills, gap buckets, and "
        "eligibility against bars like GPA or graduation year.",
        {"saved_job_id": {"type": "string", "description": "id from list_saved_jobs"}},
    ),
    _spec(
        GET_ROADMAP,
        "The user's current 4-week learning plan (1 hour/day) across their top saved jobs.",
    ),
    _spec(GET_RESUME_TEXT, "The extracted text of the user's latest processed resume."),
]

TOOL_NAMES = frozenset(spec["function"]["name"] for spec in TOOL_SPECS)


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "..."


async def load_resume(db: AsyncSession, user: User, resume_id: uuid.UUID | None) -> Resume | None:
    """The given resume (ownership-checked, 404/403 as the resume routes
    answer), or the caller's newest one with extracted text."""
    if resume_id is not None:
        return await _get_owned_resume(db, resume_id, user)
    resume: Resume | None = await db.scalar(
        select(Resume)
        .where(
            Resume.user_id == user.id,
            Resume.status == ResumeStatus.SUCCEEDED.value,
            Resume.extracted_text.is_not(None),
        )
        .order_by(Resume.created_at.desc())
        .limit(1)
    )
    return resume


async def _profile(db: AsyncSession, user: User) -> dict[str, Any]:
    profile = await db.get(Profile, user.id)
    if profile is None:
        return {"profile": None, "note": "The user has not filled in a profile yet."}
    roles = await _load_target_roles(db, user.id)
    skills = await _load_target_skills(db, user.id)
    return {
        "profile": _to_response(profile, roles, skills).model_dump(
            mode="json", exclude={"user_id", "created_at", "updated_at"}
        )
    }


async def _skill_profile(db: AsyncSession, user: User) -> dict[str, Any]:
    view = await read_skill_profile(current_user=user, db=db)
    return {
        "summary": view.summary.model_dump(mode="json"),
        "skills": [
            {
                "name": entry.skill_name,
                "category": entry.skill_category,
                "status": entry.status,
                "sources": entry.sources,
                "evidence": [
                    {"from": row.source_label, "excerpt": _clip(row.excerpt, _EXCERPT_MAX_CHARS)}
                    for row in entry.evidence[:_EVIDENCE_PER_SKILL]
                ],
            }
            for entry in view.skills
        ],
    }


async def _saved_jobs(db: AsyncSession, user: User) -> dict[str, Any]:
    jobs = await list_saved_jobs(current_user=user, db=db)
    rows = []
    for rank, job in enumerate(jobs, start=1):
        match = await read_saved_job_match(job.id, user, db)
        rows.append(
            {
                "saved_job_id": str(job.id),
                "priority_rank": rank,
                "company": job.company,
                "title": job.title,
                "location": job.location,
                "match_score": match.overall_score,
                "required_matched": match.required_matched,
                "required_total": match.required_total,
            }
        )
    return {"saved_jobs": rows}


async def _job_fit(db: AsyncSession, user: User, saved_job_id: uuid.UUID) -> dict[str, Any]:
    job = await read_saved_job(saved_job_id, user, db)
    match = await read_saved_job_match(saved_job_id, user, db)
    gaps = await read_saved_job_gaps(saved_job_id, user, db)
    eligibility = await read_saved_job_eligibility(saved_job_id, user, db)
    return {
        "job": {
            "company": job.company,
            "title": job.title,
            "location": job.location,
            "description": _clip(job.description, JOB_DESCRIPTION_MAX_CHARS),
        },
        "match": {
            "match_score": match.overall_score,
            "required_matched": match.required_matched,
            "required_total": match.required_total,
            "matched": [
                {"skill": s.skill_name, "level": s.requirement_level, "status": s.candidate_status}
                for s in match.matched_skills
            ],
            "missing": [
                {"skill": s.skill_name, "level": s.requirement_level} for s in match.missing_skills
            ],
        },
        "gaps": {
            "totals": gaps.totals.model_dump(mode="json"),
            "required": [g.skill_name for g in gaps.required_gaps],
            "preferred": [g.skill_name for g in gaps.preferred_gaps],
            "mentioned": [g.skill_name for g in gaps.informational_gaps],
            "has_but_unconfirmed": [g.skill_name for g in gaps.needs_confirmation],
            "user_rejected": [g.skill_name for g in gaps.rejected_requirements],
        },
        "eligibility": {
            "flag": eligibility.flag,
            "requirements": [
                {"type": r.requirement_type, "state": r.state, "reason": r.reason}
                for r in eligibility.requirements
            ],
        },
    }


async def _roadmap(db: AsyncSession, user: User) -> dict[str, Any]:
    plan = await read_roadmap(
        top_n=None,
        duration_days=28,
        hours_per_day=1.0,
        narrate=False,
        current_user=user,
        db=db,
    )
    if not plan.has_selected_jobs:
        return {"roadmap": None, "note": "No saved jobs yet, so there is no roadmap."}
    # An item is nested under every week it spans; list each once.
    items: dict[str, dict[str, Any]] = {}
    for week in plan.weeks:
        for item in week.items:
            items.setdefault(
                item.item_id,
                {
                    "skill": item.skill_name,
                    "state": item.state,
                    "days": f"{item.start_day}-{item.end_day}",
                    "estimated_hours": item.estimated_hours,
                    "why": item.why,
                    "jobs": [f"{j.title} at {j.company}" for j in item.affected_jobs],
                },
            )
    return {
        "duration_days": plan.duration_days,
        "hours_per_day": plan.hours_per_day,
        "selected_job_count": plan.selected_job_count,
        "coverage": plan.coverage,
        "items": list(items.values()),
    }


async def _resume_text(db: AsyncSession, user: User) -> dict[str, Any]:
    resume = await load_resume(db, user, None)
    if resume is None or resume.extracted_text is None:
        return {"resume": None, "note": "No processed resume yet."}
    return {
        "filename": resume.original_filename,
        "uploaded_at": resume.created_at.date().isoformat(),
        "truncated": len(resume.extracted_text) > RESUME_TEXT_MAX_CHARS,
        "text": _clip(resume.extracted_text, RESUME_TEXT_MAX_CHARS),
    }


async def run_tool(name: str, raw_arguments: str | None, db: AsyncSession, user: User) -> Any:
    """Run one tool call. Never raises for a bad call: the model gets an
    `error` it can read and recover from, and the loop carries on."""
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError:
        return {"error": "arguments were not valid JSON"}
    if not isinstance(arguments, dict):
        return {"error": "arguments must be a JSON object"}

    try:
        if name == GET_PROFILE:
            return await _profile(db, user)
        if name == GET_SKILL_PROFILE:
            return await _skill_profile(db, user)
        if name == LIST_SAVED_JOBS:
            return await _saved_jobs(db, user)
        if name == GET_JOB_FIT:
            try:
                saved_job_id = uuid.UUID(str(arguments.get("saved_job_id")))
            except ValueError:
                return {"error": "saved_job_id must be an id from list_saved_jobs"}
            return await _job_fit(db, user, saved_job_id)
        if name == GET_ROADMAP:
            return await _roadmap(db, user)
        if name == GET_RESUME_TEXT:
            return await _resume_text(db, user)
    except HTTPException as exc:
        # The route's own 404/403, e.g. a job id that is not the caller's.
        return {"error": str(exc.detail)}
    return {"error": f"unknown tool {name!r}"}
