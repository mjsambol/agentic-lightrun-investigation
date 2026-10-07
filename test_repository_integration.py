"""Verify session lifetime and tool registration without Jira/model/network calls."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("LIGHTRUN_API_KEY", "test-token")
os.environ.setdefault("JIRA_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("JIRA_EMAIL", "agent@example.com")
os.environ.setdefault("JIRA_API_TOKEN", "test-token")

from langchain.messages import AIMessage
import agentic_lr_investigation as app
from repository_context import charge_github_request, repository_session


class RepositoryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_survives_retry_and_resets_for_next_ticket(self):
        sessions = []

        class Agent:
            calls = 0

            async def astream(self, *args, **kwargs):
                self.calls += 1
                sessions.append(repository_session.get())
                charge_github_request()
                if self.calls == 1:
                    raise RuntimeError("simulated model failure")
                yield {"model": {"messages": [AIMessage(content="Investigation result")]}}

        agent = Agent()
        checkpointer = SimpleNamespace(adelete_thread=AsyncMock())
        issue = {"key": "TEST-1", "fields": {"description": "Investigate finance activity"}}
        with patch.object(app, "add_jira_comment", AsyncMock()), self.assertLogs(app.logger, level="INFO"):
            await app.run_agent_for_issue(agent, checkpointer, None, issue)
            self.assertIsNone(repository_session.get())
            await app.run_agent_for_issue(agent, checkpointer, None, {**issue, "key": "TEST-2"})
        self.assertIs(sessions[0], sessions[1])
        self.assertEqual(sessions[0].requests_used, 2)
        self.assertIsNot(sessions[1], sessions[2])
        self.assertEqual(sessions[2].requests_used, 1)
        checkpointer.adelete_thread.assert_awaited_once()

    async def test_only_local_repository_tools_are_registered(self):
        mcp = Mock()
        mcp.get_tools = AsyncMock(return_value=[
            SimpleNamespace(name="snapshot_status"), SimpleNamespace(name="snapshot_cancel")])
        with (patch.object(app, "MultiServerMCPClient", return_value=mcp) as factory,
              patch.object(app, "create_agent") as create,
              patch.object(app, "jira_polling_worker", AsyncMock()),
              patch.dict(os.environ, {"JIRA_TRIGGER_MODE": "poll"})):
            await app.main()
        self.assertEqual(set(factory.call_args.args[0]), {"Lightrun"})
        names = {t.name for t in create.call_args.kwargs["tools"]}
        self.assertTrue({"read_repository_map", "list_repository_structure", "read_repository_file",
                         "search_repository_code", "update_repository_map"} <= names)
        self.assertNotIn("search_code", names)
        self.assertNotIn("read_cached_source", names)


if __name__ == "__main__":
    unittest.main()
