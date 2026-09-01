import sys
import os
import queue
import logging
from watchdog.observers.polling import PollingObserver as Observer
# pyrefly: ignore [missing-import]
from src.watcher.sync_handler import GraphSyncHandler, get_sync_queue
# pyrefly: ignore [missing-import]
from src.database.graph_client import SNAPSHOT_FILENAME
# pyrefly: ignore [missing-import]
from src.services.yaml_utils import to_yaml
# pyrefly: ignore [missing-import]
from src.query import query as _query_engine

try:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("CordycepsSearch")
except Exception:
    class _DummyMCP:
        def tool(self):
            def _decorator(fn):
                return fn
            return _decorator
        def run(self, *args, **kwargs):
            raise RuntimeError("MCP runtime not available in this environment.")
    mcp = _DummyMCP()

logger = logging.getLogger(__name__)

QUERY_DSL_HELP = """Cordyceps Query DSL Reference
================================

Use this single tool for all code graph discovery and analysis. Keywords are
case-insensitive. Quote node IDs, paths, patterns, decorators, and rules with
single or double quotes.

Quick start
-----------
  STATS
  SEARCH "create_sale" IN functions
  GLOB "**/*.py"
  METADATA FOR "api.py:create_sale"
  IMPACT OF "services.py:create_sale" DIRECTION callers DEPTH 2 MODE summary
  PATH FROM "api.py:create_sale" TO "services.py:create_sale"
  FLOW FOR "api.py:create_sale" DEPTH 5
  STACK FOR "/api/sales"

Core discovery
--------------
  GET [projection] [FROM] <type> [WHERE <expression>] [clauses]
    GET functions
    GET name, file_path FROM classes WHERE file_path CONTAINS "domain"
    GET COUNT(*) FROM functions
    GET SUM(lines_count), AVG(param_count) FROM functions GROUP BY file_path
    GET DISTINCT decorators FROM functions
    GET functions WHERE is_async == true ORDER BY lines_count DESC LIMIT 20
    GET functions WITH callees DEPTH 2 LIMIT 10

  SEARCH [BODIES] [REGEX] <pattern> [OR <pattern> ...] [IN <type>] [clauses]
    SEARCH "create_sale" IN functions
    SEARCH /def\\s+test_\\w+/i IN functions
    SEARCH BODIES "transaction.atomic" IN functions
    SEARCH "login" OR "authenticate" IN functions LIMIT 20

  GLOB <path-pattern> [WHERE <expression>] [clauses]
    GLOB "**/*.py"
    GLOB "src/modules/*/services.py"
    GLOB "**/*.{py,ts,tsx}" LIMIT 100

GLOB returns File and Folder nodes only. Use SEARCH or GET to find symbols in
matching files.

Graph analysis
--------------
  METADATA FOR <node-id>
    METADATA FOR "src/api.py:create_sale"
    Returns full node metadata, direct callers/callees, structural dependencies,
    and unresolved external calls.

  IMPACT OF <node-id> [DIRECTION callers|callees] [DEPTH n]
         [MODE count|summary|detailed]
    IMPACT OF "services.py:create_sale"
    IMPACT OF "services.py:create_sale" DIRECTION callers DEPTH 2 MODE summary
    IMPACT OF "services.py:create_sale" DIRECTION callees MODE count

  PATH FROM <start-node-id> TO <end-node-id>
    PATH FROM "api.py:create_sale" TO "services.py:create_sale"
    Finds the shortest full dependency path. PATH includes structural edges;
    callers/callees and IMPACT use executable edges only.

  FLOW [FOR|OF|FROM] <node-id> [DEPTH n]
    FLOW FOR "api.py:create_sale" DEPTH 5
    FLOW THROUGH "/api/sales" DEPTH 5
    FLOW THROUGH mode traces route, middleware, handler, and service pipeline.

  STACK [FOR|OF] <endpoint-or-node-id>
    STACK FOR "/api/sales"
    STACK FOR "api.py:create_sale"
    Traces frontend component/hook/API client to backend handler when links exist.

Architecture and structure
--------------------------
  STATS [FOR|OF] [path]
    STATS
    STATS FOR "src/modules/sales"
    STATS FOR "/absolute/path/to/workspace/src/modules/sales"
    Bare STATS summarizes the indexed workspace. Absolute workspace paths are
    accepted and normalized to indexed relative paths.

  CHECK LAYERS <layer-path> AGAINST <forbidden-layer-path>
    CHECK LAYERS "domain" AGAINST "infrastructure"
    CHECK LAYERS "src/modules/sales" AGAINST "src/modules/comptabilite"

  LAYERS OF <layer-path>
    LAYERS OF "src/modules/sales"
    Categorizes imports as stdlib, third_party, or project. Root (".") is not
    a layer; an error includes a valid indexed-directory example.

  FIND IMPLEMENTS <base-class-or-protocol> [IN classes]
    FIND IMPLEMENTS "Repository"
    FIND IMPLEMENTS "Protocol" IN classes

  FIND DECORATED WITH <decorator> [IN functions|classes] [WHERE <expression>] [clauses]
    FIND DECORATED WITH "@dataclass"
    FIND DECORATED WITH "router" IN functions LIMIT 50

  ENFORCE <rule> [IN <scope>]
    ENFORCE "domain MUST_NOT_IMPORT infrastructure"
    ENFORCE "domain <- application <- infrastructure"
    ENFORCE "NO_CIRCULAR_DEPENDENCIES"
    ENFORCE "classes IN 'domain/entities' MUST_BE decorated_with 'dataclass'"

Types, predicates, and clauses
------------------------------
  Types: functions, classes, files, folders, routes, middlewares, declarations,
         modules, packages, ALL

  Predicates: ==, =, !=, >, >=, <, <=, LIKE, CONTAINS, STARTSWITH, ENDSWITH,
              =~ / MATCHES, !~ / NOT MATCHES, NOT LIKE, NOT CONTAINS,
              NOT STARTSWITH, NOT ENDSWITH, NOT IN
    GET functions WHERE name LIKE "test_*" AND lines_count > 10
    GET functions WHERE file_path CONTAINS "sales" OR name STARTSWITH "create_"
    GET functions WHERE NOT is_async == true

  Common clauses: ORDER BY <field> [ASC|DESC], LIMIT n, OFFSET n, RANGE start:end,
                  LIMIT ALL, GROUP BY <field>, WITH callers|callees, DEPTH n
    GET functions ORDER BY lines_count DESC LIMIT 20
    SEARCH "service" IN functions OFFSET 20 LIMIT 20
    GET functions RANGE 0:50

Pagination and output
---------------------
  GET and FIND DECORATED default to 100 results. SEARCH and GLOB default to 20.
  Explicit finite limits are capped at 1000; LIMIT ALL bypasses the cap.
  Paginated results include meta.truncated and a next-page hint.
  Symbol listings are compact and grouped by file for token safety. Use a field
  projection or METADATA FOR <node-id> for complete metadata.
  Set expand_body=true on this tool call to return full body text; otherwise
  body_preview is limited to 150 characters.

Recovery
--------
  Parse errors include a command-specific example. If a node ID is unknown,
  use SEARCH first or rely on the "Did you mean" suggestion. Query results set
  meta.index_stale=true when parser/index configuration changed and a rescan is
  required.
"""

_event_handler = None

# Track whether API call re-linking is needed since last rebuild
_api_relink_needed = False


def _drain_sync_queue():
    """Process all pending file-change events on the calling thread.
    Called from MCP tool handlers so all Rust calls stay on the main thread.
    """
    global _event_handler, _api_relink_needed
    eh = _event_handler
    if eh is None:
        return
    sync_queue = get_sync_queue()
    events = []
    while True:
        try:
            events.append(sync_queue.get_nowait())
        except queue.Empty:
            break
    if not events:
        return

    needs_api_relink = False
    for ev_action, ev_path in events:
        if ev_action == 'update':
            try:
                eh.update_file_in_graph(ev_path, skip_rebuild=True)
            except Exception as e:
                logger.error(f"Failed to update {ev_path}: {e}")
        elif ev_action == 'delete':
            try:
                eh.remove_file_from_graph(ev_path, skip_rebuild=True)
            except Exception as e:
                logger.error(f"Failed to remove {ev_path}: {e}")
        # If a urls.py, views.py, or frontend file changed, re-link API calls
        if ev_path.endswith(('.py', '.tsx', '.ts', '.jsx', '.js')):
            needs_api_relink = True

    # pyrefly: ignore [missing-import]
    from src.database import get_graph_db
    db = get_graph_db(eh.workspace_path)

    if needs_api_relink or _api_relink_needed:
        db.client.clear_generated_edges()
        db.client.repopulate_edges()
        db.client.resolve_import_edges()
        db.client.resolve_django_relations()
        db.client.resolve_url_patterns()
        db.client.resolve_mount_prefixes()
        db.client.resolve_middleware_edges()
        db.client.resolve_api_calls()
        _api_relink_needed = False

    db.client.rebuild()

@mcp.tool()
def query_dsl_help():
    """
     Practical reference for every supported Cordyceps Query DSL command.
    """
    return QUERY_DSL_HELP

    # Legacy raw grammar retained below for source-level parser debugging only.
    # The MCP tool intentionally returns the operational reference above.
    return  r"""?start: query

query: stats_query | get_query | search_query | glob_query | metadata_query | impact_query | path_query | flow_query | stack_query | check_layers_query | layers_of_query | find_implements_query | find_decorated_query | enforce_query

// ── GLOB query ──
glob_query: GLOB_KW (QSTRING | IDENTIFIER) (WHERE_KW bool_expr)? get_clause*

// ── SEARCH query ──
search_query: SEARCH_KW BODIES_KW? REGEX_KW? search_pattern (OR_KW search_pattern)* (IN_KW (node_type | STAR))? (WHERE_KW bool_expr)? get_clause*
search_pattern: REGEX_PATTERN | QSTRING

// ── GET query ──
get_query: GET_KW DISTINCT_KW projection FROM_KW node_type (WHERE_KW bool_expr)? get_clause*
         | GET_KW DISTINCT_KW projection? node_type (WHERE_KW bool_expr)? get_clause*
         | GET_KW projection FROM_KW node_type (WHERE_KW bool_expr)? get_clause*
         | GET_KW FROM_KW node_type (WHERE_KW bool_expr)? get_clause*
         | GET_KW projection? node_type (WHERE_KW bool_expr)? get_clause*
         | GET_KW node_type projection (WHERE_KW bool_expr)? get_clause*   // type-first agg: GET functions SUM(lines)
         | GET_KW projection (WHERE_KW bool_expr)? get_clause*          // projection-only (agg, etc.)

projection: projection_item (COMMA projection_item)* -> proj_items
          | COUNT_KW -> proj_count      // bare COUNT (backward compat)
          | STAR -> proj_all

projection_item: agg_func "(" (STAR | field_name) ")" -> proj_agg
               | field_name -> proj_field

projection_fields: field_name (COMMA field_name)*
field_name: IDENTIFIER | FILE_PATH_KW

agg_func: SUM_KW | COUNT_KW | AVG_KW | MIN_KW | MAX_KW

?bool_expr: or_expr
?or_expr: and_expr (OR_KW and_expr)*
?and_expr: not_expr (AND_KW not_expr)*
?not_expr: NOT_KW not_expr -> not_expr
         | primary_expr

?primary_expr: "(" bool_expr ")"
             | condition

?get_clause: with_clause | limit_clause | offset_clause | range_clause | order_clause | depth_clause | group_by_clause
with_clause: WITH_KW graph_op
limit_clause: LIMIT_KW (INT (COMMA INT)? | ALL_KW | STAR)
offset_clause: OFFSET_KW INT
range_clause: RANGE_KW INT (COLON | COMMA | RANGE_SEP) INT
order_clause: ORDER_KW BY_KW field_name (ASC_KW | DESC_KW)?
group_by_clause: GROUP_KW BY_KW group_by_field (COMMA group_by_field)*
group_by_field: FILE_PATH_KW | TYPE | IDENTIFIER
depth_clause: DEPTH_KW INT

node_type: TYPE | ALL_KW

condition: field operator value -> field_condition
         | literal_condition -> cond_literal
literal_condition: (INT | FLOAT | BOOLEAN) OP_EQ (INT | FLOAT | BOOLEAN) -> lit_cond
                 | (INT | FLOAT | BOOLEAN) OP_NE (INT | FLOAT | BOOLEAN) -> lit_cond
                 | BOOLEAN -> lit_bool
field: IDENTIFIER
operator: OP_GTE | OP_LTE | OP_GT | OP_LT | OP_EQ | OP_NE | OP_LIKE | OP_CONTAINS | OP_STARTSWITH | OP_ENDSWITH | OP_REGEX | OP_NOT_REGEX | OP_NOT_LIKE | OP_NOT_CONTAINS | OP_NOT_STARTSWITH | OP_NOT_ENDSWITH | OP_NOT_IN
value: QSTRING | INT | FLOAT | BOOLEAN | "[" list_value "]"
list_value: QSTRING ("," QSTRING)*

graph_op: CALLERS_KW | CALLEES_KW | TREE_KW

// ── METADATA query ──
metadata_query: METADATA_KW FOR_KW QSTRING

// ── IMPACT query ──
impact_query: IMPACT_KW OF_KW QSTRING (DIRECTION_KW direction)? (DEPTH_KW INT)? (MODE_KW impact_mode)?
impact_mode: COUNT_KW | SUMMARY_KW | DETAILED_KW
direction: CALLERS_KW | CALLEES_KW

// ── PATH query ──
path_query: PATH_KW FROM_KW QSTRING TO_KW QSTRING

// ── FLOW query ──
flow_query: FLOW_KW (FOR_KW | OF_KW | FROM_KW)? QSTRING (THROUGH_KW IDENTIFIER+)? (DEPTH_KW INT)?
          | FLOW_KW THROUGH_KW QSTRING (DEPTH_KW INT)?

// ── STACK query ──
stack_query: STACK_KW (FOR_KW | OF_KW)? QSTRING

// ── CHECK LAYERS query ──
check_layers_query: CHECK_KW LAYERS_KW QSTRING AGAINST_KW QSTRING

// ── LAYERS OF query ──
layers_of_query: LAYERS_KW OF_KW QSTRING

// ── FIND IMPLEMENTS query ──
find_implements_query: FIND_KW IMPLEMENTS_KW QSTRING (IN_KW node_type)?

// ── FIND DECORATED WITH query ──
find_decorated_query: FIND_KW DECORATED_KW WITH_KW QSTRING (IN_KW node_type)? (WHERE_KW bool_expr)? get_clause*

// ── ENFORCE query ──
enforce_query: ENFORCE_KW QSTRING (IN_KW QSTRING)?

// ── STATS query ──
stats_query: STATS_KW (FOR_KW | OF_KW)? QSTRING?

// ── Terminals ──
// Keywords
GET_KW: "GET"i
DISTINCT_KW: "DISTINCT"i
SEARCH_KW: "SEARCH"i
GLOB_KW: "GLOB"i
IN_KW: "IN"i
WHERE_KW: "WHERE"i
AND_KW: "AND"i
OR_KW: "OR"i
NOT_KW: "NOT"i
WITH_KW: "WITH"i
LIMIT_KW: "LIMIT"i
OFFSET_KW: "OFFSET"i
RANGE_KW: "RANGE"i
ORDER_KW: "ORDER"i
BY_KW: "BY"i
GROUP_KW: "GROUP"i
ASC_KW: "ASC"i
DESC_KW: "DESC"i
COUNT_KW: "COUNT"i
SUMMARY_KW: "SUMMARY"i
DETAILED_KW: "DETAILED"i
MODE_KW: "MODE"i
SUM_KW: "SUM"i
AVG_KW: "AVG"i
MIN_KW: "MIN"i
MAX_KW: "MAX"i
COLON: ":"
COMMA: ","
RANGE_SEP: ".."
DEPTH_KW: "DEPTH"i
ALL_KW.2: "ALL"i
METADATA_KW: "METADATA"i
FOR_KW: "FOR"i
IMPACT_KW: "IMPACT"i
OF_KW: "OF"i
DIRECTION_KW: "DIRECTION"i
CALLERS_KW: "callers"i
CALLEES_KW: "callees"i
TREE_KW: "tree"i
STAR: "*"
PATH_KW: "PATH"i
FROM_KW: "FROM"i
TO_KW: "TO"i
FLOW_KW: "FLOW"i
THROUGH_KW: "THROUGH"i
STACK_KW: "STACK"i
CHECK_KW: "CHECK"i
LAYERS_KW: "LAYERS"i
AGAINST_KW: "AGAINST"i
FIND_KW: "FIND"i
IMPLEMENTS_KW: "IMPLEMENTS"i
DECORATED_KW: "DECORATED"i
ENFORCE_KW: "ENFORCE"i
STATS_KW: "STATS"i
REGEX_KW: "REGEX"i
BODIES_KW: "BODIES"i

// Node types
TYPE.2: /(?:function|file|folder|middleware|route|module|package|declaration)s?|class(?:es)?/i

// Identifiers
IDENTIFIER.1: /[a-zA-Z_][a-zA-Z0-9_\.]*/
FILE_PATH_KW.3: "file_path"i

// Operators
OP_GTE: ">="
OP_LTE: "<="
OP_GT: ">"
OP_LT: "<"
OP_EQ: "==" | "="
OP_NE: "!="
OP_LIKE: "LIKE"i
OP_CONTAINS: "CONTAINS"i
OP_STARTSWITH: "STARTSWITH"i
OP_ENDSWITH: "ENDSWITH"i
OP_REGEX: "=~" | /matches/i
OP_NOT_REGEX: "!~" | "!=~" | /not\s+matches/i
OP_NOT_LIKE: /not\s+like/i
OP_NOT_CONTAINS: /not\s+contains/i
OP_NOT_STARTSWITH: /not\s+startswith/i
OP_NOT_ENDSWITH: /not\s+endswith/i
OP_NOT_IN: /not\s+in/i

// Strings, Numbers & Booleans
REGEX_PATTERN.2: /\/(?:\\\/|[^\/\n])+\/[a-zA-Z]*/
QSTRING: /"[^"]*"/ | /'[^']*'/
BOOLEAN.2: "true"i | "false"i

%import common.INT
%import common.FLOAT
%import common.WS
%ignore WS


    """

@mcp.tool()
def query_dsl(raw: str, expand_body: bool = False) -> str:
    """Execute a Cordyceps Query DSL string against the code graph.

    Start with these common, copy-ready queries:
      - Project summary: STATS
      - Find a symbol: SEARCH "create_sale" IN functions
      - List Python files: GLOB "**/*.py"
      - Inspect a node: METADATA FOR "src/api.py:create_sale"
      - Find callers before editing: IMPACT OF "src/services.py:create_sale" DIRECTION callers DEPTH 2
      - Follow a flow: FLOW FOR "src/api.py:create_sale" DEPTH 5
      - Trace a frontend API: STACK FOR "/api/sales"
      - Find a shortest dependency path: PATH FROM "a.py:foo" TO "b.py:bar"
      - Architecture check: CHECK LAYERS "domain" AGAINST "infrastructure"
      - Count functions: GET COUNT(*) FROM functions WHERE file_path CONTAINS "src"

    Call query_dsl_help for the complete command reference. Parse errors include a relevant
    example to help construct the next query.

    Parameters:
      - raw: The query DSL string.
      - expand_body: If True, returns full 'body' contents in results. If False (default), returns a truncated 'body_preview'.

    Available query types:

    GET [projection] [FROM] <type> WHERE <conditions> [ORDER BY field [ASC|DESC]] [LIMIT n] [OFFSET n]
      - type: functions, classes, files, folders, routes, modules, packages (or singular, or ALL/*)
      - modules/packages are computed from file layout (module dirs, __init__.py packages)
      - projection: *, name, lines_count, file_path, etc. or COUNT(*)
      - conditions: field ==/!=/LIKE/CONTAINS/STARTSWITH/ENDSWITH/=~ value with AND, OR, NOT, ()

    SEARCH "<pattern>" [IN <type>] [WHERE <conditions>] [ORDER BY ...]
      - Regex or plain text search across function/class bodies, names, and file paths. Supports /pattern/flags syntax.

    GLOB "<glob_pattern>" [WHERE <conditions>] [ORDER BY ...] [LIMIT n]
      - Path-aware glob matching (**/*.py, src/*/*.ts) across file paths.

    FLOW FOR '<node_id>' [DEPTH n]
      - End-to-end business logic workflow visualization.

    STACK FOR '<api_endpoint>'
      - Full-stack React component/hook to Django backend API tracing.

    CHECK LAYERS '<layer_path>' AGAINST '<forbidden_layer>'
      - Clean Architecture layer violation detection. Finds files in the first
        layer that import from the second (forbidden) layer.

    LAYERS OF '<layer_path>'
      - Shows all external dependencies of a given layer, categorized by
        stdlib / third-party / project-internal layers.

    FIND IMPLEMENTS '<interface_name>'
      - Finds all classes that implement a given interface / base class / protocol.

    FIND DECORATED WITH '<decorator>'
      - Finds all classes and functions decorated with a given decorator.
        Supports '@' prefix (e.g. '@dataclass') or bare name ('dataclass').

    STATS [FOR] "<path>"
      - One-shot project/module summary: file/function/class counts, LOC,
        per-module breakdown, decorator frequency, and test coverage.

    ENFORCE "<rule>" [IN "<scope>"]
      - Architectural gatekeeper. Supports 4 rule types:
        "<layer> MUST_NOT_IMPORT <forbidden>"     — forbid cross-layer imports
        "<a> <- <b> <- <c>"                       — enforce dependency direction chain
        "NO_CIRCULAR_DEPENDENCIES"                — detect circular imports
        "MUST_BE decorated_with '<decorator>'"    — enforce structural patterns (e.g. all domain entities must be @dataclass)

    IMPACT OF '<node_id>' [DIRECTION callers|callees] [DEPTH n] [MODE count|summary|detailed]
      - Blast radius and dependency analysis.
        MODE count: aggregate numbers only (~99% smaller). MODE summary: counts
        grouped by type + module with direct callers/callees (~98% smaller).
        Default detailed: full per-file listing.

    PATH FROM '<start_id>' TO '<end_id>'
      - Shortest dependency call path between two nodes.

    METADATA FOR '<node_id>'
      - Full node metadata and direct callers/callees.
    """
    _drain_sync_queue()
    # pyrefly: ignore [missing-import]
    from src.database import get_graph_db
    db = get_graph_db()
    result = _query_engine(db.client, raw, expand_body=expand_body)
    return to_yaml(result)


if __name__ == "__main__":
    WORKSPACE_PATH = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WORKSPACE_PATH", os.getcwd())
    WORKSPACE_PATH = os.path.abspath(WORKSPACE_PATH)
    os.environ["WORKSPACE_PATH"] = WORKSPACE_PATH
    logger.info(f"Starting CordycepsSearch MCP Server (LOCKED) for workspace: {WORKSPACE_PATH}")

    # pyrefly: ignore [missing-import]
    from src.database import get_graph_db
    db = get_graph_db(WORKSPACE_PATH)
    event_handler = GraphSyncHandler(WORKSPACE_PATH)

    logger.info("Performing initial scan of the workspace...")
    EXCLUDED_DIRS = {
        'node_modules', 'venv', 'env', '.venv', '.env',
        '__pycache__', 'target', 'dist', 'build', 'out',
        'dist-electron', 'electron', 'assets',
        'migrations', 'alembic', '.git', '.idea', '.vscode',
        'coverage', 'htmlcov', '.pytest_cache', '.hypothesis',
        '.next', '.nuxt', 'storybook-static', '.nyc_output', 'esm', 'cjs',
        # Known test fixture directories (NOT actual test dirs — those are indexed)
        'route_detection_tests', 'fixtures', 'test_fixtures', 'test_data',
    }
    extra_exclude = os.environ.get("CORDYCEPS_EXCLUDE", "")
    if extra_exclude:
        EXCLUDED_DIRS = EXCLUDED_DIRS | {d.strip() for d in extra_exclude.split(",") if d.strip()}

    source_files = []
    for root, dirs, files in os.walk(WORKSPACE_PATH):
        dirs[:] = [d for d in dirs if not d.startswith('.')
                   and not GraphSyncHandler.is_excluded_dir(d, os.path.join(root, d), EXCLUDED_DIRS)]
        for file in files:
            if file.endswith(event_handler.supported_extensions):
                p = os.path.join(root, file)
                # Never index our own persistence sidecars — the meta file
                # contains its own manifest and would flag itself as changed
                # on every boot.
                if GraphSyncHandler.is_internal_artifact(p):
                    continue
                source_files.append(p)

    def _rel(p: str) -> str:
        return os.path.relpath(p, WORKSPACE_PATH).replace(os.sep, "/")

    # Warm start: trust the persisted snapshot when the workspace is unchanged.
    snapshot_path = os.path.join(WORKSPACE_PATH, SNAPSHOT_FILENAME)
    meta_data = db.client.load_index_meta()
    warmed = False
    if (os.path.exists(snapshot_path)
            and db.client.snapshot_loaded()
            and meta_data
            and isinstance(meta_data.get("file_manifest"), dict)
            and meta_data["file_manifest"]
            and not db.client.is_index_stale()):
        try:
            current = {}
            for p in source_files:
                st = os.stat(p)
                current[_rel(p)] = [st.st_mtime_ns, st.st_size]
            saved = meta_data["file_manifest"]
            reindex = [r for r, sig in current.items() if saved.get(r) != sig]
            removed = [r for r in saved if r not in current]
            # SAFETY: the Rust engine restores a snapshot for reads, but the
            # first mutation switches to an in-memory session seeded EMPTY
            # (pre-existing nodes are dropped). Until that is fixed engine-side,
            # warm starts are only safe when NOTHING changed; dirty workspaces
            # take the full-scan path below.
            warm_dirty = bool(reindex or removed)
            if warm_dirty:
                logger.info(f"Warm start skipped: {len(reindex)} changed / "
                            f"{len(removed)} removed files need a full rescan")
                warmed = False
            else:
                warmed = True
                logger.info(f"Warm start: {len(current)} unchanged "
                            f"(clean — skipping rebuild)")
            event_handler._has_source_files.cache_clear()
            for rel in removed:
                event_handler.remove_file_from_graph(
                    os.path.join(WORKSPACE_PATH, rel), skip_rebuild=True)
            abs_by_rel = {_rel(p): p for p in source_files}
            for rel in reindex:
                ap = abs_by_rel[rel]
                try:
                    data = event_handler.parser.parse_file(ap)
                    event_handler.update_file_in_graph(ap, skip_rebuild=True,
                                                       pre_parsed_data=data)
                except Exception as e:
                    logger.warning(f"Warm re-index failed for {rel}: {e}")
        except Exception as e:
            logger.warning(f"Warm-start validation failed ({e}); performing full scan")
            warmed = False

    if not warmed and source_files:
        from concurrent.futures import ThreadPoolExecutor
        try:
            num_workers = os.cpu_count() or 4
        except:
            num_workers = 4

        logger.info(f"Parsing {len(source_files)} files using {num_workers} workers...")

        def parse_one(path):
            try:
                return path, event_handler.parser.parse_file(path)
            except Exception as e:
                return path, e

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            results = list(executor.map(parse_one, source_files))

            logger.info("Injecting parsed data into EngramDB...")
            for path, data in results:
                if isinstance(data, Exception):
                    logger.debug(f"Failed to parse {path}: {data}")
                    continue
                try:
                    event_handler.update_file_in_graph(path, skip_rebuild=True, pre_parsed_data=data)
                except Exception as e:
                    logger.debug(f"Failed to index {path}: {e}")

    db.client.clear_generated_edges()
    db.client.repopulate_edges()
    db.client.resolve_import_edges()
    db.client.resolve_django_relations()
    db.client.resolve_url_patterns()
    db.client.resolve_mount_prefixes()
    db.client.resolve_middleware_edges()
    db.client.resolve_api_calls()

    if warmed:
        # Drift insurance: a corrupted/truncated snapshot would restore few
        # nodes while the manifest claims full coverage — verify before serving.
        stats = db.client.get_stats()
        expected = meta_data.get("node_count")
        if (isinstance(expected, int) and expected > 0
                and abs(stats["nodes"] - expected) > max(50, expected * 0.05)):
            logger.warning(f"Snapshot drift ({stats['nodes']} nodes vs "
                           f"{expected} expected); falling back to full scan")
            warmed = False
        else:
            db.client.write_index_meta(node_count=stats["nodes"],
                                       file_manifest=meta_data["file_manifest"])
            logger.info(f"Warm start complete. {stats['nodes']} nodes indexed.")

    if not warmed:
        db.client.clean_stale_files()
        db.client.build()
        file_manifest = {}
        for p in source_files:
            try:
                st = os.stat(p)
                file_manifest[_rel(p)] = [st.st_mtime_ns, st.st_size]
            except OSError:
                continue
        db.client.write_index_meta(file_manifest=file_manifest)
        stats = db.client.get_stats()
    logger.info(f"Initial scan complete. {stats['nodes']} nodes indexed.")

    _event_handler = event_handler

    observer = Observer()
    observer.schedule(event_handler, WORKSPACE_PATH, recursive=True)
    observer.start()
    logger.info("Watchdog Observer started successfully.")

    try:
        mcp.run(transport="stdio")
    finally:
        observer.stop()
        observer.join()
