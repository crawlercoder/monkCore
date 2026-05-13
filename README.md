# ai-agent-service

Production-ready FastAPI backend for an async AI agent system (Bedrock + DynamoDB + EventBridge).

## Layout

```
app/
  main.py              FastAPI app factory, lifespan, middleware wiring
  config.py            pydantic-settings Settings + cached get_settings()
  logging.py           structured JSON / console logging with request-id context
  errors.py            AppError hierarchy + JSON error envelope handlers
  middleware.py        request-id + access-log middleware
  api/
    router.py          aggregates versioned routers under /api
    deps.py            reusable FastAPI dependencies
    v1/
      health.py        /health (liveness) and /ready (readiness)
  services/
    readiness.py       registry of async probes surfaced via /ready
  agents/base.py       abstract Agent[InputT, OutputT]
  rag/base.py          Retriever interface + RetrievedChunk
  db/session.py        init_db / close_db lifecycle hooks
  workers/base.py      EventHandler + Event envelope
  models/health.py     health/readiness response schemas
requirements.txt
.env.example           template for env vars
.env                   local defaults (never commit real secrets)
```

## Run

### Backend (FastAPI)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then edit as needed
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- API base: `http://127.0.0.1:8000` — `GET /docs` is Swagger when not in production.

### Next.js UI (`web/` — create job, job status)

```bash
cd web && npm install && npm run dev
```

- App: `http://127.0.0.1:9000` — `/`, `/job/...`, org/repo routes. Proxies `/api/*` to the API (`API_PROXY_TARGET`, default `http://127.0.0.1:8000`).

### Optional: Vite UI (`frontend/` — org/repo setup)

```bash
cd frontend && npm install && npm run dev
```

- Dev server: `http://127.0.0.1:9000` (proxies `/api` to port 8000).

### API + Next.js together (one command)

From the repo root (after `pip install` and `cd web && npm install` once):

```bash
./scripts/dev-stack.sh
```

Same as: `bash scripts/dev-stack.sh`. Stops both on Ctrl+C.

### API + Next.js together in the background

```bash
./scripts/dev-stack-bg.sh            # start (detached)
./scripts/dev-stack-bg.sh status     # show PIDs / URLs
./scripts/dev-stack-bg.sh logs       # tail -F both logs
./scripts/dev-stack-bg.sh stop       # stop both
./scripts/dev-stack-bg.sh restart
```

Logs and PID files live under `.dev-stack/` (git-ignored). API on
`http://127.0.0.1:8000`, Web on `http://127.0.0.1:9000`. Override ports with
`API_PORT`, `WEB_PORT`, or `API_PROXY_TARGET` env vars.

- `GET /health` — liveness
- `GET /ready` — readiness, runs all registered probes
- `GET /api/v1/health` — versioned copy
- `GET /docs` — Swagger UI (disabled automatically in staging/prod)

## AWS on your laptop

**Using your account (Bedrock, DynamoDB, Secrets Manager)** — pick one or combine:

1. **AWS CLI profiles** (good default): install the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), then run `aws configure` (or `aws configure sso`) and set `AWS_PROFILE` in `.env` if you do not use the default profile.
2. **Environment variables**: set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and optionally `AWS_SESSION_TOKEN` for temporary credentials. Optional: `AWS_REGION` or `AWS_DEFAULT_REGION` (this app also reads `AWS_REGION` via settings).
3. **Bedrock API keys**: optional `AWS_BEARER_TOKEN_BEDROCK` for API-key auth to `bedrock-runtime` (see comments in `.env.example`).

**No AWS credentials on the machine** — with `ENVIRONMENT=local`, when `LOCAL_AWS_AUTOFALLBACK=true` (default) and boto3 cannot resolve credentials, the app switches to in-memory jobs/orgs/repos, inline secrets, and local GitLab token files under `WORKSPACE_ROOT/.local-gitlab-tokens`. Data in memory is lost when the process exits. Set `LOCAL_AWS_AUTOFALLBACK=false` to fail fast instead. If you use **DynamoDB Local** (`DYNAMODB_ENDPOINT_URL`), persistence stays on DynamoDB; set dummy `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` as required by your local server. **LocalStack**: set `AWS_ENDPOINT_URL` / `DYNAMODB_ENDPOINT_URL` and the same dummy key pattern as in the LocalStack docs.

**Paths on macOS** — set `BASE_STORAGE_PATH` and `WORKSPACE_ROOT` to writable directories (for example `./.local-ai-agent` and `./.local-workspace`); system paths like `/ai-agent` are often not writable locally.

## Adding a new endpoint

1. Create `app/api/v1/<feature>.py` with an `APIRouter`.
2. Include it in `app/api/v1/__init__.py`.
3. Put request/response models in `app/models/<feature>.py`.
4. Put business logic in `app/services/<feature>.py`.
5. If it talks to an LLM, put the prompt + client in `app/agents/<feature>.py`.
6. If it reads from the codebase index, use a `Retriever` from `app/rag/`.

The API layer stays thin: validate, call a service, return. Side effects
live in services or workers.

## Adding a readiness probe

```python
from app.services.readiness import register_probe

async def _bedrock_probe() -> tuple[bool, str | None]:
    ok, detail = await ping_bedrock()
    return ok, detail

register_probe("bedrock", _bedrock_probe)
```

Probes run concurrently with a 2-second timeout per probe; one slow
dependency can't stall `/ready`.

## Logging

- `LOG_FORMAT=json` (default in prod): one JSON object per line, every
  record carries `request_id`, ready to ship to CloudWatch / Datadog / Loki.
- `LOG_FORMAT=console` (local): human-readable.
- Use `from app.logging import get_logger; log = get_logger(__name__)`.
- Extra fields: `log.info("message", extra={"workflow_id": "..."})`.

## Errors

Raise any subclass of `AppError` (`NotFoundError`, `BadRequestError`,
`ConflictError`, `UpstreamError`) from services/routes. The registered
handler renders:

```json
{ "error": { "code": "...", "message": "...", "details": {...}, "request_id": "..." } }
```

Unhandled exceptions are logged with a stack trace and returned as a
generic `internal_error`; stack traces never leak to the client.

## Environments

`ENVIRONMENT=prod|staging` automatically disables `/docs`, `/redoc`, and
`/openapi.json`. All other behavior is controlled by env vars — the same
image runs locally and in prod with only config changes.
