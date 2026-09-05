"""Projection of graph metadata into the compact records returned to agents.

Only an explicit allow-list of fields leaves the server: no source bodies, no
raw ``extra_json``, no parser-internal binding tables. Agents already have
``read`` for source text; these records exist to tell them *where* things are
and *how* they relate.
"""
from __future__ import annotations

from typing import Any

from .index_view import IndexView, lines_span

MAX_DOCSTRING = 240
MAX_SIGNATURE = 200
MAX_INLINE_ITEMS = 20
MAX_INLINE_TEXT = 240


def _truncate(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v not in (None, "")]
    return [value]


def bounded_texts(value: Any, limit: int = MAX_INLINE_ITEMS) -> tuple[list[str], int]:
    values = [_truncate(item, MAX_INLINE_TEXT) or "" for item in _as_list(value)]
    return bounded(values, limit)


def brief_record(view: IndexView, node_id: str) -> dict:
    """Minimal identification: enough to disambiguate and to call other tools."""
    meta = view.get(node_id) or {}
    rec: dict[str, Any] = {
        "id": node_id,
        "kind": meta.get("type", "Unknown"),
        "name": view.qualified_name(node_id),
        "file": view.file_of(node_id),
    }
    span = lines_span(meta)
    if span:
        rec["lines"] = span
    sig = _truncate(meta.get("signature"), MAX_SIGNATURE)
    if sig:
        rec["signature"] = sig
    return rec


def symbol_record(view: IndexView, node_id: str) -> dict:
    """Full (allow-listed) definition record for ``lookup_symbol``."""
    meta = view.get(node_id) or {}
    rec = brief_record(view, node_id)
    kind = rec["kind"]

    container = view.container_of(node_id)
    if container and container != rec["file"]:
        rec["container"] = container

    doc = _truncate(meta.get("docstring"), MAX_DOCSTRING)
    if doc:
        rec["docstring"] = doc

    for key in ("decorators", "base_classes"):
        values = _as_list(meta.get(key))
        if values:
            rec[key], omitted = bounded_texts(values)
            if omitted:
                rec[f"{key}_omitted"] = omitted

    for flag in ("is_async", "is_generator", "is_exported"):
        if meta.get(flag) is True:
            rec[flag] = True
    if isinstance(meta.get("param_count"), int) and kind == "Function":
        rec["param_count"] = meta["param_count"]

    if kind == "Route":
        rec["url"] = meta.get("full_url") or meta.get("url") or rec["name"]
        methods = _as_list(meta.get("methods"))
        if methods:
            rec["methods"], omitted = bounded_texts(methods)
            if omitted:
                rec["methods_omitted"] = omitted
        handlers = _as_list(meta.get("view_names")) or _as_list(meta.get("view_name"))
        if handlers:
            rec["handlers"], omitted = bounded_texts(handlers)
            if omitted:
                rec["handlers_omitted"] = omitted
    elif kind == "Declaration" and meta.get("call"):
        rec["initializer_call"] = meta["call"]
    elif kind == "Middleware" and meta.get("middleware_type"):
        rec["middleware_type"] = meta["middleware_type"]
    elif kind == "File":
        rec.pop("signature", None)
        rec.pop("lines", None)
        lines = meta.get("lines") or {}
        if isinstance(lines, dict) and lines.get("end"):
            rec["line_count"] = lines["end"]
        frameworks = _as_list(meta.get("frameworks"))
        if frameworks:
            rec["frameworks"], omitted = bounded_texts(frameworks)
            if omitted:
                rec["frameworks_omitted"] = omitted

    api = meta.get("api_endpoint")
    if isinstance(api, dict) and api.get("url"):
        rec["endpoint"] = {k: api[k] for k in ("url", "methods", "framework") if api.get(k)}
        for key in ("url", "framework"):
            if key in rec["endpoint"]:
                rec["endpoint"][key] = _truncate(rec["endpoint"][key], MAX_INLINE_TEXT)
        if isinstance(rec["endpoint"].get("methods"), list):
            methods, omitted = bounded_texts(rec["endpoint"]["methods"])
            rec["endpoint"]["methods"] = methods
            if omitted:
                rec["endpoint"]["methods_omitted"] = omitted
    return rec


def compact_entry(view: IndexView, node_id: str, depth: int | None = None) -> str:
    """One-line ``<qualified> [<Kind>] L<start>-<end>`` used in grouped listings.

    The node ID is always ``<file>:<qualified>``; the file is the group key.
    """
    meta = view.get(node_id) or {}
    parts = [view.qualified_name(node_id), f"[{meta.get('type', 'Unknown')}]"]
    span = lines_span(meta)
    if span and meta.get("type") not in ("File", "Folder"):
        parts.append(f"L{span}")
    if depth is not None:
        parts.append(f"depth={depth}")
    return " ".join(parts)


def group_by_file(view: IndexView, node_ids: list[str], depths: dict[str, int] | None = None) -> dict[str, list[str]]:
    """``{file_path: [compact entries...]}`` ordered by file then line."""
    grouped: dict[str, list[str]] = {}
    ordered = sorted(node_ids, key=lambda i: (view.file_of(i), (view.get(i) or {}).get("lines", {}).get("start") or 0, i))
    for nid in ordered:
        key = view.file_of(nid) or "(unknown)"
        grouped.setdefault(key, []).append(
            compact_entry(view, nid, depths.get(nid) if depths else None))
    return grouped


def bounded(items: list, limit: int) -> tuple[list, int]:
    """Return ``(head, omitted_count)``."""
    if len(items) <= limit:
        return list(items), 0
    return list(items[:limit]), len(items) - limit


def import_statements(meta: dict) -> list[str]:
    """Unique import statements of a file in source order.

    The parser records both the module spec (``os``) and the statement
    (``import os``) for every import; the statement form carries more
    information, so it is preferred whenever present.
    """
    raw = [str(i) for i in (meta.get("imports") or []) if i]
    statements = [i for i in raw if " " in i.strip()]
    chosen = list(dict.fromkeys(statements or raw))
    lines = meta.get("import_lines") or {}
    if isinstance(lines, dict):
        chosen.sort(key=lambda i: (lines.get(i, 10 ** 9), i))
    return [_truncate(item, MAX_INLINE_TEXT) or "" for item in chosen]


# Call names that carry no dependency information for an agent: language
# builtins and ubiquitous container/string methods. Filtered from
# ``unresolved_calls`` so that real external dependencies stand out.
_PY_BUILTINS = frozenset(n for n in dir(__import__("builtins")) if not n.startswith("_"))
_JS_GLOBALS = frozenset({"require", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
                         "parseInt", "parseFloat", "isNaN", "encodeURIComponent", "decodeURIComponent",
                         "String", "Number", "Boolean", "Array", "Object", "Error", "Promise", "Date", "Map", "Set"})
_NOISE_RECEIVERS = frozenset({"console", "JSON", "Math", "Object", "Array", "Promise", "Number",
                              "String", "Date", "logger", "logging"})
# Deliberately excludes verbs that are meaningful on ORMs/HTTP clients
# (get, update, add, find, count, insert, remove, ...).
_COMMON_METHODS = frozenset({
    "strip", "rstrip", "lstrip", "split", "rsplit", "join", "replace", "startswith", "endswith",
    "lower", "upper", "format", "encode", "decode", "append", "extend", "pop", "items", "keys",
    "values", "setdefault", "sort", "reverse", "push", "map", "filter", "forEach", "reduce", "then",
    "catch", "finally", "toString", "includes", "indexOf", "slice", "splice", "concat", "trim",
    "toLowerCase", "toUpperCase", "stringify", "entries", "isArray",
})


def is_noise_call(call: str) -> bool:
    call = call.strip()
    if "." not in call:
        return call in _PY_BUILTINS or call in _JS_GLOBALS
    receiver, tail = call.split(".", 1)[0], call.rsplit(".", 1)[-1]
    return receiver in _NOISE_RECEIVERS or tail in _COMMON_METHODS
