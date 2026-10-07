import io
import logging
import os
import unittest
from unittest.mock import patch

import httpx
from uvicorn.logging import AccessFormatter, DefaultFormatter

import cache_git_files as cache
from diagnose_github import diagnose
from jira_webhook_server import request_log_config
from logging_utils import configure_logging


class LoggingTests(unittest.TestCase):
    def test_access_and_lifecycle_logs_have_timestamps(self):
        config = request_log_config()
        cases = [
            ("access", AccessFormatter, "uvicorn.access", '%s - "%s %s HTTP/%s" %d',
             ("127.0.0.1:54321", "GET", "/health/live", "1.1", 200)),
            ("default", DefaultFormatter, "uvicorn.error", "Server started", ()),
        ]
        for name, cls, logger, message, args in cases:
            record = logging.LogRecord(logger, logging.INFO, "", 1, message, args, None)
            text = cls(fmt=config["formatters"][name]["fmt"], use_colors=False).format(record)
            self.assertRegex(text, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO uvicorn\.')
            if name == "access":
                self.assertIn("GET /health/live HTTP/1.1", text)
        self.assertNotIn("asctime", __import__("uvicorn").config.LOGGING_CONFIG["formatters"]["access"]["fmt"])

    def test_application_info_logging_enabled_without_sdk_debug_noise(self):
        with patch("logging_utils.logging.basicConfig") as basic, patch("logging_utils.logging.getLogger") as get:
            configure_logging()
        basic.assert_called_once()
        self.assertEqual(basic.call_args.kwargs["level"], logging.WARNING)
        names = {call.args[0] for call in get.call_args_list}
        self.assertTrue({"logging_utils", "jira_webhook_server", "cache_git_files"} <= names)
        self.assertNotIn("httpx", names)


class GitHubDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_contains_endpoint_and_request_id_without_logging_credentials(self):
        def handle(request):
            return httpx.Response(404, json={"message": "Not Found"}, headers={"x-github-request-id": "TEST-ID"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle),
                                     headers={"Authorization": "Bearer secret-value"}) as client:
            with self.assertLogs("cache_git_files", level="INFO") as output:
                with self.assertRaises(cache.GitHubAPIError) as raised:
                    await cache.github_get_json(client, "https://api.github.com/repos/example/demo/commits/main")
        self.assertEqual(raised.exception.endpoint, "/repos/example/demo/commits/main")
        self.assertEqual(raised.exception.request_id, "TEST-ID")
        self.assertNotIn("secret-value", "\n".join(output.output))
        self.assertIn("HTTP 404", "\n".join(output.output))

    async def run_diagnostic(self, failure_stage=None):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            self.assertEqual(request.headers["Authorization"], "Bearer test-token")
            stage = len(calls)
            if stage == failure_stage:
                return httpx.Response(404, json={"message": "Not Found"})
            data = {1: {"default_branch": "main"},
                    2: {"sha": "a" * 40, "commit": {"tree": {"sha": "b" * 40}}},
                    3: {"tree": [], "truncated": False},
                    4: {"tree": [], "truncated": False}}[stage]
            return httpx.Response(200, json=data)
        original = httpx.AsyncClient
        with (patch.dict(os.environ, {"GITHUB_MCP_PAT": "test-token"}),
              patch("httpx.AsyncClient", side_effect=lambda **kw: original(
                  **kw, transport=httpx.MockTransport(handle))),
              self.assertLogs("diagnose_github", level="INFO") as output):
            result = await diagnose("example", "demo", "main")
        self.assertNotIn("test-token", "\n".join(output.output))
        return result, calls, "\n".join(output.output)

    async def test_diagnostic_distinguishes_repository_and_ref_failures(self):
        result, calls, logs = await self.run_diagnostic(1)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("repository visibility", logs)
        result, calls, logs = await self.run_diagnostic(2)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("configured ref resolution", logs)

    async def test_diagnostic_identifies_tree_object_mismatch_if_present(self):
        result, calls, logs = await self.run_diagnostic(3)
        self.assertEqual(result, 1)
        self.assertTrue(calls[-1].endswith("b" * 40))
        self.assertIn("tree-discovery implementation issue", logs)

    async def test_diagnostic_success(self):
        result, calls, logs = await self.run_diagnostic()
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("checks passed", logs)


if __name__ == "__main__":
    unittest.main()
