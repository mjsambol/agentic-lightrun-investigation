"""Per-investigation repository scope and HTTP budget, shared by all tools."""

from contextvars import ContextVar
from dataclasses import dataclass, field
import os


class RepositoryBudgetExceeded(RuntimeError):
    pass


@dataclass
class RepositorySession:
    owner: str
    repo: str
    request_limit: int = 10
    ttl_seconds: int = 86400
    requests_used: int = 0
    resolved_refs: dict = field(default_factory=dict)
    observed_commits: set[str] = field(default_factory=set)

    @classmethod
    def from_environment(cls, owner: str, repo: str):
        limit = int(os.getenv("REPO_GITHUB_REQUEST_LIMIT", "10"))
        ttl = int(os.getenv("REPO_CACHE_TTL_SECONDS", "86400"))
        if limit < 0 or ttl <= 0:
            raise ValueError("Repository request limit must be nonnegative and TTL must be positive")
        return cls(owner, repo, limit, ttl)

    def budget(self) -> dict:
        return {"requests_used": self.requests_used,
                "request_limit": self.request_limit,
                "requests_remaining": self.request_limit - self.requests_used}


repository_session: ContextVar[RepositorySession | None] = ContextVar(
    "repository_session", default=None
)


def check_repository(owner: str, repo: str) -> None:
    session = repository_session.get()
    if session and (owner, repo) != (session.owner, session.repo):
        raise ValueError("Repository is outside this investigation's scope")


def charge_github_request() -> None:
    session = repository_session.get()
    if session is None:
        return
    # No await between checking and reserving: parallel async tools share this
    # object and cannot exceed the limit. Failed requests also consume budget.
    if session.requests_used >= session.request_limit:
        raise RepositoryBudgetExceeded(
            "GitHub request budget exhausted. Use local evidence or explain "
            "what is still unknown; this does not mean the code is absent."
        )
    session.requests_used += 1


def cache_ttl_seconds() -> int:
    session = repository_session.get()
    return session.ttl_seconds if session else int(
        os.getenv("REPO_CACHE_TTL_SECONDS", "86400")
    )
