from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from repository_learning import KnowledgeUpdate, save_repository_knowledge
import test_repository_tools as fixtures

COMMIT = fixtures.COMMIT


class LearningTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.RepositoryToolsTests.setUp
    new_session = fixtures.RepositoryToolsTests.new_session
    reset_sessions = fixtures.RepositoryToolsTests.reset_sessions
    handle = fixtures.RepositoryToolsTests.handle
    call = fixtures.RepositoryToolsTests.call
    read = fixtures.RepositoryToolsTests.read
    # Reuse mocked GitHub fixtures; the learning pass must not use their HTTP transport.
    async def test_consolidation_writes_map_without_github_requests(self):
        await self.read()
        before = len(self.calls)
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=KnowledgeUpdate(notes=[{
            "component": "training/finance", "summary": "Finance training lives in training/finance.",
            "evidence_paths": ["README.md"], "commit_sha": COMMIT, "confidence": "confirmed",
        }])))
        with patch("repository_learning.init_chat_model") as init:
            init.return_value.with_structured_output.return_value = model
            await save_repository_knowledge("TEST-1", "main")
        self.assertEqual(len(self.calls), before)
        mapping = await self.call("read_repository_map")
        self.assertEqual(len(mapping["entries"]), 1)
        self.assertTrue(Path(mapping["map_path"]).is_file())

    async def test_uninspected_citation_rejected_and_failure_does_not_escape(self):
        await self.read()
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=KnowledgeUpdate(notes=[{
            "component": "invented", "summary": "Unsupported", "evidence_paths": ["unknown.java"],
            "commit_sha": COMMIT, "confidence": "inferred",
        }])))
        with patch("repository_learning.init_chat_model") as init, self.assertLogs("repository_learning", level="ERROR"):
            init.return_value.with_structured_output.return_value = model
            await save_repository_knowledge("TEST-1", "main")
        self.assertEqual((await self.call("read_repository_map"))["entries"], [])

    async def test_no_evidence_skips_model(self):
        with patch("repository_learning.init_chat_model") as init, self.assertLogs("repository_learning", level="INFO") as logs:
            await save_repository_knowledge("TEST-1", "main")
        init.assert_not_called()
        self.assertIn("no inspected source evidence", "\n".join(logs.output))


class ReplyOrderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_runs_after_all_reply_chunks(self):
        import test_repository_integration  # sets test-only environment before app import
        import agentic_lr_investigation as app
        from langchain.messages import AIMessage
        events = []
        class Agent:
            async def astream(self, *args, **kwargs):
                yield {"model": {"messages": [AIMessage(content="abcdef")]}}
        async def comment(client, key, content):
            events.append(content)
        async def save(*args):
            events.append("saved")
        with patch.object(app, "add_jira_comment", comment), patch.object(app, "save_repository_knowledge", save), patch.object(app, "COMMENT_CHUNK_SIZE", 3):
            await app.run_agent_for_issue(Agent(), None, None, {"key": "TEST-1", "fields": {"description": "investigate"}})
        self.assertEqual(events[-3:], ["Agent response, part 1:\n\nabc", "Agent response, part 2:\n\ndef", "saved"])

    async def test_error_reply_precedes_saving_partial_evidence(self):
        import test_repository_integration
        import agentic_lr_investigation as app
        events = []
        async def comment(*args):
            events.append("failure reply")
        async def save(*args):
            events.append("saved")
        with (patch.object(app, "run_agent_for_issue", AsyncMock(side_effect=RuntimeError("failed"))),
              patch.object(app, "add_jira_comment", comment),
              patch.object(app, "set_issue_labels", AsyncMock()),
              patch.object(app, "save_repository_knowledge", save), self.assertLogs(app.logger, level="ERROR")):
            with self.assertRaises(RuntimeError):
                await app.process_issue(None, None, None, {"key": "TEST-1"})
        self.assertEqual(events, ["failure reply", "saved"])
