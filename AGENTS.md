# Cordyceps MCP — AGENTS.md

## Commands

```sh
uv run python main.py [workspace_path]   # Start MCP server (stdio); defaults to $WORKSPACE_PATH, then cwd
uv run python -m pytest                  # Full suite
uv run python -m pytest -m unit          # test_ast_parser, test_language_adapter, test_yaml_utils, test_structure, test_mcp_server
uv run python -m pytest -m integration   # test_indexing (real parse -> index -> tools on temp workspaces)
uv run python -m pytest -xvs test_structure.py   # Fast loop on tool logic (in-memory fake graph, no Rust)
cd ../engramedb && maturin develop       # Rebuild Rust engine after Rust changes (sibling checkout only)
```

**conftest.py** defines fixtures (`isolated_temp_dir`, `python_test_file`, `js_test_file`) and the `unit` / `integration` markers (`slow` / `thread_safety` are registered but unused).

## Git & Release Layout

- This `cordyceps_mcp/` directory is the Git root. The sibling `../engramedb/` checkout is a separate repository that publishes `engramedb` to PyPI.
- `engramedb` is imported as `engramdb` (`from engramdb import PyMetadataEngine`). `pyproject.toml` constrains the compatible PyPI API; a local editable engine needs a temporary `[tool.uv.sources]` entry (see the comment in `pyproject.toml`).

## Architecture

- **`main.py`** — MCP entrypoint (`FastMCP("Cordyceps")`). Exactly three tools: `code_map`, `lookup_symbol`, `impact`. Each goes through `_answer()`, which drains pending file events, builds a `CodeStructure`, and serialises the dict to YAML. `serve()` owns the lifecycle (index, watcher, stdio). There is no query language and no text search: the agent's own `read/grep/glob` cover that.
- **`src/structure/`** — the tools' logic, pure over the graph client:
  - `index_view.py` — `IndexView` = one `get_all_metadata()` snapshot per request + lookup tables; the *only* place that parses node IDs. `normalize_path()` handles `.`/`./`/absolute-in-workspace paths.
  - `resolver.py` — `resolve_symbol()` → `Resolution(status: resolved|ambiguous|not_found)`. Tiers: exact id → `<file>:<qual>` shorthand → bare/dotted name → file/folder path → case-insensitive. **Never picks one of several matches**; ambiguity is returned as candidates.
  - `records.py` — allow-listed projections (`brief_record`, `symbol_record`, `compact_entry`, `group_by_file`). No `body`, no `extra_json`, no raw `calls`. `is_noise_call()` filters builtins from `unresolved_calls`.
  - `service.py` — `CodeStructure.map/lookup/impact`. Bounds: `MAX_MAP_ENTRIES=300`, `MAX_RELATED=50`, `MAX_IMPACT_NODES=300`, `MAX_IMPACT_DEPTH=25`. `impact` is a Python BFS over `get_callers`/`get_callees` recording hop depth; it reports `truncated` and `more_beyond_depth` explicitly.
- **`src/indexing/workspace.py`** — `WorkspaceIndex`: `collect_source_files()` (uses `GraphSyncHandler`'s exclusion policy — single source of truth), `initial_scan()` (warm start only when *nothing* changed on disk; drift → full re-parse), `resolve_cross_file_edges()` (the ordered pipeline), `drain_pending_events()` (records failures in `sync_errors`, surfaced as `meta.warnings`), `start_watching()`.
- **`src/database/`** — `EngramClient` wraps Rust `PyMetadataEngine`; `get_graph_db()` singleton keyed by abs workspace path. Cross-file passes: `resolve_import_edges`, `resolve_django_relations`, `resolve_url_patterns`, `resolve_mount_prefixes`, `resolve_middleware_edges`, `resolve_api_calls`.
- **`src/database/javascript_resolver.py`** — conservative O(nodes + calls) JS/TS resolver. Uses structured ESM/CommonJS bindings, exports/re-exports, lexical scope, `this`/`super`, and typed/direct-construction receivers. JS raw calls stay in `extra_json`; generated executable edges are globally replaced each resolution pass. No global-name fallback.
- **`src/database/parser/`** — `UniversalCodeParser` (tree-sitter). `languages/*.yaml` register `.py`, `.js/.jsx`, `.ts/.tsx`. Non-code extensions are body-only `File` nodes.
- **`src/watcher/sync_handler.py`** — `GraphSyncHandler`: watchdog events → queue; `update_file_in_graph()` injects nodes/edges for one file (called on the main thread by `WorkspaceIndex`).
- **`src/services/yaml_utils.py`** — `to_yaml()` (`sort_keys=False`); the only serialisation boundary.

## Conventions

- **Package manager:** `uv`. `uv.lock` is generated locally and ignored; `pytest` is a dev dependency group.
- **Workspace path:** CLI arg → `WORKSPACE_PATH` env → `os.getcwd()`. `serve()` sets `WORKSPACE_PATH` for the process.
- **Thread safety:** all Rust calls on the main thread (`drain_pending_events()` before each tool). The engine's `Arc<RwLock>` protects single calls, not multi-call batches.
- **Node IDs:** `rel_file_path:Qualified.Name` (methods/nested defs), `rel_file_path:name` (declarations), `rel_file_path:<url>` (routes, may end with `/`), `rel_file_path:mw:<name>` (middleware), files = `rel_file_path`, folders = `relative/folder`.
- **Edge types:** callers/callees/impact use **executable** edges only (resolved calls + generated route/HTTP/middleware links). `get_dependencies()/get_dependents()` also include structural edges (containment: child → parent; imports: imported file → importer). Never use structural edges for blast radius.
- **Response envelope:** `{ok, tool, meta{workspace, index_stale, graph_built, warnings?}, ...}`. Lists are bounded; omissions are reported (`*_omitted`, `truncated`).

## Gotchas

- Call resolution is contextual for Python and JS/TS (`CONTEXTUAL_CALL_RESOLUTION_EXTENSIONS` in `service.py`). JS/TS calls are quarantined from Rust's generic resolver and resolved in `javascript_resolver.py`; dynamic/factory receivers and unresolved package aliases deliberately remain unresolved.
- `resolve_api_calls` Tier 3 (name guessing) only runs when no URL matched (Tier 1/2); numeric path segments normalise to `{id}`.
- Index staleness (`.cordyceps_index_meta.json` fingerprint vs `compute_index_fingerprint()`) covers the full flattened language YAML configs + `PARSER_SCHEMA_VERSION`. Bump `PARSER_SCHEMA_VERSION` when extraction logic changes outside those configs.
- The engine restores a snapshot for reads but its first mutation starts an empty in-memory session; hence warm start only on a byte-identical manifest.
- `opencode.jsonc` (here and in the parent) allows the `cordyceps_*` MCP tool prefix through OpenCode permissions.
