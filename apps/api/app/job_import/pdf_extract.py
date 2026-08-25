"""Deterministic extraction of job fields from PDF text (Prompt 4.1b).

PURE: text in, draft out. No network, no database, no LLM, no model
inference — so every rule here is testable without fixtures, the same
split app/skill_matching.py has from app/skill_extraction.py.

CONSERVATIVE BY DESIGN. This reads only fields the document EXPLICITLY
LABELS — "Company: Acme", "Job Title: Backend Engineer". It never infers
a company from the first line, from a heading, or from anything that
merely looks like a name.

That restraint is the point. Anything extracted here is shown to the
user as a pre-filled form field, so a wrong guess is worse than a blank
box: a blank box asks to be filled, while a plausible-looking wrong
value invites being accepted at a glance. And Prompt 4.2 will read the
saved description for skills, so a fabricated field would propagate.
Leaving a field None means "we could not determine this", never "we
guessed something".

The same reasoning as app/skill_matching.py's list-context guard, which
accepts losing real matches rather than inventing false ones.
"""

import re
from dataclasses import dataclass, field

# Labels a posting uses to state a field outright. Matched only at the
# start of a line and only when followed by a colon, so prose that
# happens to contain the word ("our company culture") never matches.
_COMPANY_LABELS = ("company", "employer", "organisation", "organization", "hiring organization")
_TITLE_LABELS = ("job title", "position title", "title", "position", "role")
_LOCATION_LABELS = ("location", "job location", "based in", "office", "work location")
_EMPLOYMENT_LABELS = ("employment type", "job type", "contract type", "employment")

# Recognised employment phrasings, mapped onto the closed vocabulary in
# app.schemas.saved_job.EmploymentType. Anything unrecognised yields
# None and the user picks from the dropdown.
_EMPLOYMENT_PHRASES: dict[str, str] = {
    "full time": "full_time",
    "fulltime": "full_time",
    "permanent": "full_time",
    "part time": "part_time",
    "parttime": "part_time",
    "contract": "contract",
    "contractor": "contract",
    "freelance": "contract",
    "internship": "internship",
    "intern": "internship",
    "temporary": "temporary",
    "temp": "temporary",
    "seasonal": "temporary",
}

# A label's value has to be short to be a real field. A 300-character
# "Location:" line is a paragraph that happens to start with the word,
# not a location.
_MAX_FIELD_LENGTH = 120
_MIN_DESCRIPTION_CHARS = 40


@dataclass
class JobDraft:
    """Extracted, UNVERIFIED job fields.

    Every field except `description` may be None — a null means "we could
    not determine this", which the UI renders as an empty box for the
    user to fill. It never means a guess.
    """

    description: str
    company: str | None = None
    title: str | None = None
    location: str | None = None
    employment_type: str | None = None
    source_url: str | None = None
    # Plain-language prompts for the review step.
    notes: list[str] = field(default_factory=list)


def _labelled_value(line: str, labels: tuple[str, ...]) -> str | None:
    """The value of `Label: value`, when the line really is one.

    Anchored at the start of the line and requiring the colon, so
    "Company: Acme" matches while "We are the best company: truly" does
    not — the label must be what the line is ABOUT, not a word in it.
    """
    match = re.match(r"^\s*([A-Za-z][A-Za-z /]{0,30}?)\s*[:\-–]\s*(.+?)\s*$", line)
    if not match:
        return None
    label = match.group(1).strip().casefold()
    if label not in labels:
        return None
    value = match.group(2).strip()
    if not value or len(value) > _MAX_FIELD_LENGTH:
        return None
    return value


def _employment_type_from(value: str) -> str | None:
    """Map a stated employment phrase onto the closed vocabulary.

    Normalizes separators so "Full-Time", "full_time" and "FULL TIME"
    all resolve. Only the WHOLE value is considered: a sentence
    mentioning "contract" in passing is not a declaration of the
    employment type.
    """
    normalized = re.sub(r"[\s_\-]+", " ", value.strip().casefold())
    return _EMPLOYMENT_PHRASES.get(normalized)


def extract_job_draft_from_text(text: str) -> JobDraft | None:
    """Build a draft from extracted PDF text, or None when there is no
    usable description.

    None is a legitimate outcome — a scanned page with no text layer, for
    instance. The caller turns it into a clean failure and the user
    pastes manually, rather than being handed an empty draft.
    """
    cleaned = text.strip()
    if len(cleaned) < _MIN_DESCRIPTION_CHARS:
        return None

    draft = JobDraft(description=cleaned)

    # Only the first lines are scanned for labels. A posting states its
    # metadata up top; a "Location:" appearing 200 lines down is far more
    # likely to be inside a benefits table or an office list.
    for line in cleaned.splitlines()[:40]:
        if draft.company is None:
            draft.company = _labelled_value(line, _COMPANY_LABELS)
        if draft.title is None:
            draft.title = _labelled_value(line, _TITLE_LABELS)
        if draft.location is None:
            draft.location = _labelled_value(line, _LOCATION_LABELS)
        if draft.employment_type is None:
            stated = _labelled_value(line, _EMPLOYMENT_LABELS)
            if stated:
                draft.employment_type = _employment_type_from(stated)

    missing = [
        label
        for label, value in (
            ("company", draft.company),
            ("job title", draft.title),
            ("location", draft.location),
        )
        if not value
    ]
    if missing:
        # Name exactly what is missing rather than a generic "check the
        # details" — the user should not have to hunt for the gap.
        draft.notes.append(
            "We read the description from your PDF. Please add the " + ", ".join(missing) + "."
        )
    else:
        draft.notes.append("We read these details from your PDF — please check them.")
    return draft
