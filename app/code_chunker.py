"""Split source files into RAG-style code chunks with stable ids and hashes."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Final, List, Sequence, Tuple

from app.logging import get_logger

log = get_logger(__name__)

# large_file: 150–300 line chunks (200 default; merge if tail < 150)
_LARGE_CHUNK_LINES: Final[int] = 200
_LARGE_MERGE_IF_TAIL_LT: Final[int] = 150
_FALLBACK_CHUNK_LINES: Final[int] = 200
_OVERSIZE_SPLIT: Final[int] = 300

_RE_JAVA_CLASS = re.compile(
    r"^\s*(?:public|protected|private)?\s*(?:abstract\s+)?"
    r"(?:final\s+)?(class|interface|enum|record)\s+(\w+)",
    re.MULTILINE,
)
# Conservative: name before '(' must look like a method, not a keyword path.
_RE_JAVA_METHOD = re.compile(
    r"^\s*(?:public|protected|private)\s+"
    r"[\s\w<>\[\],.?]+\s+(\w+)\s*\(",
    re.MULTILINE,
)
_RE_TS_FUNC = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*(\w+)\s*\(",
    re.MULTILINE,
)
_RE_TS_CLASS = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE
)
_RE_TS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)\s*=>|\s*\(|\w+\s*=>)",
    re.MULTILINE,
)
_RE_PHP = re.compile(
    r"^\s*(?:abstract\s+)?class\s+(\w+)|^\s*function\s+(\w+)\s*\(",
    re.MULTILINE,
)


@dataclass(frozen=True)
class _Range:
    start_line: int  # 1-based inclusive
    end_line: int  # 1-based inclusive
    symbol: str


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8", errors="replace")).hexdigest()


def _make_chunk_id(
    org_id: str,
    repo_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    chunk_hash: str,
) -> str:
    base = f"{org_id}\0{repo_id}\0{file_path}\0{start_line}\0{end_line}\0{chunk_hash}"
    return hashlib.sha256(base.encode("utf-8", errors="replace")).hexdigest()[:32]


def _get_lines_and_content(file_data: dict[str, Any]) -> tuple[list[str], str]:
    content = (file_data.get("content") or "").replace("\r\n", "\n").replace("\r", "\n")
    if not content:
        return [], ""
    return content.splitlines(), content


def _line_slice(lines: list[str], start: int, end: int) -> str:
    """1-based inclusive line numbers."""
    if not lines or start < 1 or end < start or start > len(lines):
        return ""
    e = min(end, len(lines))
    s = min(start, e)
    return "\n".join(lines[s - 1 : e])


def _split_by_line_window(
    n: int, window: int, merge_tail_under: int, symbol_prefix: str
) -> list[Tuple[int, int, str]]:
    if n == 0:
        return []
    w = min(max(1, window), n)
    segs: list[Tuple[int, int]] = []
    i = 0
    while i < n:
        take = min(w, n - i)
        segs.append((i + 1, i + take))
        i += take
    if len(segs) >= 2 and segs[-1][1] - segs[-1][0] + 1 < merge_tail_under:
        a0, _ = segs[-2]
        b1 = segs[-1][1]
        segs.pop()
        segs[-1] = (a0, b1)
    return [(a, b, f"{symbol_prefix}{a}-{b}") for a, b in segs]


def _split_oversize_ranges_v2(ranges: list[_Range], max_lines: int) -> list[_Range]:
    out: list[_Range] = []
    for r in ranges:
        span = r.end_line - r.start_line + 1
        if span <= max_lines or span < 1:
            out.append(r)
            continue
        s, end = r.start_line, r.end_line
        part = 0
        while s <= end:
            part += 1
            e = min(s + _FALLBACK_CHUNK_LINES - 1, end)
            sym = f"{r.symbol}#part{part}" if r.symbol else f"block#part{part}"
            out.append(_Range(s, e, sym))
            s = e + 1
    return out


def _python_ranges(content: str) -> list[_Range] | None:
    try:
        tree = ast.parse(content, mode="exec")
    except SyntaxError as exc:
        log.debug("code_chunker: ast parse failed (python): %s", exc)
        return None
    out: list[_Range] = []

    def add_top(node: ast.AST) -> None:
        if not hasattr(node, "lineno") or not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            return
        end = getattr(node, "end_lineno", None) or node.lineno
        name = getattr(node, "name", "?")
        nlines = end - node.lineno + 1
        if isinstance(node, ast.ClassDef) and nlines > _OVERSIZE_SPLIT:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and hasattr(
                    item, "lineno"
                ):
                    e2 = getattr(item, "end_lineno", None) or item.lineno
                    out.append(
                        _Range(
                            item.lineno,
                            e2,
                            f"{name}.{getattr(item, 'name', '?')}",
                        )
                    )
                elif isinstance(item, ast.ClassDef) and hasattr(item, "lineno"):
                    e2 = getattr(item, "end_lineno", None) or item.lineno
                    out.append(
                        _Range(
                            item.lineno,
                            e2,
                            f"{name}.{getattr(item, 'name', '?')}",
                        )
                    )
        else:
            out.append(_Range(node.lineno, end, str(name)))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add_top(node)
    if not out:
        return None
    out.sort(key=lambda r: (r.start_line, r.end_line))
    return out


def _end_from_line_index(lines: list[str], idx: int) -> int | None:
    """0-based first line; find matching ``}`` for first ``{`` with basic comment/string scan."""
    t = "\n".join(lines[idx:])
    depth = 0
    st = 0
    j = 0
    first_open: int | None = None
    while j < len(t):
        c = t[j]
        nxt = t[j + 1] if j + 1 < len(t) else ""
        if st == 0:
            if t[j : j + 2] == "//":
                st = 1
                j += 2
                continue
            if t[j : j + 2] == "/*":
                st = 2
                j += 2
                continue
            if c == "'":
                st = 3
            elif c == '"':
                st = 4
            elif c == "`":
                st = 5
            elif c == "{":
                if first_open is None:
                    first_open = j
                depth += 1
            elif c == "}" and depth and first_open is not None:
                depth -= 1
                if depth == 0:
                    return idx + t[: j + 1].count("\n") + 1
        elif st == 1:
            if c == "\n":
                st = 0
        elif st == 2:
            if c == "*" and nxt == "/":
                st = 0
                j += 2
                continue
        elif st in (3, 4, 5):
            if c == "\\" and st != 5:
                j += 1
            elif (st == 3 and c == "'") or (st == 4 and c == '"') or (st == 5 and c == "`"):
                st = 0
        j += 1
    return None


def _contained_in_any(r: _Range, others: list[_Range]) -> bool:
    for o in others:
        if o is r:
            continue
        if o.start_line <= r.start_line and r.end_line <= o.end_line:
            return True
    return False


def _match_line_no(content: str, m: re.Match[str]) -> int:
    return content[: m.start()].count("\n") + 1


def _java_class_ranges(lines: list[str], content: str) -> list[_Range]:
    ranges: list[_Range] = []
    for m in _RE_JAVA_CLASS.finditer(content):
        line_no = _match_line_no(content, m)
        name = m.group(2)
        e = _end_from_line_index(lines, line_no - 1) or min(line_no + 12, len(lines))
        e = min(max(e, line_no), len(lines))
        ranges.append(_Range(line_no, e, str(name)))
    return _dedupe_sort(ranges)


def _java_method_ranges_in_scope(
    lines: list[str], _content: str, start: int, end: int, class_name: str
) -> list[_Range]:
    """1-based [start, end] slice of file; method name lines (with brace end)."""
    sub = "\n".join(lines[start - 1 : end])
    out: list[_Range] = []
    skip_names = {
        "if",
        "for",
        "while",
        "try",
        "catch",
        "switch",
        "synchronized",
    }
    for m in _RE_JAVA_METHOD.finditer(sub):
        name = m.group(1)
        if name in skip_names:
            continue
        rel_line = sub[: m.start()].count("\n") + 1
        abs_line = start - 1 + rel_line
        e2 = _end_from_line_index(lines, abs_line - 1) or min(abs_line + 20, end)
        e2 = min(max(e2, abs_line), end)
        out.append(_Range(abs_line, e2, f"{class_name}.{name}"))
    return out


def _java_ranges(lines: list[str], content: str) -> list[_Range] | None:
    classes = _java_class_ranges(lines, content)
    if not classes:
        return None
    out: list[_Range] = []
    for c in classes:
        span = c.end_line - c.start_line + 1
        if span > _OVERSIZE_SPLIT:
            methods = _java_method_ranges_in_scope(
                lines, content, c.start_line, c.end_line, c.symbol
            )
            if len(methods) >= 1:
                out.extend(methods)
            else:
                out.append(c)
        else:
            out.append(c)
    if not out:
        return None
    return _dedupe_sort(out)


def _ts_js_ranges(lines: list[str], content: str) -> list[_Range] | None:
    class_ranges: list[_Range] = []
    for m in _RE_TS_CLASS.finditer(content):
        line_no = _match_line_no(content, m)
        g = m.group(1)
        e = _end_from_line_index(lines, line_no - 1) or min(line_no + 20, len(lines))
        e = min(max(e, line_no), len(lines))
        class_ranges.append(_Range(line_no, e, g))

    class_ranges = _dedupe_sort(class_ranges)
    out: list[_Range] = list(class_ranges)

    for pat in (_RE_TS_FUNC, _RE_TS_ARROW):
        for m in pat.finditer(content):
            line_no = _match_line_no(content, m)
            g = m.group(1) if m.lastindex else "?"
            e = _end_from_line_index(lines, line_no - 1) or min(line_no + 25, len(lines))
            e = min(max(e, line_no), len(lines))
            r = _Range(line_no, e, g)
            if not _contained_in_any(r, class_ranges):
                out.append(r)

    if not out:
        return None
    return _dedupe_sort(out)


def _php_ranges(lines: list[str], content: str) -> list[_Range] | None:
    ranges: list[_Range] = []
    for m in _RE_PHP.finditer(content):
        line_no = _match_line_no(content, m)
        g = m.group(1) or m.group(2) or "?"
        e = _end_from_line_index(lines, line_no - 1) or min(line_no + 40, len(lines))
        e = min(max(e, line_no), len(lines))
        ranges.append(_Range(line_no, e, g))
    if not ranges:
        return None
    return _dedupe_sort(ranges)


def _dedupe_sort(ranges: list[_Range]) -> list[_Range]:
    seen: set[Tuple[int, int, str]] = set()
    out: list[_Range] = []
    for r in sorted(ranges, key=lambda x: (x.start_line, x.end_line, x.symbol)):
        key = (r.start_line, r.end_line, r.symbol)
        if key in seen or r.end_line < r.start_line:
            continue
        seen.add(key)
        out.append(r)
    return out


def _fallback_line_ranges(
    n: int, window: int, symbol: str
) -> list[Tuple[int, int, str]]:
    if n == 0:
        return []
    return _split_by_line_window(n, window, 40, f"{symbol or 'lines '}")


def _ranges_to_dicts(
    file_path: str,
    language: str,
    org_id: str,
    repo_id: str,
    lines: list[str],
    ranges: Sequence[_Range],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for r in ranges:
        if r.end_line < r.start_line or r.start_line < 1:
            continue
        e = min(r.end_line, len(lines))
        s = min(r.start_line, e)
        code = _line_slice(lines, s, e).strip()
        if not code:
            log.debug("code_chunker: skip empty range %s %s-%s", file_path, s, e)
            continue
        h = _hash_code(code)
        result.append(
            {
                "chunk_id": _make_chunk_id(org_id, repo_id, file_path, s, e, h),
                "chunk_hash": h,
                "org_id": org_id,
                "repo_id": repo_id,
                "file": file_path,
                "language": language,
                "symbol": r.symbol,
                "code": code,
                "start_line": s,
                "end_line": e,
            }
        )
    return result


def _structural_ranges(language: str, lines: list[str], content: str) -> list[_Range] | None:
    if language == "python":
        return _python_ranges(content)
    if language == "java" and lines:
        return _java_ranges(lines, content)
    if language in ("javascript", "typescript", "js", "ts") and lines:
        return _ts_js_ranges(lines, content)
    if language == "php" and lines:
        return _php_ranges(lines, content)
    return None


def chunk_code(file_data: dict, org_id: str, repo_id: str) -> List[dict]:
    """
    Build chunk dicts for one file. Expects ``file_data`` with at least
    ``file_path`` (or ``file``), ``language``, ``content``, and
    ``large_file`` (bool) from :func:`app.repo_scanner.scan_repo`.

    * **large_file** → 150–300 line windows (default 200, merge tail if ``< 150``).
    * **Otherwise:** Python via ``ast``; Java / JS / TS / PHP via heuristics
      (``class`` / ``function`` / methods); **fallback** 200-line windows.
    * Oversize structural units are split to ≤300 lines, then 200-line parts.
    * ``chunk_hash`` = SHA-256 of ``code``; ``chunk_id`` = deterministic 32-hex
      from org, repo, file, line span, and hash.
    """
    file_path = str(
        file_data.get("file_path")
        or file_data.get("file")
        or ""
    ).replace("\\", "/")
    language = (file_data.get("language") or "unknown").lower()
    is_large = bool(file_data.get("large_file", False))
    lines, content = _get_lines_and_content(file_data)
    n = len(lines)

    if n == 0 or not (content and content.strip()):
        log.info("code_chunker: no content for %s", file_path)
        return []

    if is_large:
        segs = _split_by_line_window(
            n, _LARGE_CHUNK_LINES, _LARGE_MERGE_IF_TAIL_LT, "lines "
        )
        res: list[dict[str, Any]] = []
        for a, b, sym in segs:
            res.extend(
                _ranges_to_dicts(
                    file_path, language, org_id, repo_id, lines, [_Range(a, b, sym)]
                )
            )
        log.info("code_chunker: large file %d lines -> %d chunks", n, len(res))
        return res

    struct = _structural_ranges(language, lines, content)
    if not struct:
        log.debug("code_chunker: structural miss for %s, fallback 200 lines", file_path)
        segs = _fallback_line_ranges(n, _FALLBACK_CHUNK_LINES, f"file:{file_path}")
        struct = [_Range(a, b, sym) for a, b, sym in segs]
    if not struct:
        return []

    struct = _split_oversize_ranges_v2(struct, _OVERSIZE_SPLIT)
    out = _ranges_to_dicts(
        file_path, language, org_id, repo_id, lines, struct
    )
    log.info(
        "code_chunker: %s (%s) -> %d chunks",
        file_path,
        language,
        len(out),
    )
    return out


__all__ = ["chunk_code"]
