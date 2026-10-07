import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

os.environ.setdefault("GITHUB_MCP_PAT", "test-token")

import cache_git_files as cache
from repository_context import RepositorySession, repository_session
from repository_tools import create_repository_tools

OWNER, REPO = "example", "demo"
COMMIT = "a" * 40
NEXT_COMMIT = "b" * 40
TRAINING_TREE = "c" * 40
FINANCE_TREE = "d" * 40
SOURCE = "training/finance/java/src/main/java/com/lightrun/exercise/rest/LightrunExerciseServer.java"
FILES = {
    "README.md": b"Applications: trading lives in trade-demo; finance training lives in training/finance.\n",
    "training/finance/README.md": b"Finance training handles user interaction in LightrunExerciseServer.userSelect.\n",
    SOURCE: b"class LightrunExerciseServer {\n  void userSelect() { recordSelection(); }\n}\n",
}


class RepositoryToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root_patch = patch.object(cache, "SOURCE_CACHE_ROOT", Path(self.directory.name).resolve())
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.now = 100000.0
        self.clock_cache = patch("cache_git_files.time", lambda: self.now)
        self.clock_tools = patch("repository_tools.time", lambda: self.now)
        self.clock_cache.start()
        self.clock_tools.start()
        self.addCleanup(self.clock_cache.stop)
        self.addCleanup(self.clock_tools.stop)
        self.calls = []
        self.head = COMMIT
        self.http_failure = False
        self.truncated = False
        self.tokens = []
        self.new_session()
        self.addCleanup(self.reset_sessions)
        original_client = httpx.AsyncClient
        self.client_patch = patch("httpx.AsyncClient", side_effect=lambda **kw: original_client(
            **kw, transport=httpx.MockTransport(self.handle)))
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.tools = {t.name: t for t in create_repository_tools(OWNER, REPO, "main")}

    def new_session(self, limit=10, ttl=86400):
        session = RepositorySession(OWNER, REPO, limit, ttl)
        self.tokens.append(repository_session.set(session))
        return session

    def reset_sessions(self):
        for token in reversed(self.tokens):
            repository_session.reset(token)

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        if self.http_failure:
            return httpx.Response(403, json={"message": "forbidden"})
        if "/commits/" in path:
            return httpx.Response(200, json={"sha": self.head})
        if "/git/trees/" in path:
            sha = path.rsplit("/", 1)[1]
            recursive = "recursive" in request.url.params
            if self.truncated:
                if sha in {COMMIT, NEXT_COMMIT}:
                    entries = [{"path": "training", "type": "tree", "sha": TRAINING_TREE}]
                elif sha == TRAINING_TREE:
                    entries = [{"path": "finance", "type": "tree", "sha": FINANCE_TREE}]
                else:
                    entries = [{"path": "README.md", "type": "blob", "sha": "e" * 40}]
                return httpx.Response(200, json={"tree": entries,
                    "truncated": recursive and sha in {COMMIT, NEXT_COMMIT}})
            return httpx.Response(200, json={"tree": [
                {"path": name, "type": "blob", "sha": cache.calculate_git_blob_sha(content),
                 "size": len(content)} for name, content in FILES.items()], "truncated": False})
        if "/contents/" in path:
            name = path.split("/contents/", 1)[1]
            if name not in FILES:
                return httpx.Response(404, json={"message": "not found"})
            content = FILES[name]
            return httpx.Response(200, json={"type": "file", "sha": cache.calculate_git_blob_sha(content),
                                             "size": len(content)})
        if "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[1]
            content = next(v for v in FILES.values() if cache.calculate_git_blob_sha(v) == sha)
            return httpx.Response(200, content=content)
        if path == "/search/code":
            return httpx.Response(200, json={"total_count": 0, "incomplete_results": False, "items": []})
        raise AssertionError(f"Unexpected URL: {request.url}")

    async def call(self, name, **args):
        return await self.tools[name].ainvoke(args)

    async def read(self, path="README.md", **kwargs):
        return await self.call("read_repository_file", target_file=path, **kwargs)

    async def note(self, **kwargs):
        return await self.call("update_repository_map", component="training/finance",
            summary="Finance training records user interaction; inspect the REST handler.",
            evidence_paths=["README.md"], commit_sha=COMMIT, confidence="inferred", **kwargs)

    async def test_cold_orientation_and_persistent_warm_map_without_filename_input(self):
        initial = await self.call("read_repository_map")
        self.assertEqual(initial["entries"], [])
        self.assertEqual(len(self.calls), 0)
        tree = await self.call("list_repository_structure")
        self.assertIn("training", tree["top_level"])
        await self.read()
        await self.read("training/finance/README.md")
        source = await self.read(SOURCE)
        self.assertIn("2 |   void userSelect()", source["source"])
        self.assertEqual(source["requests_used"], 8)  # commit + tree + three metadata/blob pairs
        await self.note()
        markdown = Path(initial["map_path"]).read_text()
        self.assertIn("Finance training", markdown)
        self.assertIn(COMMIT, markdown)
        self.new_session()
        self.tools = {t.name: t for t in create_repository_tools(OWNER, REPO, "main")}
        warm = await self.call("read_repository_map")
        self.assertEqual(len(warm["entries"]), 1)
        self.assertFalse(warm["revision_checked"])
        result = await self.read(SOURCE)
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["requests_used"], 1)  # fresh branch resolution only

    async def test_expired_source_revalidates_without_redownloading_blob(self):
        await self.read()
        self.now += 86401
        result = await self.read()
        self.assertTrue(result["cache_hit"])
        self.assertEqual(len(self.calls), 4)
        self.assertIn("/contents/", self.calls[-1].url.path)
        again = await self.read()
        self.assertEqual(again["requests_used"], 4)

    async def test_expired_notes_and_source_do_not_bypass_exhausted_budget(self):
        await self.read()
        await self.note()
        self.now += 86401
        session = repository_session.get()
        session.request_limit = session.requests_used
        result = await self.read()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertNotIn("source", result)
        notes = await self.call("read_repository_map")
        self.assertTrue(notes["entries"][0]["needs_review"])
        rejected = await self.note()
        self.assertEqual(rejected["status"], "error")

    async def test_new_ticket_resolves_new_head_and_marks_old_notes_for_review(self):
        await self.read()
        await self.note()
        self.head = NEXT_COMMIT
        self.new_session()
        result = await self.read()
        self.assertEqual(result["commit_sha"], NEXT_COMMIT)
        self.assertFalse(result["cache_hit"])
        notes = await self.call("read_repository_map")
        self.assertTrue(notes["entries"][0]["needs_review"])

    async def test_parallel_reads_cannot_exceed_http_budget(self):
        self.new_session(limit=4)
        results = await asyncio.gather(*(self.read(path) for path in FILES))
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(any(r.get("status") == "budget_exhausted" for r in results))
        self.assertTrue(all(r["requests_used"] <= 4 for r in results))
        self.assertEqual(sum("/commits/" in r.url.path for r in self.calls), 1)

    async def test_failed_http_requests_count_and_errors_are_not_empty_searches(self):
        self.new_session(limit=1)
        self.http_failure = True
        result = await self.call("search_repository_code", terms="finance")
        self.assertEqual(result["status"], "github_error")
        self.assertEqual(result["http_status"], 403)
        result = await self.call("search_repository_code", terms="finance")
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(self.calls), 1)

    async def test_truncated_tree_falls_back_to_directory_traversal(self):
        self.truncated = True
        root = await self.call("list_repository_structure")
        self.assertTrue(root["github_truncated"])
        result = await self.call("list_repository_structure", directory="training/finance")
        self.assertFalse(result["github_truncated"])
        self.assertEqual(result["entries"][0]["path"], "training/finance/README.md")
        self.assertEqual(len(self.calls), 5)

    async def test_pagination_and_fresh_tree_reuse_no_requests(self):
        first = await self.call("list_repository_structure", limit=1)
        second = await self.call("list_repository_structure", offset=first["next_offset"], limit=1)
        self.assertNotEqual(first["entries"], second["entries"])
        self.assertEqual(len(self.calls), 2)
        self.now += 86401
        await self.call("list_repository_structure")
        self.assertEqual(len(self.calls), 3)

    async def test_evidence_required_and_paths_cannot_escape_cache(self):
        result = await self.note()
        self.assertEqual(result["status"], "error")
        result = await self.read("../../outside")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.calls), 0)

    async def test_scope_and_zero_budget(self):
        self.new_session(limit=0)
        result = await self.read()
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(len(self.calls), 0)
        self.assertEqual((await self.call("read_repository_map"))["entries"], [])
        with self.assertRaises(ValueError):
            await cache.resolve_ref_to_commit(None, "other", REPO, COMMIT)

    async def test_invalid_ranges_and_search_scope_are_rejected_before_http(self):
        self.assertEqual((await self.read(start_line=0))["status"], "error")
        result = await self.call("search_repository_code", terms="repo:other/private")
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.calls), 0)

    async def test_read_logs_actual_range_cache_hit_and_budget_failure(self):
        with self.assertLogs("repository_tools", level="INFO") as logs:
            result = await self.read()
            await self.read()
        self.assertEqual((result["start_line"], result["end_line"]), (1, 1))
        text = "\n".join(logs.output)
        self.assertIn("returned_lines=1-1", text)
        self.assertIn("cache_hit=False", text)
        self.assertIn("cache_hit=True", text)
        self.assertIn("requests_remaining=7", text)
        self.assertIn(COMMIT, text)
        self.new_session(limit=0)
        with self.assertLogs("repository_tools", level="WARNING") as logs:
            await self.read()
        self.assertIn("status=budget_exhausted", "\n".join(logs.output))

    async def test_out_of_range_read_is_not_recorded_as_inspected_source(self):
        with self.assertLogs("repository_tools", level="WARNING") as logs:
            result = await self.read(start_line=20, end_line=30)
        self.assertIsNone(result["start_line"])
        self.assertIsNone(result["end_line"])
        self.assertFalse(repository_session.get().inspected_source)
        self.assertIn("empty_or_out_of_range", "\n".join(logs.output))

    def test_default_request_budget_and_environment_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(RepositorySession.from_environment(OWNER, REPO).request_limit, 20)
        with patch.dict(os.environ, {"REPO_GITHUB_REQUEST_LIMIT": "7"}):
            self.assertEqual(RepositorySession.from_environment(OWNER, REPO).request_limit, 7)

    async def test_note_ttl_uses_evidence_time_not_summary_write_time(self):
        await self.read()
        self.now += 86000
        await self.note()
        self.now += 401
        notes = await self.call("read_repository_map")
        self.assertTrue(notes["entries"][0]["needs_review"])


if __name__ == "__main__":
    unittest.main()
