import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx


os.environ.setdefault("GITHUB_MCP_PAT", "test-token")

import cache_git_files as cache  # noqa: E402


OWNER = "example-owner"
REPOSITORY = "example-repository"
COMMIT_SHA = "a" * 40


class MultiFileCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        cache._resolved_refs.clear()

    async def test_caches_and_reads_two_files_from_the_same_commit(self) -> None:
        contents = {
            "src/alpha.py": b"alpha = 1\nalpha += 1\n",
            "src/beta.py": b"beta = 2\nbeta += 2\n",
        }
        blobs = {
            path: cache.calculate_git_blob_sha(content)
            for path, content in contents.items()
        }
        content_by_blob = {
            blobs[path]: content for path, content in contents.items()
        }

        async def get_metadata(client, owner, repo, repository_path, commit_sha):
            content = contents[repository_path]
            return {
                "blob_sha": blobs[repository_path],
                "size": len(content),
            }

        async def download(client, owner, repo, blob_sha):
            return content_by_blob[blob_sha]

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory).resolve()
            with (
                patch.object(cache, "SOURCE_CACHE_ROOT", cache_root),
                patch.object(
                    cache,
                    "resolve_ref_to_commit",
                    AsyncMock(return_value=COMMIT_SHA),
                ),
                patch.object(cache, "get_file_metadata", get_metadata),
                patch.object(cache, "download_blob", download),
            ):
                await asyncio.gather(
                    cache.cache_github_file.ainvoke(
                        {
                            "owner": OWNER,
                            "repo": REPOSITORY,
                            "ref": "main",
                            "target_file": "/src/alpha.py/",
                        }
                    ),
                    cache.cache_github_file.ainvoke(
                        {
                            "owner": OWNER,
                            "repo": REPOSITORY,
                            "ref": "main",
                            "target_file": "src/beta.py",
                        }
                    ),
                )

                alpha = cache.read_cached_source.invoke(
                    {
                        "owner": OWNER,
                        "repo": REPOSITORY,
                        "target_file": "src\\alpha.py",
                        "commit_sha": COMMIT_SHA,
                        "start_line": 1,
                        "end_line": 2,
                    }
                )
                beta = cache.read_cached_source.invoke(
                    {
                        "owner": OWNER,
                        "repo": REPOSITORY,
                        "target_file": "src/beta.py",
                        "commit_sha": COMMIT_SHA,
                        "start_line": 1,
                        "end_line": 2,
                    }
                )

                self.assertIn("path=src/alpha.py", alpha)
                self.assertIn("     1 | alpha = 1", alpha)
                self.assertIn("path=src/beta.py", beta)
                self.assertIn("     2 | beta += 2", beta)

                alpha_manifest = cache.manifest_path(
                    OWNER,
                    REPOSITORY,
                    COMMIT_SHA,
                    "src/alpha.py",
                )
                beta_manifest = cache.manifest_path(
                    OWNER,
                    REPOSITORY,
                    COMMIT_SHA,
                    "src/beta.py",
                )
                self.assertNotEqual(alpha_manifest, beta_manifest)
                self.assertTrue(alpha_manifest.is_file())
                self.assertTrue(beta_manifest.is_file())

                alpha_metadata = cache.get_cached_source_metadata.invoke(
                    {
                        "owner": OWNER,
                        "repo": REPOSITORY,
                        "target_file": "/src/alpha.py",
                        "commit_sha": COMMIT_SHA,
                    }
                )
                beta_metadata = cache.get_cached_source_metadata.invoke(
                    {
                        "owner": OWNER,
                        "repo": REPOSITORY,
                        "target_file": "src/beta.py",
                        "commit_sha": COMMIT_SHA,
                    }
                )
                self.assertTrue(alpha_metadata["cache_integrity_valid"])
                self.assertTrue(beta_metadata["cache_integrity_valid"])

    async def test_combined_tool_caches_and_returns_numbered_source(self) -> None:
        content = b"first = 1\nsecond = 2\nthird = 3\n"
        blob_sha = cache.calculate_git_blob_sha(content)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(
                    cache,
                    "SOURCE_CACHE_ROOT",
                    Path(temporary_directory).resolve(),
                ),
                patch.object(
                    cache,
                    "resolve_ref_to_commit",
                    AsyncMock(return_value=COMMIT_SHA),
                ),
                patch.object(
                    cache,
                    "get_file_metadata",
                    AsyncMock(
                        return_value={
                            "blob_sha": blob_sha,
                            "size": len(content),
                        }
                    ),
                ),
                patch.object(
                    cache,
                    "download_blob",
                    AsyncMock(return_value=content),
                ),
            ):
                result = await cache.cache_and_read_github_file.ainvoke(
                    {
                        "owner": OWNER,
                        "repo": REPOSITORY,
                        "ref": "main",
                        "target_file": "src/example.py",
                        "start_line": 2,
                        "end_line": 3,
                    }
                )

        self.assertEqual(result["commit_sha"], COMMIT_SHA)
        self.assertEqual(result["line_count"], 3)
        self.assertFalse(result["cache_hit"])
        self.assertIn("     2 | second = 2", result["source"])
        self.assertIn("     3 | third = 3", result["source"])

    async def test_second_cache_call_reuses_verified_file(self) -> None:
        content = b"value = 1\n"
        blob_sha = cache.calculate_git_blob_sha(content)
        get_metadata = AsyncMock(
            return_value={"blob_sha": blob_sha, "size": len(content)}
        )
        download = AsyncMock(return_value=content)

        with tempfile.TemporaryDirectory() as temporary_directory:
            with (
                patch.object(
                    cache,
                    "SOURCE_CACHE_ROOT",
                    Path(temporary_directory).resolve(),
                ),
                patch.object(
                    cache,
                    "resolve_ref_to_commit",
                    AsyncMock(return_value=COMMIT_SHA),
                ),
                patch.object(cache, "get_file_metadata", get_metadata),
                patch.object(cache, "download_blob", download),
            ):
                arguments = {
                    "owner": OWNER,
                    "repo": REPOSITORY,
                    "ref": "main",
                    "target_file": "src/example.py",
                }
                first = await cache.cache_github_file.ainvoke(arguments)
                second = await cache.cache_github_file.ainvoke(arguments)

        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        get_metadata.assert_awaited_once()
        download.assert_awaited_once()

    async def test_full_commit_sha_requires_no_resolution_request(self) -> None:
        client = AsyncMock()

        resolved = await cache.resolve_ref_to_commit(
            client,
            OWNER,
            REPOSITORY,
            COMMIT_SHA.upper(),
        )

        self.assertEqual(resolved, COMMIT_SHA)
        client.get.assert_not_awaited()

    async def test_parallel_calls_resolve_a_branch_only_once(self) -> None:
        client = AsyncMock()
        github_get_json = AsyncMock(return_value={"sha": COMMIT_SHA})

        with patch.object(cache, "github_get_json", github_get_json):
            resolved = await asyncio.gather(
                cache.resolve_ref_to_commit(
                    client, OWNER, REPOSITORY, "main"
                ),
                cache.resolve_ref_to_commit(
                    client, OWNER, REPOSITORY, "main"
                ),
            )

        self.assertEqual(resolved, [COMMIT_SHA, COMMIT_SHA])
        github_get_json.assert_awaited_once()

    async def test_missing_candidate_file_is_recoverable(self) -> None:
        missing_file = "src/missing.py"

        with (
            patch.object(
                cache,
                "resolve_ref_to_commit",
                AsyncMock(return_value=COMMIT_SHA),
            ),
            patch.object(
                cache,
                "get_file_metadata",
                AsyncMock(
                    side_effect=cache.RepositoryFileNotFoundError(
                        OWNER,
                        REPOSITORY,
                        missing_file,
                        COMMIT_SHA,
                    )
                ),
            ),
        ):
            result = await cache.cache_and_read_github_file.ainvoke(
                {
                    "owner": OWNER,
                    "repo": REPOSITORY,
                    "ref": "main",
                    "target_file": missing_file,
                }
            )

        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["commit_sha"], COMMIT_SHA)
        self.assertEqual(result["repository_path"], missing_file)
        self.assertIn("Search the repository again", result["message"])

    async def test_metadata_translates_only_a_404_to_file_not_found(self) -> None:
        request = httpx.Request("GET", "https://api.github.test/file")
        response = httpx.Response(404, request=request, text="not found")
        client = Mock()
        client.get = AsyncMock(return_value=response)

        with self.assertRaises(cache.RepositoryFileNotFoundError) as raised:
            await cache.get_file_metadata(
                client,
                OWNER,
                REPOSITORY,
                "src/missing.py",
                COMMIT_SHA,
            )

        self.assertEqual(raised.exception.commit_sha, COMMIT_SHA)

    async def test_metadata_does_not_mask_non_404_github_failures(self) -> None:
        request = httpx.Request("GET", "https://api.github.test/file")
        response = httpx.Response(403, request=request, text="forbidden")
        client = Mock()
        client.get = AsyncMock(return_value=response)

        with self.assertRaises(cache.GitHubAPIError) as raised:
            await cache.get_file_metadata(
                client,
                OWNER,
                REPOSITORY,
                "src/example.py",
                COMMIT_SHA,
            )

        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
