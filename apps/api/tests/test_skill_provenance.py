"""Unit tests for evidence provenance labels (app/skill_provenance.py,
Prompt 3.4).

`label_for` is pure, so every labelling rule is testable here without a
database — the same separation tests/test_skill_matching.py has from
tests/test_skill_extraction.py. The user-scoped loader is covered
end-to-end in tests/test_skill_profile_api.py instead, where a real
resume row exists to resolve against.
"""

import uuid

from app.skill_provenance import label_for


def test_resume_resolves_to_its_filename() -> None:
    resume_id = str(uuid.uuid4())
    assert label_for("resume", resume_id, {resume_id: "cv.pdf"}) == "cv.pdf"


def test_github_uses_the_identifier_unchanged() -> None:
    """It is already "owner/repo", which is the readable form."""
    assert label_for("github", "ada/scheduler", {}) == "ada/scheduler"


def test_manual_has_no_label() -> None:
    """The identifier is the user's own id — echoing it back says
    nothing the source type has not already said."""
    assert label_for("manual", str(uuid.uuid4()), {}) is None


def test_an_unresolvable_resume_is_none_not_an_error() -> None:
    """A resume deleted after its evidence was written. The dangling
    polymorphic reference is expected by design (docs/decisions.md), so
    this must degrade to None rather than raise."""
    assert label_for("resume", str(uuid.uuid4()), {}) is None


def test_a_malformed_resume_identifier_does_not_raise() -> None:
    """Keys are compared as strings, never coerced to UUID, so garbage
    simply fails to match."""
    assert label_for("resume", "not-a-uuid", {"x": "cv.pdf"}) is None


def test_an_empty_github_identifier_is_none_rather_than_blank() -> None:
    """An empty string would render as a stray separator in the UI."""
    assert label_for("github", "", {}) is None


def test_an_unknown_source_type_is_none() -> None:
    """A source type added later without a labelling rule must degrade
    quietly, not crash the whole response."""
    assert label_for("linkedin", "whatever", {}) is None


def test_resume_labels_never_leak_across_source_types() -> None:
    """A GitHub identifier that happens to collide with a resume id key
    must still use the GitHub rule."""
    collide = str(uuid.uuid4())
    assert label_for("github", collide, {collide: "cv.pdf"}) == collide
