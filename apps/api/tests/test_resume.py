"""Tests for resume upload, list, fetch-metadata, and delete (Prompt
2.1): validation (unsupported type, oversized, empty, unsafe filename),
successful PDF/DOCX upload, that responses never expose a storage path,
list/fetch/delete behavior, and ownership enforcement (a user can never
read or delete another user's resume)."""

import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.db import get_db
from app.main import app
from app.rate_limit import _request_log
from app.settings import get_settings
from app.storage import get_resume_storage
from app.storage.local import LocalResumeStorage
from tests.conftest import isolated_schema_override

_EMAIL_A = "alice@example.com"
_EMAIL_B = "bob@example.com"
_PASSWORD = "correct-horse-battery"

_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Minimal-but-real file signatures: a genuine PDF starts with "%PDF-"; a
# genuine DOCX (OOXML) file is a ZIP archive and starts with the ZIP
# local-file-header signature. Neither needs to be a fully valid,
# parseable document for Prompt 2.1 — nothing here reads past the first
# few bytes (text extraction is a later prompt).
_PDF_BYTES = b"%PDF-1.4\n%fake but signature-valid test content\n"
_DOCX_BYTES = b"PK\x03\x04" + b"fake but signature-valid docx bytes"

_EXPECTED_RESPONSE_KEYS = {
    "id",
    "user_id",
    "original_filename",
    "content_type",
    "file_size_bytes",
    "status",
    "error_message",
    "created_at",
    "updated_at",
}


@pytest.fixture(autouse=True)
def _use_isolated_schema() -> Generator[None, None, None]:
    app.dependency_overrides[get_db] = isolated_schema_override()
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _use_temp_storage(tmp_path: Path) -> Generator[None, None, None]:
    # Test uploads must never land in the real RESUME_STORAGE_DIR — a
    # fresh, auto-cleaned pytest tmp_path instead, the same way the DB
    # tests use an isolated schema rather than the dev database.
    app.dependency_overrides[get_resume_storage] = lambda: LocalResumeStorage(tmp_path)
    yield
    app.dependency_overrides.pop(get_resume_storage, None)


@pytest.fixture(autouse=True)
def _stub_enqueue_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests are about upload/list/get/delete, not extraction (see
    # tests/test_extraction.py for that) — stubbed so they never touch a
    # real Celery/Redis broker, the same way DB/storage are swapped for
    # isolated fakes above rather than the real dev instances.
    monkeypatch.setattr("app.api.v1.resume.enqueue_extraction", lambda resume_id: None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    # Every test registers/logs in at least one user via the shared
    # register/login/refresh rate-limit budget (app/rate_limit.py) —
    # without resetting between tests, earlier tests' requests would
    # count against later ones and eventually 429. Same fixture as
    # tests/test_auth.py and tests/test_profile.py.
    _request_log.clear()


def _register_and_login(
    client: TestClient, email: str, password: str = _PASSWORD
) -> tuple[str, str]:
    """Registers and logs in a fresh user. Returns (access_token, user_id)."""
    register = client.post("/api/v1/auth/register", json={"email": email, "password": password})
    assert register.status_code == 201
    user_id = register.json()["id"]

    login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"], user_id


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(
    client: TestClient,
    token: str,
    *,
    filename: str = "resume.pdf",
    content: bytes = _PDF_BYTES,
    content_type: str = "application/pdf",
) -> Response:
    return client.post(
        "/api/v1/resumes",
        headers=_auth_headers(token),
        files={"file": (filename, content, content_type)},
    )


def _list(client: TestClient, token: str) -> Response:
    return client.get("/api/v1/resumes", headers=_auth_headers(token))


def _get(client: TestClient, token: str, resume_id: object) -> Response:
    return client.get(f"/api/v1/resumes/{resume_id}", headers=_auth_headers(token))


def _delete(client: TestClient, token: str, resume_id: object) -> Response:
    return client.delete(f"/api/v1/resumes/{resume_id}", headers=_auth_headers(token))


def test_upload_requires_authentication(client: TestClient) -> None:
    response = client.post(
        "/api/v1/resumes", files={"file": ("resume.pdf", _PDF_BYTES, "application/pdf")}
    )
    assert response.status_code == 401


def test_list_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/resumes").status_code == 401


def test_get_requires_authentication(client: TestClient) -> None:
    assert client.get(f"/api/v1/resumes/{uuid.uuid4()}").status_code == 401


def test_delete_requires_authentication(client: TestClient) -> None:
    assert client.delete(f"/api/v1/resumes/{uuid.uuid4()}").status_code == 401


def test_upload_pdf_succeeds_and_response_never_includes_a_storage_path(
    client: TestClient,
) -> None:
    token, user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(client, token, filename="My Resume.pdf")

    assert response.status_code == 201
    body = response.json()
    assert set(body.keys()) == _EXPECTED_RESPONSE_KEYS  # exactly this — no storage_key, no path
    assert body["original_filename"] == "My Resume.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["file_size_bytes"] == len(_PDF_BYTES)
    assert body["status"] == "queued"
    assert body["error_message"] is None
    assert body["user_id"] == user_id


def test_upload_docx_succeeds(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(
        client,
        token,
        filename="resume.docx",
        content=_DOCX_BYTES,
        content_type=_DOCX_CONTENT_TYPE,
    )

    assert response.status_code == 201
    assert response.json()["content_type"] == _DOCX_CONTENT_TYPE


def test_upload_rejects_an_unsupported_extension(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(
        client, token, filename="resume.txt", content=b"just text", content_type="text/plain"
    )

    assert response.status_code == 422


def test_upload_rejects_content_that_does_not_match_its_declared_type(client: TestClient) -> None:
    """Filename and Content-Type both claim a PDF, but the actual bytes
    aren't one — exactly what "evil.exe renamed to resume.pdf" looks
    like. Only the magic-byte check catches this; extension and
    Content-Type alone would both wave it through."""
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(client, token, filename="resume.pdf", content=b"MZ\x90\x00 not a real pdf")

    assert response.status_code == 422


def test_upload_rejects_extension_content_type_mismatch(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(
        client, token, filename="resume.pdf", content=_DOCX_BYTES, content_type=_DOCX_CONTENT_TYPE
    )

    assert response.status_code == 422


def test_upload_rejects_an_empty_file(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(client, token, content=b"")

    assert response.status_code == 422


def test_upload_rejects_a_file_over_the_configured_size_limit(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)
    oversized = _PDF_BYTES + b"0" * get_settings().resume_max_size_bytes

    response = _upload(client, token, content=oversized)

    assert response.status_code == 422


def test_upload_rejects_a_filename_with_a_path_separator(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(client, token, filename="../../etc/passwd.pdf")

    assert response.status_code == 422


def test_rejected_upload_leaves_no_database_row_and_no_file(
    client: TestClient, tmp_path: Path
) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _upload(
        client,
        token,
        filename="resume.exe",
        content=b"MZ fake exe",
        content_type="application/octet-stream",
    )

    assert response.status_code == 422
    assert _list(client, token).json() == []
    assert list(tmp_path.rglob("*")) == []


def test_list_returns_only_the_callers_own_resumes_newest_first(client: TestClient) -> None:
    token_a, _user_a = _register_and_login(client, _EMAIL_A)
    token_b, _user_b = _register_and_login(client, _EMAIL_B)
    _upload(client, token_b, filename="bobs-resume.pdf")
    first = _upload(client, token_a, filename="first.pdf").json()
    second = _upload(client, token_a, filename="second.pdf").json()

    response = _list(client, token_a)

    assert response.status_code == 200
    ids = [r["id"] for r in response.json()]
    assert ids == [second["id"], first["id"]]  # newest first, and Bob's is absent


def test_get_resume_by_id_returns_its_metadata(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)
    created = _upload(client, token, filename="resume.pdf").json()

    response = _get(client, token, created["id"])

    assert response.status_code == 200
    assert response.json() == created


def test_get_nonexistent_resume_returns_404(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _get(client, token, uuid.uuid4())

    assert response.status_code == 404


def test_delete_nonexistent_resume_returns_404(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)

    response = _delete(client, token, uuid.uuid4())

    assert response.status_code == 404


def test_delete_removes_the_database_row_and_the_stored_file(
    client: TestClient, tmp_path: Path
) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)
    created = _upload(client, token, filename="resume.pdf").json()
    assert list(tmp_path.rglob("*.pdf")), (
        "expected the uploaded file to exist on disk before deleting"
    )

    response = _delete(client, token, created["id"])

    assert response.status_code == 204
    assert _get(client, token, created["id"]).status_code == 404
    assert list(tmp_path.rglob("*.pdf")) == []


def test_deleting_an_already_deleted_resume_returns_404(client: TestClient) -> None:
    token, _user_id = _register_and_login(client, _EMAIL_A)
    created = _upload(client, token, filename="resume.pdf").json()
    assert _delete(client, token, created["id"]).status_code == 204

    response = _delete(client, token, created["id"])

    assert response.status_code == 404


def test_cannot_get_another_users_resume(client: TestClient) -> None:
    token_a, _user_a = _register_and_login(client, _EMAIL_A)
    token_b, _user_b = _register_and_login(client, _EMAIL_B)
    resume_a = _upload(client, token_a, filename="alices-secret-resume.pdf").json()

    response = _get(client, token_b, resume_a["id"])

    assert response.status_code == 403
    assert "alice" not in response.text.lower()


def test_cannot_delete_another_users_resume(client: TestClient) -> None:
    token_a, _user_a = _register_and_login(client, _EMAIL_A)
    token_b, _user_b = _register_and_login(client, _EMAIL_B)
    resume_a = _upload(client, token_a, filename="alices-secret-resume.pdf").json()

    response = _delete(client, token_b, resume_a["id"])

    assert response.status_code == 403
    # Alice's resume must be completely unaffected by Bob's attempt.
    still_there = _get(client, token_a, resume_a["id"])
    assert still_there.status_code == 200
    assert still_there.json()["original_filename"] == "alices-secret-resume.pdf"
