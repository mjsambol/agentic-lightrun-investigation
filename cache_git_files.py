import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from time import time
from typing import Any
from urllib.parse import quote

import httpx
from langchain.tools import tool
from repository_context import (
    cache_ttl_seconds, charge_github_request, check_repository, repository_session,
)

logger = logging.getLogger(__name__)

GITHUB_RATE_LIMIT_RETRY_PATTERN = re.compile(
    r"GitHub API rate limit exceeded\.\s*Retry after (\d+(?:\.\d+)?)s\.",
    re.IGNORECASE,
)
FULL_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

# ---------------------------------------------------------------------------
# Fixed security boundary
# ---------------------------------------------------------------------------

SOURCE_CACHE_ROOT = Path(os.getenv("REPO_CACHE_ROOT", ".agent-source-cache")).resolve()

GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

MAX_SOURCE_SIZE = 10 * 1024 * 1024  # 10 MiB
MAX_LINES_PER_READ = 1000
DEFAULT_INITIAL_READ_LINES = 400

# Sessions pin refs for one investigation, including parallel file reads.
# The fallback dictionary is used only by standalone callers/tests.
_resolved_refs: dict[tuple[str, str, str], str] = {}
_ref_resolution_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Internal helpers — not exposed to the agent
# ---------------------------------------------------------------------------

class GitHubAPIError(RuntimeError):
    """A GitHub API response that preserves its HTTP status for callers."""

    def __init__(self, status_code: int, detail: str, *, endpoint: str = "",
                 request_id: str = "") -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        self.request_id = request_id
        location = f" for GET {endpoint}" if endpoint else ""
        super().__init__(f"GitHub API returned HTTP {status_code}{location}: {detail}")


class RepositoryFileNotFoundError(FileNotFoundError):
    """The requested repository path does not exist at the resolved commit."""

    def __init__(
        self,
        owner: str,
        repo: str,
        repository_path: str,
        commit_sha: str,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.repository_path = repository_path
        self.commit_sha = commit_sha
        super().__init__(
            f"{owner}/{repo}:{repository_path} was not accessible at commit "
            f"{commit_sha} (HTTP 404: missing path or insufficient repository access)"
        )


def github_headers() -> dict[str, str]:
    token = os.environ["GITHUB_MCP_PAT"]

    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "lightrun-source-cache-agent",
    }


def normalize_repository_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")

    if not normalized:
        raise ValueError("Repository path must not be empty")

    if ".." in Path(normalized).parts:
        raise ValueError("Repository path may not contain '..'")

    return normalized


def safe_cache_directory(owner: str, repo: str, commit_sha: str) -> Path:
    if (
        len(commit_sha) != 40
        or any(character not in "0123456789abcdef" for character in commit_sha)
    ):
        raise ValueError(f"Invalid Git commit SHA: {commit_sha}")

    directory = (
        SOURCE_CACHE_ROOT
        / owner
        / repo
        / commit_sha
    ).resolve()

    try:
        directory.relative_to(SOURCE_CACHE_ROOT)
    except ValueError as exc:
        raise ValueError("Cache directory escapes the cache root") from exc

    return directory


def safe_cached_file(owner: str, repo: str, commit_sha: str, repository_path: str) -> Path:
    normalized = normalize_repository_path(repository_path)
    destination = (safe_cache_directory(owner, repo, commit_sha) / normalized).resolve()

    try:
        destination.relative_to(SOURCE_CACHE_ROOT)
    except ValueError as exc:
        raise ValueError("Cache file escapes the cache root") from exc

    return destination


def calculate_git_blob_sha(content: bytes) -> str:
    """
    Reproduce Git's SHA-1 calculation for a blob:

        SHA1(b"blob " + length + b"\\0" + content)
    """
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


async def github_get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """Count and log requests without exposing credentials or response bodies."""
    charge_github_request()
    endpoint = httpx.URL(url).path
    logger.info("GitHub GET %s", endpoint)
    try:
        response = await client.get(url, **kwargs)
    except httpx.HTTPError:
        logger.warning("GitHub GET %s failed before an HTTP response", endpoint)
        raise
    request_id = response.headers.get("x-github-request-id", "")
    log = logger.warning if response.is_error else logger.info
    log("GitHub GET %s -> HTTP %s (request_id=%s)",
        endpoint, response.status_code, request_id or "unavailable")
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise GitHubAPIError(
            response.status_code, response.text[:1000],
            endpoint=endpoint, request_id=request_id,
        ) from exc
    return response


async def github_get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = await github_get(client, url, params=params)

    payload = response.json()

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an unexpected JSON response")

    return payload


async def resolve_ref_to_commit(client: httpx.AsyncClient, owner: str, repo: str, ref: str) -> str:
    """
    Resolve a branch, tag, abbreviated SHA, or full SHA to an immutable
    40-character commit SHA.
    """
    check_repository(owner, repo)
    session = repository_session.get()
    resolved_refs = session.resolved_refs if session else _resolved_refs
    clean_ref = ref.strip()

    if not clean_ref:
        raise ValueError("Git reference must not be empty")

    if FULL_COMMIT_SHA_PATTERN.fullmatch(clean_ref):
        if session:
            session.observed_commits.add(clean_ref.lower())
        return clean_ref.lower()

    cache_key = (owner, repo, clean_ref)
    cached_commit = resolved_refs.get(cache_key)
    if cached_commit is not None:
        return cached_commit

    async with _ref_resolution_lock:
        cached_commit = resolved_refs.get(cache_key)
        if cached_commit is not None:
            return cached_commit

        encoded_ref = quote(clean_ref, safe="")

        url = (
            f"{GITHUB_API_ROOT}/repos/"
            f"{quote(owner, safe='')}/"
            f"{quote(repo, safe='')}/"
            f"commits/{encoded_ref}"
        )

        payload = await github_get_json(client, url)
        commit_sha = payload.get("sha")

        if not isinstance(commit_sha, str) or len(commit_sha) != 40:
            raise RuntimeError(
                f"GitHub did not return a valid commit SHA for ref {ref!r}"
            )

        resolved_commit = commit_sha.lower()
        resolved_refs[cache_key] = resolved_commit
        if session:
            session.observed_commits.add(resolved_commit)
        return resolved_commit


async def get_file_metadata(client: httpx.AsyncClient, owner: str, repo: str, repository_path: str, commit_sha: str) -> dict[str, Any]:
    """
    Retrieve the tree entry metadata for the file at one exact commit.
    """
    encoded_path = quote(repository_path, safe="/")

    url = (
        f"{GITHUB_API_ROOT}/repos/"
        f"{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}/"
        f"contents/{encoded_path}"
    )

    try:
        payload = await github_get_json(
            client,
            url,
            params={"ref": commit_sha},
        )
    except GitHubAPIError as exc:
        if exc.status_code == 404:
            raise RepositoryFileNotFoundError(
                owner,
                repo,
                repository_path,
                commit_sha,
            ) from exc
        raise

    if payload.get("type") != "file":
        raise ValueError(
            f"Repository path is not a regular file: {repository_path}"
        )

    blob_sha = payload.get("sha")
    size = payload.get("size")

    if not isinstance(blob_sha, str) or len(blob_sha) != 40:
        raise RuntimeError("GitHub did not return a valid blob SHA")

    if not isinstance(size, int) or size < 0:
        raise RuntimeError("GitHub did not return a valid file size")

    if size > MAX_SOURCE_SIZE:
        raise ValueError(
            f"Source file is {size} bytes; limit is {MAX_SOURCE_SIZE}"
        )

    return {
        "blob_sha": blob_sha.lower(),
        "size": size,
    }


async def download_blob(client: httpx.AsyncClient, owner: str, repo: str, blob_sha: str) -> bytes:
    """
    Download the exact bytes of a Git blob.

    This endpoint is used rather than copying the contents-API response
    through the language model.
    """
    url = (
        f"{GITHUB_API_ROOT}/repos/"
        f"{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}/"
        f"git/blobs/{blob_sha}"
    )

    response = await github_get(
        client, url,
        headers={
            **github_headers(),
            "Accept": "application/vnd.github.raw+json",
        },
    )

    return response.content


def count_source_lines(content: bytes) -> int:
    """
    Return the same logical line count used by read_cached_source.

    An empty file has zero lines. A trailing newline does not create an
    additional source line.
    """
    if not content:
        return 0

    return len(content.splitlines())


def manifest_path(
    owner: str,
    repo: str,
    commit_sha: str,
    repository_path: str,
) -> Path:
    """Return a collision-free metadata path for one cached source file."""
    normalized = normalize_repository_path(repository_path)
    path_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    cache_directory = safe_cache_directory(owner, repo, commit_sha)
    manifest_directory = (
        cache_directory.parent / f".{commit_sha}.manifests"
    ).resolve()

    try:
        manifest_directory.relative_to(SOURCE_CACHE_ROOT)
    except ValueError as exc:
        raise ValueError("Cache manifest escapes the cache root") from exc

    return manifest_directory / f"{path_digest}.json"


def load_manifest(
    owner: str,
    repo: str,
    commit_sha: str,
    repository_path: str,
) -> dict[str, Any]:
    normalized = normalize_repository_path(repository_path)
    path = manifest_path(owner, repo, commit_sha, normalized)

    # Read caches created by the earlier single-file implementation when its
    # one manifest happens to describe the requested file.
    if not path.is_file():
        legacy_path = (
            safe_cache_directory(owner, repo, commit_sha) / "manifest.json"
        )
        if legacy_path.is_file():
            legacy_payload = json.loads(legacy_path.read_text(encoding="utf-8"))
            if (
                isinstance(legacy_payload, dict)
                and legacy_payload.get("repository_path") == normalized
            ):
                return legacy_payload

    if not path.is_file():
        raise FileNotFoundError(
            "No cache manifest exists for "
            f"{repository_path} at commit {commit_sha}"
        )

    payload = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(payload, dict):
        raise RuntimeError("Invalid cache manifest")

    return payload


def load_valid_cached_manifest(
    owner: str,
    repo: str,
    commit_sha: str,
    repository_path: str,
) -> dict[str, Any] | None:
    """Return a manifest only when its cached bytes still pass verification."""
    try:
        manifest = load_manifest(
            owner,
            repo,
            commit_sha,
            repository_path,
        )
        source_path = safe_cached_file(
            owner,
            repo,
            commit_sha,
            repository_path,
        )
        content = source_path.read_bytes()
    except (FileNotFoundError, json.JSONDecodeError, OSError, RuntimeError):
        return None

    if (
        manifest.get("owner") != owner
        or manifest.get("repository") != repo
        or manifest.get("commit_sha") != commit_sha
        or manifest.get("repository_path") != repository_path
        or hashlib.sha256(content).hexdigest() != manifest.get("sha256")
        or calculate_git_blob_sha(content) != manifest.get("git_blob_sha")
    ):
        return None

    return manifest


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

@tool
async def cache_github_file(owner: str, repo: str, ref: str, target_file: str) -> dict[str, Any]:
    """
    Fetch the approved source file directly from GitHub and cache its exact
    Git blob bytes locally.

    Use this before selecting a source line for Lightrun instrumentation.

    Args:
        owner: The owner of the git repository
        repo: The name of the git repository
        ref: Git branch, tag, or commit SHA. Prefer the deployed commit SHA
             when known. A branch such as "main" is resolved to an immutable
             commit before the file is downloaded.
        target_file: path to the file which should be retrieved from git and cached locally
    """
    repository_path = normalize_repository_path(target_file)

    timeout = httpx.Timeout(
        connect=10.0,
        read=30.0,
        write=10.0,
        pool=10.0,
    )

    async with httpx.AsyncClient(
        headers=github_headers(),
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        commit_sha = await resolve_ref_to_commit(client, owner, repo, ref)

        cached_manifest = load_valid_cached_manifest(
            owner,
            repo,
            commit_sha,
            repository_path,
        )
        if cached_manifest is not None and (
            time() - cached_manifest.get("validated_at", 0) < cache_ttl_seconds()
        ):
            return {**cached_manifest, "cache_hit": True}

        metadata = await get_file_metadata(client, owner, repo, repository_path, commit_sha)

        expected_blob_sha = metadata["blob_sha"]
        expected_size = metadata["size"]

        if cached_manifest is not None and (
            cached_manifest["git_blob_sha"] == expected_blob_sha
            and cached_manifest["size_bytes"] == expected_size
        ):
            cached_manifest["validated_at"] = time()
            manifest_path(owner, repo, commit_sha, repository_path).write_text(
                json.dumps(cached_manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            return {**cached_manifest, "cache_hit": True}

        content = await download_blob(client, owner, repo, expected_blob_sha)

    if len(content) != expected_size:
        raise RuntimeError(
            "Downloaded source size does not match GitHub metadata: "
            f"expected {expected_size}, received {len(content)}"
        )

    actual_blob_sha = calculate_git_blob_sha(content)

    if actual_blob_sha != expected_blob_sha:
        raise RuntimeError(
            "Downloaded source failed Git blob verification: "
            f"expected {expected_blob_sha}, calculated {actual_blob_sha}"
        )

    # Reject binary files and source that cannot be read deterministically.
    if b"\x00" in content:
        raise ValueError("Target file appears to be binary")

    try:
        content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "Target source file is not valid UTF-8"
        ) from exc

    destination = safe_cached_file(owner, repo, commit_sha, repository_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Write bytes, not text, so no newline or encoding conversion occurs.
    temporary_path = destination.with_suffix(
        destination.suffix + ".tmp"
    )
    temporary_path.write_bytes(content)
    temporary_path.replace(destination)

    sha256 = hashlib.sha256(content).hexdigest()

    manifest = {
        "owner": owner,
        "repository": repo,
        "requested_ref": ref,
        "commit_sha": commit_sha,
        "repository_path": repository_path,
        "git_blob_sha": expected_blob_sha,
        "sha256": sha256,
        "size_bytes": len(content),
        "line_count": count_source_lines(content),
        "local_path": str(destination),
        "cache_hit": False,
        "validated_at": time(),
    }

    manifest_file = manifest_path(
        owner,
        repo,
        commit_sha,
        repository_path,
    )
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    return manifest


@tool
async def cache_and_read_github_file(
    owner: str,
    repo: str,
    ref: str,
    target_file: str,
    start_line: int = 1,
    end_line: int = DEFAULT_INITIAL_READ_LINES,
) -> dict[str, Any]:
    """Cache a GitHub source file and return authoritative numbered source.

    Use this as soon as repository search identifies a likely file. It combines
    immutable revision resolution, verified downloading, local caching, and the
    first source read in one operation. Calls for multiple candidate files may
    be made in parallel. The default returns the first 400 lines; when search
    results identify a relevant area, request a focused 200-400 line window.

    Args:
        owner: The owner of the git repository.
        repo: The name of the git repository.
        ref: Git branch, tag, or commit SHA.
        target_file: Repository-relative path to cache and read.
        start_line: First one-based source line to return. Defaults to 1.
        end_line: Last one-based source line to return. Defaults to 400.
    """
    try:
        manifest = await cache_github_file.ainvoke(
            {
                "owner": owner,
                "repo": repo,
                "ref": ref,
                "target_file": target_file,
            }
        )
    except RepositoryFileNotFoundError as exc:
        # A search result can contain a stale, renamed, or merely inferred path.
        # Return that expected miss to the model so it can try another candidate;
        # all non-404 API and transport failures still raise.
        return {
            "status": "not_found",
            "commit_sha": exc.commit_sha,
            "repository_path": exc.repository_path,
            "message": (
                f"The file {exc.repository_path!r} was not accessible in "
                f"{exc.owner}/{exc.repo} at commit {exc.commit_sha} (HTTP 404). "
                "This can mean a missing path or insufficient repository access. "
                "Search the repository again and retry with an exact path."
            ),
        }
    source = read_cached_source.invoke(
        {
            "owner": owner,
            "repo": repo,
            "target_file": manifest["repository_path"],
            "commit_sha": manifest["commit_sha"],
            "start_line": start_line,
            "end_line": end_line,
        }
    )

    return {
        "commit_sha": manifest["commit_sha"],
        "repository_path": manifest["repository_path"],
        "line_count": manifest["line_count"],
        "cache_hit": manifest["cache_hit"],
        "start_line": start_line if start_line <= manifest["line_count"] else None,
        "end_line": min(end_line, manifest["line_count"]) if start_line <= manifest["line_count"] else None,
        "source": source,
    }


@tool
def read_cached_source(owner: str, repo: str, target_file: str, commit_sha: str, start_line: int, end_line: int) -> str:
    """
    Read authoritative numbered lines from the previously cached source.

    The displayed numbers are the line numbers that must be used for
    Lightrun instrumentation.

    Args:
        owner: The owner of the git repository
        repo: The name of the git repository
        target_file: Repository-relative path returned by
                     cache_and_read_github_file.
        commit_sha: Exact 40-character commit SHA returned by
                    cache_and_read_github_file.
        start_line: First one-based source line to return.
        end_line: Last one-based source line to return, inclusive.
    """
    if start_line < 1:
        raise ValueError("start_line must be at least 1")

    if end_line < start_line:
        raise ValueError(
            "end_line must be greater than or equal to start_line"
        )

    requested_count = end_line - start_line + 1

    if requested_count > MAX_LINES_PER_READ:
        raise ValueError(
            f"At most {MAX_LINES_PER_READ} lines may be read at once"
        )

    repository_path = normalize_repository_path(target_file)
    manifest = load_manifest(owner, repo, commit_sha, repository_path)

    if manifest.get("repository_path") != repository_path:
        raise RuntimeError("Cache manifest refers to an unexpected file")

    source_path = safe_cached_file(owner, repo, commit_sha, repository_path)

    if not source_path.is_file():
        raise FileNotFoundError(
            f"Cached source does not exist for commit {commit_sha}"
        )

    content = source_path.read_bytes()

    actual_sha256 = hashlib.sha256(content).hexdigest()
    expected_sha256 = manifest.get("sha256")

    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "Cached source integrity check failed; cache contents changed"
        )

    actual_blob_sha = calculate_git_blob_sha(content)
    expected_blob_sha = manifest.get("git_blob_sha")

    if actual_blob_sha != expected_blob_sha:
        raise RuntimeError(
            "Cached source no longer matches the verified Git blob"
        )

    text = content.decode("utf-8", errors="strict")
    lines = text.splitlines()

    if start_line > len(lines):
        return (
            f"The cached file contains {len(lines)} lines; "
            f"requested start line was {start_line}."
        )

    actual_end = min(end_line, len(lines))

    header = (
        f"repository={owner}/{repo}\n"
        f"commit={commit_sha}\n"
        f"path={repository_path}\n"
        f"blob_sha={expected_blob_sha}\n"
        f"sha256={expected_sha256}\n"
        f"lines={start_line}-{actual_end}\n"
        "---"
    )

    numbered_lines = "\n".join(
        f"{line_number:6d} | {lines[line_number - 1]}"
        for line_number in range(start_line, actual_end + 1)
    )

    return f"{header}\n{numbered_lines}"


@tool
def get_cached_source_metadata(owner: str, repo: str, target_file: str, commit_sha: str) -> dict[str, Any]:
    """
    Return verified metadata for an already cached source revision.

    Args:
        owner: The owner of the git repository
        repo: The name of the git repository
        target_file: Repository-relative path returned by cache_github_file.
        commit_sha: Exact commit SHA returned by cache_github_file.
    """
    repository_path = normalize_repository_path(target_file)
    manifest = load_manifest(owner, repo, commit_sha, repository_path)

    if manifest.get("repository_path") != repository_path:
        raise RuntimeError("Cache manifest refers to an unexpected file")

    source_path = safe_cached_file(owner, repo, commit_sha, repository_path)
    content = source_path.read_bytes()

    current_sha256 = hashlib.sha256(content).hexdigest()
    current_blob_sha = calculate_git_blob_sha(content)

    return {
        **manifest,
        "cache_integrity_valid": (
            current_sha256 == manifest.get("sha256")
            and current_blob_sha == manifest.get("git_blob_sha")
        ),
    }


def _github_rate_limit_retry_seconds(error: BaseException) -> float | None:
    """Extract GitHub's requested delay from an exception or exception group."""
    pending: list[BaseException] = [error]
    seen: set[int] = set()

    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        match = GITHUB_RATE_LIMIT_RETRY_PATTERN.search(str(current))
        if match:
            return float(match.group(1))

        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)

    return None
