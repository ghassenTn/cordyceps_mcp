"""Symbol resolution: turn what an agent typed into canonical node IDs.

Agents rarely know the exact node ID. They type ``create_sale``,
``SalesAPI.create_sale``, ``api.py:create_sale`` or a path copied from an
editor. The resolver accepts all of these and returns either one canonical ID,
an explicit list of candidates, or nothing plus close-match suggestions.

Design rule: **never silently pick one of several matches**. An agent planning
an edit must see the ambiguity; choosing the first hit was the main source of
wrong answers in the previous implementation.
"""
from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field

from .index_view import IndexView, SYMBOL_TYPES, DOTTED_TYPES, is_under, line_start, normalize_path

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"

MAX_SUGGESTIONS = 5


@dataclass
class Resolution:
    query: str
    status: str
    node_id: str | None = None
    candidates: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == RESOLVED


def resolve_symbol(view: IndexView, symbol: str, path: str | None = None,
                   workspace_path: str | None = None) -> Resolution:
    """Resolve ``symbol`` (optionally restricted to files under ``path``)."""
    raw = str(symbol or "").strip()
    norm = _normalize_symbol(raw, workspace_path)
    scope = normalize_path(path, workspace_path) if path else ""

    def in_scope(nid: str) -> bool:
        return is_under(view.file_of(nid), scope)

    if not norm:
        return Resolution(raw, NOT_FOUND)

    # 1. Canonical node ID.
    if norm in view.nodes and in_scope(norm):
        return Resolution(raw, RESOLVED, norm)

    # 2. "<file>:<qualified>" shorthand (file may be a suffix such as api.py).
    if ":" in norm:
        candidates = _match_id_shorthand(view, norm)
    else:
        candidates = _match_name(view, norm)
        if not candidates:
            candidates = _match_path(view, norm.rstrip("/"))

    candidates = _ordered_unique(view, (c for c in candidates if in_scope(c)))
    if len(candidates) == 1:
        return Resolution(raw, RESOLVED, candidates[0])
    if candidates:
        return Resolution(raw, AMBIGUOUS, None, candidates)
    return Resolution(raw, NOT_FOUND, None, [], _suggest(view, norm, scope))


def _normalize_symbol(raw: str, workspace_path: str | None) -> str:
    """Path-like clean-up that keeps node IDs intact.

    Unlike :func:`normalize_path` this never strips a trailing ``/`` because
    Route IDs legitimately end with one (``urls.py:users/``).
    """
    text = raw.replace("\\", "/")
    if workspace_path and text.startswith("/"):
        ws = os.path.abspath(workspace_path).replace("\\", "/").rstrip("/") + "/"
        if text.startswith(ws):
            text = text[len(ws):]
    while text.startswith("./"):
        text = text[2:]
    return text


# ── matching tiers ───────────────────────────────────────────────────────

def _match_id_shorthand(view: IndexView, text: str) -> list[str]:
    file_part, qual = text.split(":", 1)
    file_part = file_part.strip("/")
    exact: list[str] = []
    suffix: list[str] = []
    loose: list[str] = []
    for nid in view.symbol_ids():
        fp = view.file_of(nid)
        if not (fp == file_part or fp.endswith("/" + file_part)):
            continue
        node_qual = view.qualified_name(nid)
        if node_qual == qual:
            exact.append(nid)
        elif view.type_of(nid) in DOTTED_TYPES and node_qual.endswith("." + qual):
            suffix.append(nid)
        elif node_qual.lower() == qual.lower():
            loose.append(nid)
    return exact or suffix or loose


def _match_name(view: IndexView, text: str) -> list[str]:
    """Bare (``bar``) or dotted (``Foo.bar``) definition names."""
    exact: list[str] = []
    suffix: list[str] = []
    for nid in view.symbol_ids():
        qual = view.qualified_name(nid)
        if qual == text or view.name_of(nid) == text:
            exact.append(nid)
        elif view.type_of(nid) in DOTTED_TYPES and qual.endswith("." + text):
            suffix.append(nid)
    if exact or suffix:
        return exact or suffix
    # Case-insensitive fallback, still restricted to definitions.
    return [nid for nid in view.ids_named(text) if view.type_of(nid) in SYMBOL_TYPES]


def _match_path(view: IndexView, text: str) -> list[str]:
    """File (by relative path or basename) then folder nodes."""
    files = [nid for fp, nid in ((fp, view.file_node(fp)) for fp in view.file_paths)
             if fp == text or fp.endswith("/" + text)]
    if files:
        return [f for f in files if f]
    return [nid for nid, meta in view.nodes.items()
            if meta.get("type") == "Folder" and (nid == text or nid.endswith("/" + text))]


def _ordered_unique(view: IndexView, ids) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for nid in ids:
        if nid not in seen:
            seen.add(nid)
            out.append(nid)
    out.sort(key=lambda i: (view.file_of(i), line_start(view.get(i)), i))
    return out


def _suggest(view: IndexView, text: str, scope: str) -> list[str]:
    """Close matches over definition names and IDs, as hints only."""
    names: dict[str, str] = {}
    for nid in view.symbol_ids():
        if not is_under(view.file_of(nid), scope):
            continue
        names.setdefault(view.qualified_name(nid), nid)
        names.setdefault(view.name_of(nid), nid)
    probe = text.split(":", 1)[1] if ":" in text else text
    probe = probe.rsplit(".", 1)[-1] if probe and "." in probe and "/" not in probe else probe
    close = difflib.get_close_matches(probe, list(names), n=MAX_SUGGESTIONS, cutoff=0.6)
    if not close:
        close = difflib.get_close_matches(text, list(view.nodes), n=MAX_SUGGESTIONS, cutoff=0.5)
        return close
    return [names[c] for c in close]
