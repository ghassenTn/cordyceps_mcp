"""Read-only, request-scoped view over the code graph metadata.

Every structural tool starts by taking one snapshot of the metadata index and
building the small lookup tables it needs (by name, by file, by folder). This
keeps the tools O(N) per request instead of re-scanning the index for every
symbol, and it centralises the node-identity rules so the rest of the package
never has to parse node IDs by hand.

Node ID conventions (produced by ``src/watcher/sync_handler.py``):

    <rel_file_path>                          File
    <relative/folder/path>                   Folder
    <rel_file_path>:<Qualified.Name>         Class / Function (methods, nested defs)
    <rel_file_path>:<name>                   Declaration
    <rel_file_path>:<url>                    Route
    <rel_file_path>:mw:<name>                Middleware
"""
from __future__ import annotations

import os
from typing import Any, Iterable

# Node types that represent code definitions (as opposed to containers).
SYMBOL_TYPES = frozenset({"Class", "Function", "Declaration", "Route", "Middleware"})
CONTAINER_TYPES = frozenset({"File", "Folder"})
# Types whose qualified names nest with "." (Outer.Inner, Class.method, outer.inner).
DOTTED_TYPES = frozenset({"Class", "Function", "Declaration"})


def normalize_path(path: str | None, workspace_path: str | None = None) -> str:
    """Normalise a user-supplied path to the workspace-relative form used by node IDs.

    ``""``, ``"."`` and ``"./"`` all mean the workspace root and return ``""``.
    Absolute paths inside the workspace are made relative; backslashes are
    converted; leading ``./`` and trailing ``/`` are stripped.
    """
    raw = str(path or "").strip().replace("\\", "/")
    if workspace_path and raw:
        ws = os.path.abspath(workspace_path).replace("\\", "/").rstrip("/")
        if raw == ws:
            return ""
        if raw.startswith(ws + "/"):
            raw = raw[len(ws) + 1:]
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.rstrip("/")
    return "" if raw == "." else raw


def is_under(file_path: str, prefix: str) -> bool:
    """True when ``file_path`` equals ``prefix`` or lives below it (component-safe)."""
    if not prefix:
        return True
    return file_path == prefix or file_path.startswith(prefix + "/")


def lines_span(meta: dict | None) -> str | None:
    """Render ``{"start": s, "end": e}`` as ``"s-e"`` (or ``None`` when unknown)."""
    lines = (meta or {}).get("lines")
    if not isinstance(lines, dict):
        return None
    start, end = lines.get("start"), lines.get("end")
    if not start:
        return None
    return f"{start}-{end or start}"


def line_start(meta: dict | None) -> int:
    lines = (meta or {}).get("lines")
    if isinstance(lines, dict) and isinstance(lines.get("start"), int):
        return lines["start"]
    return 0


class IndexView:
    """Immutable snapshot of the metadata index with derived lookup tables."""

    def __init__(self, client: Any):
        self.client = client
        nodes = client.get_all_metadata() or {}
        self.nodes: dict[str, dict] = {
            nid: (dict(meta) if not isinstance(meta, dict) else meta)
            for nid, meta in nodes.items()
        }
        self._by_name: dict[str, list[str]] = {}
        self._symbols_by_file: dict[str, list[str]] = {}
        self._files: dict[str, str] = {}  # file_path -> File node id
        for nid, meta in self.nodes.items():
            ntype = meta.get("type", "")
            name = str(meta.get("name", ""))
            if name:
                self._by_name.setdefault(name.lower(), []).append(nid)
            if ntype == "File":
                self._files[str(meta.get("file_path") or nid)] = nid
            elif ntype in SYMBOL_TYPES:
                fp = str(meta.get("file_path", ""))
                self._symbols_by_file.setdefault(fp, []).append(nid)
        for ids in self._symbols_by_file.values():
            ids.sort(key=lambda i: (line_start(self.nodes[i]), i))

    # ── basic accessors ──────────────────────────────────────────────

    def get(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def type_of(self, node_id: str) -> str:
        return str((self.nodes.get(node_id) or {}).get("type", ""))

    def file_of(self, node_id: str) -> str:
        meta = self.nodes.get(node_id) or {}
        return str(meta.get("file_path") or (node_id if meta.get("type") == "File" else ""))

    def name_of(self, node_id: str) -> str:
        return str((self.nodes.get(node_id) or {}).get("name", ""))

    def qualified_name(self, node_id: str) -> str:
        """The part of the ID after ``<file>:`` (or the name for files/folders)."""
        meta = self.nodes.get(node_id) or {}
        ntype = meta.get("type", "")
        if ntype in CONTAINER_TYPES:
            return str(meta.get("name") or node_id)
        fp = str(meta.get("file_path", ""))
        if fp and node_id.startswith(fp + ":"):
            return node_id[len(fp) + 1:]
        if ":" in node_id:
            return node_id.split(":", 1)[1]
        return str(meta.get("name") or node_id)

    def container_of(self, node_id: str) -> str | None:
        """Enclosing definition (class / outer function) or the file for top-level symbols."""
        meta = self.nodes.get(node_id)
        if not meta or meta.get("type") in CONTAINER_TYPES:
            return None
        fp = self.file_of(node_id)
        qual = self.qualified_name(node_id)
        if meta.get("type") in DOTTED_TYPES and "." in qual:
            parent = f"{fp}:{qual.rsplit('.', 1)[0]}"
            if parent in self.nodes:
                return parent
        return fp if fp in self._files or fp in self.nodes else None

    # ── collections ──────────────────────────────────────────────────

    @property
    def file_paths(self) -> list[str]:
        return sorted(self._files)

    def file_node(self, file_path: str) -> str | None:
        return self._files.get(file_path)

    def symbols_in_file(self, file_path: str) -> list[str]:
        return list(self._symbols_by_file.get(file_path, ()))

    def files_under(self, prefix: str) -> list[str]:
        return [fp for fp in self.file_paths if is_under(fp, prefix)]

    def ids_named(self, name: str) -> list[str]:
        return list(self._by_name.get(name.lower(), ()))

    def symbol_ids(self) -> Iterable[str]:
        for ids in self._symbols_by_file.values():
            yield from ids

    def members_of(self, node_id: str) -> list[str]:
        """Direct nested definitions (``node_id.<child>``), ordered by line."""
        prefix = node_id + "."
        out = [
            nid for nid in self.symbols_in_file(self.file_of(node_id))
            if nid.startswith(prefix) and "." not in nid[len(prefix):]
        ]
        return out
