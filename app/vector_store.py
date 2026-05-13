"""Per-organization FAISS vector store with JSON metadata on disk.

Layout::

    <root>/vector_store/{org_id}/
        vectors.faiss
        metadata.json
        .lock

Set ``VECTOR_STORE_ROOT`` to override the default base path (else ``./vector_store``
if unset; see :func:`_vector_store_base`).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

import faiss
import numpy as np
from filelock import FileLock

from app.logging import get_logger

log = get_logger(__name__)

_DEFAULT_STORE_SUBDIR: Final[str] = "vector_store"
_FAISS_FILENAME: Final[str] = "vectors.faiss"
_METADATA_FILENAME: Final[str] = "metadata.json"
_LOCK_FILENAME: Final[str] = ".lock"
_VERSION: Final[int] = 1

# Default flat index dimension (Amazon Titan Text Embeddings v2); must match
# inputs passed to :func:`index_chunks`.
_DEFAULT_DIMENSION: Final[int] = 1024

# Search / lock
_LOCK_TIMEOUT: Final[float] = 120.0

# Metadata keys that identify a row and must not change via
# :func:`update_chunk_metadata`. ``chunk_hash`` is the primary key that keeps
# the FAISS index and the metadata list in lockstep; rewriting it would
# silently corrupt the store.
_PROTECTED_METADATA_KEYS: Final[tuple[str, ...]] = ("chunk_hash",)

_memory_cache: dict[str, "OrgVectorState"] = {}


def _vector_store_base() -> Path:
    override = (os.environ.get("VECTOR_STORE_ROOT") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return (Path.cwd() / _DEFAULT_STORE_SUBDIR).resolve()


def _org_path(org_id: str) -> Path:
    if not org_id or not str(org_id).strip():
        raise ValueError("org_id must be non-empty")
    safe = str(org_id).strip().replace("..", "_")
    return _vector_store_base() / safe


def _lock_path(org_id: str) -> Path:
    p = _org_path(org_id)
    p.mkdir(parents=True, exist_ok=True)
    return p / _LOCK_FILENAME


@dataclass
class OrgVectorState:
    """Holds a FAISS :class:`faiss.IndexFlatL2` and parallel metadata rows."""

    org_id: str
    path: Path
    dimension: int
    index: faiss.IndexFlatL2
    # Same order as FAISS row ids: chunk_hash, inner metadata, no duplicate embedding in RAM list
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ntotal(self) -> int:
        return int(self.index.ntotal)


def _read_metadata(path: Path) -> tuple[int, list[dict[str, Any]] | None]:
    if not path.is_file():
        return _DEFAULT_DIMENSION, None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    dim = int(data.get("dimension", _DEFAULT_DIMENSION))
    entries = data.get("entries")
    return dim, entries if isinstance(entries, list) else None


def _write_metadata(
    path: Path,
    dimension: int,
    entries: list[dict[str, Any]],
    embeddings: list[list[float]] | None,
) -> None:
    body: dict[str, Any] = {
        "version": _VERSION,
        "dimension": dimension,
        "entries": entries,
    }
    if embeddings is not None and len(embeddings) == len(entries):
        body["embeddings_json"] = embeddings
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=True, indent=2)
        f.write("\n")


def _state_from_disk(org_id: str) -> OrgVectorState:
    root = _org_path(org_id)
    fpath = root / _FAISS_FILENAME
    mpath = root / _METADATA_FILENAME
    if not fpath.is_file() or not mpath.is_file():
        raise FileNotFoundError(
            f"vector store for org {org_id!r} is not initialised: missing {fpath} or {mpath}"
        )
    index = cast(faiss.IndexFlatL2, faiss.read_index(str(fpath)))
    dim, ent_list = _read_metadata(mpath)
    if index.d != dim:
        log.warning(
            "vector_store: metadata dimension %s != FAISS d=%s, using FAISS", dim, index.d
        )
    entries: list[dict[str, Any]] = []
    if ent_list and len(ent_list) == index.ntotal:
        for e in ent_list:
            if not isinstance(e, dict):
                continue
            h = e.get("chunk_hash", "")
            meta = e.get("metadata", {})
            if not isinstance(meta, dict):
                meta = {}
            entries.append({"chunk_hash": h, "metadata": meta})
    elif index.ntotal > 0:
        raise ValueError("metadata length does not match FAISS ntotal; rebuild or re-init")
    return OrgVectorState(
        org_id=org_id,
        path=root,
        dimension=index.d,
        index=index,
        entries=entries,
    )


def _save_state(st: OrgVectorState) -> None:
    st.path.mkdir(parents=True, exist_ok=True)
    fpath = st.path / _FAISS_FILENAME
    mpath = st.path / _METADATA_FILENAME
    faiss.write_index(st.index, str(fpath))
    serial_entries: list[dict[str, Any]] = [
        {
            "chunk_hash": e.get("chunk_hash", ""),
            "metadata": e.get("metadata", {}),
        }
        for e in st.entries
    ]
    embeds: list[list[float]] = []
    n = st.index.ntotal
    for i in range(n):
        v = st.index.reconstruct(int(i))
        embeds.append([float(x) for x in v.tolist()])
    _write_metadata(mpath, st.dimension, serial_entries, embeds)
    _memory_cache[st.org_id] = st
    log.info("vector_store: saved org_id=%s ntotal=%s", st.org_id, n)


def _validate_chunk_row(row: dict[str, Any], idx: int) -> tuple[str, list[float], dict[str, Any]]:
    h = str(row.get("chunk_hash", "")).strip()
    if not h:
        raise ValueError(f"chunks[{idx}]: missing chunk_hash")
    emb = row.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise ValueError(f"chunks[{idx}]: missing or bad embedding")
    vec = [float(x) for x in emb]
    meta = row.get("metadata", {})
    if not isinstance(meta, dict):
        raise ValueError(f"chunks[{idx}]: metadata must be an object")
    for k in ("org_id", "repo_id", "file", "symbol"):
        if k not in meta:
            log.debug("vector_store: chunks[%s] missing metadata.%s (optional)", idx, k)
    return h, vec, cast(dict[str, Any], meta)


def init_index(
    org_id: str, *, dimension: int = _DEFAULT_DIMENSION, force: bool = False
) -> None:
    """Create an empty org directory, FAISS :class:`faiss.IndexFlatL2` (L2 on raw vectors)
    and empty ``metadata.json``. Fails if already present unless ``force=True``."""
    root = _org_path(org_id)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        fpath = root / _FAISS_FILENAME
        if fpath.is_file() and not force:
            raise FileExistsError(
                f"vector store for org {org_id!r} already exists; use load_index or force=True"
            )
        idx = faiss.IndexFlatL2(int(dimension))
        st = OrgVectorState(
            org_id=org_id,
            path=root,
            dimension=dimension,
            index=cast(faiss.IndexFlatL2, idx),
            entries=[],
        )
        _write_metadata(
            root / _METADATA_FILENAME,
            dimension,
            [],
            [],
        )
        faiss.write_index(st.index, str(fpath))
        _memory_cache[org_id] = st
    log.info("vector_store: init org_id=%s dim=%s path=%s", org_id, dimension, root)


def load_index(org_id: str) -> OrgVectorState:
    """Load the org’s index and metadata from disk (cached in-process)."""
    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        st = _state_from_disk(org_id)
        _memory_cache[org_id] = st
    return st


def save_index(org_id: str) -> None:
    """Persist the cached state for ``org_id``; raises if the org is unknown."""
    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        st = _memory_cache.get(org_id) or _state_from_disk(org_id)
        _save_state(st)
    log.info("vector_store: save done org_id=%s", org_id)


def index_chunks(
    org_id: str, chunks_with_embeddings: list[dict[str, Any]]
) -> dict[str, int]:
    """
    Add chunks that are not already present (keyed by ``chunk_hash``).

    Each item should look like::

        {
            "chunk_hash": "...",
            "embedding": [float, ...],
            "metadata": {"org_id", "repo_id", "file", "symbol", ...},
        }

    Returns ``{"added": n, "skipped": m}``.
    """
    if not chunks_with_embeddings:
        return {"added": 0, "skipped": 0}

    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        st = _state_from_disk(org_id)
        existing: set[str] = {
            e.get("chunk_hash", "") for e in st.entries if e.get("chunk_hash")
        }
        skipped = 0
        final_rows: list[dict[str, Any]] = []
        final_mat: list[np.ndarray] = []

        for i, row in enumerate(chunks_with_embeddings):
            h, vec, meta = _validate_chunk_row(row, i)
            if h in existing:
                skipped += 1
                log.debug("vector_store: skip duplicate chunk_hash=%s org_id=%s", h, org_id)
                continue
            if len(vec) != st.dimension:
                raise ValueError(
                    f"chunk {h!r} embedding dim {len(vec)} != index dimension {st.dimension}"
                )
            existing.add(h)
            final_rows.append({"chunk_hash": h, "metadata": meta})
            final_mat.append(np.array(vec, dtype=np.float32).reshape(1, -1))

        if not final_mat:
            log.info("vector_store: org_id=%s index_chunks added=0 skipped=%s", org_id, skipped)
            return {"added": 0, "skipped": skipped}

        batch = np.vstack(final_mat)
        st.entries.extend(final_rows)
        st.index.add(batch)
        _save_state(st)
        added = int(batch.shape[0])
        log.info(
            "vector_store: org_id=%s index_chunks added=%s skipped=%s (total n=%s)",
            org_id,
            added,
            skipped,
            st.index.ntotal,
        )
        return {"added": added, "skipped": skipped}


def search(
    org_id: str, query_embedding: list[float], top_k: int
) -> list[dict[str, Any]]:
    """
    Return the ``top_k`` nearest neighbours (L2 on stored vectors) as::

        {
            "chunk_hash": str,
            "embedding": [float, ...],
            "metadata": {...},
            "distance": float,
        }
    """
    if not query_embedding:
        raise ValueError("query_embedding must be non-empty")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        st = _state_from_disk(org_id)
        n = st.ntotal
        if n == 0:
            return []
        k = min(int(top_k), n)
        q = np.array(query_embedding, dtype=np.float32).reshape(1, -1)
        if int(q.shape[1]) != st.dimension:
            raise ValueError(
                f"query dim {q.shape[1]} != index dimension {st.dimension}"
            )
        t0 = time.perf_counter()
        d, idx = st.index.search(q, k)  # (1, k) distances, indices
        elapsed = (time.perf_counter() - t0) * 1000.0
        out: list[dict[str, Any]] = []
        for rank in range(k):
            row = int(idx[0, rank])
            dist = float(d[0, rank])
            v = st.index.reconstruct(row)
            ent = st.entries[row]
            out.append(
                {
                    "chunk_hash": ent.get("chunk_hash", ""),
                    "embedding": [float(x) for x in v.tolist()],
                    "metadata": ent.get("metadata", {}),
                    "distance": dist,
                }
            )
    log.info(
        "vector_store: search org_id=%s top_k=%s hit=%s latency_ms=%.2f",
        org_id,
        k,
        len(out),
        elapsed,
    )
    return out


def remove_repo_chunks(org_id: str, repo_id: str) -> int:
    """Remove every vector whose ``metadata["repo_id"]`` matches. Returns how many were removed."""
    if not (repo_id and str(repo_id).strip()):
        raise ValueError("repo_id must be non-empty")
    target = str(repo_id).strip()

    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        st = _state_from_disk(org_id)
        n = st.ntotal
        if n == 0:
            return 0
        keep_idx: list[int] = []
        for i in range(n):
            meta = st.entries[i].get("metadata", {}) if i < len(st.entries) else {}
            if not isinstance(meta, dict):
                meta = {}
            if str(meta.get("repo_id", "")).strip() != target:
                keep_idx.append(i)

        removed = n - len(keep_idx)
        if removed == 0:
            log.info("vector_store: remove_repo org_id=%s repo_id=%s removed=0", org_id, target)
            return 0

        if not keep_idx:
            st.index = cast(faiss.IndexFlatL2, faiss.IndexFlatL2(st.dimension))
            st.entries = []
        else:
            new_rows: list[dict[str, Any]] = [st.entries[i] for i in keep_idx]
            vecs = np.vstack(
                [st.index.reconstruct(int(i)).reshape(1, -1) for i in keep_idx]
            )
            st.index = cast(faiss.IndexFlatL2, faiss.IndexFlatL2(st.dimension))
            st.index.add(vecs)
            st.entries = new_rows
        _save_state(st)
        log.info("vector_store: remove_repo org_id=%s repo_id=%s removed=%s ntotal=%s", org_id, target, removed, st.ntotal)
        return removed


def get_chunks_without_summary(org_id: str) -> list[dict[str, Any]]:
    """Return chunks in the org that do not yet have a non-empty ``summary``.

    Each row looks like::

        {"chunk_hash": "...", "metadata": {...}}

    Embeddings are intentionally *not* included — callers running a
    summarisation pass only need the code and identifiers, and excluding the
    vectors keeps the result small and satisfies the "do not touch embeddings"
    contract.

    A chunk is considered "without summary" when its ``metadata["summary"]`` is
    missing, ``None``, not a string, or only whitespace. Returns an empty list
    when the org's vector store does not exist yet (so a post-ingest
    summarisation worker can run unconditionally).
    """
    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        try:
            st = _state_from_disk(org_id)
        except FileNotFoundError:
            log.debug(
                "vector_store: get_chunks_without_summary org_id=%s store_missing",
                org_id,
            )
            return []

        pending: list[dict[str, Any]] = []
        for entry in st.entries:
            meta = entry.get("metadata", {})
            if not isinstance(meta, dict):
                meta = {}
            summary = meta.get("summary")
            if isinstance(summary, str) and summary.strip():
                continue
            pending.append(
                {
                    "chunk_hash": str(entry.get("chunk_hash", "")),
                    # Shallow copy so callers can't mutate our in-memory state.
                    "metadata": dict(meta),
                }
            )
        total = st.ntotal

    log.info(
        "vector_store: get_chunks_without_summary org_id=%s pending=%s total=%s",
        org_id,
        len(pending),
        total,
    )
    return pending


def update_chunk_metadata(
    org_id: str,
    chunk_hash: str,
    new_fields: dict[str, Any],
) -> int:
    """Shallow-merge ``new_fields`` into the metadata of the chunk identified
    by ``chunk_hash``.

    The FAISS index and the stored embedding vector are left untouched; only
    the JSON metadata is rewritten.

    Guarantees:
        * Existing metadata keys that are *not* mentioned in ``new_fields`` are
          preserved exactly.
        * ``chunk_hash`` cannot be changed — passing a different value raises
          :class:`ValueError`; passing the same value is a no-op for that key.
        * When a key in ``new_fields`` would replace an existing non-empty
          value with a different value, an overwrite warning is logged (the
          update still proceeds — the caller asked for it — but it is visible
          in the logs so accidental clobbers are not silent).
        * No disk write happens when ``new_fields`` results in zero effective
          changes.

    Returns the number of updated chunks: ``0`` when the chunk is not found or
    no field actually changed, ``1`` otherwise. The count is always logged at
    info level.
    """
    if not isinstance(chunk_hash, str) or not chunk_hash.strip():
        raise ValueError("chunk_hash must be a non-empty string")
    if not isinstance(new_fields, dict):
        raise TypeError("new_fields must be a dict")

    target_hash = chunk_hash.strip()

    if "chunk_hash" in new_fields:
        candidate = str(new_fields["chunk_hash"]).strip()
        if candidate and candidate != target_hash:
            raise ValueError(
                "chunk_hash is immutable; update_chunk_metadata cannot rewrite it"
            )

    # Drop protected keys from the merge payload so identity stays intact even
    # if the caller accidentally echoes them back.
    clean_new: dict[str, Any] = {
        k: v for k, v in new_fields.items() if k not in _PROTECTED_METADATA_KEYS
    }
    if not clean_new:
        log.info(
            "vector_store: update_chunk_metadata org_id=%s chunk_hash=%s updated_chunks=0 "
            "reason=no_updatable_fields",
            org_id,
            target_hash,
        )
        return 0

    with FileLock(_lock_path(org_id), timeout=_LOCK_TIMEOUT):
        st = _state_from_disk(org_id)

        target_idx: int | None = None
        for i, entry in enumerate(st.entries):
            if str(entry.get("chunk_hash", "")).strip() == target_hash:
                target_idx = i
                break

        if target_idx is None:
            log.warning(
                "vector_store: update_chunk_metadata org_id=%s chunk_hash=%s updated_chunks=0 "
                "reason=not_found",
                org_id,
                target_hash,
            )
            return 0

        entry = st.entries[target_idx]
        existing_meta = entry.get("metadata", {})
        if not isinstance(existing_meta, dict):
            existing_meta = {}
        merged: dict[str, Any] = dict(existing_meta)

        added_keys: list[str] = []
        changed_keys: list[str] = []
        unchanged = 0
        for key, value in clean_new.items():
            if key in merged:
                prev = merged[key]
                if prev == value:
                    unchanged += 1
                    continue
                if prev not in (None, "", [], {}):
                    log.warning(
                        "vector_store: update_chunk_metadata overwrite org_id=%s chunk_hash=%s "
                        "key=%s prev_type=%s new_type=%s",
                        org_id,
                        target_hash,
                        key,
                        type(prev).__name__,
                        type(value).__name__,
                    )
                changed_keys.append(key)
            else:
                added_keys.append(key)
            merged[key] = value

        if not added_keys and not changed_keys:
            log.info(
                "vector_store: update_chunk_metadata org_id=%s chunk_hash=%s updated_chunks=0 "
                "reason=no_effective_changes unchanged=%s",
                org_id,
                target_hash,
                unchanged,
            )
            return 0

        # Preserve the original chunk_hash string exactly (do not trim in place).
        st.entries[target_idx] = {
            "chunk_hash": entry.get("chunk_hash", target_hash),
            "metadata": merged,
        }
        _save_state(st)

    log.info(
        "vector_store: update_chunk_metadata org_id=%s chunk_hash=%s updated_chunks=1 "
        "added=%s changed=%s unchanged=%s",
        org_id,
        target_hash,
        len(added_keys),
        len(changed_keys),
        unchanged,
    )
    return 1


__all__ = [
    "OrgVectorState",
    "init_index",
    "load_index",
    "save_index",
    "index_chunks",
    "search",
    "remove_repo_chunks",
    "get_chunks_without_summary",
    "update_chunk_metadata",
]