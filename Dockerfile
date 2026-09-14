FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JIRA_WEBHOOK_HOST=0.0.0.0 \
    JIRA_WEBHOOK_PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10001 agent \
    && chown agent:agent /app

COPY --chown=agent:agent logging_utils.py ./
COPY --chown=agent:agent agentic_lr_investigation.py ./
COPY --chown=agent:agent cache_git_files.py ./
COPY --chown=agent:agent jira_access_tools.py ./
COPY --chown=agent:agent jira_webhook_server.py ./

RUN python -m compileall -q .

USER agent

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2)"]

CMD ["python", "agentic_lr_investigation.py"]
