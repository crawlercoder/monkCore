#!/usr/bin/env python3
"""End-to-end smoke test for storage_manager, repo_initializer, and artifact_manager.

Run from the repository root::

    .venv/bin/python scripts/test_storage.py

Requires network for ``git clone`` of a public sample repo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

# Project root: scripts/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / ".env", override=True)

# --- app imports (after path + env) ---

from app import artifact_manager, storage_manager
from app import repo_initializer
from app.config import get_settings

SAMPLE_GIT = "https://github.com/octocat/Hello-World.git"
DEFAULT_ORG = "org_smoke_test"
DEFAULT_REPO = "repo_hello"
DEFAULT_URL = SAMPLE_GIT


def _line(msg: str, *, ok: bool | None = None) -> None:
    tag = {True: "OK  ", False: "FAIL", None: "    "}[ok]
    line = f"[{tag}] {msg}" if ok is not None else f"      {msg}"
    print(line)


def main() -> int:
    print("=" * 60)
    print("  Storage system smoke test")
    print("=" * 60)

    s = get_settings()
    _line(f"BASE_STORAGE_PATH (resolved): {s.base_storage_path!r}", ok=True)
    print()

    # 1) init storage
    print("\n(1) Initialize storage\n")
    storage_manager.init_storage()
    base = Path(s.base_storage_path).expanduser().resolve()
    for name in ("repos", "artifacts", "cache", "tmp"):
        p = base / name
        ok = p.is_dir()
        _line(f"{p}", ok=ok)
        if not ok:
            return 1

    # 2) dummy org / repo ids
    org_id = DEFAULT_ORG
    repo_id = DEFAULT_REPO
    _line(f"org_id  = {org_id!r}", ok=True)
    _line(f"repo_id = {repo_id!r}", ok=True)

    # 3) clone public sample repo
    print("\n(2) Clone sample public repository\n")
    dest = Path(
        storage_manager.get_repo_path(org_id, repo_id).rstrip("/\\")
    ).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"      (remove existing: {dest})")
        shutil.rmtree(dest)
    r = subprocess.run(
        [
            "git", "clone", "--depth", "1",
            DEFAULT_URL,
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        _line(f"git clone failed: {r.stderr[:500]!r}", ok=False)
        return 1
    _line(f"cloned -> {dest}", ok=True)

    # 4) .agent metadata
    print("\n(3) .agent metadata\n")
    repo_initializer.init_repo_metadata(dest, org_id, repo_id)
    meta_p = dest / ".agent" / "metadata.json"
    branch_p = dest / ".agent" / "branch_info.json"
    _line(str(meta_p), ok=meta_p.is_file())
    _line(str(branch_p), ok=branch_p.is_file())
    if meta_p.is_file():
        m = json.loads(meta_p.read_text(encoding="utf-8"))
        print(f"      metadata repo_id: {m.get('repo_id')!r} status: {m.get('status')!r}")

    # 4b) structure under BASE/repos/org/repo
    print("      Structure check (expected children present)")
    _line(".git exists", ok=(dest / ".git").exists())
    for sub in (".agent",):
        _line(f"{sub}/", ok=(dest / sub).is_dir())

    # 5) job artifacts
    print("\n(4) Job artifacts (dummy job_id)\n")
    job_id = f"job_smoke_{uuid.uuid4().hex[:8]}"
    print(f"      job_id = {job_id!r}")

    artifact_manager.init_job_artifacts(job_id)
    art_base = Path(
        storage_manager.get_artifact_path(job_id).rstrip("/\\")
    ).resolve()
    for sub in ("logs", "diffs", "plan", "questions"):
        p = art_base / sub
        _line(f"{p}", ok=p.is_dir())
        if not p.is_dir():
            return 1

    artifact_manager.write_log(job_id, "smoke.log", "line1\nline2\n")
    artifact_manager.save_diff(job_id, "file.patch", "--- a\n+++ b\n")
    artifact_manager.save_plan(job_id, {"steps": ["a", "b"]})
    artifact_manager.save_questions(job_id, [{"id": 1, "text": "ok?"}])

    logf = art_base / "logs" / "smoke.log"
    planf = art_base / "plan" / "plan.json"
    qf = art_base / "questions" / "questions.json"
    _line(str(logf), ok=logf.is_file())
    _line(str(planf), ok=planf.is_file())
    _line(str(qf), ok=qf.is_file())

    # Summary
    print("\n" + "=" * 60)
    print("  Summary")
    print("=" * 60)
    print(f"  Storage root:  {base}")
    print(f"  Clone (repo):  {dest}")
    print(f"  Artifacts:     {art_base}")
    print("  All checks above must show [OK  ].")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
