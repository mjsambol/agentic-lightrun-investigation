"""Read-only GitHub access diagnostics using the deployed agent's PAT."""
import argparse
import asyncio
import logging
import os
from urllib.parse import quote

from dotenv import load_dotenv
import httpx

from cache_git_files import GITHUB_API_ROOT, GitHubAPIError, github_get_json, github_headers
from logging_utils import configure_logging

logger = logging.getLogger(__name__)


async def diagnose(owner: str, repo: str, ref: str) -> int:
    if not os.getenv("GITHUB_MCP_PAT"):
        logger.error("GITHUB_MCP_PAT is missing or empty in this process")
        return 2
    base = f"{GITHUB_API_ROOT}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
    stage = "repository visibility"
    async with httpx.AsyncClient(headers=github_headers(), timeout=30, follow_redirects=False) as client:
        try:
            metadata = await github_get_json(client, base)
            logger.info("Repository is accessible; default branch=%s; configured ref=%s",
                        metadata.get("default_branch"), ref)
            stage = "configured ref resolution"
            commit = await github_get_json(client, f"{base}/commits/{quote(ref, safe='')}")
            sha = commit["sha"]
            stage = "repository tree discovery"
            try:
                await github_get_json(client, f"{base}/git/trees/{sha}", params={"recursive": "1"})
            except GitHubAPIError as error:
                tree_sha = commit.get("commit", {}).get("tree", {}).get("sha")
                if error.status_code == 404 and tree_sha and tree_sha != sha:
                    await github_get_json(client, f"{base}/git/trees/{tree_sha}", params={"recursive": "1"})
                    logger.error("Tree lookup succeeds with the root tree SHA but fails with the commit SHA; "
                                 "report this as a tree-discovery implementation issue")
                    return 1
                raise
        except GitHubAPIError as error:
            # Do not print headers, token, or response bodies.
            logger.error("Failed during %s: HTTP %s at %s (request_id=%s)",
                         stage, error.status_code, error.endpoint, error.request_id or "unavailable")
            if stage == "repository visibility":
                logger.error("Check owner/repo, PAT repository selection and Contents read permission, "
                             "token expiry, and organization approval/SSO authorization. "
                             "GitHub can hide private-repository access failures behind HTTP 404.")
            elif stage == "configured ref resolution":
                logger.error("Repository visibility succeeded; check that configured ref %r exists", ref)
            return 1
        except httpx.HTTPError:
            logger.error("Transport failure during %s; check container connectivity to api.github.com", stage)
            return 1
    logger.info("GitHub repository, ref, and tree checks passed using GITHUB_MCP_PAT. "
                "No source files or credentials were printed.")
    return 0


if __name__ == "__main__":
    load_dotenv()
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default="lightrun-platform")
    parser.add_argument("--repo", default="se-repo")
    parser.add_argument("--ref", default="main")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(diagnose(args.owner, args.repo, args.ref)))
