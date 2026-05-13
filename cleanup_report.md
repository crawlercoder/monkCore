# Codebase Cleanup Report — `monkCore`

Scope: `/Users/utkarsh/PP/monkCore` (Python `app/` package — 61 files,
~14.9k LOC). Out of scope: `.venv/`, `.git/`, `frontend/`, `web/.next/`,
`web/node_modules/`, generated artifacts under `repos/`.

This report follows the requested phase structure. **Phase 2 (Safe Cleanup)**
applies only the changes flagged "SAFE — APPLIED". Everything else is left in
place pending a human decision.

---

## 1. Unused Files

| File | Status | Notes |
| --- | --- | --- |
| `app/agents/__init__.py` | REVIEW | Doc-only; no symbols beyond a docstring. |
| `app/agents/base.py` | REVIEW | Defines `Agent[InputT, OutputT]` ABC. **Zero importers** in `app/`. Referenced only by `README.md`. Likely architectural placeholder for "future agents". |
| `app/rag/__init__.py` | REVIEW | Doc-only. |
| `app/rag/base.py` | REVIEW | Defines `Retriever` / `RetrievedChunk` ABCs. **Zero importers**. README mentions it as the "retriever interface" (aspirational). Real retrieval lives in `app/retrieval_service.py`, which never uses these ABCs. |
| `app/workers/base.py` | REVIEW | `Event` / `EventHandler` ABCs are **only** re-exported by `app/workers/__init__.py`; no concrete subclass exists in the repo. The actual worker (`workers/repo_ingest.py`) does **not** subclass `EventHandler`. Documented as a future EventBridge envelope. |
| `repos/` (directory) | UNSURE | Empty runtime placeholder, gitignored. `app/storage_manager.py` writes clones into it. Removing the empty dir is a no-op for git but the runtime auto-creates it. Leave alone. |
| `scripts/test_phase1.py` | REVIEW | Real Phase-1 smoke test, but not wired into CI. Not safe to remove without confirmation. |
| `scripts/test_phase2.py` | REVIEW | Same — Phase-2 smoke test. |
| `scripts/test_storage.py` | REVIEW | Same — storage-manager smoke test. |
| `scripts/dev-stack.sh`, `dev-stack-bg.sh` | REVIEW | Local dev orchestration. Likely still relevant; leave. |
| `scripts/nginx/`, `scripts/systemd/` | REVIEW | Deployment-time configs — never imported by Python; expected. |

**No file is SAFE-deletable** with high confidence today. The agents/rag/workers
ABCs are documented in `README.md` and look like forward-leaning interfaces;
removing them would amount to an architectural decision (see §4).

---

## 2. Unused Functions

The codebase is unusually clean here. Static AST scan + cross-file grep found
**zero** module-level functions or classes that are entirely unreferenced
inside `app/` (excluding the abstract base classes already covered in §1).

A few items deserve a callout because they are **only** referenced through
re-exports in `__init__.py` files (intentional public API), not by direct
runtime callers in this repo:

| Symbol | File | Confidence it's used | Note |
| --- | --- | --- | --- |
| `put_secret`, `put_secret_json`, `invalidate_secret`, `get_secret_field` | `app/services/secrets.py` | HIGH | Used by `app/services/gitlab_tokens.py` and the public `app/services/__init__.py` API; do not remove. |
| `Event`, `EventHandler` | `app/workers/base.py` | LOW (REVIEW) | Re-exported but no concrete subclass. See §1. |
| `Agent` | `app/agents/base.py` | LOW (REVIEW) | Re-exported only via package docstring. See §1. |
| `Retriever`, `RetrievedChunk` | `app/rag/base.py` | LOW (REVIEW) | Same. |
| `now_iso = _now_iso` | `app/db/dynamodb.py:497` | MEDIUM | Public alias of the private `_now_iso`. Mentioned in `app/services/registry.py` *docstring* but not actually called from there. Keep — public surface. |

---

## 3. Duplicate Logic

### 3.1 JSON parsing — already consolidated ✅

`_strip_code_fences` and `_first_json_object` live in
`app/code_understanding.py` and are reused by:

- `app/planning_engine.py`
- `app/code_generation.py`
- `app/onboarding/repo_manifest.py`

No further consolidation needed. **Note**: this means
`app/code_understanding.py` doubles as both a summarizer and a "shared LLM
JSON-cleanup util". A future tidy-up could move `_strip_code_fences` /
`_first_json_object` into a small `app/llm_utils.py` module so the
dependency arrow is intentional rather than accidental. **REVIEW**, not
applied — would touch import lines in three files.

### 3.2 Bedrock dispatch — duplicated across three files

The "embed/invoke per family" dispatcher pattern is repeated:

| File | Helpers |
| --- | --- |
| `app/code_understanding.py` | `_invoke_anthropic`, `_invoke_titan_text`, `_invoke_llama`, `_dispatch`, `_invoke_llm` |
| `app/planning_engine.py` | `_invoke_anthropic_flex`, `_invoke_titan_flex`, `_invoke_llama_flex`, `_dispatch_flex`, `_invoke_planning_llm` |
| `app/embedding_service.py` | Bedrock invocation for embeddings (Cohere/Titan) |

The bodies differ in **prompt format, max-token logic, and response field
names** per model family / per use-case (planner vs summarizer vs embedder).
A single helper would need to thread several config knobs and would risk
breaking model-specific quirks. **REVIEW** — flag for a deliberate
`app/bedrock_runtime.py` extension, not a quick refactor. Note that
`app/bedrock_runtime.py` already exists (88 LOC) and could host this.

### 3.3 LLM JSON post-processing — minor duplication

Pattern repeated verbatim:

```python
raw = _first_json_object(_strip_code_fences(text))
obj = json.loads(raw)
```

Appears in:
- `app/code_understanding.py:366`
- `app/planning_engine.py:233`
- `app/onboarding/repo_manifest.py:134`

Three lines, but each call site already has site-specific validation right
after. Consolidating into one helper saves <10 lines net. **REVIEW** — not
applied.

### 3.4 `_build_user_message` name collision (not duplication)

Three modules each define a *private* `_build_user_message` with completely
different signatures (`feature_flag.py`, `test_generator.py`,
`code_generation.py`). They are **not** duplicates; they are distinct prompt
builders. No action.

### 3.5 `_max_tokens()` accessor pattern

Each LLM module has a `_xxx_max_tokens()` helper that reads the same
`Settings`. Cosmetic duplication; consolidating would couple modules through a
shared util for negligible gain. **REVIEW** — not applied.

### 3.6 Validation / logging / utility helpers

No meaningful duplication found. The repo already centralizes:
- `app.logging` → `get_logger`, `log_event`, `log_status_transition`,
  `log_context`.
- `app.errors` → typed `AppError` subclasses + handler registration.
- `app.db.dynamodb` → `_build_update_expression`, `_filter_update_fields`,
  `_now_iso` reused by sibling DB modules.
- `app.services.git_clone` → `_inject_token`, `_redact_url`, `_scrub_text`
  reused by `mr_pipeline.py` and `services/git_service.py`.

---

## 4. Redundant Folders

| Folder | Status | Notes |
| --- | --- | --- |
| `app/agents/` | REVIEW | One ABC, never subclassed. Documentation-grade scaffolding. Removing would silently delete an architectural placeholder. |
| `app/rag/` | REVIEW | Same — documented but unused. Real RAG lives in `app/retrieval_service.py` and `app/vector_store.py`. |
| `repos/` | UNSURE / KEEP | Empty, gitignored, runtime clone target. Leave. |
| `app/__pycache__/`, `app/**/__pycache__/` | KEEP | Auto-generated bytecode caches. Already gitignored implicitly via `.venv/` + dev tooling; not committed. Removing them is a no-op (Python recreates on next import). |
| `web/.next/cache/` | OUT OF SCOPE | Next.js build cache; user's frontend stack manages it. |
| `frontend/` (Vite) and `web/` (Next.js) co-existing | REVIEW | Two SPA scaffolds — confirm whether both are intentional. Out of scope for this Python cleanup. |

No empty source folders inside `app/`.

---

## 5. Large / Complex Files

LOC is the count from `wc -l` — total lines, not non-blank only.

### REFACTOR (≥600 LOC OR multiple responsibilities)

```text
File: app/mr_pipeline.py
LOC: 1410
Responsibilities currently mixed:
  - URL/path utilities (_to_https_url, _project_path_for_gitlab_api)
  - Safe filesystem writes + overwrite/similarity guards
  - Retry helpers (_retry_async, _retry_sync)
  - Git ops (_git, _commit_if_dirty, _git_push, _checkout_new_branch)
  - GitLab REST (_create_merge_request)
  - Deployment hook (_maybe_deploy, _staging_url)
  - Plan-to-repo selection (_collect_repo_ids)
  - MR description rendering (_build_mr_description)
  - Per-repo artifact preparation (_prepare_repo_artifacts_sync)
  - Per-repo orchestration (_process_one_repo)
  - Job status + step orchestrators (load/init/build_context/generate_plan/...)
  - Public entrypoint (run_job, run_job_sync)
  - RepositoryProcessingError exception type

Suggested split (kept simple, no abstractions):
  - app/mr_pipeline.py            (run_job + step orchestrators only)
  - app/mr/file_writer.py         (_safe_write_under_root, _apply_changes_list,
                                   safety constants)
  - app/mr/git_ops.py             (_git, _commit_if_dirty, _git_push,
                                   _checkout_new_branch)
  - app/mr/gitlab_api.py          (_create_merge_request, _staging_url,
                                   _to_https_url, _project_path_for_gitlab_api)
  - app/mr/retry.py               (_retry_async, _retry_sync)
  - app/mr/repo_processor.py      (_prepare_repo_artifacts_sync,
                                   _process_one_repo, _branch_name,
                                   _spec_title, _build_mr_description)
  - app/mr/deploy.py              (_maybe_deploy)

Prerequisite: every helper is currently `_private`; splitting requires
promoting them to package-internal (drop the underscore or expose a
single public façade). Treat as a multi-PR refactor.

REVIEW — not applied.
```

```text
File: app/knowledge_map.py
LOC: 741
Function: generate_org_map (230 LOC)
Problems:
  - Mixes parsing, classification, dependency extraction, file writing
  - Single function does ~5 things in sequence
Suggested split:
  - app/knowledge_map/parsers.py       (_parse_imports, _drop_local_refs,
                                        _normalise_language)
  - app/knowledge_map/classifier.py    (_classify_purpose, _is_api_*,
                                        _detect_api_endpoint)
  - app/knowledge_map/serializer.py    (_atomic_write_json, _iso_utc_now,
                                        _module_from_path)
  - app/knowledge_map/__init__.py      (generate_org_map)

REVIEW — not applied.
```

```text
File: app/config.py
LOC: 651
Problems:
  - Six `BaseSettings` classes + helpers in one file
  - Cross-cutting validators
Suggested split (low risk):
  - app/config/__init__.py        (Settings + get_settings)
  - app/config/aws.py             (AWSSettings, BedrockSettings)
  - app/config/secrets.py         (SecretsManagerSettings)
  - app/config/email.py           (EmailSettings)
  - app/config/summarization.py   (SummarizationSettings)
  - app/config/dynamo.py          (DynamoDBSettings)

Caveat: every consumer imports `from app.config import Settings,
get_settings, resolve_bedrock_text_model_id_for_region`; the package
shim must re-export those names so callers don't break.

REVIEW — not applied.
```

```text
File: app/services/secrets.py
LOC: 650
Problems:
  - Local + boto-backed implementations live in one file
  - Sync helpers (_inline_*) and async public surface mixed
Suggested split:
  - app/services/secrets/__init__.py   (public functions + AppSecrets)
  - app/services/secrets/client.py     (SecretsManagerClient, boto)
  - app/services/secrets/inline.py     (_inline_* file-backed mode)

REVIEW — not applied.
```

```text
File: app/workers/repo_ingest.py
LOC: 623
Function: _process_loaded (147 LOC)
Problems:
  - Intermixes status transitions, summarization scheduling, vector index
    refresh, and error handling
Suggested split:
  - app/workers/repo_ingest/__init__.py  (RepoIngestWorker + entrypoints)
  - app/workers/repo_ingest/pipeline.py  (_process_loaded chunked into smaller
                                          private steps)
  - app/workers/repo_ingest/utils.py     (_safe_url, _regenerate_org_map)

REVIEW — not applied.
```

```text
File: app/services/git_clone.py
LOC: 604
Problems:
  - Error taxonomy + url helpers + git executor + clone/update commands all in
    one module
Suggested split:
  - app/services/git_clone/__init__.py    (clone_repo public API)
  - app/services/git_clone/errors.py      (GitCloneError + subclasses,
                                           _classify_git_error)
  - app/services/git_clone/url_helpers.py (_inject_token, _strip_credentials,
                                           _redact_url, _scrub_text)
  - app/services/git_clone/runner.py      (_run_git, GitCloneService)

REVIEW — not applied.
```

```text
File: app/vector_store.py
LOC: 590
Function: update_chunk_metadata (130 LOC)
Problems:
  - FAISS file IO, validation, search, mutation all in one file
Suggested split (only after tests):
  - app/vector_store/store.py        (init_index, load_index, save_index)
  - app/vector_store/index.py        (index_chunks, search, remove_repo_chunks)
  - app/vector_store/metadata.py     (get_chunks_without_summary,
                                      update_chunk_metadata)

REVIEW — not applied (refactor risk on the metadata mutation paths is
non-trivial).
```

### REVIEW (300–600 LOC)

| File | LOC | Note |
| --- | --- | --- |
| `app/code_understanding.py` | 530 | Monolithic LLM summarizer; clean. |
| `app/db/dynamodb.py` | 515 | Repository pattern; reasonably scoped. |
| `app/db/repos.py` | 485 | Same — keep. |
| `app/code_chunker.py` | 469 | One topic (chunking by language); fine. |
| `app/logging.py` | 446 | Single-purpose module. |
| `app/planning_engine.py` | 413 | Mixed prompt + dispatch + parse — see §3.2. |
| `app/api/v1/orgs.py` | 397 | HTTP layer; OK. |
| `app/api/v1/jobs.py` | 397 | HTTP layer; OK (`create_job`/`get_job` are 76–77 LOC each — fine for FastAPI handlers). |
| `app/services/registry.py` | 376 | OK. |
| `app/db/orgs.py` | 359 | OK. |
| `app/services/gitlab_tokens.py` | 318 | OK. |
| `app/embedding_service.py` | 305 | One topic. |

### SAFE (<300 LOC)

All remaining files. Nothing flagged.

---

## 6. God Functions

Functions ≥100 LOC, sorted by size:

| Function | File | LOC | Notes |
| --- | --- | --- | --- |
| `generate_org_map` | `app/knowledge_map.py:509` | 230 | Multiple responsibilities (see §5). |
| `process_unsummarized_chunks` | `app/summary_worker.py:79` | 209 | Worker loop with retries + metrics; coherent. |
| `ingest_repo` | `app/ingestion_pipeline.py:55` | 181 | Orchestrator; could split into `_scan`, `_chunk`, `_embed`, `_persist` private steps. |
| `search_code` | `app/retrieval_service.py:81` | 162 | Retrieval scoring — single concern; long but coherent. |
| `_process_loaded` | `app/workers/repo_ingest.py:191` | 147 | Highest-leverage split target after `mr_pipeline`. |
| `run_job` | `app/mr_pipeline.py:1259` | 139 | After this session's refactor, run_job is mostly orchestration; remaining length comes from cancel + failure paths. Could trim by extracting cancel handler. |
| `update_chunk_metadata` | `app/vector_store.py:449` | 130 | Critical path; keep behind tests before splitting. |
| `_process_one_repo` | `app/mr_pipeline.py:695` | 115 | Splittable: prepare → write → branch → push → MR. |
| `generate_embeddings_batch` | `app/embedding_service.py:194` | 106 | Coherent batch loop; OK. |
| `process_repositories_step` | `app/mr_pipeline.py:974` | 104 | OK after this session's refactor; consider a `_run_one_repo_iter` helper. |
| `finalize_success_step` | `app/mr_pipeline.py:1080` | 103 | Coherent. |
| `summarize_chunk` | `app/code_understanding.py:427` | 101 | LLM call + parse + validate; fine. |
| `build_context` | `app/context_builder.py:56` | 101 | Recently refactored in this session; mostly docstring + filter loop. |

For each of these, the file-level "Suggested split" in §5 already points to
the right extraction path.

---

## 7. Risky Areas

### 7.1 Broad `except Exception` blocks (76 occurrences in `app/`)

Most are intentional — best-effort artifact IO, status-write failures that
must not mask the primary exception. Hot spots worth a re-review:

| File | Lines | Why it matters |
| --- | --- | --- |
| `app/mr_pipeline.py` | 17 occurrences | The pipeline already uses `_scrub_text` + structured logs around each, but several `# noqa: BLE001 — best-effort` blocks (e.g. `init_job_artifacts`, `save_summary`, `save_plan`) silently downgrade to `log.warning`. Acceptable for IO, but consolidating into a single `_best_effort` helper would make intent obvious. **REVIEW.** |
| `app/workers/repo_ingest.py` | 7 | `process_repo` swallows summarizer errors so ingestion still flips to READY — this is a deliberate design decision documented inline; do not change. |
| `app/api/v1/jobs.py` | 3 | All convert to typed `AppError`. Fine. |

### 7.2 Silent failures (resolved this session, watch list)

- `app/mr_pipeline.process_repositories_step` previously continued on
  per-repo `Exception` and then returned a "success" — **fixed earlier in
  this conversation** (now collects `errors` and the caller raises
  `RepositoryProcessingError`).
- `app/mr_pipeline._collect_repo_ids` previously fell back to "all READY
  repos" when the plan was empty — **fixed earlier in this conversation**
  (now strict, raises if no plan-selected repos).

### 7.3 Dangerous overwrite logic — guarded but worth a test

`_safe_write_under_root` (`app/mr_pipeline.py:123`) now enforces:

- Path-traversal protection (`..`, escape via `commonpath`).
- Suspicious-size guard (new > 1.5× old → reject).
- Similarity guard (`change_type == "modify"` and similarity < 0.4 → reject).

These are correct but **untested** (no test under `scripts/` exercises a
modify-then-reject path). **REVIEW**: add a smoke test against a
known-good fixture before scaling. Not a code-cleanup concern.

### 7.4 `__import__("logging").ERROR` workaround

`app/services/registry.py:115` uses `__import__("logging").ERROR` instead
of importing `logging` once. Confusing — and `import logging` is already
present at the top of the file but unused. **SAFE — APPLIED below.**

### 7.5 Module-level `from datetime import` inside `_now_ts`

`app/mr_pipeline.py:64` re-imports `datetime` and `timezone` on every call.
Cheap (cached by Python's import machinery) but distracting. **SAFE —
APPLIED below** (moved to module-level imports).

---

## Phase 2 — SAFE Cleanup Applied

The following were applied automatically. Every change is byte-compile clean
and produces no new lint warnings. None alter business logic or external
interfaces.

### Removed unused imports

| File | Symbol | Reason |
| --- | --- | --- |
| `app/api/v1/jobs.py` | `Depends` (from `fastapi`) | No `Depends(...)` call site. |
| `app/code_understanding.py` | `re` (top-level) | No `re.` usage. |
| `app/ingestion_pipeline.py` | `List` (from `typing`) | Unused type alias. |
| `app/services/git_clone.py` | `Tuple` (from `typing`) | Unused type alias. |

### Refactored `__import__` workaround

| File | Change |
| --- | --- |
| `app/services/registry.py` | Replaced `level=__import__("logging").ERROR` with `level=logging.ERROR` and now actually use the existing `import logging`. No behavior change. |

### Tightened `_now_ts` in `app/mr_pipeline.py`

| File | Change |
| --- | --- |
| `app/mr_pipeline.py` | Moved `from datetime import datetime, timezone` to the module-level import block. Removes a per-call import statement; output is identical. |

---

## Phase 3 — Final Output

### Removed Files

None. Every file we considered for removal was found to be either documented
public scaffolding (the `agents` / `rag` / `workers/base` ABCs), or
runtime/dev-only and out of scope.

### Removed Functions

None. No high-confidence dead function detected.

### Consolidated Logic

None applied. Two consolidation candidates were identified and **deferred**
to REVIEW (Bedrock dispatch into `app/bedrock_runtime.py`; LLM JSON
post-processing into `app/llm_utils.py`).

### Split Files

None applied. Six refactor candidates documented in §5 with concrete split
proposals: `mr_pipeline.py`, `knowledge_map.py`, `config.py`,
`services/secrets.py`, `workers/repo_ingest.py`, `services/git_clone.py`.

### Remaining Large Files

Top ten by LOC, after Phase 2:

```
1410  app/mr_pipeline.py        REFACTOR
 741  app/knowledge_map.py      REFACTOR
 651  app/config.py             REFACTOR
 650  app/services/secrets.py   REFACTOR
 623  app/workers/repo_ingest.py REFACTOR
 604  app/services/git_clone.py REFACTOR
 590  app/vector_store.py       REFACTOR
 530  app/code_understanding.py REVIEW
 515  app/db/dynamodb.py        REVIEW
 485  app/db/repos.py           REVIEW
```

### Remaining Risk Areas

1. `mr_pipeline._safe_write_under_root` overwrite/similarity guards are
   defensive but untested. Add a fixture test under `scripts/` or a unit
   test before scaling traffic.
2. The Bedrock-dispatch duplication across `code_understanding.py`,
   `planning_engine.py`, and `embedding_service.py` will silently diverge
   over time. Schedule a consolidation into `app/bedrock_runtime.py`.
3. `app/agents/`, `app/rag/`, and `app/workers/base.py` ABCs are
   documented placeholders. Decide explicitly whether to evolve them or
   remove them; do not let them rot.
4. Two SPA scaffolds (`frontend/` Vite, `web/` Next.js) co-exist. Confirm
   ownership.

---

**Net effect of Phase 2**: 4 unused imports removed, one `__import__`
workaround replaced with a normal reference, and one per-call `from` import
hoisted to module scope. No runtime behavior change.
