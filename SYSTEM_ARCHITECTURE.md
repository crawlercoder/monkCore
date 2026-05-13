# `monkCore` — System Architecture

Read-only architectural reference for the AI merge-request agent.
Scope: `app/` package (62 Python files, ~15.0k LOC). Out of scope:
`.venv/`, `frontend/` (Vite SPA), `web/` (Next.js SPA). All references in this
document are to source files inside `/Users/utkarsh/PP/monkCore`.

> Uncertainty markers (**[UNSURE]**) flag claims that could not be verified
> directly from source. Everything else has a citation.

---

## 1. System Overview

### What it does

`monkCore` is an HTTP-driven, **AI agent that turns a natural-language
"spec" into a multi-repo GitLab merge request**. A user submits an org id
plus a free-form change description; the system retrieves grounding context
from the org's pre-indexed code, has Bedrock plan and write the changes,
applies the changes to local checkouts of the org's repos, commits, pushes
agent-named branches, and opens MRs against the default branch — all
asynchronously.

### Primary workflow (high level)

1. **Onboard an org** — `POST /v1/org/create` provisions an org row and
   stores its GitLab token in AWS Secrets Manager.
2. **Register a repo** — `POST /v1/repo/register` adds a `Repo` row in
   `PENDING` state under that org.
3. **Ingest the repo** (`POST /v1/repo/process/{repo_id}` or background
   trigger) — clone → scan → chunk → Bedrock embed → FAISS insert. Worker
   then schedules a fire-and-forget LLM summarisation pass.
4. **Submit a job** — `POST /v1/jobs` creates a `Job` row with the spec
   and schedules `mr_pipeline.run_job(job_id)` as an `asyncio.create_task`.
5. **`run_job`** — load job → build context (FAISS retrieval) → LLM plan
   → strict plan-only repo selection → per-repo: LLM code-gen → LLM
   tests → LLM feature-flag wrap → safe write → git checkout/commit/push
   → GitLab MR creation → optional staging deploy webhook.
6. **Poll** — `GET /v1/jobs/{job_id}` returns the latest status, MR URL,
   staging URL, and a tail of the per-job artifact log.

### Main responsibilities (by layer)

| Layer | Modules | Role |
| --- | --- | --- |
| HTTP | `app/main.py`, `app/api/`, `app/middleware.py`, `app/errors.py` | FastAPI app, request id, error envelope, CORS. |
| API v1 | `app/api/v1/{health,orgs,repos,jobs}.py` | Thin handlers; delegate to services / pipeline. |
| Services | `app/services/{registry,gitlab_tokens,secrets,git_clone,git_service,readiness}.py` | Business façades, Secrets Manager, git auth, health probes. |
| Persistence | `app/db/{dynamodb,orgs,repos,memory_store,session}.py` | DynamoDB repositories; in-process `memory_store` when `PERSIST_BACKEND=memory` (explicit or via `LOCAL_AWS_AUTOFALLBACK` on a laptop without credentials). |
| Workers | `app/workers/repo_ingest.py` | Repo clone + index lifecycle (PENDING → CLONING → READY). |
| RAG | `app/repo_scanner.py`, `app/code_chunker.py`, `app/embedding_service.py`, `app/vector_store.py`, `app/retrieval_service.py`, `app/context_builder.py`, `app/ingestion_pipeline.py`, `app/knowledge_map.py` | Code → chunks → vectors on disk; query path. |
| LLM | `app/bedrock_runtime.py`, `app/code_understanding.py`, `app/planning_engine.py`, `app/code_generation.py`, `app/test_generator.py`, `app/feature_flag.py`, `app/summary_worker.py`, `app/onboarding/repo_manifest.py` | Bedrock InvokeModel orchestration with JSON-only outputs. |
| MR pipeline | `app/mr_pipeline.py` | Spec → plan → code → tests → flag → git → MR → deploy. |
| Storage | `app/storage_manager.py`, `app/repo_initializer.py`, `app/repo_sync.py`, `app/artifact_manager.py` | Filesystem layout for clones and per-job artifacts. |
| Cross-cutting | `app/config.py`, `app/aws_local.py`, `app/logging.py`, `app/models/` | Settings (`get_settings`), local boto3 credential probe, structured logging, public Pydantic schemas. |

### External integrations

* **AWS credential chain** — DynamoDB clients, Secrets Manager, and SES use
  ordinary **boto3** resolution (`AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` / optional `AWS_SESSION_TOKEN`, shared `~/.aws/` files,
  `AWS_PROFILE` / SSO, instance/container roles). Probed at settings load via
  `app/aws_local.py:boto_session_has_resolved_credentials()` for optional local
  fallbacks described below (not applied in staging/prod).
* **AWS Bedrock Runtime** (`bedrock-runtime`) — text generation (Anthropic
  Claude / Meta Llama / Amazon Titan via per-family payload dispatch) and
  embeddings (Cohere `embed-*`, Titan `*embed*`). One module-cached client in
  `app/bedrock_runtime.py:get_bedrock_runtime`.
  **`AWS_BEARER_TOKEN_BEDROCK`** / `AWSSettings.bearer_token_bedrock` can be used
  for Bedrock API key auth alongside or instead of SigV4; LLM/embed paths still
  require credentials or this token — they are **not** disabled when persistence
  falls back to memory.
* **AWS Secrets Manager** — per-org GitLab token (`{"gitlab_token": ...}`),
  via `app/services/secrets.py:SecretsManagerClient` with TTL cache + per-key
  locks. Local-dev fallback writes to `<workspace_root>/.local-gitlab-tokens/`
  when `gitlab_token_storage=local`. Explicit `SECRETS_BACKEND=inline` uses
  `LOCAL_SECRETS_FILE`; see `app/config.py:SecretsBackend`.
* **AWS DynamoDB** — three tables: `jobs`, `orgs`, `repos`
  (`app/db/dynamodb.py`, `app/db/orgs.py`, `app/db/repos.py`). `repos` has a
  `org_id-index` GSI on `(org_id, created_at)`. `PERSIST_BACKEND=memory`
  swaps the entire layer for `app/db/memory_store.py`. **`DYNAMODB_ENDPOINT_URL`**
  targets DynamoDB Local or similar while keeping `persist_backend=dynamodb`; pair
  with dummy static AWS keys per local-server docs.
* **Local laptop autoconfig (`ENVIRONMENT=local`)** — when
  **`LOCAL_AWS_AUTOFALLBACK=true`** (default) and boto3 resolves **no** credentials,
  `Settings._apply_local_aws_autofallback` (in `app/config.py`) may switch:
  **`PERSIST_BACKEND=memory`** (unless `DYNAMODB_ENDPOINT_URL` is set),
  **`SECRETS_BACKEND=inline`**, and **`GITLAB_TOKEN_STORAGE=local`** so the HTTP
  API can start without AWS. Set **`LOCAL_AWS_AUTOFALLBACK=false`** to opt out.
  Operational detail: `README.md` and `.env.example` document laptops paths
  (`BASE_STORAGE_PATH`, `WORKSPACE_ROOT`).
* **GitLab REST v4** — `_create_merge_request` POSTs to
  `<gitlab_api_v4_url>/projects/<encoded_path>/merge_requests` with the org's
  `PRIVATE-TOKEN`. Auth on git push is `oauth2:<token>@host` injected
  per-call (never persisted to `.git/config`).
* **Git CLI** — direct `subprocess.run(["git", ...])` calls in
  `mr_pipeline._git`, `repo_sync._run_git`, and `services/git_clone._run_git`.
* **FAISS** (`faiss-cpu` `IndexFlatL2`) — per-org vector store on disk at
  `<VECTOR_STORE_ROOT>/<org_id>/{vectors.faiss, metadata.json, .lock}`
  guarded by `filelock.FileLock`.
* **Optional deploy webhook** — `_maybe_deploy` POSTs a JSON payload to
  `settings.mr_deploy_webhook_url` after a successful run.

---

## 2. End-to-End Request Flow

The complete happy-path trace from `POST /v1/jobs` to a published MR.

### Step 1 — HTTP entry: `POST /v1/jobs`

* **File**: `app/api/v1/jobs.py:create_job` (lines 137–225)
* **Inputs**: `JobCreateRequest{org_id, spec}` (Pydantic).
* **Outputs**: HTTP 201 `JobCreateResponse{job_id, status: "CREATED"}`.
* **Side effects**:
  1. `RegistryService.get_org(org_id)` — fail-fast 404.
  2. `JobsRepository.create_job({org_id, spec, status: "CREATED"})` — DynamoDB put.
  3. `init_job_artifacts(job_id)` (best-effort, threadpool).
  4. `_schedule_pipeline(job_id)` → `asyncio.create_task(run_job(job_id))`
     held in `_pipeline_tasks` for cancellation.

### Step 2 — Job loaded and PROCESSING flip

* **File**: `app/mr_pipeline.py:run_job` (lines ~1259–1397).
* `load_job_step(job_id)` → `JobsRepository.get_job` (with retries).
  Raises `JobNotFoundError` if absent.
* Cancel-pre-check: if stored status is already `cancelled` → early
  return.
* `initialize_job_step(job_id)` → `init_job_artifacts` (creates
  `<artifacts>/{job_id}/{logs,diffs,plan,questions,summary}/`) + re-fetches
  the row to detect a cancellation race.
* `_set_status(jobs, job_id, "running")` — flips the row.

### Step 3 — Context retrieval (RAG)

* **File**: `app/mr_pipeline.py:build_context_step` calling
  `app/context_builder.py:build_context(org_id, spec)`.
* **Inputs**: `org_id`, `spec`.
* **Flow**:
  1. `query = spec.strip()`. **Single source of truth**: only the spec is
     embedded; no paraphrase, no second hidden query.
  2. `app/retrieval_service.py:search_code(org_id, query, top_k=12,
     min_score=0.25)` → embeds via Bedrock + FAISS search.
  3. Filters in `build_context`: drop empty / `<20`-char / duplicate
     snippets; cap to **5** items; truncate each `code` to **2000** chars.
* **Outputs**: `{"spec": str, "context": [{repo_id, file, code}, …]}`.
* **Side effects**: structured log `context_builder.retrieval` with
  drop-reason counters; no DB writes.

### Step 4 — Planning (Bedrock LLM)

* **File**: `app/mr_pipeline.py:generate_plan_step` →
  `app/planning_engine.py:generate_plan(spec, context)`.
* **Inputs**: `spec` (string) — primary; `context` dict (RAG bundle).
* **Flow**:
  1. Build user prompt: `## Spec` block followed by `## Supporting context`
     (with `spec` stripped from the JSON to avoid double-feeding).
  2. `_invoke_planning_llm(_SYSTEM, user, model_id=...)` →
     family dispatcher (`_dispatch_flex`) over Anthropic / Titan / Llama
     payload shapes, with `_RETRIABLE` Bedrock error retries.
  3. `_parse_plan_json(text)` = `_first_json_object(_strip_code_fences(text))`
     + `json.loads`. Up to `_PARSE_RETRIES=2` with `_STRICT_FOLLOW` reminder.
  4. `_coerce_plan(data)` → normalized `{"repos": [...], "tasks": [...],
     "feature_flag": {required, flag_name}}`.
* **Outputs**: plan dict + counts.
* **Side effects**: `save_plan(job_id, plan)` writes `plan/plan.json` (best-
  effort).

### Step 5 — Strict repo selection

* **File**: `app/mr_pipeline.py:_collect_repo_ids` (called from
  `process_repositories_step`).
* **Inputs**: `plan`, `org_repos = RegistryService.list_repos_by_org(org_id)`.
* **Flow**: union of `plan["tasks"][*].repo_id` and `plan["repos"][*].repo_id`,
  intersect with org registry, sort. **No fallback** — if the resulting set
  is empty, `process_repositories_step` raises `ValueError("No repositories
  selected from plan")` and the job fails.
* **Outputs**: deterministic `list[str]` of repo ids.
* **Side effects**: `mr_pipeline.repo_resolution` log with `valid_repo_ids`,
  `rejected_repo_ids`, counts.

### Step 6 — Per-repo loop (`_process_one_repo`)

For each repo id, in order:

#### 6a — Generate code, tests, feature flag

* **File**: `app/mr_pipeline.py:_prepare_repo_artifacts_sync` runs in
  `asyncio.to_thread`.
* `app/code_generation.py:generate_repo_changes(spec, plan, context, repo_id)`
  → LLM produces `{"changes": [{file, change_type:"modify"|"create", code}]}`.
* `app/test_generator.py:generate_tests(repo_id, raw_list)` — only if
  there are code changes; produces `{"tests": [{file, code}]}`.
* `app/feature_flag.py:add_feature_flag(plan, raw_list)` — when
  `plan["feature_flag"]["required"] is True`, the LLM rewrites the change
  list to wire the flag (boolean, default OFF) and appends a config file.
* Result: `to_write = [{file, code, change_type}]`.

#### 6b — Branch checkout, write, commit

* `_checkout_new_branch(root, branch, target_branch, token, repo_url)` —
  `git fetch` (auth-injected URL) + `git checkout -B <agent-branch>
  origin/<target>`; degrades to local HEAD on fetch failure.
* `_apply_changes_list(root, to_write)` →
  **`_safe_write_under_root(root, rel, code, change_type=...)`** with:
  - Path-traversal guard.
  - Reads existing file (UTF-8) when present.
  - Rejects writes where new size > **1.5×** old size.
  - For `change_type=="modify"`, computes
    `difflib.SequenceMatcher.ratio()`; rejects when `< 0.4`.
  - Writes atomically with `newline="\n"`.
* `_commit_if_dirty(root, job_id, paths)` — `git add` each path, then
  `git -c user.name=ai-mr-pipeline -c user.email=ai-mr+<jobhead>@local.invalid
  commit -m "feat: agent update [job <job_id>]"`.

#### 6c — Push and MR

* `_git_push(root, branch, token, repo_url)` — push via `oauth2:<token>@`
  URL, `HEAD:refs/heads/<agent-branch>`. Wrapped in `_retry_async`.
* `_create_merge_request(settings, project_path=..., token=..., source=...,
  target=..., title=spec_title, description=...)` — POST to
  `gitlab_api_v4_url`. Failures captured into `mr_error` on the per-repo
  result dict (so push-without-MR is detected later).

#### 6d — Per-repo result + error tracking

* Returns `{repo_id, branch, target_branch, files_changed, mr_url,
  staging_url, mr_error?}`.
* The loop in `process_repositories_step` catches per-repo `Exception`,
  appends `{repo_id, error}` to `errors`, and continues. `errors`
  non-empty after the loop → `RepositoryProcessingError(errors, results)`.

### Step 7 — Finalisation

* If success: `finalize_success_step`
  - Aggregates first non-empty `mr_url` / `staging_url`.
  - **Push-without-MR guard**: if any `mr_error` and no MR URL surfaced,
    raises `ValueError("merge request could not be created: ...")`.
  - `_maybe_deploy(settings, job_id, org_id, plan)` — POSTs the deploy
    webhook (best-effort, retries; never fails the job).
  - `JobsRepository.update_job(job_id, {"status":"succeeded","mr_url":...,
    "staging_url":...})`.
  - `save_summary(job_id, {...})` — `summary/summary.json`.
* If `RepositoryProcessingError` (or any other `Exception`):
  `finalize_failure_step` writes failure summary including `repo_errors`
  and partial `mrs`, then `_set_status(failed)` and re-raises. The
  `asyncio.create_task` `_done` callback in `app/api/v1/jobs.py` logs the
  failure but does no recovery.

### Step 8 — Polling

* `GET /v1/jobs/{job_id}` (`app/api/v1/jobs.py:get_job`) merges the row,
  the `summary/summary.json`, and a tail of `logs/*` into the response.

---

## 3. Repository Structure

```
monkCore/
├── app/                               # Python package — service code
│   ├── __init__.py                    # __version__ = "0.1.0"
│   ├── main.py                        # FastAPI app factory + uvicorn entrypoint
│   ├── middleware.py                  # x-request-id + access logs
│   ├── errors.py                      # AppError + handlers
│   ├── logging.py                     # structured logs, contextvars, log_event
│   ├── config.py                      # Settings (Pydantic) — grouped BaseSettings + local AWS fallback
│   ├── aws_local.py                   # boto3 credential probe; DynamoDB-local signal for fallback
│   ├── bedrock_runtime.py             # singleton boto3 bedrock-runtime client
│   ├── mr_pipeline.py                 # run_job — the MR pipeline orchestrator
│   ├── context_builder.py             # spec → RAG bundle (filtered)
│   ├── planning_engine.py             # LLM plan generator
│   ├── code_generation.py             # LLM per-repo code changes
│   ├── test_generator.py              # LLM tests for changes
│   ├── feature_flag.py                # LLM feature-flag rewrite
│   ├── code_understanding.py          # LLM chunk summariser + JSON helpers
│   ├── summary_worker.py              # post-ingest summary fan-out
│   ├── retrieval_service.py           # search_code: embed + FAISS query
│   ├── embedding_service.py           # Bedrock embeddings (Titan / Cohere)
│   ├── vector_store.py                # FAISS index + metadata sidecar
│   ├── code_chunker.py                # source → ranged chunks per language
│   ├── repo_scanner.py                # walk a clone, return code files
│   ├── ingestion_pipeline.py          # scan → chunk → embed → FAISS insert
│   ├── knowledge_map.py               # org-level dependency / API map
│   ├── repo_initializer.py            # `.agent/metadata.json` writer
│   ├── repo_sync.py                   # fetch + ff-only update of a checkout
│   ├── storage_manager.py             # /<root>/{repos,artifacts,cache,tmp}
│   ├── artifact_manager.py            # per-job artifact directory layout
│   ├── api/
│   │   ├── __init__.py                # api_router re-export
│   │   ├── deps.py                    # get_settings / get_registry_service
│   │   ├── router.py                  # v1 + alias mount
│   │   └── v1/
│   │       ├── __init__.py            # v1_router (health/orgs/repos/jobs)
│   │       ├── health.py              # /health, /ready
│   │       ├── orgs.py                # /v1/org/create, /v1/orgs/...
│   │       ├── repos.py               # /v1/repo/register, /v1/repos/...
│   │       └── jobs.py                # /v1/jobs (POST/GET/cancel)
│   ├── db/
│   │   ├── __init__.py                # public re-exports
│   │   ├── dynamodb.py                # JobsRepository + shared primitives
│   │   ├── orgs.py                    # OrgsRepository
│   │   ├── repos.py                   # ReposRepository (+ org_id GSI)
│   │   ├── memory_store.py            # in-process fallback
│   │   └── session.py                 # init_db / close_db lifecycle
│   ├── models/
│   │   ├── __init__.py
│   │   ├── jobs.py                    # JobId, SpecText, Job, JobStatus
│   │   ├── orgs.py                    # OrgId, OrgName, SecretName, Org
│   │   ├── repos.py                   # RepoId, RepoUrl, BranchName, RepoStatus, Repo
│   │   └── health.py                  # HealthResponse, ReadinessResponse
│   ├── services/
│   │   ├── __init__.py                # public re-exports
│   │   ├── registry.py                # RegistryService (orgs + repos + tokens)
│   │   ├── gitlab_tokens.py           # GitlabTokensService
│   │   ├── secrets.py                 # SecretsManagerClient (TTL cache)
│   │   ├── git_clone.py               # GitCloneService (+ error taxonomy)
│   │   ├── git_service.py             # high-level clone-or-update wrapper
│   │   └── readiness.py               # /ready probe registry
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── base.py                    # Event + EventHandler ABCs (unused)
│   │   └── repo_ingest.py             # claim → clone → ingest → READY
│   ├── onboarding/
│   │   ├── __init__.py                # generate_repo_manifest re-export
│   │   └── repo_manifest.py           # LLM repo manifest from README + tree
│   ├── agents/                        # placeholder ABC; not subclassed
│   │   ├── __init__.py
│   │   └── base.py
│   └── rag/                           # placeholder Retriever ABC; unused
│       ├── __init__.py
│       └── base.py
├── frontend/                          # Vite SPA (separate)
├── web/                               # Next.js SPA (separate)
├── scripts/
│   ├── test_phase1.py                 # Phase 1 smoke (clone + ingest + search)
│   ├── test_phase2.py                 # Phase 2 smoke (summary worker)
│   ├── test_storage.py                # storage / artifact smoke
│   ├── dev-stack.sh / dev-stack-bg.sh # local dev orchestration
│   ├── nginx/, systemd/               # deployment configs
├── repos/                             # gitignored runtime clone target
├── requirements.txt                   # Python deps
├── README.md                          # canonical project docs
├── cleanup_report.md                  # generated maintainability report
├── SYSTEM_ARCHITECTURE.md             # this document
├── .env / .env.example                # local config
└── .venv/                             # local virtualenv
```

### Key folder notes

* **`app/db/`** — three sibling DynamoDB modules; all share helpers
  (`get_resource`, `now_iso`, `build_update_expression`, `wrap_client_error`,
  `table_is_present`, `probe_table_reachable`) defined in `dynamodb.py`.
* **`app/services/`** — composition layer. `registry.py` is the only
  module the API layer talks to for orgs/repos.
* **`app/api/v1/`** — every handler is thin: validate → call service →
  map errors via `app/errors.py:AppError` subclasses.
* **`app/agents/` and `app/rag/`** — abstract bases referenced only in
  `README.md`; no concrete subclass exists in this repo.

### Configuration and local AWS

* **Singleton** — `app/config.py:get_settings()` (LRU cached) constructs
  `Settings` from env vars and `.env` (pydantic-settings).
* **Backends** — `persist_backend` (`dynamodb` | `memory`),
  `secrets_backend` (`secrets_manager` | `inline`),
  `gitlab_token_storage` (`secrets_manager` | `local`).
  Restrictions: `memory`, `inline`, and `gitlab_token_storage=local` require
  `environment=local` (validated on the model).
* **`LOCAL_AWS_AUTOFALLBACK`** — Root-level boolean (env `LOCAL_AWS_AUTOFALLBACK`).
  On `environment=local` with default `True`, absence of boto3 credentials
  triggers `_apply_local_aws_autofallback`: may set memory persistence unless
  `dynamodb.endpoint_url` is set (DynamoDB Local), forces inline secrets and local
  GitLab token files, and emits a **`logging`** warning naming the applied knobs.
  `app/aws_local.py:skip_local_dynamodb_memory_fallback` distinguishes that case.
  This does **not** stub Bedrock; ingestion, summarisation, and MR LLM stages
  remain AWS-dependent unless real credentials exist.

---

## 4. Entry Points

### FastAPI process

* **File**: `app/main.py:create_app` (line 30).
* **Mounts**: `health_router` at `/`; `api_router` at `/api`. The
  versioned router (`v1_router`) is mounted under `/v1`. `jobs.router` is
  also mounted at `/api` (outside `/v1`) so `POST /api/jobs` works
  alongside `POST /api/v1/jobs` (excluded from OpenAPI to avoid duplicate
  ops). Source: `app/api/router.py:13–23`.
* **Middleware**: `RequestContextMiddleware` (request id + access log)
  then CORS.
* **Lifespan**: `init_db(settings)` → in `environment=LOCAL`, tries to ensure
  DynamoDB tables exist (skipped when `persist_backend=memory`); registers jobs/orgs/repos
  readiness probes (memory backends report healthy via `memory_store`).
  `close_db()` is a no-op.
* **`__main__`**: `uvicorn.run("app.main:app", host=..., port=...,
  reload=settings.debug)` (line 108).

### HTTP routes (entry points for external traffic)

| Route | Handler | File |
| --- | --- | --- |
| `GET /health` | `app/api/v1/health.py:health` | health.py:31 |
| `GET /ready` | `app/api/v1/health.py:ready` | health.py:46 |
| `POST /v1/org/create` | `app/api/v1/orgs.py:create_org` | orgs.py:126 |
| `PUT /v1/orgs/{org_id}/token` | `orgs.py:update_org_token` | orgs.py:191 |
| `GET /v1/orgs` | `orgs.py:list_orgs` | orgs.py:245 |
| `GET /v1/orgs/{org_id}` | `orgs.py:get_org` | orgs.py:270 |
| `GET /v1/orgs/{org_id}/repos` | `orgs.py:list_repos_for_org` | orgs.py:303 |
| `GET /v1/orgs/{org_id}/jobs` | `orgs.py:list_jobs_for_org` | orgs.py:350 |
| `POST /v1/repo/register` | `app/api/v1/repos.py:register_repo` | repos.py:101 |
| `GET /v1/repos/{repo_id}` | `repos.py:get_repo` | repos.py:163 |
| `POST /v1/repo/process/{repo_id}` | `repos.py:process_repo_endpoint` | repos.py:195 |
| `POST /v1/jobs` | `app/api/v1/jobs.py:create_job` | jobs.py:149 |
| `POST /v1/jobs/{job_id}/cancel` | `jobs.py:cancel_job` | jobs.py:253 |
| `GET /v1/jobs/{job_id}` | `jobs.py:get_job` | jobs.py:322 |
| `POST /api/jobs` | `jobs.py:create_job` (alias) | jobs.py:149 |
| `GET /api/jobs/{job_id}` | `jobs.py:get_job` (alias) | jobs.py:322 |

### Background async tasks

* **`run_job(job_id)`** — `mr_pipeline.run_job`, scheduled via
  `asyncio.create_task` inside `app/api/v1/jobs.py:_schedule_pipeline`
  (line 98). References held in `_pipeline_tasks` for cancel.
* **`process_repo(repo_id)`** — `app/workers/repo_ingest.py:process_repo`
  (line 604). Triggered synchronously from `POST /v1/repo/process/...`
  (it is the "manual operator trigger"). **No event-driven scheduler
  exists in this repo today**; the README docstring on `repo_ingest.py`
  speaks of "EventBridge / SQS / cron" but those are external concerns.
  **[UNSURE]** whether production wires another invocation path.
* **Post-ingest summarisation** — `repo_ingest._process_loaded` schedules
  `summary_worker.process_unsummarized_chunks(org_id)` via
  `asyncio.to_thread(...)` and stores the task in `_BG_TASKS` to keep a
  strong reference (`app/workers/repo_ingest.py:93`).

### Worker / CLI entry points

* `app/mr_pipeline.py:run_job_sync(job_id)` — blocking wrapper for
  scripts.
* `scripts/test_phase1.py`, `scripts/test_phase2.py`,
  `scripts/test_storage.py` — manual smoke tests.

---

## 5. API Layer

### Pattern

Handlers are intentionally thin (`app/api/v1/jobs.py:11`). They:

1. Parse Pydantic request → known typed values.
2. Call into `RegistryService` / `JobsRepository` / `mr_pipeline.run_job`.
3. Translate domain errors into `AppError` subclasses
   (`BadRequestError 400`, `NotFoundError 404`, `ConflictError 409`,
   `UpstreamError 502/504`). The handlers in `app/errors.py` render the
   shared envelope `{"error": {"code","message","details","request_id"}}`.
4. Emit `log_event` with the request bound to `request_id` / `job_id` /
   `org_id` / `repo_id` ContextVars.

### Endpoint detail

| Endpoint | Inbound model | Service call | Side effects | Downstream |
| --- | --- | --- | --- | --- |
| `POST /v1/org/create` | `OrgCreateRequest{name, gitlab_token}` | `RegistryService.create_or_get_org` | Secret put, then DynamoDB put (in that order so "row implies secret") | `GitlabTokensService.put_secret_direct`, `OrgsRepository.create`/`find_by_name` |
| `PUT /v1/orgs/{org_id}/token` | `OrgTokenUpdateRequest` | `RegistryService.update_org_token` | Secret rotate + cache invalidate | Secrets Manager |
| `GET /v1/orgs` | — | `RegistryService.list_orgs` | DynamoDB Scan (bounded) | OrgsRepository |
| `GET /v1/orgs/{org_id}` | — | `RegistryService.get_org` | DynamoDB GetItem | OrgsRepository |
| `GET /v1/orgs/{org_id}/repos` | — | `RegistryService.list_repos_by_org` (after org-existence 404) | DynamoDB Query on GSI | ReposRepository |
| `GET /v1/orgs/{org_id}/jobs` | — | `JobsRepository.list_jobs_by_org` | DynamoDB Scan with filter | JobsRepository |
| `POST /v1/repo/register` | `RepoRegisterRequest{repo_url, org_id, branch}` | `RegistryService.register_or_get_repo` | DynamoDB put (status=PENDING) | OrgsRepository.get + ReposRepository.find_by_org_and_url / create |
| `GET /v1/repos/{repo_id}` | — | `RegistryService.get_repo` | DynamoDB GetItem | ReposRepository |
| `POST /v1/repo/process/{repo_id}` | — | `process_repo` (worker) | Clone, ingest, summarise (fire-and-forget) | git, FAISS, Bedrock |
| `POST /v1/jobs` | `JobCreateRequest{org_id, spec}` | `JobsRepository.create_job` + `_schedule_pipeline` | DynamoDB put + asyncio task | run_job |
| `POST /v1/jobs/{job_id}/cancel` | — | `JobsRepository.update_job(status="cancelled")` then `Task.cancel()` | DB write + same-process task cancel | — |
| `GET /v1/jobs/{job_id}` | — | `JobsRepository.get_job` + `read_summary` + `read_logs` | filesystem read of artifacts | — |
| `GET /health` | — | `time.monotonic` | — | — |
| `GET /ready` | — | `services/readiness.run_readiness_checks` | — | DynamoDB pings |

---

## 6. Job Pipeline Architecture (`run_job`)

### Sequence (textual)

```
[client]
   │  POST /v1/jobs (org_id, spec)
   ▼
[api/v1/jobs.create_job] ─── DynamoDB put (status=CREATED) ───►  jobs table
   │
   │  asyncio.create_task(run_job(job_id))
   ▼
[mr_pipeline.run_job]                                           ┌──────────────┐
   ├─► load_job_step(job_id)         ─── get_job ─────────────► │  jobs table  │
   ├─► (cancel-pre-check)             ─── get_job ─────────────► │  jobs table  │
   ├─► initialize_job_step            ─── init_job_artifacts ──► artifacts/<id>
   ├─► _set_status("running")         ─── update_job ──────────► jobs table
   ├─► (validate spec, org_id)
   │
   ├─► build_context_step(org_id, spec)
   │       └── retrieval_service.search_code (top_k=12, min_score=0.25)
   │             ├── embedding_service.generate_embedding ─────► Bedrock
   │             └── vector_store.search ─────────────────────► FAISS file
   │       └── filter empty/short/duplicate; cap to 5; truncate 2000 chars
   │
   ├─► generate_plan_step(spec, context)
   │       └── planning_engine.generate_plan ─────────────────► Bedrock (text)
   │       └── _coerce_plan → {"repos","tasks","feature_flag"}
   │       └── save_plan(job_id, plan)  ──────────────────────► artifacts/<id>/plan/plan.json
   │
   ├─► process_repositories_step(job_id, org_id, spec, plan, context)
   │       ├── RegistryService.list_repos_by_org ─────────────► repos GSI
   │       ├── _collect_repo_ids (strict; raises if empty)
   │       ├── GitlabTokensService.get_gitlab_token_for_org ──► Secrets Manager
   │       └── for each rid:
   │             _process_one_repo (see §6 step 6 above)
   │
   ├─► finalize_success_step
   │       ├── _maybe_deploy ──────────────────────────────────► webhook
   │       ├── update_job(status=succeeded, mr_url, staging_url) ► jobs table
   │       └── save_summary(...) ─────────────────────────────► artifacts/<id>/summary/summary.json
   │
   └─► (on Exception)  finalize_failure_step
           ├── save_summary(repo_errors, mrs) ────────────────► artifacts/<id>/summary/summary.json
           └── update_job(status=failed) ─────────────────────► jobs table
```

### Step-by-step details

| # | Helper | File:line | Inputs | Outputs | Failures | Retries |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | `run_job` | `mr_pipeline.py:1259` | `job_id` | summary dict | `JobNotFoundError`, `ValueError`, `RepositoryProcessingError`, `Exception` | per-step (see below); `asyncio.CancelledError` honoured |
| 1 | `load_job_step` | `mr_pipeline.py:865` | `job_id` | DynamoDB row dict | `JobNotFoundError` | `_retry_async("fetch_job", ...)` |
| 2 | `initialize_job_step` | `mr_pipeline.py:893` | `job_id` | re-fetched row or None | best-effort; warns | `_retry_async("fetch_job_pre_run", ...)` |
| 3 | `_set_status(running)` | `mr_pipeline.py:844` | jobs, job_id, status | None | swallowed (logged) | `_retry_async(...)` |
| 4 | `build_context_step` | `mr_pipeline.py:921` | `org_id, spec` | `(context, n_chunks)` | propagates | none (the underlying `search_code` retries Bedrock) |
| 5 | `generate_plan_step` | `mr_pipeline.py:944` | `spec, context` | `(plan, n_tasks, n_plan_repos)` | `ValueError("planning: could not obtain valid plan JSON")` | parse retry × 2; LLM throttle retry inside `_invoke_planning_llm` |
| 6 | `save_plan` (inline) | `mr_pipeline.py:1290` | `job_id, plan` | None | warns | none |
| 7 | `process_repositories_step` | `mr_pipeline.py:953` | full state | `(results, errors)` | `ValueError("No repositories selected from plan")` if empty selection; per-repo `Exception` collected | per-repo continue; `_retry_async("fetch_gitlab_token", ...)` |
| 7a | `_process_one_repo` | `mr_pipeline.py:695` | settings, job_id, org_id, spec, plan, context, repo, gitlab_token | per-repo dict or `None` | propagates | `_retry_async("git_push", ...)`, `_retry_async("create_mr", ...)` |
| 7b | `_prepare_repo_artifacts_sync` | `mr_pipeline.py:628` | spec, plan, context, repo | `(branch, repo_url, target, to_write)` | propagates | none |
| 7c | `_checkout_new_branch` | `mr_pipeline.py:343` | root, branch, target, token, repo_url | None | falls back to HEAD if fetch fails | `_retry_sync("git_fetch", ...)`, `_retry_sync("git_fetch_origin", ...)` |
| 7d | `_apply_changes_list` → `_safe_write_under_root` | `mr_pipeline.py:214 / 123` | root, files | int / None | raises `ValueError` on overwrite/similarity guard | none — fail-fast |
| 7e | `_commit_if_dirty` | `mr_pipeline.py:307` | root, job_id, paths | `bool` (committed?) | propagates | none |
| 7f | `_git_push` | `mr_pipeline.py:334` | root, branch, token, repo_url | git stdout | propagates | wrapped via `_retry_async` |
| 7g | `_create_merge_request` | `mr_pipeline.py:411` | settings, project_path, token, source/target, title, desc | MR `web_url` | captured into `mr_error` (does not fail the per-repo result) | wrapped via `_retry_async` |
| 8 | `finalize_success_step` | `mr_pipeline.py:1080` | job_id, results, kwargs | summary dict | raises `ValueError("merge request could not be created")` if every push lacked an MR URL | `_retry_async("finalize_succeeded", ...)` for the row update |
| 8a | `_maybe_deploy` | `mr_pipeline.py:457` | settings, job_id, org_id, plan | URL or None | logs error, returns None | `_retry_sync("deploy_webhook", ...)` |
| 9 | `finalize_failure_step` | `mr_pipeline.py:1185` | job_id, error, kwargs | None | best-effort summary write | `_set_status(failed)` retries internally |

### Cancellation

* Same-process cancel: `cancel_job` calls `Task.cancel()` on the asyncio
  task held in `_pipeline_tasks`. `run_job` catches `CancelledError`,
  flips status to `cancelled`, writes a cancellation summary, and
  re-raises.
* Cross-process cancel: cooperative — the worker re-fetches the row
  early (`initialize_job_step`) and short-circuits if a peer flipped
  the status to `cancelled` before the PROCESSING flip.

### Retry behaviour summary

* **Bedrock LLM**: family-aware retry inside `_invoke_planning_llm` on
  `ThrottlingException`, `ServiceUnavailableException`,
  `TooManyRequestsException`, `InternalServerException`,
  `ModelTimeoutException`. Effective attempts = `_APP_RETRY_MULTIPLIER (3)
  × bedrock.max_retries`.
* **JSON parse**: 2 attempts with `_STRICT_FOLLOW` reminder.
* **Bedrock embeddings**: same retriable set, internal backoff in
  `embedding_service`.
* **DynamoDB / Secrets Manager**: `_retry_async` wraps reads/writes with
  exponential backoff (`_BASE_BACKOFF=1.0`, max 30s, `_MAX_ATTEMPTS=3`).
* **Git**: `_retry_async("git_push", ...)` / `_retry_sync("git_fetch", ...)`
  / `_retry_async("create_mr", ...)`.
* **Webhook**: `_retry_sync("deploy_webhook", ...)`, errors swallowed.

---

## 7. LLM Usage Map

Bedrock client is `app/bedrock_runtime.py:get_bedrock_runtime()` (single
cached `boto3.client("bedrock-runtime")`).

### Family dispatch

* **Anthropic Claude** (`*claude*`) — Messages API, `system` + `messages` array.
* **Amazon Titan text** (`*titan*` not embedding) — `inputText`,
  `textGenerationConfig{maxTokenCount, temperature, topP}`.
* **Meta Llama** (`*meta.llama*`) — `prompt: <|system|>...<|user|>...<|assistant|>`.
* `code_understanding._invoke_llm` and `planning_engine._dispatch_flex`
  reproduce the same dispatch shape with slightly different parameter
  envelopes (one tuned for summarisation, one for planning/code/test/flag).

### Embedding family dispatch

* **Cohere embed** (`*cohere*embed*`) — multi-text per request.
* **Titan embed** (`*titan*embed*`) — one text per call, parallelised via
  `ThreadPoolExecutor`.

### Per-call inventory

| # | File | Function | Purpose | Input | Output | Model role | Prompt summary |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `code_understanding.py` | `summarize_chunk(chunk)` | Per-chunk structured summary at index time. | Single chunk dict (`code`, `file`, `repo_id`, `symbol`, …) | `{summary, purpose, inputs, outputs, dependencies, type}` | Bedrock text (`bedrock.model_id`) | "You are a senior software engineer doing a focused code review of ONE chunk. Return EXACTLY this JSON shape … no prose, no markdown." (≤700 max tokens) |
| 2 | `planning_engine.py` | `generate_plan(spec, context)` | Spec → multi-repo implementation plan. | `spec` (job string) + `context` (RAG bundle minus `spec`) | `{repos:[{repo_id, type}], tasks:[{repo_id, description, files_to_modify}], feature_flag:{required, flag_name}}` | Bedrock text | "You are a staff engineer producing an implementation plan as JSON only … Spec wins over context." `_max_gen_tokens()` (≥256, ≤8000) |
| 3 | `code_generation.py` | `generate_repo_changes(spec, plan, context, repo_id)` | Per-repo concrete file changes. | spec, plan, RAG context filtered to this repo, repo_id | `{changes:[{file, change_type:"modify"\|"create", code}]}` | Bedrock text | "You are a senior engineer. Output one JSON object only. Feature spec drives WHAT; context_chunks ground existing code. Minimal changes. `change_type=create` for new files; `modify` for existing." `_code_gen_max_tokens()` ≤16000 |
| 4 | `test_generator.py` | `generate_tests(repo_id, changes)` | MVP unit/integration tests for the generated diff. | `repo_id`, list of code changes | `{tests:[{file, code}]}` | Bedrock text | "You are a senior engineer writing MVP test code. Match project's existing test runner (pytest / vitest / jest). Skip pure config. JSON only." `_test_gen_max_tokens()` ≤16000 |
| 5 | `feature_flag.py` | `add_feature_flag(plan, changes)` | Wrap new behaviour behind a boolean flag (default OFF). Skipped when `plan.feature_flag.required is False`. | plan, current changes list, sanitized flag name | `[{file, code}, …]` (rewritten changes + config file) | Bedrock text | "Boolean flag, default OFF. Add or update one small config file with the flag. Wrap only new logic. No unrelated rewrites. JSON only." |
| 6 | `summary_worker.py` → `code_understanding.summarize_chunk` | `process_unsummarized_chunks(org_id)` | Backfill per-chunk LLM summaries into FAISS metadata after ingest. | `org_id` | stats dict; per-chunk `update_chunk_metadata` writes | Bedrock text (same as #1) | re-uses `summarize_chunk` |
| 7 | `onboarding/repo_manifest.py` | `generate_repo_manifest(repo_path)` | LLM-generated repo-level manifest (`description`, `tech_stack`, `modules`). | local clone path (README + `os.walk` tree) | `{description, tech_stack:[...], modules:[{name, description, files:[...]}]}` | Bedrock text | "You describe a software repository from the given README excerpt and directory listing. Use only paths from listing. ≤10 modules, ≤50 files. JSON only." |
| 8 | `embedding_service.py` | `generate_embedding(text)` / `generate_embeddings_batch(texts)` | Vectorise code chunks (ingest) and queries (retrieval). | text strings | `list[float]` per text | Bedrock embedding (Cohere or Titan) | n/a — embedding payloads, not text prompts |

### Shared LLM JSON post-processing

`code_understanding._strip_code_fences` + `_first_json_object` are the
canonical "extract one JSON object from possibly-fenced LLM output"
helpers, reused by `planning_engine`, `code_generation` (transitively via
`_parse_plan_json`), and `onboarding/repo_manifest`. The `_STRICT_FOLLOW`
reminder lives in `planning_engine` and is appended on the second parse
attempt across all four call sites.

---

## 8. Context Builder Analysis

### Flow

1. **Input validation** — `org_id`, `spec` non-empty (post strip).
2. **Single retrieval call** — `retrieval_service.search_code(org_id,
   query=spec.strip(), top_k=12, min_score=0.25)`.
   * `search_code` embeds via `embedding_service.generate_embedding`,
     then `vector_store.search(org_id, embedding, top_k)`.
   * FAISS L2 distance → similarity = `1 / (1 + d)`.
   * Optional lexical `summary_boost` (default 0.0) — disabled here.
3. **Filtering loop** in `context_builder.build_context`:
   * Drop reasons (counted): `dropped_empty`, `dropped_too_short`
     (`< 20` chars), `dropped_duplicate` (exact stripped code match),
     `dropped_over_cap` (already kept 5).
   * Per-snippet truncation via `_truncate_code` to `_MAX_CODE_CHARS=2000`.
4. **Output** — `{"spec": query, "context": [{repo_id, file, code}, ...]}`
   with at most 5 items.

### Tunables

| Constant | Value | Effect |
| --- | --- | --- |
| `_SEARCH_TOP_K` | 12 | FAISS rows requested upstream. |
| `_SEARCH_MIN_SCORE` | 0.25 | Floor enforced by `search_code`. |
| `_CONTEXT_MAX_CHUNKS` | 5 | Hard cap on returned items. |
| `_MIN_SNIPPET_CHARS` | 20 | Drop low-signal stubs. |
| `_MAX_CODE_CHARS` | 2000 | Per-snippet truncation. |

### Weaknesses

* **Single-query retrieval** — only the literal stripped spec is embedded.
  No paraphrase, no decomposition. A spec describing two distinct
  capabilities is one vector; relevant code for either may be missed.
* **Hard cap at 5 chunks** — predictable token budget but means a complex
  multi-repo change can lose grounding for some repos. Code generation
  silently runs without any chunks for such repos (see §9 "no tasks and
  no context → empty changes" path in `code_generation.py:245`).
* **No reranking** — `search_code` returns FAISS-distance order; the
  optional `summary_boost` is off.
* **Whole-snippet duplicate detection only** — near-duplicate snippets
  (different whitespace, different headers) bypass the dedup; the
  20-char minimum filters tiny stubs but not redundancy.
* **No org-/path-level diversification** — five chunks could all come
  from one file in one repo if it's the closest match.

---

## 9. Code Generation Flow

### 1. Repo selection (`_collect_repo_ids`)

* **Inputs**: plan dict, `org_repos` from registry, `job_id`.
* **Logic**: union of plan-mentioned `repo_id`s, intersected with
  registry, sorted. **No fallback**.
* **Failure**: empty result → `ValueError("No repositories selected from
  plan")`.

### 2. Planning (already covered in §7 #2)

* `plan` shape: `{repos: [{repo_id, type}], tasks:
  [{repo_id, description, files_to_modify}], feature_flag: {required, flag_name}}`.

### 3. Per-repo generation pipeline (`_prepare_repo_artifacts_sync`)

```
plan + context + spec
        │
        ▼
generate_repo_changes(spec, plan, context, repo_id)
        │  filters tasks/context for this repo
        │  if no tasks AND no context → returns {"changes": []}
        │  else LLM:
        ▼
{changes: [{file, change_type, code}]}
        │
        ▼
generate_tests(repo_id, raw_list)   [skipped if raw_list empty]
        │
        ▼
add_feature_flag(plan, raw_list)    [no-op if !plan.feature_flag.required]
        │
        ▼
to_write = dedupe_by_file(final + tests)
```

### 4. Generated structure

Each item in `to_write`:

```python
{
    "file": "app/feature/something.py",   # repo-relative POSIX path
    "code": "<full file body, UTF-8>",
    "change_type": "modify" | "create" | "" (tests default to "create"),
}
```

### 5. Validation pipeline

* **`_coerce_changes`** in `code_generation.py:181` —
  - Drop entries with empty `file`.
  - Coerce unknown `change_type` to `"modify"`.
  - Drop `create` with empty code; warn but keep `modify` with empty
    code.
* **JSON schema enforced via prompt only** — there is no structural
  validator beyond `_first_json_object` + `json.loads`.

### 6. Application (`_apply_changes_list` → `_safe_write_under_root`)

Sequence per file:

1. Path-traversal check (`..`, `commonpath` escape).
2. Read existing file (UTF-8) if present; binary/decoding errors
   skip the safety checks but still write.
3. **Suspicious-size guard**: `len(new) > 1.5 * len(old)` → reject.
4. **Similarity guard** for `change_type=="modify"`:
   `difflib.SequenceMatcher(autojunk=False).ratio() < 0.4` → reject.
5. Atomic-ish write with `newline="\n"`.

### Risks (current, file-overwrite model)

* Whole-file overwrite is the only patch primitive; partial diffs are
  not supported. The 1.5× growth + 0.4 similarity guards are heuristic
  protections, **not** semantic verification.
* "modify" with low similarity is rejected, but a **complete rewrite at
  ≥40% similarity** is allowed — cosmetic identifiers can carry that
  alone in some files.
* Per-repo failures stop that repo only; the rest of the loop proceeds
  before the job is marked `failed` (see §6 step 7).
* Tests are written before the feature-flag pass on the unflagged
  changes (`raw_list`); if the flag pass alters file paths or wraps
  logic, test paths can diverge from the final shipped code. **[UNSURE]**
  — depends on whether the LLM keeps file paths stable.

---

## 10. Git + MR Flow

All git operations happen on the local clone at
`<base_storage_path>/repos/<org_id>/<repo_id>/`, populated earlier by
`workers/repo_ingest.py` via `services/git_clone.py`.

### Auth flow

1. **Token resolution** —
   `RegistryService` → `GitlabTokensService.get_gitlab_token_for_org(org_id)`
   → `OrgsRepository.get` (DynamoDB) → `SecretsManagerClient.get_secret_json`
   (TTL cached) → `GitlabSecret{gitlab_token}`.
2. **Token injection at push/fetch** — `mr_pipeline._inject_token` (alias
   into `services/git_clone._inject_token`):
   `https://oauth2:<urlencoded_token>@host/group/repo.git`.
3. **Token in MR call** — sent as `PRIVATE-TOKEN` header; never written
   to disk.
4. **Redaction** — every command result and log line passes through
   `_redact_url` / `_scrub_text`. Even on git failure, stderr is scrubbed
   before `RuntimeError(f"git {args} failed: ...")`.

### Branch creation

* **File**: `mr_pipeline._checkout_new_branch` (line 343).
* Branch name from `_branch_name(job_id, repo_id)` →
  `agent-<job_head>-<repo_tail>`, sanitized to `[A-Za-z0-9._-]`, max 200
  chars.
* Strategy: `git fetch` (auth-injected URL, depth=1) then `git checkout
  -B <branch> origin/<target>`. If fetch fails, fall back to local HEAD
  (logged warning).

### Commit

* `_commit_if_dirty(root, job_id, paths)` — `git add` each generated
  path, `git status --porcelain` to detect a dirty tree, then commit
  with hard-coded `user.name=ai-mr-pipeline` and
  `user.email=ai-mr+<job_head>@local.invalid`. Message:
  `"feat: agent update [job <job_id>]"`.
* Returns `False` if there is no diff after applying changes — that repo
  is treated as "no change" (`mr_pipeline.repo_clean`).

### Push

* `_git_push(root, branch, token, repo_url)` — single git push to
  `oauth2:<token>@host` URL, `HEAD:refs/heads/<branch>`. Wrapped in
  `_retry_async("git_push", ..., max_attempts=3)`.

### Merge request

* `_create_merge_request(settings, project_path, token, source_branch,
  target_branch, title, description)` — `urllib.request.urlopen` POST to
  `gitlab_api_v4_url + "/projects/" + url_quote(project_path) +
  "/merge_requests"`.
* `project_path` derived by `_project_path_for_gitlab_api(repo_url)` from
  the parsed HTTPS form.
* Title = `_spec_title(spec)` truncated to 120 chars; description =
  `_build_mr_description` (markdown body listing tasks, files, flag).
* Failure is **caught** and stored as `mr_error` on the per-repo result.
  The aggregate guard in `finalize_success_step` later raises if no MR
  URL was produced for any repo.

### Deployment webhook

* `_maybe_deploy(settings, job_id, org_id, plan)` — POSTs JSON
  `{job_id, org_id, event:"deploy_staging", plan_excerpt:...}` to
  `settings.mr_deploy_webhook_url` (if configured). `_retry_sync`,
  errors swallowed.

### Failure handling

* Auth / 401 / 403 from `_create_merge_request` → captured into
  `mr_error`; if **every** repo's push succeeded but no MR URL emerged,
  `finalize_success_step` raises `ValueError("merge request could not
  be created: …")` and the job fails.
* Per-repo `Exception` from any git step → caught by
  `process_repositories_step`, recorded in `errors` list, loop
  continues. Non-empty errors → job fails with `RepositoryProcessingError`.
* Push without MR is **never reported as success**.

---

## 11. Data Models

### Job (`app/models/jobs.py`)

| Field | Type | Notes |
| --- | --- | --- |
| `job_id` | `JobId` (32-char uuid4 hex by default; URL-safe) | DynamoDB partition key. |
| `org_id` | `OrgId` | FK → orgs table. |
| `spec` | str (≤20 000 chars) | Free-form, validated as `SpecText`. |
| `status` | `JobStatus` (CREATED / PROCESSING / COMPLETED / FAILED / CANCELLED) | Public label; mapped from internal lowercase via `STATUS_DB_TO_API`. |
| `mr_url` | str | First non-empty MR URL across processed repos. |
| `staging_url` | str | First non-empty staging URL. |
| `logs` | `List[str]` | Tail of `artifacts/<job_id>/logs/*` (NOT in DB). |
| `created_at` | datetime | ISO-8601. |

Stored row also has `updated_at`, `questions`, `answers` (defaulted to
`[]`), and possibly `repo_errors` / `mrs` only inside the artifact summary
(not on the row).

### Org (`app/models/orgs.py`)

| Field | Type | Notes |
| --- | --- | --- |
| `org_id` | `OrgId` | DynamoDB PK. |
| `name` | `OrgName` | 1–200 chars; `find_by_name` does normalised lookup. |
| `secret_name` | `SecretName` | Secrets Manager secret holding `{gitlab_token}`. Built from `secrets_manager.secret_name_for(org_id)`. |
| `created_at` / `updated_at` | datetime | Maintained by `OrgsRepository`. |

### Repo (`app/models/repos.py`)

| Field | Type | Notes |
| --- | --- | --- |
| `repo_id` | `RepoId` | DynamoDB PK. |
| `org_id` | `OrgId` | FK; GSI `org_id-index`. |
| `repo_url` | `RepoUrl` | Validated HTTPS / SSH / `git@host:path` shape. |
| `branch` | `BranchName` | Default `"main"`. |
| `status` | `RepoStatus` | `PENDING` → `CLONING` → `READY` (only). |
| `created_at` / `updated_at` | datetime | Repository-managed. |

### Relationships

```
Org (1) ──< (N) Repo
Org (1) ──< (N) Job
Job has spec, status; references Org but not specific Repos
       (the plan picks repos at run time; selection persists in
        artifacts/<job_id>/plan/plan.json + summary mrs[].repo_id).
Org → SecretName → Secrets Manager → {"gitlab_token"}
```

### Artifact structure

`artifacts/<job_id>/`:

| Subpath | Owner | Purpose |
| --- | --- | --- |
| `logs/pipeline.log` | `mr_pipeline._append_job_log` | Per-job pipeline narrative. |
| `logs/*` | open-ended | Other workers may append; `read_logs` merges all. |
| `diffs/*` | `artifact_manager.save_diff` | Reserved (no current writer in pipeline). |
| `plan/plan.json` | `save_plan` | Final coerced plan. |
| `questions/questions.json` | `save_questions` | Reserved for clarifier flow (not in current `run_job`). |
| `summary/summary.json` | `save_summary` | End-of-run state: status, mr_url, staging_url, plan summary, mrs[], deploy_url, repo_errors. |

Vector / repo storage layout (`storage_manager`):

```
<base_storage_path>/
  ├── repos/<org_id>/<repo_id>/        # git checkouts
  │     └── .agent/{metadata.json, branch_info.json}
  ├── artifacts/<job_id>/              # per-job
  ├── cache/                           # reserved
  └── tmp/<job_id>/                    # reserved
```

```
<vector_store_root>/<org_id>/
  ├── vectors.faiss
  ├── metadata.json     # parallel list of {chunk_hash, metadata}
  └── .lock             # FileLock guard
```

---

## 12. Storage Architecture

| Concern | Backend | Module | Notes |
| --- | --- | --- | --- |
| Org & repo metadata | DynamoDB (`orgs`, `repos`) | `app/db/orgs.py`, `app/db/repos.py` | `repos` has GSI `org_id-index` on `(org_id, created_at)`. |
| Job state | DynamoDB (`jobs`) | `app/db/dynamodb.py` | PK `job_id`. ETL'd updates whitelisted via `_ALLOWED_UPDATE_FIELDS`. |
| In-memory dev mode | dict + asyncio lock | `app/db/memory_store.py` | Explicit `PERSIST_BACKEND=memory` or automatic when `LOCAL_AWS_AUTOFALLBACK` applies and `DYNAMODB_ENDPOINT_URL` is unset. |
| Laptop without boto3 credentials | Same as rows above | `app/config.py`, `app/aws_local.py` | `environment=local` + `LOCAL_AWS_AUTOFALLBACK` (default): may coerce memory persist + inline secrets + `gitlab_token_storage=local` so the API starts; Bedrock/embeddings unchanged. |
| GitLab tokens | AWS Secrets Manager (or local file) | `app/services/secrets.py`, `app/services/gitlab_tokens.py` | `<workspace_root>/.local-gitlab-tokens/<org_id>.json` in `gitlab_token_storage=local` mode. |
| Repo clones | Filesystem | `app/storage_manager.py`, `app/services/git_clone.py`, `app/repo_initializer.py` | `<base>/repos/<org_id>/<repo_id>/`, with `.agent/metadata.json` sidecar. |
| Per-job artifacts | Filesystem | `app/artifact_manager.py` | `<base>/artifacts/<job_id>/{logs,diffs,plan,questions,summary}/`. |
| Vectors & metadata | FAISS file + JSON sidecar | `app/vector_store.py` | `<vector_store_root>/<org_id>/{vectors.faiss, metadata.json, .lock}`. |
| Code chunks / summaries | Same `metadata.json` | `app/vector_store.update_chunk_metadata` | Summaries written by `summary_worker`. |
| Knowledge map | Filesystem JSON | `app/knowledge_map.generate_org_map` → `<repos>/<org_id>/__org_map__.json` (or similar **[UNSURE — confirm path with `_org_dir`]**) | Refreshed at end of `ingest_repo`. |

### Concurrency

* **Vector store** — per-org `filelock.FileLock` (`<org>/.lock`) gates
  `init_index` / `index_chunks` / `update_chunk_metadata`. The in-memory
  `_memory_cache` is only valid inside one process.
* **DynamoDB** — `compare_and_set_status` on `repos` enforces the
  PENDING → CLONING transition cross-process via a conditional update.
* **Bedrock client** — single `_client` guarded by a `threading.Lock`.
* **Secrets cache** — TTL + per-secret asyncio lock so concurrent readers
  share a fetch.

---

## 13. Current Problems / Risks

### 13.1 Noisy / shallow retrieval

* **Why**: single-vector retrieval over a literal spec, no rerank, no
  diversity filter, hard cap at 5 chunks.
* **Files**: `app/context_builder.py:56`, `app/retrieval_service.py:81`.
* **Effect**: complex multi-repo specs may underfeed code generation;
  per-repo code-gen sometimes runs with `ctx_items=[]` and only the
  task description, increasing hallucination risk.

### 13.2 Whole-file overwrite is the only patch primitive

* **Why**: LLM emits full file bodies; there is no diff/patch path.
* **Files**: `app/code_generation.py:30+ (system prompt mandates "full
  file"), `app/mr_pipeline.py:_safe_write_under_root`.
* **Mitigations in place**: 1.5× growth guard + 0.4 similarity guard for
  `modify`. Path-traversal protected.
* **Residual**: a 40%+ similar full rewrite is allowed and can change
  semantics; binary or non-UTF8 files skip the safety checks; no
  syntactic / lint / unit-test execution before commit.

### 13.3 Large / mixed-responsibility files

See §14. The biggest live concern is **`mr_pipeline.py` (1410 LOC)**: it
mixes URL helpers, retry, git, GitLab REST, deploy, plan helpers, MR
description, per-repo orchestration, step-level orchestration, the
`RepositoryProcessingError` exception, and the public entrypoint.

### 13.4 Silent failures (controlled but pervasive)

Almost every `_set_status`, `save_summary`, `save_plan`,
`init_job_artifacts`, and post-success bookkeeping write is wrapped in
`try/except Exception ... log.warning`. This is **deliberate** —
artifact writes must never mask the primary error — but the sheer
volume (`mr_pipeline.py` has ~17 such blocks) makes it hard to spot a
genuinely silent regression. Files: `app/mr_pipeline.py`,
`app/workers/repo_ingest.py`.

### 13.5 Repo selection — strict but inflexible

* `_collect_repo_ids` rightly refuses to fall back to "all READY
  repos", but it also has **no recovery path**: a planner that omits a
  necessary repo id from the plan kills the entire job.
* No interactive clarification flow today (`questions/` artifact
  directory exists but the pipeline never writes there).

### 13.6 Concurrency / scalability

* `mr_pipeline.run_job` is invoked via `asyncio.create_task` inside the
  HTTP process. Long jobs + restarts ⇒ orphaned tasks; the cancellation
  path notes this is "best-effort, single process" (`app/api/v1/jobs.py:91`).
* Vector store is FAISS files on local disk, with `filelock` —
  horizontally non-scalable. Multiple replicas would compete for the
  same on-disk store and serialise on the lock.
* Bedrock retries multiply (`_APP_RETRY_MULTIPLIER × bedrock.max_retries
  × _PARSE_RETRIES`). One stuck job can burn ~9 LLM round trips per LLM
  step.
* DynamoDB ListAll for orgs is a `Scan` (`OrgsRepository.list_all`,
  documented as "fine while small"). Same for `list_jobs_by_org` —
  filtered Scan.

### 13.7 LLM dispatch duplication

`_invoke_anthropic*`, `_invoke_titan*`, `_invoke_llama*`, `_dispatch*`
exist in **three** modules (`code_understanding.py`,
`planning_engine.py`, `embedding_service.py` for the embedding family).
Tuning per-family quirks risks silent divergence. See §14 / cleanup
report §3.2.

### 13.8 Tests are written against pre-flag changes

`_prepare_repo_artifacts_sync` calls `generate_tests(rid, raw_list)`
**before** `add_feature_flag(plan, raw_list)`. If the flag pass changes
file paths or wraps logic, the test code may target obsolete shapes.
**[UNSURE]** — only matters when the LLM picks divergent paths.

### 13.9 No MR re-use across runs

Re-running a succeeded job opens a **new** branch (`agent-<jobhead>-...`)
and a **new** MR; there is no dedup against the previous run. Documented
in `run_job`'s docstring (`mr_pipeline.py:1085`).

### 13.10 SPA scaffolds duplication (out-of-scope)

Both `frontend/` (Vite) and `web/` (Next.js) directories are present.
**[UNSURE]** which is canonical. Out of scope for this Python service
but risky for confusion.

---

## 14. Large Files / Complexity Report

LOC counts from `wc -l` (whole-file, including blanks).

| File | LOC | Responsibilities | Why complex | Suggested future split (analysis only) |
| --- | --- | --- | --- | --- |
| `app/mr_pipeline.py` | 1410 | URL helpers, file-write safety, retry, git, GitLab REST, deploy, plan helpers, MR description, per-repo orchestration, step orchestrators, public entrypoint, custom exception | Eight distinct concerns in one file; god functions `run_job` (139 LOC), `_process_one_repo` (115), `process_repositories_step` (104), `finalize_success_step` (103), `_safe_write_under_root` (89) | `mr/run_job.py` (orchestrator), `mr/repo_processor.py` (per-repo), `mr/file_writer.py` (safety), `mr/git_ops.py`, `mr/gitlab_api.py`, `mr/retry.py`, `mr/deploy.py` |
| `app/knowledge_map.py` | 741 | Code-graph: parse imports + classify purpose + detect APIs + build symbol graph + serialize JSON | `generate_org_map` is 230 LOC and runs five sequential phases | `knowledge_map/parsers.py`, `knowledge_map/classifier.py`, `knowledge_map/serializer.py`, `knowledge_map/__init__.py` |
| `app/config.py` | 708 | Grouped `BaseSettings` subclasses + enums + `_apply_local_aws_autofallback` + validators | Env surface is large; local-AWS probing couples settings to boto3 lazily | `config/__init__.py` (Settings + helpers), `config/{aws,bedrock,secrets,email,summarization,dynamo}.py` |
| `app/services/secrets.py` | 650 | TTL-caching boto3 client + sync-inline file-backed mode + AppSecrets | Two implementations side by side | `services/secrets/{__init__,client,inline}.py` |
| `app/workers/repo_ingest.py` | 623 | Status machine + clone + ingest + summarise + cleanup | `_process_loaded` is 147 LOC | `workers/repo_ingest/{__init__,pipeline,utils}.py` |
| `app/services/git_clone.py` | 604 | Errors taxonomy + URL helpers + git executor + clone + update | Tightly coupled but four sub-topics | `services/git_clone/{__init__,errors,url_helpers,runner}.py` |
| `app/vector_store.py` | 590 | FAISS init/load/save + index_chunks + search + metadata mutate + repo deletion | `update_chunk_metadata` is 130 LOC; mutation paths are subtle | `vector_store/{store,index,metadata}.py` (only after a real test suite) |
| `app/code_understanding.py` | 530 | LLM client per family + JSON cleanup + chunk summarise | Single feature, but 100+ LOC summarise function | Extract `llm_utils.py` (`_first_json_object`, `_strip_code_fences`); leave summarise_chunk |
| `app/db/dynamodb.py` | 515 | Resource cache + jobs repository + shared helpers | Functional but mixes shared helpers with one repository class | Move shared helpers to `db/_common.py`; jobs repo stays. |
| `app/db/repos.py` | 485 | repos repo + status state machine + GSI list | `compare_and_set_status` is 95 LOC | Coherent — keep; consider extracting state-machine constants. |

Files under 300 LOC are listed in `cleanup_report.md` as SAFE; no
analysis here.

---

## 15. Suggested Future Architecture (high level)

> **Analysis only.** No implementation. Layers below are intended as
> conceptual buckets for future code organisation; current code already
> partially aligns with each one.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Onboarding Layer                                                   │
│  - Org provisioning, GitLab token storage/rotation                  │
│  - Repo registration                                                │
│  - First-time clone + repo manifest + indexing kickoff              │
│  Existing modules: services/registry, services/gitlab_tokens,       │
│  workers/repo_ingest, services/git_service, onboarding/repo_manifest│
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Repository Intelligence Layer                                      │
│  - Source scanning, language-aware chunking                         │
│  - Embeddings (Bedrock)                                             │
│  - Vector storage (FAISS or replacement)                            │
│  - Per-chunk LLM summaries                                          │
│  - Org-level knowledge map (dependency / API graph)                 │
│  Existing modules: repo_scanner, code_chunker, embedding_service,   │
│  vector_store, summary_worker, knowledge_map, ingestion_pipeline    │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Selection Layer                                                    │
│  - Spec → retrieved context (multi-strategy retrieval, re-rank,     │
│    diversity by repo / file / module, optional summarisation)       │
│  - Repo selection given a plan (deterministic, with explicit        │
│    fallback rules and clarification escalation)                     │
│  Existing modules: context_builder, retrieval_service,              │
│  mr_pipeline._collect_repo_ids                                      │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Generation Layer                                                   │
│  - Planner agent (spec + context → plan)                            │
│  - Code agent (per-repo file changes)                               │
│  - Test agent (tests for diff)                                      │
│  - Feature-flag agent (boolean rollout wrap)                        │
│  - Shared Bedrock dispatch (Anthropic / Titan / Llama / future)     │
│  Existing modules: planning_engine, code_generation, test_generator,│
│  feature_flag, code_understanding (JSON helpers), bedrock_runtime   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Patch Layer (future expansion)                                     │
│  - Move from "full file overwrite" toward structured patches:       │
│    * Range edits, AST-aware patches                                 │
│    * Diff generation + reapply (3-way merge against fresh HEAD)     │
│  - Today: `_apply_changes_list` writes whole files; future patch    │
│    layer would sit between LLM output and filesystem.               │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Validation Layer (future expansion)                                │
│  - Static syntactic check (per-language linters / parsers)          │
│  - Lightweight unit-test execution in a sandbox                     │
│  - Diff size + similarity guards (currently in _safe_write_under_   │
│    root) graduated into a dedicated component                       │
│  - Pre-commit / pre-push gates with explicit reject reasons         │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Delivery Layer                                                     │
│  - Branch creation, commit, push                                    │
│  - GitLab REST (MR creation, with retry + push-without-MR guard)    │
│  - Deploy webhook                                                   │
│  Existing modules: mr_pipeline (git helpers + MR REST + deploy),    │
│  services/git_clone (lower-level), repo_sync                        │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  Orchestration / Observability Layer                                │
│  - Job lifecycle, state machine, cancellation, retries              │
│  - Per-job artifacts (plan, logs, summary, diffs)                   │
│  - Structured logs with correlation IDs (already centralised)       │
│  - Readiness probes, health probes                                  │
│  Existing modules: mr_pipeline (run_job + steps),                   │
│  artifact_manager, logging, services/readiness, errors              │
└─────────────────────────────────────────────────────────────────────┘
```

### Cross-cutting suggestions (analysis only)

1. **Fold the duplicated Bedrock dispatch** into a single
   `app/bedrock_runtime.py` (already exists at 88 LOC) as a small
   "invoke text model with payload" entrypoint per family. Each LLM
   module then only owns its prompt + parser.
2. **Promote `_strip_code_fences` / `_first_json_object`** into a
   tiny `app/llm_utils.py` so the dependency from `planning_engine`,
   `code_generation`, `feature_flag`, `test_generator`,
   `onboarding/repo_manifest`, and `summary_worker` becomes intentional.
3. **Split `mr_pipeline.py`** along the layer boundaries above — the
   public `run_job` keeps its place, but git, GitLab, deploy, retry,
   and file-writer move into siblings. Treat as a multi-PR refactor.
4. **Make repo selection clarifier-aware** — when
   `_collect_repo_ids` returns empty, write a `questions/questions.json`
   artifact and surface a `JobStatus.PROCESSING` status mapped from the
   internal `awaiting_human` (already in `STATUS_DB_TO_API`). This
   wires up an existing-but-unused part of the schema.
5. **Persistence beyond DynamoDB Scan** — replace `list_orgs` and
   `list_jobs_by_org` Scans with GSIs once volume grows.
6. **Out-of-process worker** — move `run_job` out of the FastAPI
   process to an SQS/EventBridge consumer (`workers/base.py` already
   defines an `Event` envelope) so HTTP restarts don't orphan jobs.

---

*End of document.*
