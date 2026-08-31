# 🦠 Cordyceps Search

**Cordyceps Search** is a high-performance codebase semantic search, dependency tracking, and AST-aware refactoring engine. It is packaged as an MCP (Model Context Protocol) server designed to empower developer agents with the structural understanding and refactoring capabilities of a mature IDE.

By combining the blazing speed of a Rust-native CSR (Compressed Sparse Row) graph engine with the precision of `tree-sitter` AST parsers, Cordyceps Search enables instant semantic discovery, caller/callee blast radius auditing, and cross-file refactoring.

---

## 🚀 Key Features

* **Zero-Copy CSR Graph Engine**: Backed by a high-performance Rust core (`engramdb`) for microsecond-level graph traversals and memory efficiency.
* **Multi-Language AST Parsing**: Full structural extraction (classes, methods, functions, calls, returns, docstrings) for Python, JavaScript, JSX, TypeScript, and TSX using `tree-sitter`.
* **Cross-Boundary API Tracking**: Traces connections between frontend HTTP requests (`fetch`, `axios`) and backend endpoints (Django, Flask, FastAPI).
* **AST-Aware Safe Refactoring**: Supports syntax-validated node edits, structured node creation, and AST-aware renaming with cross-file reference propagation.
* **Watchdog-Driven Real-Time Sync**: Automatically detects file additions, modifications, and deletions in the workspace, debouncing events, and keeping the code graph dynamically updated.
* **Thread-Safe RW Concurrency**: Protected by a read-write lock mechanism to ensure safe multi-threaded reads while keeping the unsendable Rust engine execution serial.

---

## Supported Languages

Indexed via `tree-sitter`. Adding a language = drop a YAML config in `src/database/parser/languages/` — no code changes required. Other files are ingested as body-only `File` nodes (searchable, no AST).

| Language | Extensions | Parser | Notes |
|---|---|---|---|
| Python | `.py` | `tree_sitter_python` | Full contextual resolution — imports/aliases, lexical scopes, receiver types, `self`/`cls`/`super()` MRO, shadowed locals, union annotations |
| JavaScript | `.js` | `tree_sitter_javascript` | Functions, classes, calls, imports/exports, routes, HTTP calls |
| JavaScript (JSX) | `.jsx` | `tree_sitter_javascript` | Same as JS + JSX components |
| TypeScript | `.ts` | `tree_sitter_typescript` | Types, interfaces, generics, inheritance |
| TypeScript (TSX) | `.tsx` | `tree_sitter_typescript` | Same as TS + JSX/TSX components |

Body-only file nodes (no AST, searchable as files): `.json`, `.md`, `.html`, `.css`, `.yml`, `.yaml`, `.toml`, `.txt`, `.sql`.

Excluded from indexing: `node_modules`, `venv`, `.venv`, `__pycache__`, `target`, `dist`, `build`, `migrations`, `.git`, `.idea`, `.vscode`, `coverage`, `.next`, `.nuxt`, `fixtures`, `test_fixtures`, `test_data` and any dotfile dirs. Extend via `CORDYCEPS_EXCLUDE` env var (comma-separated).

---

## 🏗️ Architecture

![alt text](image.png)

* **`main.py`**: The stdio transport server entrypoint using `FastMCP`.
* **`src/database/`**: Core client interface wrapping the Rust engine, managing Django ORM/URL resolutions, and enforcing read-write thread safety.
* **`src/database/parser/`**: Language-specific grammar configurations and the `UniversalCodeParser` that walks tree-sitter ASTs.
* **`src/watcher/`**: File system events monitor built on `watchdog` to coordinate incremental rebuilds.
* **`src/services/`**: Logical business operations supporting graph queries, fuzzy searches, and AST modification edits.

---

## Getting Started

### Prerequisites

* Python `>= 3.11`
* [uv](https://github.com/astral-sh/uv) (Fast Python Package Installer)
* Rust toolchain (only for local `engramedb` development)

### Installation

```bash
git clone https://github.com/ghassenTn/cordyceps_mcp.git
cd cordyceps_mcp
uv sync --python 3.11
```

This installs `engramedb` from PyPI as declared in `pyproject.toml`. No local engine checkout required for normal use.

### Local Development (engine + MCP)

For editable installs where MCP uses a sibling `engramedb` checkout:

```bash
git clone https://github.com/ghassenTn/cordyceps_mcp.git
git clone https://github.com/ghassenTn/engramedb.git
# layout: cordyceps_mcp/  ../engramedb/
cd cordyceps_mcp
uv sync --python 3.11  # uses [tool.uv.sources] path = "../engramedb"
```

Engine development:

```bash
cd ../engramedb
maturin develop --release
```

---

## 📂 Command Guide

Run the following commands using the `uv` toolchain:

| Command | Description |
| :--- | :--- |
| `uv run python main.py [workspace_path]` | Starts the MCP server (stdio transport). Defaults to current directory. |
| `uv run python -m pytest` | Runs the full test suite (89 tests passing). |
| `uv run python -m pytest -m unit` | Runs only parser and YAML serialization unit tests. |
| `uv run python -m pytest -m integration` | Runs graph database and editor integration tests. |
| `uv run python -m pytest -xvs test_ast_parser.py` | Fast feedback loop for debugging parser tests. |

---

## MCP Tools Provided

The server exposes a single unified tool. Legacy standalone tools (`search_nodes`, `analyse_impact`, `trace_business_flow`, etc.) were removed — all capabilities are now via the query DSL.

### `query_dsl` — Unified Code Graph Query

Executes a Cordyceps Query DSL string against the live CSR graph. All outputs are YAML.

**Parameters**

* `raw` (str, required): DSL query string
* `expand_body` (bool, default `false`): when `true` returns full `body`, otherwise `body_preview` (150 chars)

`query_dsl_help` returns the full DSL grammar.

**DSL quick reference**

| Query | Example | Purpose |
|---|---|---|
| `GET` | `GET functions WHERE name LIKE 'create_*' LIMIT 20` | Filtered listing with projections, `ORDER BY`, `LIMIT`/`OFFSET` |
| `SEARCH` | `SEARCH "sale" IN functions WHERE file_path CONTAINS 'sales'` | Fuzzy / regex search |
| `GLOB` | `GLOB "src/modules/sales/*.py"` | File glob |
| `METADATA` | `METADATA FOR "src/api.py:create_sale"` | Full node metadata + callers/callees |
| `IMPACT` | `IMPACT OF "src/services.py:create_sale" DIRECTION callers DEPTH 2` | Blast radius (callers/callees) |
| `PATH` | `PATH FROM "a.py:foo" TO "b.py:bar"` | Shortest dependency path |
| `FLOW` | `FLOW FOR "src/api.py:create_sale" DEPTH 5` | Business-flow tree |
| `STACK` | `STACK FOR "/api/sales"` | Frontend hook → backend handler trace |
| `STATS` | `STATS FOR "src/modules/sales"` | Module stats (LOC, counts) |
| `CHECK LAYERS` | `CHECK LAYERS "domain" AGAINST "infra"` | Architecture layer violation check |

All legacy search / impact / flow / full-stack capabilities are available through `query_dsl` with the appropriate verb — see `query_dsl_help` for the complete grammar.

---

## 🧪 Testing

The test coverage covers parsing, synchronization safety, and graph queries. To run tests, use:
```bash
uv run python -m pytest
```
All tests use pytest fixtures defined in `conftest.py` with custom thread-safety and environment isolated markers.

---

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
