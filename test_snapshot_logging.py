import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from logging_utils import log_agent_tool_call, snapshot_log_details


class SnapshotLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_placement_logged_before_and_after_call_without_results_or_unrelated_args(self):
        args = {"filePath": "src/Example.java", "lineNumber": 42,
                "condition": "order != null", "expressions": ["order.id", "order.total"],
                "authorization": "do-not-log"}
        request = SimpleNamespace(tool_call={"name": "snapshot_create", "args": args})
        handler = AsyncMock(return_value={"captured": "private-result"})
        with self.assertLogs("logging_utils", level="INFO") as logs:
            result = await log_agent_tool_call.awrap_tool_call(request, handler)
        self.assertEqual(result, {"captured": "private-result"})
        self.assertEqual(len(logs.output), 2)
        for message in logs.output:
            self.assertIn('"file": "src/Example.java"', message)
            self.assertIn('"line": 42', message)
            self.assertIn('"condition": "order != null"', message)
            self.assertIn('"watch_expressions": ["order.id", "order.total"]', message)
            self.assertNotIn("do-not-log", message)
            self.assertNotIn("private-result", message)
        handler.assert_awaited_once_with(request)

    async def test_failed_call_includes_placement(self):
        request = SimpleNamespace(tool_call={"name": "snapshot_create", "args": {"file": "A.java", "line": 7}})
        with self.assertLogs("logging_utils", level="INFO") as logs:
            with self.assertRaises(RuntimeError):
                await log_agent_tool_call.awrap_tool_call(request, AsyncMock(side_effect=RuntimeError("failed")))
        self.assertIn('"file": "A.java"', logs.output[-1])
        self.assertNotIn("snapshot created", "\n".join(logs.output))

    def test_optional_fields_and_newlines(self):
        text = snapshot_log_details({"location": {"filename": "A.java", "line": 7},
                                     "watchExpressions": ["first\nsecond"]})
        self.assertNotIn("\n", text)
        self.assertEqual(json.loads(text), {"file": "A.java", "line": 7,
                         "condition": None, "watch_expressions": ["first\nsecond"]})
        self.assertEqual(json.loads(snapshot_log_details({}))["watch_expressions"], [])


if __name__ == "__main__":
    unittest.main()
