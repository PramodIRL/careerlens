from app.storage.base import ResumeStorage
from app.storage.local import LocalResumeStorage, get_resume_storage

__all__ = ["LocalResumeStorage", "ResumeStorage", "get_resume_storage"]
