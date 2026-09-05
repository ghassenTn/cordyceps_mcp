"""Cordyceps MCP server: structural code understanding for coding agents.

Three tools, all answered from a persisted code graph (tree-sitter + Rust CSR):

    code_map       outline of a directory or file
    lookup_symbol  where a symbol is defined + its direct relationships
    impact         transitive callers/callees (blast radius) of a symbol

Text search, globbing and reading files are deliberately NOT here: the agent
already has those. This server answers the questions those tools cannot:
"what is the shape of this codebase", "which definition is this exactly", and
"what else depends on it".

Usage: ``python main.py [workspace_path]`` (falls back to ``$WORKSPACE_PATH``,
then the current directory).
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from src.indexing import WorkspaceIndex
from src.services.yaml_utils import to_yaml
from src.structure import CodeStructure, DEFAULT_IMPACT_DEPTH

logger = logging.getLogger(__name__)

mcp = FastMCP("Cordyceps")

# Set once in ``serve()``; tools are no-ops until the index exists.
_index: WorkspaceIndex | None = None
MapDepth = Annotated[int, Field(ge=1, le=4)]
ImpactDepth = Annotated[int, Field(ge=1, le=25)]
ImpactDirection = Literal["callers", "callees"]


def _answer(tool: str, run) -> str:
    """Sync pending file events, run ``run(structure)`` and serialise the result."""
    if _index is None:
        return to_yaml({"ok": False, "tool": tool,
                        "meta": {"workspace": None, "index_stale": None, "graph_built": False},
                        "error": "Workspace index not initialised; start the server with `python main.py <workspace>`."})
    try:
        _index.drain_pending_events()
        structure = CodeStructure(_index.client, _index.workspace_path, health=_index.health_warnings)
        return to_yaml(run(structure))
    except Exception as exc:
        logger.exception("%s failed", tool)
        return to_yaml({"ok": False, "tool": tool,
                        "meta": {"workspace": _index.workspace_path,
                                 "index_stale": None, "graph_built": None},
                        "error": str(exc)})


@mcp.tool()
def code_map(path: str = ".", depth: MapDepth = 1) -> str:
    """Outline of a directory or file from the code graph.

    Directory: immediate sub-folders and files with file/symbol counts and the
    top-level definition names of each file. ``depth`` (1-4) expands nested
    folders. File: the definition tree (classes, methods, functions, nested
    defs, routes, declarations) with line ranges and signatures, plus imports.

    Use it first to orient in an unfamiliar codebase, then ``lookup_symbol``
    for a specific definition. Paths are workspace-relative ("." = root);
    absolute paths inside the workspace are accepted.
    """
    return _answer("code_map", lambda s: s.map(path, depth))


@mcp.tool()
def lookup_symbol(symbol: str, path: str | None = None) -> str:
    """Find where a symbol is defined and what it is directly connected to.

    ``symbol`` may be a bare name (create_sale), a qualified name
    (SalesAPI.create_sale), ``<file>:<name>`` (api.py:create_sale), a full node
    id, or a file path. ``path`` optionally restricts matches to one folder.

    Returns status ``resolved`` (one definition: id, kind, file, lines,
    signature, container, callers, callees, members, unresolved calls),
    ``ambiguous`` (several candidates; call again with an exact id), or
    ``not_found`` (with close-match suggestions). Never picks one of several
    matches silently.
    """
    return _answer("lookup_symbol", lambda s: s.lookup(symbol, path))


@mcp.tool()
def impact(symbol: str, depth: ImpactDepth = DEFAULT_IMPACT_DEPTH,
           direction: ImpactDirection = "callers") -> str:
    """Blast radius: everything that transitively depends on a symbol.

    ``direction='callers'`` (default) answers "what could break if I change
    this"; ``'callees'`` answers "what does this rely on". ``depth`` is the
    number of call hops to follow (default 2, max 25). Results are grouped by
    file with the hop distance of each entry; ``meta.direct`` vs
    ``meta.total`` separates direct from transitive, and
    ``meta.more_beyond_depth`` / ``meta.truncated`` say when the picture is
    incomplete. Only executable edges (calls, route/HTTP links) are followed,
    never imports or containment.
    """
    return _answer("impact", lambda s: s.impact(symbol, depth, direction))


def serve(workspace_path: str) -> None:
    """Index ``workspace_path``, watch it for changes and serve MCP over stdio."""
    global _index
    workspace_path = os.path.abspath(workspace_path)
    os.environ["WORKSPACE_PATH"] = workspace_path
    logger.info("Starting Cordyceps MCP server for %s", workspace_path)

    _index = WorkspaceIndex(workspace_path)
    observer = _index.start_watching()
    try:
        summary = _index.initial_scan()
        logger.info("Initial scan complete: %s", summary)
        mcp.run(transport="stdio")
    finally:
        observer.stop()
        observer.join()
        _index.db.close()


def cli() -> None:
    """Console-script entrypoint."""
    serve(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WORKSPACE_PATH", os.getcwd()))


if __name__ == "__main__":
    cli()
