import asyncio
import hashlib
import hmac
import json
import os
import unittest

import httpx


# jira_access_tools reads these settings when it is imported.
os.environ.setdefault("JIRA_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("JIRA_EMAIL", "agent@example.com")
os.environ.setdefault("JIRA_API_TOKEN", "test-token")

from jira_access_tools import (  # noqa: E402
    COMPLETED_LABEL,
    PROCESSING_LABEL,
    is_pending_agent_task,
)
from jira_webhook_server import (  # noqa: E402
    create_webhook_app,
    signature_is_valid,
)


SECRET = "demo-webhook-secret"


def signed_headers(body: bytes, delivery_id: str = "delivery-1") -> dict[str, str]:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature": f"sha256={digest}",
        "X-Atlassian-Webhook-Identifier": delivery_id,
    }


class JiraWebhookAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        app = create_webhook_app(self.queue, SECRET)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_manual_trigger_accepts_key_and_numeric_id_without_auth(self):
        for issue_id in ("DEMO-42", "10042"):
            response = await self.client.post("/investigations", json={"issue_id": issue_id})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), {"status": "queued", "issue_id": issue_id})
            self.assertEqual(self.queue.get_nowait(), issue_id)

    async def test_manual_trigger_rejects_invalid_input(self):
        for payload in ({}, {"issue_id": "../other"}, {"issue_id": 42},
                        {"issue_id": "DEMO-42", "extra": True}, {"issue_id": ""}):
            response = await self.client.post("/investigations", json=payload)
            self.assertEqual(response.status_code, 422)
        self.assertTrue(self.queue.empty())

    async def test_manual_trigger_full_queue(self):
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait("DEMO-1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
                app=create_webhook_app(queue, SECRET)), base_url="http://test") as client:
            response = await client.post("/investigations", json={"issue_id": "DEMO-2"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(queue.get_nowait(), "DEMO-1")

    async def test_rest_without_secret_accepts_manual_but_disables_webhook(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(
                app=create_webhook_app(self.queue, "")), base_url="http://test") as client:
            response = await client.post("/investigations", json={"issue_id": "DEMO-42"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual((await client.post("/webhooks/jira", json={})).status_code, 503)

    async def test_accepts_and_queues_created_issue(self) -> None:
        body = json.dumps(
            {
                "webhookEvent": "jira:issue_created",
                "issue": {"key": "DEMO-42"},
            }
        ).encode()

        response = await self.client.post(
            "/webhooks/jira",
            content=body,
            headers=signed_headers(body),
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.queue.get_nowait(), "DEMO-42")

    async def test_rejects_invalid_signature(self) -> None:
        body = b'{"webhookEvent":"jira:issue_created"}'

        response = await self.client.post(
            "/webhooks/jira",
            content=body,
            headers={"X-Hub-Signature": "sha256=wrong"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertTrue(self.queue.empty())

    async def test_ignores_duplicate_delivery(self) -> None:
        body = json.dumps(
            {
                "webhookEvent": "jira:issue_created",
                "issue": {"key": "DEMO-42"},
            }
        ).encode()
        headers = signed_headers(body)

        first = await self.client.post(
            "/webhooks/jira", content=body, headers=headers
        )
        second = await self.client.post(
            "/webhooks/jira", content=body, headers=headers
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 204)
        self.assertEqual(self.queue.qsize(), 1)

    async def test_ignores_other_events(self) -> None:
        body = json.dumps(
            {
                "webhookEvent": "jira:issue_updated",
                "issue": {"key": "DEMO-42"},
            }
        ).encode()

        response = await self.client.post(
            "/webhooks/jira",
            content=body,
            headers=signed_headers(body),
        )

        self.assertEqual(response.status_code, 204)
        self.assertTrue(self.queue.empty())


class JiraIssueEligibilityTests(unittest.TestCase):
    def make_issue(self, labels: list[str] | None = None) -> dict:
        return {
            "fields": {
                "summary": "Agent",
                "issuetype": {"name": "Task"},
                "assignee": {"accountId": "jira-agent"},
                "labels": labels or [],
            }
        }

    def test_accepts_pending_agent_task(self) -> None:
        self.assertTrue(
            is_pending_agent_task(self.make_issue(), "jira-agent")
        )

    def test_rejects_already_claimed_or_completed_task(self) -> None:
        for label in (PROCESSING_LABEL, COMPLETED_LABEL):
            with self.subTest(label=label):
                self.assertFalse(
                    is_pending_agent_task(
                        self.make_issue([label]),
                        "jira-agent",
                    )
                )

    def test_rejects_wrong_assignee(self) -> None:
        self.assertFalse(
            is_pending_agent_task(self.make_issue(), "someone-else")
        )


class JiraSignatureTests(unittest.TestCase):
    def test_matches_atlassian_documentation_example(self) -> None:
        self.assertTrue(
            signature_is_valid(
                b"Hello World!",
                (
                    "sha256="
                    "a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace"
                    "1f2937e133263c9"
                ),
                "It's a Secret to Everybody",
            )
        )


if __name__ == "__main__":
    unittest.main()
