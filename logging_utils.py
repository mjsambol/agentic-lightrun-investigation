from langchain.agents.middleware import wrap_model_call, wrap_tool_call
import asyncio
import logging
from time import monotonic
from cache_git_files import _github_rate_limit_retry_seconds

logger = logging.getLogger(__name__)


@wrap_tool_call
async def log_agent_tool_call(request, handler):
    """Log agent-selected tools without exposing their potentially sensitive args."""
    tool_name = request.tool_call["name"]
    tool_label = tool_name
    if tool_name == "skill_start":
        skill_name = request.tool_call["args"]["skillName"]
        tool_label = f"{tool_name} ({skill_name})"
    started_at = monotonic()

    if tool_name == "snapshot_create":
        logger.info("Creating Lightrun snapshot...")
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
            "Lightrun snapshot created in %.1f seconds",
            monotonic() - started_at,
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
