import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx


logger = logging.getLogger(__name__)


JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]

POLL_INTERVAL_SECONDS = 30
COMMENT_CHUNK_SIZE = 4_000

PROCESSING_LABEL = "ai-agent-processing"
COMPLETED_LABEL = "ai-agent-completed"
FAILED_LABEL = "ai-agent-failed"


def classify_failure(error: BaseException) -> str:
    """Identify the remote service involved in an exception, including groups."""
    jira_host = urlparse(JIRA_BASE_URL).hostname
    services: set[str] = set()
    pending: list[BaseException] = [error]
    seen: set[int] = set()

    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)

        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                pending.append(linked)

        request = getattr(current, "request", None)
        url = getattr(request, "url", None)
        host = getattr(url, "host", None)
        if host is None and url is not None:
            host = urlparse(str(url)).hostname

        if host == jira_host:
            services.add("Jira")
        elif host == "app.lightrun.com":
            services.add("Lightrun")
        elif host in {"api.github.com", "api.githubcopilot.com"}:
            services.add("GitHub")
        elif host in {"api.openai.com", "openai.com"}:
            services.add("OpenAI")

    if services:
        return "/".join(sorted(services))
    return "agent or unidentified tool"


def adf_to_text(node: Any) -> str:
    """Extract readable plain text from a Jira ADF document."""
    if node is None:
        return ""

    if isinstance(node, str):
        return node

    if isinstance(node, list):
        return "".join(adf_to_text(item) for item in node)

    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")

    if node_type == "text":
        return node.get("text", "")

    if node_type == "hardBreak":
        return "\n"

    content = adf_to_text(node.get("content", []))

    if node_type in {
        "paragraph",
        "heading",
        "blockquote",
        "codeBlock",
        "listItem",
    }:
        return content + "\n"

    if node_type in {"bulletList", "orderedList"}:
        return content + "\n"

    return content


def text_to_adf(text: str) -> dict[str, Any]:
    """Create a simple Jira ADF document from plain text."""
    paragraphs: list[dict[str, Any]] = []

    for block in text.split("\n\n"):
        lines = block.splitlines()
        content: list[dict[str, Any]] = []

        for index, line in enumerate(lines):
            if index:
                content.append({"type": "hardBreak"})

            if line:
                content.append(
                    {
                        "type": "text",
                        "text": line,
                    }
                )

        paragraphs.append(
            {
                "type": "paragraph",
                "content": content,
            }
        )

    return {
        "version": 1,
        "type": "doc",
        "content": paragraphs or [{"type": "paragraph", "content": []}],
    }

def jira_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=JIRA_BASE_URL,
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )


async def find_pending_agent_tasks(
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    response = await client.post(
        "/rest/api/3/search/jql",
        json={
            "jql": (
                'assignee = currentUser() '
                'AND issuetype = Task '
                'AND summary ~ "\\"Agent\\"" '
                f'AND (labels IS EMPTY OR labels NOT IN '
                f'("{PROCESSING_LABEL}", "{COMPLETED_LABEL}")) '
                "ORDER BY created ASC"
            ),
            "maxResults": 20,
            "fields": [
                "summary",
                "description",
                "labels",
                "status",
            ],
        },
    )
    response.raise_for_status()

    issues = response.json().get("issues", [])

    # JQL text matching is not necessarily exact, so enforce exact equality.
    return [
        issue
        for issue in issues
        if issue.get("fields", {}).get("summary") == "Agent"
    ]


async def get_jira_account_id(client: httpx.AsyncClient) -> str:
    """Return the account ID used by this Jira client."""
    response = await client.get("/rest/api/3/myself")
    response.raise_for_status()
    return response.json()["accountId"]


async def get_issue(
    client: httpx.AsyncClient,
    issue_key: str,
) -> dict[str, Any]:
    """Fetch the current Jira fields used to decide whether to run the agent."""
    response = await client.get(
        f"/rest/api/3/issue/{issue_key}",
        params={
            "fields": "summary,description,labels,status,issuetype,assignee",
        },
    )
    response.raise_for_status()
    return response.json()


def is_pending_agent_task(
    issue: dict[str, Any],
    jira_account_id: str,
) -> bool:
    """Apply the polling query's eligibility rules to a webhook issue."""
    fields = issue.get("fields", {})
    labels = set(fields.get("labels") or [])
    assignee = fields.get("assignee") or {}
    issue_type = fields.get("issuetype") or {}

    return (
        # In the demo context, all webhook invocations are intended
        # for the demo and will be accepted without further filtering.
        # In a real-world application, filters like those below should be considered.
        #
        # fields.get("summary") == "Agent"
        # and issue_type.get("name") == "Task"
        # and assignee.get("accountId") == jira_account_id and
        PROCESSING_LABEL not in labels
        and COMPLETED_LABEL not in labels
    )


async def set_issue_labels(
    client: httpx.AsyncClient,
    issue_key: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    update: dict[str, list[dict[str, str]]] = {}

    operations: list[dict[str, str]] = []

    for label in remove or []:
        operations.append({"remove": label})

    for label in add or []:
        operations.append({"add": label})

    if operations:
        update["labels"] = operations

    response = await client.put(
        f"/rest/api/3/issue/{issue_key}",
        json={"update": update},
    )
    response.raise_for_status()


async def add_jira_comment(
    client: httpx.AsyncClient,
    issue_key: str,
    text: str,
) -> None:
    response = await client.post(
        f"/rest/api/3/issue/{issue_key}/comment",
        json={
            "body": text_to_adf(text),
        },
    )
    response.raise_for_status()
