"""Build a concise per-org knowledge map from ingested code chunks.

Produces ``./repos/{org_id}/org_map.json`` (or under ``BASE_STORAGE_PATH``
if the env is set) containing:

* ``services``        — purpose-based groupings (``auth``, ``cart-checkout``,
                        ``api-layer``, …) derived from chunk ``summary`` /
                        ``purpose`` when the code-understanding pass has run,
                        with a safe fallback to symbol + file-path keywords.
* ``api_endpoints``   — list of HTTP/API entrypoints detected via the LLM
                        ``type == "api"`` signal, common route decorators in
                        the chunk code (FastAPI / Flask / Spring / NestJS /
                        Express / Go), or file-path heuristics.
* ``dependencies``    — per-repo ``{file: [imported_modules]}`` adjacency map
                        built from the LLM ``dependencies`` field when
                        available, else parsed from ``import`` / ``require``
                        statements in the raw code.
* ``modules``         — ``{repo_id: [module_path, ...]}`` from folder
                        structure (kept from v1 for continuity).
* ``key_symbols``     — most frequent ``metadata.symbol`` values.
* ``common_patterns`` — suffix / language / file-ext histograms.

Every list is capped so the map stays bounded on very large orgs.

The function is always called with whatever metadata is present — it works
on raw chunks from :mod:`app.code_chunker` *and* on vector-store entries of
the shape ``{"chunk_hash", "metadata": {...}}``.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, List

from app.logging import get_logger

log = get_logger(__name__)

_ORG_MAP_FILENAME: Final[str] = "org_map.json"
_REPOS_DIRNAME: Final[str] = "repos"
_TOP_MODULE_DEPTH: Final[int] = 2

# Sizing caps — keep the JSON readable even on large orgs.
_MAX_MODULES_PER_REPO: Final[int] = 200
_MAX_KEY_SYMBOLS: Final[int] = 50
_MAX_SYMBOLS_PER_SERVICE: Final[int] = 100
_MAX_FILES_PER_SERVICE: Final[int] = 100
_MAX_API_ENDPOINTS: Final[int] = 500
_MAX_DEPS_PER_FILE: Final[int] = 50
_MAX_DEP_FILES_PER_REPO: Final[int] = 500

# Ignore symbols that are just line-window placeholders from the chunker (``lines 1-200``).
_LINE_WINDOW_RE: Final[re.Pattern[str]] = re.compile(r"^lines?\s+\d+-\d+$")
_FILE_SYMBOL_RE: Final[re.Pattern[str]] = re.compile(r"^file:")
_PART_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"#part\d+$")

# Common naming patterns — suffixes we treat as architectural signals.
_PATTERN_SUFFIXES: Final[tuple[str, ...]] = (
    "Service",
    "Controller",
    "Repository",
    "Handler",
    "Manager",
    "Factory",
    "Client",
    "Provider",
    "Adapter",
    "Dao",
    "Model",
    "View",
    "Middleware",
    "Gateway",
    "Listener",
    "Worker",
    "Test",
    "Spec",
)

# --------------------------------------------------------------------------- #
# Purpose classifier                                                          #
# --------------------------------------------------------------------------- #

# Ordered from specific domains to generic concerns — first match wins.
_PURPOSE_RULES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("auth", ("auth", "login", "logout", "signin", "signup", "session",
              "jwt", "oauth", "credential", "password", "token rotation")),
    ("cart-checkout", ("cart", "basket", "checkout", "order ", "orders",
                       "sku", "line item", "lineitem")),
    ("payments", ("payment", "billing", "invoice", "charge", "refund",
                  "stripe", "paypal", "subscription")),
    ("user-management", ("profile", "account", "register user", "user record",
                         "customer", "member")),
    ("notifications", ("notify", "notification", "email", "sms", "webhook",
                       "push notification")),
    ("search", ("search", "retrieval", "embed", "rerank", "faiss", "vector")),
    ("observability", ("log ", "logging", "trace", "telemetry", "metric",
                       "monitor", "instrument")),
    ("persistence", ("database", "dynamodb", "postgres", "mysql", "mongo",
                     "sql ", "query builder", "dao", "repository", "orm",
                     "schema", "table row")),
    ("configuration", ("configuration", "settings", "environment variable",
                       "feature flag")),
    ("testing", ("unit test", "integration test", "fixture", "mock ")),
    # Generic routing bucket only fires if nothing more specific matched.
    ("api-layer", ("api endpoint", "http handler", "rest endpoint", "route handler",
                   "controller", "request handler", "graphql resolver")),
    ("utilities", ("utility helper", "helper function", "shared helper",
                   "miscellaneous")),
)

_UNCATEGORIZED: Final[str] = "uncategorized"

# --------------------------------------------------------------------------- #
# API detection regexes                                                       #
# --------------------------------------------------------------------------- #

# @app.get("/path"), @router.post("/path"), @api.put("/path"), r.GET("/path"),
# srv.POST("/path"), router_v1.patch("/path") — covers FastAPI, Starlette,
# Express, Fastify, Gin, Echo.
_ROUTE_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:@|\b)"
    r"(?:app|router|router_v\d+|bp|blueprint|api|srv|server|r|e|routes)"
    r"\s*\.\s*(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\s*\("
    r"\s*['\"`]([^'\"`]+)['\"`]",
    re.IGNORECASE,
)

# Flask: @app.route("/path", methods=["GET", "POST"])
_FLASK_ROUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"@[\w\.]+\.route\(\s*['\"]([^'\"]+)['\"]"
    r"(?:[^)]*methods\s*=\s*\[([^\]]+)\])?",
    re.DOTALL,
)

# Spring: @GetMapping("/path"), @RequestMapping(value="/path", method=GET)
# NestJS: @Get("/path"), @Post("/path")
_DECORATOR_METHOD_RE: Final[re.Pattern[str]] = re.compile(
    r"@(Get|Post|Put|Delete|Patch)(?:Mapping)?\s*\(\s*"
    r"(?:value\s*=\s*)?['\"`]([^'\"`]+)['\"`]"
)

# Path fragments that strongly suggest an API/controller layer.
_API_FILE_HINTS: Final[tuple[str, ...]] = (
    "/api/", "/apis/", "/routes/", "/controllers/", "/handlers/",
    "/endpoints/", "/resources/", "/views.py", "/urls.py",
)

# Symbol-shaped hints (tail-only match to ignore class path prefixes).
_API_SYMBOL_SUFFIXES: Final[tuple[str, ...]] = (
    "Controller", "Handler", "Resource", "View", "Endpoint", "Route",
)
_API_SYMBOL_PREFIXES: Final[tuple[str, ...]] = (
    "route_", "handle_", "api_", "endpoint_",
)

# --------------------------------------------------------------------------- #
# Import parsers (one per language family + a generic fallback)               #
# --------------------------------------------------------------------------- #

# Python: ``from X import Y`` and ``import A, B.C``.
_PY_FROM_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*from\s+([\w\.]+)\s+import\b", re.MULTILINE
)
_PY_IMPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*import\s+([\w\.,\s]+?)(?:\s+as\s+\w+)?\s*$", re.MULTILINE
)

# JS/TS: ``import … from '…'``, ``require('…')``, dynamic ``import('…')``.
_JS_IMPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"""(?:
        \bimport\s+(?:[\w\*\{\}\s,]+\s+from\s+)?['"]([^'"]+)['"]
        |
        \brequire\s*\(\s*['"]([^'"]+)['"]
        |
        \bimport\s*\(\s*['"]([^'"]+)['"]
    )""",
    re.VERBOSE,
)

# Java / Kotlin / Scala: ``import a.b.c;``, ``import a.b.*;``, optional
# ``static`` and optional semicolon (Kotlin/Scala omit it).
_JAVA_IMPORT_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w\.]+)(?:\.\*)?\s*;?\s*$", re.MULTILINE
)

# Go: either single ``import "x"`` or grouped ``import ( "a"; "b" )``.
_GO_IMPORT_SINGLE_RE: Final[re.Pattern[str]] = re.compile(
    r'^\s*import\s+(?:\w+\s+)?"([^"]+)"\s*$', re.MULTILINE
)
_GO_IMPORT_GROUP_RE: Final[re.Pattern[str]] = re.compile(
    r"import\s*\(([^)]+)\)", re.DOTALL
)
_GO_IMPORT_ITEM_RE: Final[re.Pattern[str]] = re.compile(
    r'(?:\w+\s+)?"([^"]+)"'
)

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _repos_base() -> Path:
    """Where ``./repos/{org_id}`` lives.

    Precedence:
    1. env ``KNOWLEDGE_MAP_ROOT`` (explicit override)
    2. env ``BASE_STORAGE_PATH`` + ``/repos`` (matches ``app.storage_manager``)
    3. CWD-relative ``./repos`` (matches spec default)
    """
    override = (os.environ.get("KNOWLEDGE_MAP_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    base = (os.environ.get("BASE_STORAGE_PATH") or "").strip()
    if base:
        return (Path(os.path.expanduser(base)) / _REPOS_DIRNAME).resolve()
    return (Path.cwd() / _REPOS_DIRNAME).resolve()


def _org_dir(org_id: str) -> Path:
    s = (org_id or "").strip()
    if not s:
        raise ValueError("org_id must be non-empty")
    return _repos_base() / s


def _module_from_path(file_path: str, depth: int = _TOP_MODULE_DEPTH) -> str:
    """``src/auth/login.py`` -> ``src/auth`` (depth=2). Excludes file name."""
    parts = (file_path or "").replace("\\", "/").strip("/").split("/")
    parts = [p for p in parts if p not in ("", ".", "..")]
    if len(parts) <= 1:
        return parts[0] if parts else "<root>"
    return "/".join(parts[: min(depth, len(parts) - 1)])


def _clean_symbol(sym: str) -> str:
    s = (sym or "").strip()
    if not s:
        return ""
    if _LINE_WINDOW_RE.match(s):
        return ""
    if _FILE_SYMBOL_RE.match(s):
        return ""
    return _PART_SUFFIX_RE.sub("", s)


def _short_name(sym: str) -> str:
    """``ClassName.methodName`` -> ``methodName`` (tail only, for patterns)."""
    return (sym or "").rsplit(".", 1)[-1]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=True, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def _get(c: dict, key: str, default: str = "") -> str:
    """Read ``key`` from the chunk or its nested ``metadata``, as a string."""
    if key in c and isinstance(c[key], (str, int, float)):
        return str(c[key])
    meta = c.get("metadata")
    if isinstance(meta, dict) and key in meta:
        v = meta[key]
        if isinstance(v, (str, int, float)):
            return str(v)
    return default


def _get_list(c: dict, key: str) -> list[str]:
    """Read ``key`` as a ``list[str]`` from the chunk or its nested metadata."""
    for source in (c, c.get("metadata") if isinstance(c.get("metadata"), dict) else None):
        if not isinstance(source, dict):
            continue
        v = source.get(key)
        if isinstance(v, list):
            return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return []


# --------------------------------------------------------------------------- #
# Purpose classification                                                      #
# --------------------------------------------------------------------------- #


def _classify_purpose(
    *,
    summary: str,
    purpose: str,
    code_type: str,
    symbol: str,
    file_path: str,
) -> str:
    """Map a chunk to a purpose label.

    Priority:
    1. LLM ``summary`` / ``purpose`` text (strongest signal).
    2. ``symbol`` + ``file_path`` fallback when no summary is available.
    3. LLM ``type == "api"`` as a last-resort hint when nothing matches.
    4. Otherwise the chunk is ``uncategorized``.
    """
    llm_hay_parts = [summary, purpose]
    fallback_hay_parts = [symbol, file_path]
    llm_hay = " ".join(p for p in llm_hay_parts if p).lower()
    fallback_hay = " ".join(p for p in fallback_hay_parts if p).lower()

    # Try LLM signals first.
    if llm_hay:
        for label, keywords in _PURPOSE_RULES:
            if any(kw in llm_hay for kw in keywords):
                return label

    # Fall back to symbol/file keywords.
    if fallback_hay:
        for label, keywords in _PURPOSE_RULES:
            if any(kw in fallback_hay for kw in keywords):
                return label

    if (code_type or "").strip().lower() == "api":
        return "api-layer"
    return _UNCATEGORIZED


# --------------------------------------------------------------------------- #
# API endpoint detection                                                      #
# --------------------------------------------------------------------------- #


def _is_api_file(file_path: str) -> bool:
    f = ("/" + (file_path or "").replace("\\", "/").lstrip("/")).lower()
    return any(hint in f for hint in _API_FILE_HINTS)


def _is_api_symbol(symbol: str) -> bool:
    tail = _short_name(symbol)
    if not tail:
        return False
    if any(tail.endswith(s) and len(tail) > len(s) for s in _API_SYMBOL_SUFFIXES):
        return True
    low = tail.lower()
    return any(low.startswith(p) for p in _API_SYMBOL_PREFIXES)


def _extract_method_and_path(code: str) -> tuple[str, str] | None:
    """Scan ``code`` for a common route decorator / call; return (METHOD, path)."""
    if not code:
        return None

    m = _ROUTE_CALL_RE.search(code)
    if m:
        return m.group(1).upper(), m.group(2)

    m = _DECORATOR_METHOD_RE.search(code)
    if m:
        verb = m.group(1).upper()
        # Spring's @RequestMapping is ambiguous; we normalise @Get/@Post/etc.
        return verb.upper(), m.group(2)

    m = _FLASK_ROUTE_RE.search(code)
    if m:
        path = m.group(1)
        methods_block = m.group(2) or ""
        # Honour the author's declared order: pick the first literal string
        # inside ``methods=[...]``. Default to GET when none were listed.
        listed = re.findall(r"['\"](\w+)['\"]", methods_block)
        first = listed[0].upper() if listed else "GET"
        return first, path

    return None


def _detect_api_endpoint(
    *,
    repo_id: str,
    file_path: str,
    symbol: str,
    code: str,
    summary: str,
    code_type: str,
) -> dict[str, Any] | None:
    """Return an endpoint record or ``None`` if this chunk is not API-like."""
    type_is_api = (code_type or "").strip().lower() == "api"

    method_path = _extract_method_and_path(code)

    # Evidence chain: strongest → weakest.
    if method_path:
        method, path = method_path
        source = "decorator"
    elif type_is_api:
        method, path = "", ""
        source = "llm"
    elif _is_api_file(file_path) or _is_api_symbol(symbol):
        method, path = "", ""
        source = "heuristic"
    else:
        return None

    return {
        "repo_id": repo_id,
        "file": file_path,
        "symbol": symbol,
        "method": method,
        "path": path,
        "summary": summary,
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Dependency extraction                                                       #
# --------------------------------------------------------------------------- #


def _normalise_language(language: str) -> str:
    lang = (language or "").strip().lower()
    if lang in ("js", "jsx", "javascript"):
        return "javascript"
    if lang in ("ts", "tsx", "typescript"):
        return "typescript"
    if lang in ("kt", "kotlin"):
        return "kotlin"
    if lang in ("scala",):
        return "scala"
    return lang


def _parse_imports(code: str, language: str) -> set[str]:
    """Return a set of imported module/package names from ``code``."""
    if not code:
        return set()
    lang = _normalise_language(language)
    deps: set[str] = set()

    if lang == "python":
        for m in _PY_FROM_RE.finditer(code):
            deps.add(m.group(1).strip())
        for m in _PY_IMPORT_RE.finditer(code):
            for item in m.group(1).split(","):
                name = item.strip().split()[0] if item.strip() else ""
                if name:
                    deps.add(name)
    elif lang in ("javascript", "typescript"):
        for m in _JS_IMPORT_RE.finditer(code):
            mod = m.group(1) or m.group(2) or m.group(3)
            if mod:
                deps.add(mod.strip())
    elif lang in ("java", "kotlin", "scala"):
        for m in _JAVA_IMPORT_RE.finditer(code):
            deps.add(m.group(1).strip())
    elif lang == "go":
        for m in _GO_IMPORT_SINGLE_RE.finditer(code):
            deps.add(m.group(1).strip())
        for grp in _GO_IMPORT_GROUP_RE.finditer(code):
            for item in _GO_IMPORT_ITEM_RE.finditer(grp.group(1)):
                deps.add(item.group(1).strip())
    # Unknown languages: we leave parsing to the LLM dependencies field.

    return _drop_local_refs(deps)


def _drop_local_refs(deps: set[str]) -> set[str]:
    """Filter out intra-file/relative refs that don't make useful graph nodes.

    Relative Python/JS imports (``.x``, ``../y``), absolute filesystem paths
    (``/foo``), and empty strings all get dropped.
    """
    return {d for d in deps if d and not d.startswith((".", "/"))}


def _collect_dependencies(
    *,
    code: str,
    language: str,
    llm_deps: list[str],
) -> list[str]:
    """Prefer LLM-derived dependencies; fall back to code-parsed imports.

    Merged when both are present so a static-import-only module still
    contributes when the LLM missed an internal call.
    """
    llm_clean = {
        d.strip() for d in (llm_deps or [])
        if isinstance(d, str) and d.strip()
    }
    merged = _drop_local_refs(llm_clean)
    merged.update(_parse_imports(code, language))
    # Stable order + cap.
    ordered = sorted(merged)
    return ordered[:_MAX_DEPS_PER_FILE]


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def generate_org_map(org_id: str, all_chunks: List[dict]) -> dict[str, Any]:
    """
    Build and persist an org-level knowledge map from ingested chunks.

    Each item of ``all_chunks`` is expected to carry at least ``repo_id``
    (top-level or nested in ``metadata``), ``file``, ``symbol``, and
    ``language``. When the :mod:`app.summary_worker` has run, the chunks
    also carry ``summary``, ``purpose``, ``dependencies``, and ``type``
    (as produced by :mod:`app.code_understanding`). The map uses those
    signals when available and transparently falls back to the raw code /
    symbol / file path otherwise.

    Returns the written dict; also saves to
    ``{root}/repos/{org_id}/org_map.json``.
    """
    if not (org_id or "").strip():
        raise ValueError("org_id must be non-empty")

    repo_modules: dict[str, set[str]] = {}
    symbol_counts: Counter[str] = Counter()
    suffix_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    file_ext_counts: Counter[str] = Counter()
    files_seen: set[tuple[str, str]] = set()

    # services: purpose -> {repos, files, symbols, sample_summaries}
    service_repos: dict[str, set[str]] = {}
    service_files: dict[str, set[str]] = {}
    service_symbol_counts: dict[str, Counter[str]] = {}
    service_sample_summaries: dict[str, list[str]] = {}

    # dependencies: repo_id -> file -> set[str]
    deps_graph: dict[str, dict[str, set[str]]] = {}

    api_endpoints_raw: list[dict[str, Any]] = []
    chunks_in = 0
    chunks_with_summary = 0

    for c in all_chunks or []:
        if not isinstance(c, dict):
            continue
        chunks_in += 1

        repo_id = _get(c, "repo_id")
        file_ = _get(c, "file")
        raw_symbol = _get(c, "symbol")
        symbol = _clean_symbol(raw_symbol)
        language = _get(c, "language").lower()
        code = _get(c, "code")
        summary = _get(c, "summary")
        purpose = _get(c, "purpose")
        code_type = _get(c, "type")
        llm_deps = _get_list(c, "dependencies")

        if summary:
            chunks_with_summary += 1

        if not repo_id:
            continue

        # --- modules + file histograms (kept for continuity) --------------
        if file_:
            module = _module_from_path(file_)
            repo_modules.setdefault(repo_id, set()).add(module)
            files_seen.add((repo_id, file_))
            _, ext = os.path.splitext(file_)
            if ext:
                file_ext_counts[ext.lower()] += 1
        if symbol:
            symbol_counts[symbol] += 1
            short = _short_name(symbol)
            for suf in _PATTERN_SUFFIXES:
                if short != suf and short.endswith(suf) and len(short) > len(suf):
                    suffix_counts[suf] += 1
                    break
        if language:
            language_counts[language] += 1

        # --- purpose-based services --------------------------------------
        label = _classify_purpose(
            summary=summary,
            purpose=purpose,
            code_type=code_type,
            symbol=symbol or raw_symbol,
            file_path=file_,
        )
        service_repos.setdefault(label, set()).add(repo_id)
        if file_:
            service_files.setdefault(label, set()).add(file_)
        if symbol:
            service_symbol_counts.setdefault(label, Counter())[symbol] += 1
        if summary and len(service_sample_summaries.setdefault(label, [])) < 5:
            if summary not in service_sample_summaries[label]:
                service_sample_summaries[label].append(summary)

        # --- API endpoints ------------------------------------------------
        endpoint = _detect_api_endpoint(
            repo_id=repo_id,
            file_path=file_,
            symbol=symbol or raw_symbol,
            code=code,
            summary=summary,
            code_type=code_type,
        )
        if endpoint:
            api_endpoints_raw.append(endpoint)

        # --- dependency graph --------------------------------------------
        if file_:
            deps = _collect_dependencies(code=code, language=language, llm_deps=llm_deps)
            if deps:
                per_repo = deps_graph.setdefault(repo_id, {})
                bucket = per_repo.setdefault(file_, set())
                bucket.update(deps)

    # ----- modules (legacy shape, preserved) ------------------------------
    modules_by_repo: dict[str, list[str]] = {}
    for repo_id, modset in repo_modules.items():
        mods = sorted(modset)
        if len(mods) > _MAX_MODULES_PER_REPO:
            mods = mods[:_MAX_MODULES_PER_REPO]
        modules_by_repo[repo_id] = mods

    # ----- services (purpose-based) ---------------------------------------
    services: dict[str, dict[str, Any]] = {}
    # Stable order: most populous purposes first, uncategorized last.
    purpose_order = sorted(
        service_repos.keys(),
        key=lambda lbl: (
            lbl == _UNCATEGORIZED,
            -sum(service_symbol_counts.get(lbl, Counter()).values()),
            lbl,
        ),
    )
    for label in purpose_order:
        sym_counter = service_symbol_counts.get(label, Counter())
        top_syms = [s for s, _ in sym_counter.most_common(_MAX_SYMBOLS_PER_SERVICE)]
        files_sorted = sorted(service_files.get(label, set()))
        if len(files_sorted) > _MAX_FILES_PER_SERVICE:
            files_sorted = files_sorted[:_MAX_FILES_PER_SERVICE]
        services[label] = {
            "repos": sorted(service_repos.get(label, set())),
            "files": files_sorted,
            "symbols": top_syms,
            "sample_summaries": service_sample_summaries.get(label, []),
            "chunk_count": int(sum(sym_counter.values())),
        }

    # ----- API endpoints: dedup + cap -------------------------------------
    seen_endpoint_keys: set[tuple[str, str, str, str, str]] = set()
    api_endpoints: list[dict[str, Any]] = []
    for ep in api_endpoints_raw:
        key = (
            ep["repo_id"],
            ep["file"],
            ep["symbol"],
            ep["method"],
            ep["path"],
        )
        if key in seen_endpoint_keys:
            continue
        seen_endpoint_keys.add(key)
        api_endpoints.append(ep)
        if len(api_endpoints) >= _MAX_API_ENDPOINTS:
            break
    api_endpoints.sort(
        key=lambda e: (e["repo_id"], e["file"], e["path"], e["method"], e["symbol"])
    )

    # ----- dependencies: finalise sets into sorted lists + caps -----------
    dependencies: dict[str, dict[str, list[str]]] = {}
    total_edges = 0
    for repo_id, files_map in deps_graph.items():
        # Cap files per repo by insertion order (already deterministic since
        # we iterate chunks in order); then sort the final dict for readable
        # JSON output.
        items = list(files_map.items())[:_MAX_DEP_FILES_PER_REPO]
        out_repo: dict[str, list[str]] = {}
        for f, s in sorted(items, key=lambda x: x[0]):
            deps_sorted = sorted(s)[:_MAX_DEPS_PER_FILE]
            out_repo[f] = deps_sorted
            total_edges += len(deps_sorted)
        dependencies[repo_id] = out_repo

    # ----- legacy histograms ----------------------------------------------
    key_symbols = [s for s, _ in symbol_counts.most_common(_MAX_KEY_SYMBOLS)]
    common_patterns: list[dict[str, Any]] = []
    for suf, count in suffix_counts.most_common():
        common_patterns.append({"kind": "symbol_suffix", "value": suf, "count": count})
    for lang, count in language_counts.most_common():
        common_patterns.append({"kind": "language", "value": lang, "count": count})
    for ext, count in file_ext_counts.most_common(10):
        common_patterns.append({"kind": "file_ext", "value": ext, "count": count})

    body: dict[str, Any] = {
        "org_id": org_id,
        "generated_at": _iso_utc_now(),
        "version": 2,
        "services": services,
        "api_endpoints": api_endpoints,
        "dependencies": dependencies,
        "modules": modules_by_repo,
        "key_symbols": key_symbols,
        "common_patterns": common_patterns,
        "stats": {
            "chunks_considered": chunks_in,
            "chunks_with_summary": chunks_with_summary,
            "repos": len(modules_by_repo),
            "unique_files": len(files_seen),
            "unique_symbols": len(symbol_counts),
            "services_identified": len(services),
            "api_endpoints": len(api_endpoints),
            "dependency_edges": total_edges,
        },
    }

    out_path = _org_dir(org_id) / _ORG_MAP_FILENAME
    _atomic_write_json(out_path, body)
    log.info(
        "knowledge_map: wrote %s chunks=%d with_summary=%d repos=%d services=%d "
        "api_endpoints=%d dep_edges=%d",
        out_path,
        chunks_in,
        chunks_with_summary,
        len(modules_by_repo),
        len(services),
        len(api_endpoints),
        total_edges,
    )
    return body


__all__ = ["generate_org_map"]
