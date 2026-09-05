"""The three structural questions an agent asks about a codebase.

    code_map       "What is in here?"        -> directory / file outline
    lookup_symbol  "Where is X defined?"     -> canonical ID, location, neighbourhood
    impact         "What breaks if I edit X?" -> transitive callers (blast radius)

Everything is computed from the persisted code graph; nothing here reads
source files or duplicates ``grep``/``glob``. Responses are plain dicts with a
stable envelope::

    ok: bool
    tool: str
    meta: {index_stale, graph_built, ...counts/truncation..., warnings?}
    ...payload...

Output is bounded everywhere (see the ``MAX_*`` constants); when a list is cut
the response says so explicitly instead of silently dropping entries.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

from .index_view import IndexView, SYMBOL_TYPES, DOTTED_TYPES, is_under, lines_span, normalize_path
from .records import (brief_record, bounded, bounded_texts, compact_entry, group_by_file,
                      import_statements, is_noise_call, symbol_record)
from .resolver import AMBIGUOUS, NOT_FOUND, Resolution, resolve_symbol

logger = logging.getLogger(__name__)

MAX_MAP_ENTRIES = 300      # folders + files (+ symbols for a file map) per response
MAX_MAP_DEPTH = 4
MAX_TOP_LEVEL_PREVIEW = 8  # symbol names shown inline per file in a directory map
MAX_IMPORTS = 40
MAX_RELATED = 50           # callers / callees / members / candidates in lookup
MAX_UNRESOLVED = 30
MAX_IMPACT_NODES = 300
MAX_IMPACT_DEPTH = 25
DEFAULT_IMPACT_DEPTH = 2

CALLERS, CALLEES = "callers", "callees"
ENTRY_FORMAT = "<qualified_name> [<Kind>] L<start>-<end>; node id = <file>:<qualified_name>"
# Languages whose call edges are resolved with import/scope/receiver context.
# Python is resolved in Rust; JS/TS use the conservative Python-side resolver.
# Extend this only when a language gains equivalent contextual semantics.
CONTEXTUAL_CALL_RESOLUTION_EXTENSIONS = (".py", ".js", ".jsx", ".ts", ".tsx")


def _unique(items) -> list[str]:
    """Order-preserving de-duplication (duplicate call sites yield duplicate edges)."""
    return list(dict.fromkeys(items or ()))


class CodeStructure:
    """Structural queries over one workspace's code graph.

    ``client`` is an ``EngramClient``-like object (anything exposing
    ``get_all_metadata / get_callers / get_callees / get_dependencies /
    get_dependents / is_index_stale / get_stats``). ``health`` is an optional
    callable returning extra warning strings (e.g. failed incremental syncs)
    that are surfaced in every response.
    """

    def __init__(self, client: Any, workspace_path: str | None = None,
                 health: Callable[[], list[str]] | None = None):
        self.client = client
        self.workspace_path = os.path.abspath(
            workspace_path
            or getattr(client, "workspace_path", None)
            or getattr(getattr(client, "_client", None), "workspace_path", None)
            or os.environ.get("WORKSPACE_PATH", os.getcwd()))
        self._health = health

    # ── public API ───────────────────────────────────────────────────

    def map(self, path: str = ".", depth: int = 1) -> dict:
        """Outline of a directory (folders, files, symbol counts) or a file (definition tree)."""
        view = IndexView(self.client)
        rel = normalize_path(path, self.workspace_path)
        depth = max(1, min(int(depth or 1), MAX_MAP_DEPTH))

        if rel and view.file_node(rel):
            return self._map_file(view, rel)

        files = view.files_under(rel)
        if not files and rel:
            return self._error("code_map", f"'{path}' is not an indexed file or directory",
                               suggestions=self._suggest_paths(view, rel))
        return self._map_directory(view, rel, files, depth)

    def lookup(self, symbol: str, path: str | None = None) -> dict:
        """Definition(s) of ``symbol`` with location and direct graph neighbourhood."""
        view = IndexView(self.client)
        res = resolve_symbol(view, symbol, path, self.workspace_path)
        if res.status == NOT_FOUND:
            return self._not_found("lookup_symbol", res)
        if res.status == AMBIGUOUS:
            return self._ambiguous("lookup_symbol", view, res)

        node_id = res.node_id
        kind = view.type_of(node_id)
        out = {"ok": True, "tool": "lookup_symbol", "status": "resolved",
               "meta": self._meta(), "symbol": symbol_record(view, node_id)}
        if kind == "Folder":
            out["hint"] = f"'{node_id}' is a folder; use code_map(path='{node_id}') for its contents."
            return out
        if kind == "File":
            out["relationships"] = self._file_relationships(view, node_id)
        else:
            out["relationships"] = self._symbol_relationships(view, node_id)
        return out

    def impact(self, symbol: str, depth: int = DEFAULT_IMPACT_DEPTH, direction: str = CALLERS) -> dict:
        """Transitive callers (default) or callees of ``symbol`` up to ``depth`` hops."""
        direction = (direction or CALLERS).lower()
        if direction not in (CALLERS, CALLEES):
            return self._error("impact", f"direction must be '{CALLERS}' or '{CALLEES}', got '{direction}'")
        depth = DEFAULT_IMPACT_DEPTH if depth is None else max(1, min(int(depth), MAX_IMPACT_DEPTH))

        view = IndexView(self.client)
        res = resolve_symbol(view, symbol, None, self.workspace_path)
        if res.status == NOT_FOUND:
            return self._not_found("impact", res)
        if res.status == AMBIGUOUS:
            return self._ambiguous("impact", view, res)

        target = res.node_id
        raw_neighbours = self.client.get_callers if direction == CALLERS else self.client.get_callees

        def neighbours(node_id: str) -> list[str]:
            return _unique(raw_neighbours(node_id))

        direct = [n for n in neighbours(target) if n != target]
        depths, truncated, beyond = self._bfs(target, neighbours, depth)
        affected_ids = list(depths)

        meta = self._meta()
        meta.update({
            "direction": direction,
            "depth": depth,
            "direct": len(direct),
            "total": len(affected_ids),
            "files": len({view.file_of(n) for n in affected_ids}),
            "truncated": truncated,
            "more_beyond_depth": beyond,
            "entry_format": ENTRY_FORMAT,
        })
        warnings = list(meta.get("warnings", []))
        if truncated:
            warnings.append(f"Traversal stopped after {MAX_IMPACT_NODES} nodes; lower depth or start from a narrower symbol.")
        if beyond:
            warnings.append(f"Nodes at depth {depth} still have unexplored {direction}; raise depth to continue.")
        if any(view.type_of(n) in ("Route", "File", "Middleware") for n in affected_ids):
            warnings.append("Route/File/Middleware entries come from framework and HTTP-call linking, which is heuristic.")
        confidence_nodes = [target, *affected_ids]
        if any(view.type_of(n) == "Function"
               and not view.file_of(n).lower().endswith(CONTEXTUAL_CALL_RESOLUTION_EXTENSIONS)
               for n in confidence_nodes):
            warnings.append("Call edges for non-Python files are matched by name and may include false positives.")
        if warnings:
            meta["warnings"] = warnings

        out = {
            "ok": True,
            "tool": "impact",
            "target": brief_record(view, target),
            "meta": meta,
            f"direct_{direction}": direct[:MAX_RELATED],
            "affected": group_by_file(view, affected_ids, depths),
        }
        if len(direct) > MAX_RELATED:
            out[f"direct_{direction}_omitted"] = len(direct) - MAX_RELATED
        return out

    # ── code_map internals ───────────────────────────────────────────

    def _map_directory(self, view: IndexView, rel: str, files: list[str], depth: int) -> dict:
        budget = [MAX_MAP_ENTRIES, False]
        tree = self._folder_entry(view, rel, files, depth, budget)
        meta = self._meta()
        meta.update({
            "path": rel or ".",
            "kind": "directory",
            "folders": tree.pop("_folder_count"),
            "files": len(files),
            "symbols": sum(len(view.symbols_in_file(fp)) for fp in files),
            "truncated": budget[1],
        })
        if meta["truncated"]:
            meta["warnings"] = meta.get("warnings", []) + [
                f"Listing capped at {MAX_MAP_ENTRIES} entries; map a sub-folder or lower depth."]
        out = {"ok": True, "tool": "code_map", "meta": meta}
        out.update({k: v for k, v in tree.items() if k not in ("path", "files_count", "symbols_count")})
        return out

    def _folder_entry(self, view: IndexView, prefix: str, files: list[str], depth: int, budget: list[int]) -> dict:
        """Recursive folder node: immediate sub-folders (to ``depth``) and immediate files."""
        children: dict[str, list[str]] = {}
        own_files: list[str] = []
        base = len(prefix) + 1 if prefix else 0
        for fp in files:
            remainder = fp[base:]
            if "/" in remainder:
                children.setdefault(remainder.split("/", 1)[0], []).append(fp)
            else:
                own_files.append(fp)

        entry: dict[str, Any] = {
            "path": prefix or ".",
            "files_count": len(files),
            "symbols_count": sum(len(view.symbols_in_file(fp)) for fp in files),
        }
        folder_count = len(children)
        folders = []
        for name in sorted(children):
            if budget[0] <= 0:
                budget[1] = True
                break
            budget[0] -= 1
            sub_prefix = f"{prefix}/{name}" if prefix else name
            sub_files = children[name]
            if depth > 1:
                sub = self._folder_entry(view, sub_prefix, sub_files, depth - 1, budget)
                folder_count += sub.pop("_folder_count")
                folders.append(sub)
            else:
                folders.append({
                    "path": sub_prefix,
                    "files_count": len(sub_files),
                    "symbols_count": sum(len(view.symbols_in_file(fp)) for fp in sub_files),
                })
        if folders:
            entry["folders"] = folders

        file_entries = []
        for fp in own_files:
            if budget[0] <= 0:
                budget[1] = True
                break
            budget[0] -= 1
            file_entries.append(self._file_summary(view, fp))
        if file_entries:
            entry["files"] = file_entries
        entry["_folder_count"] = folder_count
        return entry

    def _file_summary(self, view: IndexView, fp: str) -> dict:
        meta = view.get(view.file_node(fp)) or {}
        symbols = view.symbols_in_file(fp)
        summary: dict[str, Any] = {"path": fp}
        lines = meta.get("lines") or {}
        if isinstance(lines, dict) and lines.get("end"):
            summary["line_count"] = lines["end"]
        if symbols:
            kinds: dict[str, int] = {}
            for nid in symbols:
                k = view.type_of(nid)
                kinds[k] = kinds.get(k, 0) + 1
            summary["symbols"] = dict(sorted(kinds.items()))
            top = [view.qualified_name(n) for n in symbols if self._is_top_level(view, n)]
            head, omitted = bounded(top, MAX_TOP_LEVEL_PREVIEW)
            if head:
                summary["top_level"] = ", ".join(head) + (f" (+{omitted})" if omitted else "")
        return summary

    def _map_file(self, view: IndexView, rel: str) -> dict:
        file_id = view.file_node(rel)
        meta_node = view.get(file_id) or {}
        symbols = view.symbols_in_file(rel)
        budget = [MAX_MAP_ENTRIES, False]
        tree = []
        for nid in symbols:
            if self._is_top_level(view, nid):
                item = self._symbol_tree(view, nid, budget)
                if item:
                    tree.append(item)

        meta = self._meta()
        lines = meta_node.get("lines") or {}
        meta.update({
            "path": rel,
            "kind": "file",
            "line_count": lines.get("end") if isinstance(lines, dict) else None,
            "symbols": len(symbols),
            "truncated": budget[1],
        })
        out: dict[str, Any] = {"ok": True, "tool": "code_map", "meta": meta}
        imports, omitted = bounded(import_statements(meta_node), MAX_IMPORTS)
        if imports:
            out["imports"] = imports
            if omitted:
                out["imports_omitted"] = omitted
        exports = meta_node.get("exports")
        if isinstance(exports, list) and exports:
            head, omitted = bounded_texts(exports, MAX_IMPORTS)
            out["exports"] = head
            if omitted:
                out["exports_omitted"] = omitted
        out["symbols"] = tree
        return out

    def _is_top_level(self, view: IndexView, nid: str) -> bool:
        if view.type_of(nid) not in DOTTED_TYPES:
            return True
        return "." not in view.qualified_name(nid)

    def _symbol_tree(self, view: IndexView, nid: str, budget: list[int]) -> dict | None:
        if budget[0] <= 0:
            budget[1] = True
            return None
        budget[0] -= 1
        meta = view.get(nid) or {}
        node: dict[str, Any] = {"name": view.qualified_name(nid).rsplit(".", 1)[-1]
                                if view.type_of(nid) in DOTTED_TYPES else view.qualified_name(nid),
                                "kind": meta.get("type", "Unknown")}
        span = lines_span(meta)
        if span:
            node["lines"] = span
        sig = meta.get("signature")
        if sig:
            node["signature"] = str(sig).strip()[:160]
        if meta.get("type") == "Route":
            node["url"] = meta.get("full_url") or meta.get("url") or node["name"]
            if meta.get("methods"):
                node["methods"], omitted = bounded_texts(meta["methods"])
                if omitted:
                    node["methods_omitted"] = omitted
            handlers = meta.get("view_names") or ([meta["view_name"]] if meta.get("view_name") else [])
            if handlers:
                node["handlers"], omitted = bounded_texts(handlers)
                if omitted:
                    node["handlers_omitted"] = omitted
        if meta.get("decorators"):
            node["decorators"], omitted = bounded_texts(meta["decorators"])
            if omitted:
                node["decorators_omitted"] = omitted
        if meta.get("base_classes"):
            node["base_classes"], omitted = bounded_texts(meta["base_classes"])
            if omitted:
                node["base_classes_omitted"] = omitted
        if meta.get("is_async"):
            node["is_async"] = True
        members = [self._symbol_tree(view, m, budget) for m in view.members_of(nid)]
        members = [m for m in members if m]
        if members:
            node["members"] = members
        return node

    # ── lookup internals ─────────────────────────────────────────────

    def _symbol_relationships(self, view: IndexView, node_id: str) -> dict:
        callers = [n for n in _unique(self.client.get_callers(node_id)) if n != node_id]
        callees = [n for n in _unique(self.client.get_callees(node_id)) if n != node_id]
        caller_set, callee_set = set(callers), set(callees)
        container = view.container_of(node_id)
        members = view.members_of(node_id)
        member_set = set(members)

        own_file = view.file_of(node_id)
        structural_in = [n for n in _unique(self.client.get_dependents(node_id))
                         if n not in caller_set and n not in member_set and n != node_id]
        structural_out = [n for n in _unique(self.client.get_dependencies(node_id))
                          if n not in callee_set and n not in (container, own_file, node_id)]
        related = _unique(n for n in structural_in + structural_out if n not in member_set)

        rel: dict[str, Any] = {}
        rel.update(self._bounded_list("callers", callers))
        rel.update(self._bounded_list("callees", callees))
        if members:
            rel.update(self._bounded_list("members", members))
        if related:
            rel.update(self._bounded_list("related", related))
        unresolved = self._unresolved_calls(view, node_id, callees)
        if unresolved:
            head, omitted = bounded(unresolved, MAX_UNRESOLVED)
            rel["unresolved_calls"] = head
            if omitted:
                rel["unresolved_calls_omitted"] = omitted
        return rel

    def _file_relationships(self, view: IndexView, file_id: str) -> dict:
        symbols = view.symbols_in_file(file_id)
        top = [n for n in symbols if self._is_top_level(view, n)]
        javascript_extensions = (".js", ".jsx", ".ts", ".tsx")
        imports = [n for n in _unique(self.client.get_dependents(file_id))
                   if view.type_of(n) == "File"
                   and not (str(n).endswith(javascript_extensions)
                            or str(file_id).endswith(javascript_extensions))]
        imported_by = [n for n in _unique(self.client.get_dependencies(file_id))
                       if view.type_of(n) == "File"
                       and not (str(n).endswith(javascript_extensions)
                                or str(file_id).endswith(javascript_extensions))]
        from src.database.javascript_resolver import JavaScriptResolver
        for target, importer in JavaScriptResolver(view.nodes).import_edges():
            if importer == file_id:
                imports.append(target)
            if target == file_id:
                imported_by.append(importer)
        imports = _unique(imports)
        imported_by = _unique(imported_by)
        callers = [n for n in _unique(self.client.get_callers(file_id)) if n != file_id]
        callees = [n for n in _unique(self.client.get_callees(file_id)) if n != file_id]

        rel: dict[str, Any] = {}
        rel.update(self._bounded_list("defines", top, formatter=lambda n: compact_entry(view, n)))
        rel.update(self._bounded_list("imports", imports))
        rel.update(self._bounded_list("imported_by", imported_by))
        if callers:
            rel.update(self._bounded_list("callers", callers))
        if callees:
            rel.update(self._bounded_list("callees", callees))
        rel["hint"] = f"code_map(path='{file_id}') returns the full definition tree."
        return rel

    def _bounded_list(self, key: str, items: list[str], formatter=None) -> dict:
        if not items:
            return {key: []} if key in ("callers", "callees", "defines", "imports", "imported_by") else {}
        head, omitted = bounded(items, MAX_RELATED)
        out = {key: [formatter(n) for n in head] if formatter else head}
        if omitted:
            out[f"{key}_omitted"] = omitted
        return out

    def _unresolved_calls(self, view: IndexView, node_id: str, callees: list[str]) -> list[str]:
        """Raw call names with no resolved callee. Conservative: a call is
        considered resolved when any callee's name or qualified name matches
        its last segment."""
        raw = (view.get(node_id) or {}).get("calls") or []
        if isinstance(raw, str):
            raw = [raw]
        resolved_names: set[str] = set()
        for c in callees:
            resolved_names.add(view.name_of(c))
            resolved_names.add(view.qualified_name(c))
        out: list[str] = []
        for call in raw:
            call = str(call).strip()
            if not call or call in out or is_noise_call(call):
                continue
            tail = call.rsplit(".", 1)[-1]
            if call in resolved_names or tail in resolved_names:
                continue
            out.append(call)
        return out

    # ── impact internals ─────────────────────────────────────────────

    def _bfs(self, target: str, neighbours: Callable[[str], list], depth: int) -> tuple[dict[str, int], bool, bool]:
        """Breadth-first traversal recording the hop count of each reached node.

        Returns ``(depth_by_node, truncated, more_beyond_depth)``. Excludes the
        target itself and follows executable edges only (callers/callees).
        """
        depth_by_node: dict[str, int] = {}
        visited = {target}
        frontier = [target]
        truncated = False
        for level in range(1, depth + 1):
            nxt: list[str] = []
            for node in frontier:
                for nb in neighbours(node):
                    if nb in visited:
                        continue
                    if len(depth_by_node) >= MAX_IMPACT_NODES:
                        return depth_by_node, True, True
                    visited.add(nb)
                    depth_by_node[nb] = level
                    nxt.append(nb)
            frontier = nxt
            if not frontier:
                break
        more = any(nb not in visited for node in frontier for nb in neighbours(node))
        return depth_by_node, truncated, more

    # ── shared helpers ───────────────────────────────────────────────

    def _meta(self) -> dict:
        meta: dict[str, Any] = {"workspace": self.workspace_path}
        warnings: list[str] = []
        try:
            meta["index_stale"] = bool(self.client.is_index_stale())
        except Exception:
            meta["index_stale"] = None
        if meta["index_stale"]:
            warnings.append("Index is stale or a graph update was interrupted; allow pending retries or restart to rescan.")
        try:
            stats = self.client.get_stats() or {}
            built = stats.get("edges") != "not built"
            meta["graph_built"] = built
            if not built:
                warnings.append("Graph edges are not compiled yet; relationship data is unavailable.")
        except Exception:
            meta["graph_built"] = None
        if self._health:
            try:
                warnings.extend(self._health() or [])
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("health callback failed: %s", e)
        if warnings:
            meta["warnings"] = warnings
        return meta

    def _error(self, tool: str, message: str, **extra) -> dict:
        out = {"ok": False, "tool": tool, "error": message, "meta": self._meta()}
        out.update({k: v for k, v in extra.items() if v})
        return out

    def _not_found(self, tool: str, res: Resolution) -> dict:
        out = self._error(tool, f"No indexed symbol matches '{res.query}'", suggestions=res.suggestions)
        out["status"] = NOT_FOUND
        out["hint"] = ("Try the bare name (create_sale), a qualified name (SalesAPI.create_sale), "
                       "or <file>:<name> (api.py:create_sale). Unindexed files are excluded.")
        return out

    def _ambiguous(self, tool: str, view: IndexView, res: Resolution) -> dict:
        head, omitted = bounded(res.candidates, MAX_RELATED)
        meta = self._meta()
        meta["candidates"] = len(res.candidates)
        out = {
            "ok": True,
            "tool": tool,
            "status": AMBIGUOUS,
            "meta": meta,
            "candidates": [brief_record(view, n) for n in head],
            "hint": "Several definitions match; call again with the exact `id` (or restrict with `path`).",
        }
        if omitted:
            out["candidates_omitted"] = omitted
        return out

    def _suggest_paths(self, view: IndexView, rel: str) -> list[str]:
        import difflib
        folders = sorted({os.path.dirname(fp) for fp in view.file_paths if os.path.dirname(fp)})
        pool = folders + view.file_paths
        probe = rel.rsplit("/", 1)[-1] if rel else rel
        by_tail = {p.rsplit("/", 1)[-1]: p for p in pool}
        close = difflib.get_close_matches(probe, list(by_tail), n=5, cutoff=0.6)
        return [by_tail[c] for c in close]
