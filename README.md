# Automated Jira investigation demo

`agentic_lr_investigation.py` demonstrates an agent that:

1. receives an investigation request from a Jira Task;
2. reads the relevant source through GitHub;
3. uses Lightrun to observe the running application;
4. posts the investigation result back to Jira.

<img width="1088" height="692" alt="Lightrun-Jira-enrichment-agent" src="https://github.com/user-attachments/assets/3e76f271-f663-44cb-ac49-e3791cda2a2e" />

Sample agent posts to Jira tickets:

<img width="1470" height="948" alt="jira-agent-image1" src="https://github.com/user-attachments/assets/9a7de842-4f25-472e-bfbd-f95936d4b5fa" />

<img width="556" height="768" alt="jira-agent-image2" src="https://github.com/user-attachments/assets/d2478ad0-174f-4e67-86c8-46325a3df5a6" />

---

The script supports two ways to discover tickets:

- `JIRA_TRIGGER_MODE=poll` searches Jira every 30 seconds. This is the default behavior, suitable for running in a local demo.
- `JIRA_TRIGGER_MODE=webhook` runs a small HTTP server. Jira calls it as soon as
  an issue is created. Requires a publicly accessible endpoint.

This guide focuses on setting up the agent to run in webhook mode on a small Ubuntu AWS EC2 instance.

## Demo architecture

```text
Jira Cloud
    |
    | HTTPS issue-created webhook
    v
Caddy container on EC2 (TLS, ports 80/443)
    |
    | Docker network, port 8080
    v
Agent container: FastAPI receiver -> in-memory queue -> one worker
                                          |
                                          +-> Jira REST API
                                          +-> GitHub REST (budgeted local tools)
                                          +-> Lightrun MCP
                                          +-> OpenAI API
```

The webhook endpoint verifies Jira's HMAC signature, queues the issue key, and
returns immediately. The worker fetches the current issue from Jira before
processing it. 

## Repository understanding and source caching

Repository access uses local wrappers around GitHub's REST API and authenticates with
`GITHUB_MCP_PAT`. The token needs read access to repository contents. 

Each investigation starts by reading the locally learned repository map. When
knowledge is insufficient, the agent lists the repository tree, reads README and
other files, and explores likely applications and their source. It records
component responsibilities, aliases, entry points, relationships and other information 
in the learned repo map, in order to minimize the need to transfer information 
from GitHub per investigation.

Human readable notes are stored under
`.agent-source-cache/<owner>/<repo>/.knowledge/repo-map.md`, with `notes.json` as
the structured backing store. 

`REPO_GITHUB_REQUEST_LIMIT` defaults to **20 GitHub HTTP requests per
investigation**.

`REPO_CACHE_TTL_SECONDS` defaults to **86400 (24 hours)** for source validation
and note evidence. 

Cold-start orientation shares the request allowance; later tickets reuse learned
knowledge. Exploration stops early when sufficient source is found. If the budget
is exhausted, the agent can still use fresh local evidence and save notes. If
that is insufficient, it reports explored components, remaining uncertainty, and
the limit encountered. These workflow choices are prompt-guided; HTTP budgeting and 
cache validation are enforced in code.

`REPO_CACHE_ROOT` can override `.agent-source-cache`. Docker Compose mounts a named
volume at `/app/.agent-source-cache`, preserving the map and cache across container
recreation. If overriding the path in Docker, update the volume mount too. Deleting
the cache is safe, but discards learned notes and starts repository orientation over.

Run local regression checks with `python -m unittest discover -v`.

### Saving learned knowledge after a Jira reply

After all Jira response chunks have been posted, the code runs a separate
knowledge-consolidation model pass. It uses source excerpts inspected
in that investigation and relevant existing component notes. This process
consumes no additional GitHub request budget. It does make
an additional model request (or requests for large batches), with a 120-second
overall timeout. Ticket text and Lightrun runtime data are not supplied to this
pass.

The returned component notes are validated against inspected paths and commits,
then saved to `notes.json` and rendered to `repo-map.md`. This step is invoked by
code rather than depending on the investigating agent to call the update tool.
Early explicit map updates remain supported. Failed investigations also attempt
to save partial knowledge after their failure comment has been posted.

## Intentional demo shortcuts

The implementation is functional, but deliberately smaller than a production
webhook system:

- The queue and webhook-delivery IDs are kept in memory. Restarting the process
  discards queued work and delivery history.
- One worker processes one ticket at a time.
- Jira labels provide issue-level duplicate protection; there is no transactional
  database lock.
- LangGraph uses `InMemorySaver`, so an interrupted investigation starts over.
- There is no dead-letter queue, metrics service, or scheduled reconciliation.

For a real high-availability service, replace the in-memory queue with SQS or
another durable queue, persist checkpoints and delivery IDs, and run the HTTP
receiver separately from one or more workers.

## Prerequisites

You need:

- a Jira Cloud site and Jira administrator access;
- a Jira user for the agent, with permission to browse and edit the demo issues
  and add comments;
- an API token for that Jira user;
- OpenAI, Lightrun, and GitHub MCP credentials;
- an AWS account;
- a DNS name you can point to the VM, such as
  `jira-agent.example.com`.

Jira requires the webhook receiver to use HTTPS with a certificate issued by a
globally trusted certificate authority. Caddy obtains and renews that certificate
automatically once DNS and ports 80/443 are configured.

## Run and test locally

From the repository root:

```bash
uv sync
.venv/bin/python -m unittest discover -v
```

You can also verify that the demo image builds:

```bash
docker build -f Dockerfile -t jira-lightrun-agent:local .
```

To run the original polling mode, set the existing Jira and API credentials and
run:

```bash
export JIRA_TRIGGER_MODE=poll
uv run python agentic_lr_investigation.py
```

Webhook mode additionally requires:

```bash
export JIRA_TRIGGER_MODE=webhook
export JIRA_WEBHOOK_SECRET="$(openssl rand -hex 32)"
export JIRA_WEBHOOK_HOST=127.0.0.1
export JIRA_WEBHOOK_PORT=8080
uv run python agentic_lr_investigation.py
```

The local health endpoint is:

```bash
curl http://127.0.0.1:8080/health/live
```

A normal unsigned `curl` call to `/webhooks/jira` returns `401`. That is
expected: the endpoint accepts only payloads signed with
`JIRA_WEBHOOK_SECRET`.

## Build and publish the image

Build the application image on your workstation, where the source repository
is checked out. Choose an immutable version tag for each revision you intend to
deploy. Sign in to Docker Hub, then build and push from the repository root:

```bash
docker login
docker build \
  --platform linux/amd64 \
  -f Dockerfile \
  -t your-dockerhub-user/jira-lightrun-agent:0.1.0 \
  .
docker push your-dockerhub-user/jira-lightrun-agent:0.1.0
```

The example uses `linux/amd64`, which matches normal Intel/AMD EC2 instances
such as `t3.small`. Use `linux/arm64` instead when deploying to an ARM instance
such as `t4g.small`.

Using a public Docker Hub repository is simplest for this demo. If the image is
private, run `docker login` on the VM before starting Compose.

## Deploy on an AWS VM

The VM pulls the published application image from Docker Hub and runs it
alongside Caddy, which obtains and renews the HTTPS certificate. The VM receives
only three deployment configuration files and the local secrets file; the
application source and Python build environment remain on the workstation.

The commands assume Ubuntu 24.04 and the default `ubuntu` user.

### 1. Launch the EC2 instance

Create an EC2 instance with:

- Ubuntu Server 24.04 LTS;
- a `t3.small` instance for a comfortable demo baseline;
- one `gp3` EBS root volume with at least 16 GiB of storage;
- a public IPv4 address, preferably an Elastic IP so it does not change;
- the standard outbound internet access.

In **Configure storage**, use these settings:

| Setting | Choice for this demo |
| --- | --- |
| Root volume | Keep the existing root volume and select `gp3`. |
| Add new volume | No additional volume is needed. |
| File systems | Select **None**. |

Configure its security group with these inbound rules:

| Port | Source | Purpose |
| --- | --- | --- |
| 22/TCP | Your IP only | SSH administration |
| 80/TCP | Anywhere | ACME certificate issuance and HTTP redirect |
| 443/TCP | Anywhere | Jira webhook and health endpoint |

Do not expose port 8080. It exists only inside the Docker network, and Caddy is
the public entry point.

Create an `A` DNS record such as `jira-agent.example.com` pointing to the
instance's public or Elastic IP. Confirm it before continuing:

```bash
dig +short jira-agent.example.com
```

### 2. Install Docker

Connect over SSH. For this demo VM, Docker's convenience installer is the
shortest setup:

```bash
sudo apt update
sudo apt install -y curl
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo docker run --rm hello-world
```

The convenience installer is intended for development and test environments,
which matches this sample. For a longer-lived or controlled environment, follow
Docker's official Ubuntu apt-repository installation instructions instead.

### 3. Copy only the deployment configuration

On your workstation, create a small directory on the VM and copy the three
non-source deployment files into it:

```bash
ssh ubuntu@<VM_ADDRESS> 'mkdir -p /home/ubuntu/jira-lightrun-demo'
scp \
  compose.yaml \
  Caddyfile \
  jira-agent.env.example \
  ubuntu@<VM_ADDRESS>:/home/ubuntu/jira-lightrun-demo/
```

No Python files, repository checkout, Dockerfile, or build dependencies are
needed on the VM.

### 4. Configure the environment

Back on the VM:

```bash
cd /home/ubuntu/jira-lightrun-demo
cp jira-agent.env.example jira-agent.env
chmod 600 jira-agent.env
```

Generate a webhook secret:

```bash
openssl rand -hex 32
```

Edit `jira-agent.env`:

```bash
nano jira-agent.env
```

Replace every placeholder, including:

```text
WEBHOOK_DOMAIN=jira-agent.example.com
JIRA_AGENT_IMAGE=your-dockerhub-user/jira-lightrun-agent
JIRA_AGENT_TAG=0.1.0

OPENAI_API_KEY=<openai-api-key>
LIGHTRUN_API_KEY=<lightrun-api-key>
GITHUB_MCP_PAT=<github-personal-access-token>

JIRA_BASE_URL=https://your-site.atlassian.net
JIRA_EMAIL=agent-user@example.com
JIRA_API_TOKEN=<jira-api-token>

JIRA_TRIGGER_MODE=webhook
JIRA_WEBHOOK_SECRET=<the-generated-webhook-secret>
JIRA_WEBHOOK_HOST=0.0.0.0
JIRA_WEBHOOK_PORT=8080

LANGSMITH_TRACING=false
```

`WEBHOOK_DOMAIN` is just the DNS name; do not include `https://` or a path.
`JIRA_AGENT_IMAGE` is the Docker Hub repository without a tag, and
`JIRA_AGENT_TAG` is the version pushed from the workstation.
Jira and the agent must use exactly the same webhook-secret value. The local
environment file is ignored by Git and excluded from Docker builds.

### 5. Pull and start the demo

Caddy must bind host ports 80 and 443, so even if you configure rootless
Docker, run Compose with `sudo`.

```bash
sudo docker compose --env-file jira-agent.env pull
sudo docker compose --env-file jira-agent.env up -d
```

Both containers use `restart: unless-stopped`, so Docker restarts them after an
application failure or VM reboot.

Check their state and follow the agent log:

```bash
sudo docker compose --env-file jira-agent.env ps
sudo docker compose --env-file jira-agent.env logs -f agent
```

Caddy requests the certificate automatically. It can take a short time on the
first start. Confirm the public endpoint:

```bash
curl https://jira-agent.example.com/health/live
```

It should return:

```json
{"status":"ok"}
```

If certificate issuance fails, first check that the DNS record points to this
instance and that inbound ports 80 and 443 are open. Inspect Caddy with:

```bash
sudo docker compose --env-file jira-agent.env logs caddy
```

## Configure Jira Cloud

The supported path for this sample is an administrator-created Jira webhook,
not a dynamic app webhook.

1. Sign in as a Jira administrator.
2. Open Jira settings, then **System**.
3. Find **WebHooks** and choose **Create a WebHook**. Jira navigation wording
   can change; search the administration page for “webhooks” if necessary.
4. Set the name to `Lightrun investigation demo`.
5. Set the URL to:

   ```text
   https://jira-agent.example.com/webhooks/jira
   ```

6. Add a narrow JQL filter. For example:

   ```text
   project = DEMO AND issuetype = Task AND summary ~ "\"Agent\""
   ```

   Jira text matching is not exact, so the Python worker checks that the summary
   is exactly `Agent` before processing. You can omit the JQL filter while first
   troubleshooting, but that sends more issue data to the endpoint.

7. Under issue events, select only **created** (`jira:issue_created`).
8. Make sure the webhook body is included.
9. Set the webhook secret to exactly the value in
   `JIRA_WEBHOOK_SECRET`.
10. Enable and save the webhook.

The webhook secret cannot generally be retrieved later. If it is lost, generate
a new one and update both Jira and `jira-agent.env`, then recreate the agent
container from `/home/ubuntu/jira-lightrun-demo`:

```bash
sudo docker compose --env-file jira-agent.env up -d --force-recreate agent
```

Jira signs the raw request body in the `X-Hub-Signature` header. The receiver
validates that signature before reading or queuing the issue.

## Run the demo

Create a Jira issue with all of these values at creation time:

- Project: the project in the webhook JQL, such as `DEMO`;
- Issue type: `Task`;
- Summary: `Agent`;
- Assignee: the user identified by `JIRA_EMAIL`;
- Description: the natural-language investigation request.

Expected sequence:

1. Jira sends the webhook.
2. The service logs `Accepted Jira webhook for issue ...`.
3. The worker adds `ai-agent-processing` and comments
   `Agent processing started.`
4. The agent investigates using GitHub and Lightrun.
5. The response is posted as one or more Jira comments.
6. The worker replaces the processing label with `ai-agent-completed`.

Watch the service while demonstrating:

```bash
sudo docker compose --env-file jira-agent.env logs -f agent
```

## Updating the VM

Build and push the new version from the repository on your workstation:

```bash
docker build \
  --platform linux/amd64 \
  -f Dockerfile \
  -t your-dockerhub-user/jira-lightrun-agent:0.2.0 \
  .
docker push your-dockerhub-user/jira-lightrun-agent:0.2.0
```

On the VM, update `JIRA_AGENT_TAG` in `jira-agent.env`, then pull and recreate
the agent:

```bash
cd /home/ubuntu/jira-lightrun-demo
nano jira-agent.env
sudo docker compose --env-file jira-agent.env pull agent
sudo docker compose --env-file jira-agent.env up -d agent
sudo docker compose --env-file jira-agent.env ps
```

Compose preserves the Caddy certificate data in named Docker volumes.
The old application image remains available for a quick rollback: restore its
tag in `jira-agent.env` and run the same `up -d agent` command.

## Troubleshooting

### Log timestamps and GitHub discovery failures

Application progress, model/tool timing, Uvicorn lifecycle messages, and HTTP
access logs (including health checks) include timestamps. GitHub requests log
their endpoint, HTTP status, and GitHub request ID without request headers or
response bodies. A tool-level GitHub failure also includes its endpoint.

`GITHUB_MCP_PAT` is used as a Bearer token for REST, as it already was for source
downloads. A 404 alone does not identify the cause: GitHub can mask private-repo
permission failures as 404, and missing refs/paths/objects also return 404.

After deploying the updated image, run these read-only checks inside the agent
container to use exactly its environment and token:

```bash
sudo docker compose --env-file jira-agent.env exec agent python diagnose_github.py
```

The check separates repository visibility, configured ref (`main` by default),
and tree discovery. It prints no token or source content and does not contact
Jira or Lightrun. If visibility fails, verify the repository name, PAT access to
that repository, expiry, and organization approval/SSO. Fine-grained PATs need
repository Contents read permission. If visibility succeeds but ref resolution
fails, check the configured branch. See
[GitHub's REST troubleshooting guide](https://docs.github.com/en/rest/using-the-rest-api/troubleshooting-the-rest-api).

### Jira never reaches the server

Check:

```bash
curl https://jira-agent.example.com/health/live
sudo docker compose --env-file jira-agent.env ps
sudo docker compose --env-file jira-agent.env logs --tail=100 caddy
sudo docker compose --env-file jira-agent.env logs --tail=100 agent
```

Verify DNS and the EC2 security-group rules. Jira requires trusted HTTPS; a
self-signed certificate is not sufficient.

### Jira receives `401 Invalid Jira webhook signature`

The secret in Jira and `JIRA_WEBHOOK_SECRET` differ, or the Jira webhook was
created without signing enabled. Update both sides with the same new secret and
restart the service.

### The webhook is accepted but the issue is ignored

The service deliberately refetches and validates the ticket. Confirm that it is
a `Task`, has summary exactly `Agent`, is assigned to the configured Jira user,
and does not already have `ai-agent-processing` or `ai-agent-completed`.

### A failed demo ticket will not run again

Failures receive `ai-agent-failed`. That label alone does not block a retry, but
`ai-agent-processing` or `ai-agent-completed` does. Remove the blocking label,
then create a new demo ticket. Webhook mode reacts only to creation, so editing
the old ticket does not emit a supported trigger.

### The VM restarted while a ticket was queued

The in-memory queue is intentionally not durable. Remove any stale
`ai-agent-processing` label and create a fresh demo ticket. For production,
replace the queue with SQS and add a reconciliation mechanism.

## Reference documentation

- [Atlassian Jira Cloud webhooks](https://developer.atlassian.com/cloud/jira/platform/webhooks/)
- [AWS EC2 security groups](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/creating-security-group.html)
- [Install Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker Compose](https://docs.docker.com/compose/)
- [Docker restart policies](https://docs.docker.com/engine/containers/start-containers-automatically/)
- [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)
