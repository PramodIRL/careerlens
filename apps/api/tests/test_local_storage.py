"""Unit tests for LocalResumeStorage (app/storage/local.py): save/read/
delete against a real temporary directory, independent of the HTTP API
or the database. Prompt 2.1's HTTP-level tests (tests/test_resume.py)
cover the same storage through the API; these cover the storage
implementation directly and in isolation."""

from pathlib import Path

import pytest

from app.storage.local import LocalResumeStorage


@pytest.fixture
def anyio_backend() -> str:
    # Pin to asyncio, matching tests/test_auth.py — without this,
    # anyio's pytest plugin would also try trio, which this project
    # doesn't use.
    return "asyncio"


@pytest.mark.anyio
async def test_save_then_read_round_trips_the_same_bytes(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)

    await storage.save("alice/resume.pdf", b"%PDF-1.4 fake but representative content")

    assert await storage.read("alice/resume.pdf") == b"%PDF-1.4 fake but representative content"


@pytest.mark.anyio
async def test_save_creates_missing_parent_directories(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)

    await storage.save("nested/does/not/exist/yet/resume.pdf", b"content")

    assert (tmp_path / "nested/does/not/exist/yet/resume.pdf").read_bytes() == b"content"


@pytest.mark.anyio
async def test_delete_removes_the_file(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)
    await storage.save("alice/resume.pdf", b"content")

    await storage.delete("alice/resume.pdf")

    assert not (tmp_path / "alice/resume.pdf").exists()


@pytest.mark.anyio
async def test_delete_of_a_missing_key_is_not_an_error(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)

    await storage.delete("never/existed.pdf")  # must not raise


@pytest.mark.anyio
async def test_read_of_a_missing_key_raises_file_not_found(tmp_path: Path) -> None:
    storage = LocalResumeStorage(tmp_path)

    with pytest.raises(FileNotFoundError):
        await storage.read("never/existed.pdf")
