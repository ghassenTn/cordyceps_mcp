# Cordyceps Search — AGENTS.md

## Commands

```sh
uv run python main.py [workspace_path]   # Start MCP server (stdio); defaults to cwd
uv run python -m pytest                  # Full suite = 244 Python tests
uv run python -m pytest -m unit          # 99 tests (test_ast_parser, test_language_adapter, test_yaml_utils)
uv run python -m pytest -m integration   # 22 tests (test_graph_service)
uv run python -m pytest -xvs test_ast_parser.py  # Fast loop on parser tests
cd ../engramedb && maturin develop       # Rebuild Rust engine after Rust changes
uv run python cordyceps_gui.py           # PyQt6 "Query Studio" GUI (dev tool)
uv run python stress_test.py             # Standalone concurrency stress test
```

**conftest.py** defines fixtures (`isolated_temp_dir`, `python_test_file`, `js_test_file`) and markers. Only `unit` and `integration` are actually used. **`slow` and `thread_safety` markers are registered but unused** (0 tests). **`test_query_engine.py` (123 tests) has NO marker — it runs only in the full suite**, not under `-m unit`/`-m integration`.

## Git & Release Layout

- **Parent `cordyceps/`** is the monorepo git (this `AGENTS.md` lives in `cordyceps_mcp/`). Layout: `cordyceps/engramedb/` (Rust engine, own git `origin ghassenTn/engramedb`, publishes `engramedb` to PyPI) + `cordyceps/cordyceps_mcp/` (MCP server, tracked by parent). `engramedb` is a gitlink in parent.
- `engramedb` on PyPI, imported as `engramdb` (`from engramdb import PyMetadataEngine`). Dev install uses `[tool.uv.sources] path = "../engramedb"`. CI: `engramedb/.github/workflows/build.yml`, triggered on `v*` tags, builds wheels for 3.11–3.13.

## Architecture

- **`main.py`** — MCP server entrypoint (`FastMCP("CordycepsSearch")`). **`query_dsl` is the only MCP tool.** Legacy standalone tools were removed (tool_wrappers.py, editor_service.py, ast_service.py, schemas/ are gone) — anything not reachable from `query_dsl` is dead code.
- **`src/query/`** — Query DSL engine: Lark grammar (`grammar.lark`) → `parser.py` → `compiler.py` → dict, then `to_yaml()`. Full reference: `CORDYCEPS_QUERY_DSL.md`.
- **`src/database/`** — `EngramClient` wraps Rust `PyMetadataEngine`. Thread safety is inside Rust via `Arc<RwLock<InnerEngineState>>`. Use `get_graph_db()` singleton factory (keyed by abs workspace path).
- **`src/database/parser/`** — `UniversalCodeParser` (tree-sitter). `languages/` ships `python.yaml`, `javascript.yaml` + `javascript_jsx.yaml` (JS/JSX via `tree_sitter_javascript`), and `typescript.yaml` + `typescript_tsx.yaml` (TS/TSX via `tree_sitter_typescript`). Non-code extensions (`.json`, `.md`, `.html`, `.css`, `.yml`, `.yaml`, `.toml`, `.txt`, `.sql`) are indexed as body-only File nodes. Adding a language = drop a YAML config in `languages/`, no code changes.
- **`src/watcher/sync_handler.py`** — `GraphSyncHandler` queues file events; main thread drains via `_drain_sync_queue()` in `main.py:36` before each tool handler.
- **`src/services/`** — `graph_service.py` (impact/flow/stack analysis consumed by `compiler.py`), `yaml_utils.py` (`to_yaml`). All tool outputs are YAML via `yaml.dump(..., sort_keys=False)`.
- **Resolution pipeline** (after edits, before queries): `repopulate_edges → resolve_django_relations → resolve_url_patterns → resolve_mount_prefixes → resolve_middleware_edges → resolve_api_calls → rebuild`.

## Conventions

- **Package manager:** `uv` (not pip/poetry). Lockfile: `uv.lock`.
- **Workspace path:** CLI arg wins, else `WORKSPACE_PATH` env var, else `os.getcwd()` (no hardcoded fallback anywhere — `main.py:157`, `graph_client.py:43`). `main.py` sets `WORKSPACE_PATH` from argv for the process.
- **Excluded dirs:** defined in `main.py:168-180` and `sync_handler.py:60-69`; extended via `CORDYCEPS_EXCLUDE` env var (comma-separated). Node_modules, venvs, `dist`, `target`, `migrations`, `.git`, and fixture dirs are always excluded.
- **Thread safety:** Rust engine coarse-grained `Arc<RwLock>`; reads use `read()`, writes use `write()`. No `py.allow_threads()` — sub-ms ops; releasing the GIL would cause GIL+Mutex deadlock. All Rust calls happen on the main thread (`_drain_sync_queue`).
- **Initial scan** (`main.py:167-228`): multi-threaded parse (ThreadPoolExecutor), single-threaded graph injection, then the resolution pipeline + `clean_stale_files()` + `build()` + `write_index_meta()`.
- **Index staleness:** `.cordyceps_index_meta.json` records the indexing fingerprint (`compute_index_fingerprint()`). `is_index_stale()` surfaces `index_stale` in query `meta` when config changed without a rescan. Changing language adapters/extensions requires a full re-scan to rebuild.
- **Node IDs:** `rel_file_path:NodeName`, `rel_file_path:ClassName.method_name`, files = `rel_file_path`, folders = `relative/folder/path`.

## Gotchas

- **Typed graph edges:** callers/callees and impact use executable edges only. `get_dependencies()` / `get_dependents()` include structural containment, imports, and ORM relationships as well. Both edge sets are persisted in snapshot version 2.
- **Query result bodies are truncated** to 150-char `body_preview` by default; pass `expand_body=True` to `query_dsl` for full bodies.
- **List queries default to compact output** for token safety: `GET` functions/classes, `SEARCH`, `GLOB`, `FIND IMPLEMENTS`, `FIND DECORATED`, and `IMPACT` affected nodes return `{file_path: ["Name: start-end", ...]}` strings (GLOB files = `{path: "start-end"}`). Full metadata stays available via projections, `WITH callers|callees`, or `METADATA FOR`. File-like types (files/folders/routes/modules/packages) keep full-dict flat lists. Built by `_group_results()` in `compiler.py`.
- **Pagination:** default page size is 100 (`DEFAULT_PAGE_SIZE`, compiler.py:23); explicit `LIMIT` honored up to `MAX_QUERY_RESULTS = 1000` (compiler.py:19); `LIMIT ALL` / `LIMIT *` (`UNLIMITED = -1` sentinel, parser.py:13) returns everything in one query, bypassing the cap. Truncated results emit a `hint` with the next `OFFSET n LIMIT n` / `RANGE a:b`.
- **`opencode.jsonc`** autoApproves the `cordyceps` MCP server's `query_dsl` tool and registers it with an absolute workspace path.
