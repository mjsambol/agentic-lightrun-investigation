from langchain.agents.middleware import wrap_model_call, wrap_tool_call
import asyncio
import logging
import json
from time import monotonic
from cache_git_files import _github_rate_limit_retry_seconds

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def snapshot_log_details(arguments: dict) -> str:
    """Log placement and expression text only, never captured values or other args."""
    location = arguments.get("location")
    location = location if isinstance(location, dict) else {}

    def field(names, default=None):
        for container in (arguments, location):
            for name in names:
                if name in container:
                    return container[name]
        return default

    # Accommodate common MCP schema spellings without dumping all arguments.
    details = {
        "file": field(("file", "filePath", "filename", "fileName", "path", "sourceFile"), "unspecified"),
        "line": field(("line", "lineNumber", "line_number"), "unspecified"),
        "condition": field(("condition",)),
        "watch_expressions": field(("expressions", "watchExpressions", "watch_expressions"), []),
    }
    # JSON escapes newlines in expressions, keeping each event on one log line.
    return json.dumps(details, ensure_ascii=True)


def configure_logging() -> None:
    """Show application progress without enabling noisy SDK/HTTP debug logs."""
    logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT)
    for name in (
        "__main__", "agentic_lr_investigation", "logging_utils",
        "jira_webhook_server", "repository_tools", "cache_git_files",
    ):
        logging.getLogger(name).setLevel(logging.INFO)


@wrap_tool_call
async def log_agent_tool_call(request, handler):
    """Log tool timing and the requested snapshot placement/expressions."""
    tool_name = request.tool_call["name"]
    tool_label = tool_name
    if tool_name == "skill_start":
        skill_name = request.tool_call["args"]["skillName"]
        tool_label = f"{tool_name} ({skill_name})"
    snapshot_details = (
        snapshot_log_details(request.tool_call.get("args", {}))
        if tool_name == "snapshot_create" else None
    )
    if snapshot_details is not None:
        tool_label = f"{tool_name} {snapshot_details}"
    started_at = monotonic()

    if tool_name == "snapshot_create":
        logger.info("Creating Lightrun snapshot: %s", snapshot_details)
    else:
        logger.info("Calling agent tool %s...", tool_label)

    try:
        result = await handler(request)
    except Exception as exc:
        retry_seconds = _github_rate_limit_retry_seconds(exc)
        if retry_seconds is None:
            logger.exception(
                "Agent tool %s failed after %.1f seconds",
                tool_label,
                monotonic() - started_at,
            )
            raise

        logger.warning(
            "GitHub rate limit reached while calling agent tool %s; "
            "retrying in %.1f seconds",
            tool_label,
            retry_seconds,
        )
        await asyncio.sleep(retry_seconds)

        try:
            result = await handler(request)
        except Exception:
            logger.exception(
                "Agent tool %s failed again after its rate-limit retry",
                tool_label,
            )
            raise

    if tool_name == "snapshot_create":
        logger.info(
            "Lightrun snapshot created in %.1f seconds: %s",
            monotonic() - started_at,
            snapshot_details,
        )
    else:
        logger.info(
            "Agent tool %s completed in %.1f seconds",
            tool_label,
            monotonic() - started_at,
        )
    return result


@wrap_model_call
async def log_agent_model_call(request, handler):
    """Log model latency without exposing prompts or responses."""
    started_at = monotonic()
    logger.info("Calling agent model...")
    try:
        result = await handler(request)
    except Exception:
        logger.exception(
            "Agent model call failed after %.1f seconds",
            monotonic() - started_at,
        )
        raise

    logger.info(
        "Agent model call completed in %.1f seconds",
        monotonic() - started_at,
    )
    return result
