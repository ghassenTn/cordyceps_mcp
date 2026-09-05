# Cordyceps

**Cordyceps** is an MCP server that gives coding agents *structural* understanding of a
codebase: what is where, which definition a name refers to, and what else depends on it.

Agents already have `read`, `grep` and `glob`. What they lack is the picture an IDE
builds in the background: the definition tree of every file, canonical identities for
symbols with the same name, and a call graph to answer "what breaks if I change this".
Cordyceps indexes the workspace with `tree-sitter`, stores the result in a Rust CSR
graph (`engramedb`), keeps it in sync with a file watcher, and exposes exactly three tools.

---

## Tools

All tools answer in YAML. Every response carries `meta.index_stale`, `meta.graph_built`
and, when relevant, `meta.warnings`, so an agent can tell a trustworthy answer from a
partial one.

### `code_map(path=".", depth=1)`

Outline of a directory or a file.

* **Directory**: immediate sub-folders (with file/symbol counts), immediate files (line
  count, symbol counts by kind, top-level definition names). `depth` (1–4) expands nested
  folders.
* **File**: the definition tree — classes with their methods, functions with nested
  definitions, routes, declarations — each with line range and signature, plus imports.

```yaml
symbols:
- name: CodeStructure
  kind: Class
  lines: 54-505
  members:
  - name: map
    kind: Function
    lines: 76-89
    signature: 'def map(self, path: str = ".", depth: int = 1) -> dict'
```

### `lookup_symbol(symbol, path=None)`

Where a symbol is defined and what it is directly connected to. `symbol` may be a bare
name (`create_sale`), a qualified name (`SalesAPI.create_sale`), `<file>:<name>`
(`api.py:create_sale`), a full node id, or a file path. `path` restricts matches to a folder.

* `status: resolved` → `symbol` (id, kind, file, lines, signature, container, docstring,
  decorators, base classes) and `relationships` (callers, callees, members, unresolved
  external calls; for files: imports, imported_by, defines).
* `status: ambiguous` → `candidates`. The server **never** picks one of several matches.
* `status: not_found` → close-match `suggestions`.

### `impact(symbol, depth=2, direction="callers")`

Blast radius. Follows executable edges only (calls and framework/HTTP links — never
imports or containment), grouped by file with the hop distance of every entry.

```yaml
meta: {direct: 2, total: 3, files: 2, truncated: false, more_beyond_depth: false}
direct_callers: [src/structure/resolver.py:resolve_symbol, ...]
affected:
  src/structure/resolver.py:
  - resolve_symbol [Function] L40-70 depth=1
  src/structure/service.py:
  - CodeStructure.lookup [Function] L92-112 depth=2
```

`direction="callees"` answers the opposite question ("what does this rely on").
`meta.more_beyond_depth` and `meta.truncated` say explicitly when the picture is incomplete.

---

## Supported languages

| Language | Extensions | Call resolution |
|---|---|---|
| Python | `.py` | Contextual: imports/aliases, lexical scope, `self`/`cls`/`super()`, annotated receivers. Ambiguous calls stay unresolved rather than guessed. |
| JavaScript / JSX | `.js`, `.jsx` | Contextual: ESM/CommonJS imports and aliases, exports/re-exports, lexical scope, shadowing, `this`/`super()`, JSX default imports. No global name fallback. |
| TypeScript / TSX | `.ts`, `.tsx` | Same as JavaScript, plus typed parameters/fields and direct `new Type()` receiver inference. |

JS/TS resolution is deliberately conservative: dynamic factory receivers, runtime
monkey-patching, ambiguous modules/exports, and unconfigured package aliases remain
unresolved instead of falling back to a same-name symbol elsewhere in the repository.

Web-framework linking (Django URLconf, Flask/FastAPI/Ninja decorators, Express routers,
frontend `fetch`/`axios` calls → backend routes) produces `Route`/`Middleware` nodes and
edges that make `impact` cross the frontend/backend boundary. These edges are heuristic
and are flagged as such.

Other files (`.json`, `.md`, `.html`, `.css`, `.yml`, `.yaml`, `.toml`, `.txt`, `.sql`) are
indexed as `File` nodes so they appear in `code_map`.

The language registry is config-driven (`src/database/parser/languages/*.yaml`), but a new
language still needs a grammar dependency, extraction rules tested against that language's
AST shapes, and fixtures for its symbol kinds. Trustworthy call resolution needs a
per-language resolver; unsupported dynamic receivers remain unresolved rather than guessed.

Excluded directories: `node_modules`, `venv`/`.venv`, `__pycache__`, `target`, `dist`,
`build`, `migrations`, `.git`, `.idea`, `.vscode`, `coverage`, `.next`, `.nuxt`, fixture
directories, and any dot-directory. Extend with `CORDYCEPS_EXCLUDE=dir1,dir2`.

---

## Architecture

```
main.py                    MCP entrypoint: 3 tools + serve()
src/structure/             the tools' logic (pure functions over the graph)
  index_view.py            request-scoped snapshot of the index + node-id rules
  resolver.py              name / qualified / <file>:<name> -> canonical id (or candidates)
  records.py               allow-listed projections; never leaks source bodies
  service.py               CodeStructure.map / lookup / impact
src/indexing/workspace.py  WorkspaceIndex: scan, warm start, incremental sync, edge pipeline
src/database/              EngramClient over the Rust engine; cross-file resolution passes
  javascript_resolver.py   contextual JS/TS calls (imports, scope, classes, receiver types)
src/database/parser/       UniversalCodeParser (tree-sitter) + languages/*.yaml
src/watcher/               watchdog handler: parses a file and injects nodes/edges
```

Every tool call first drains pending file-change events on the main thread (all Rust
calls stay on one thread), then reads the graph.

---

## Getting started

Requirements: Python ≥ 3.11 (< 3.14), [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/ghassenTn/cordyceps_mcp.git
cd cordyceps_mcp
uv sync --python 3.11
uv run python main.py /path/to/workspace     # stdio MCP server
# or, after installation:
uv run cordyceps-mcp /path/to/workspace
```

`engramedb` is installed from PyPI. For engine development use a sibling checkout:

```bash
# layout: cordyceps_mcp/  ../engramedb/
#   [tool.uv.sources] engramedb = { path = "../engramedb", editable = true }
cd ../engramedb && maturin develop --release
```

### OpenCode configuration

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "cordyceps": {
      "type": "local",
      "enabled": true,
      "timeout": 120000,
      "command": ["uv", "--directory", "/path/to/cordyceps_mcp", "run", "--python", "3.11",
                  "python", "main.py", "/path/to/your/workspace"]
    }
  },
  "permission": {"cordyceps_*": "allow"}
}
```

OpenCode prefixes MCP tools with the server name, hence the `cordyceps_*` permission.
The workspace path may also come from `WORKSPACE_PATH`; the current directory is the last
fallback. The index is persisted next to the workspace (`.engram_snapshot.bin`,
`.cordyceps_index_meta.json`) and reused on restart when no file changed.

---

## Tests

```bash
uv run python -m pytest                 # full suite
uv run python -m pytest -m unit         # parser, adapters, structural tools (in-memory fake graph), MCP boundary
uv run python -m pytest -m integration  # real workspaces: parse -> index -> tools (test_indexing.py)
```
