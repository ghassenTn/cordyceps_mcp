"""
Query Compiler — translates parsed Query AST into EngramClient engine calls.
"""

from __future__ import annotations
import os
import fnmatch
import logging
import re
from typing import Any

from .parser import GetQuery, SearchQuery, GlobQuery, MetadataQuery, ImpactQuery, PathQuery, FlowQuery, StackQuery, AuditQuery, CheckLayersQuery, LayersOfQuery, FindImplementsQuery, FindDecoratedQuery, EnforceQuery, StatsQuery, Condition, UNLIMITED

logger = logging.getLogger(__name__)

# Hard safety cap on rows returned by a single paginated query (GET/SEARCH/GLOB).
# Guards against a careless LIMIT 5000 blowing up the caller's context window.
# Pagination is still available via OFFSET/RANGE for exhaustive scans.
# Explicit `LIMIT ALL` / `LIMIT *` (UNLIMITED) bypasses this cap on purpose.
MAX_QUERY_RESULTS = 1000

# Unified default page size for every paginated query type (GET/SEARCH/GLOB/
# FIND DECORATED/STATS/IMPACT...). Overridden by an explicit LIMIT clause.
DEFAULT_PAGE_SIZE = 100


def _cap_limit(limit: int) -> int:
    return min(limit, MAX_QUERY_RESULTS) if limit else limit


def _resolve_limit(limit: int | None) -> int:
    """Effective page size for a query. UNLIMITED (-1, from `LIMIT ALL` / `LIMIT *`)
    means return everything from the offset — callers slice to the end."""
    if limit == UNLIMITED:
        return UNLIMITED
    return _cap_limit(limit if limit is not None and limit > 0 else DEFAULT_PAGE_SIZE)


def _build_page_meta(
    query_type: str,
    type_name: str,
    offset: int,
    returned_count: int,
    total: int,
    limit: int | None = 20,
    **extra,
) -> dict:
    """Build the unified 'meta' envelope shared by all paginated query types.

    Mirrors the GET/GLOB schema: ok, query_type, type, offset, count, total,
    truncated, plus a pagination hint when results are truncated.
    """
    meta = {
        "ok": True,
        "query_type": query_type,
        "type": type_name,
        "offset": offset,
        "count": returned_count,
        "total": total,
        "truncated": (offset + returned_count) < total,
    }
    if meta["truncated"]:
        next_offset = offset + returned_count
        remaining = max(0, total - next_offset)
        next_limit = min(limit or DEFAULT_PAGE_SIZE, remaining)
        meta["hint"] = (
            f"Use 'OFFSET {next_offset} LIMIT {next_limit}' "
            f"or 'RANGE {next_offset}:{next_offset + next_limit}' to fetch next page."
        )
    if extra:
        meta.update(extra)
    return meta


_TYPE_SINGULAR = {
    "functions": "function",
    "classes": "class",
    "files": "file",
    "folders": "folder",
    "middlewares": "middleware",
    "routes": "route",
    "modules": "module",
    "packages": "package",
    "declarations": "declaration",
}


def _singularize(t: str | None) -> str:
    if not t:
        return "all"
    t = t.lower().strip()
    return _TYPE_SINGULAR.get(t, t)


def _pattern_variants(pattern: str) -> list[str]:
    """Return pattern variants to try — original plus any with workspace
    path prefix stripped (enables queries from monorepo root that match
    paths stored relative to a subdirectory workspace)."""
    variants = [pattern]
    workspace = os.environ.get("WORKSPACE_PATH", "")
    if workspace:
        workspace_norm = workspace.replace("\\", "/").strip("/")
        pattern_norm = pattern.strip("/")
        parts = workspace_norm.split("/")
        for i in range(1, len(parts) + 1):
            suffix = "/".join(parts[-i:])
            if pattern_norm.lower().startswith((suffix + "/").lower()) or pattern_norm.lower() == suffix.lower():
                stripped = pattern_norm[len(suffix):].lstrip("/")
                variants.insert(0, stripped)
                break  # shortest match wins
    return variants


def _glob_match(actual_str: str, pattern: str) -> bool:
    """Path-aware glob matching: '*' matches non-slash chars (single level),
    '**' matches any path segments (recursive), '?' matches single non-slash char.
    Supports brace expansion {a,b}, character classes [ab] and [!ab], and path
    normalization (..). Simple patterns without / (e.g. *.css) match at any depth."""
    import re
    import fnmatch
    actual_norm = actual_str.replace("\\", "/").strip("/")
    pattern_norm = pattern.replace("\\", "/").strip("/")

    # Path normalization: resolve .. and .
    parts = []
    for p in pattern_norm.split("/"):
        if p == ".." and parts:
            parts.pop()
        elif p != ".":
            parts.append(p)
    pattern_norm = "/".join(parts)

    # Bare wildcard patterns (no /) match at any depth: *.css → src/a.css
    if pattern_norm and "/" not in pattern_norm and ("*" in pattern_norm or "?" in pattern_norm):
        pattern_norm = "**/" + pattern_norm

    # Build regex from glob pattern character-by-character
    regex_parts = []
    i = 0
    n = len(pattern_norm)
    while i < n:
        c = pattern_norm[i]
        if c == '!':
            # Extglob negation: !(...) — any single segment NOT matching the pattern.
            # e.g. src/modules/!(comptabilite)/services.py → any one dir except comptabilite.
            if i + 1 < n and pattern_norm[i + 1] == '(':
                close = pattern_norm.find(')', i)
                if close > i:
                    inner = pattern_norm[i + 2:close]
                    neg_inner = inner
                    # Nested brace/negation inside !(...) is not supported — escape literally.
                    if _has_glob_chars(inner) and not (inner.startswith('{') and inner.endswith('}')):
                        regex_parts.append('[^/]*')
                    elif inner.startswith('{') and inner.endswith('}'):
                        alt_body = inner[1:-1]
                        alternatives = [a.strip() for a in alt_body.split(",")]
                        neg_re = '(?:' + '|'.join(re.escape(a) for a in alternatives) + ')'
                        regex_parts.append('(?!(?:' + neg_re + ')(?=/|$))[^/]*')
                    else:
                        regex_parts.append('(?!(?:' + re.escape(inner) + ')(?=/|$))[^/]*')
                    i = close + 1
                    continue
            # Fall through: literal '!'
            regex_parts.append(re.escape(c))
            i += 1
        elif c == '*':
            if i + 1 < n and pattern_norm[i + 1] == '*':
                # '**' — match any path segments (including zero)
                i += 2
                if i < n and pattern_norm[i] == '/':
                    # '**/' — zero or more directories
                    regex_parts.append('(?:.+/)?')
                    i += 1
                else:
                    # trailing '**' — match anything remaining
                    regex_parts.append('.*')
            else:
                # '*' — match within a single path segment (no slashes)
                regex_parts.append('[^/]*')
                i += 1
        elif c == '?':
            regex_parts.append('[^/]')
            i += 1
        elif c == '{':
            # Brace expansion {a,b,c} → (a|b|c)
            close = pattern_norm.find('}', i)
            if close > i:
                inner = pattern_norm[i+1:close]
                alternatives = [alt.strip() for alt in inner.split(",")]
                regex_parts.append('(' + '|'.join(re.escape(a) for a in alternatives) + ')')
                i = close + 1
            else:
                regex_parts.append(re.escape(c))
                i += 1
        elif c == '[':
            # Character class [abc] or [a-z] or [!abc]
            close = pattern_norm.find(']', i)
            if close > i:
                inner = pattern_norm[i+1:close]
                if inner.startswith('!'):
                    regex_parts.append('[^' + inner[1:] + ']')
                elif inner.startswith('^'):
                    regex_parts.append('[^' + inner[1:] + ']')
                else:
                    regex_parts.append('[' + inner + ']')
                i = close + 1
            else:
                regex_parts.append(re.escape(c))
                i += 1
        else:
            regex_parts.append(re.escape(c))
            i += 1

    regex = '^' + ''.join(regex_parts) + '$'
    try:
        return bool(re.match(regex, actual_norm, re.IGNORECASE))
    except Exception:
        return fnmatch.fnmatch(actual_norm, pattern_norm)


def _match_value(actual: Any, op: str, value: Any) -> bool:
    """Evaluate whether an actual value matches a condition value using the given operator."""
    op = op.upper()
    if op == "=":
        op = "=="

    # Boolean comparison support
    if isinstance(value, bool) or isinstance(actual, bool):
        bool_actual = bool(actual)
        bool_val = bool(value) if isinstance(value, bool) else (str(value).lower() in ("true", "1"))
        if op in ("==", "="):
            return bool_actual == bool_val
        elif op == "!=":
            return bool_actual != bool_val

    # Numeric comparison support
    if op in (">", ">=", "<", "<=") or (op in ("==", "!=") and (isinstance(value, (int, float)) or isinstance(actual, (int, float)))):
        try:
            num_actual = float(actual) if actual is not None else 0.0
            num_val = float(value)
            if op == ">":
                return num_actual > num_val
            elif op == ">=":
                return num_actual >= num_val
            elif op == "<":
                return num_actual < num_val
            elif op == "<=":
                return num_actual <= num_val
            elif op == "==":
                return num_actual == num_val
            elif op == "!=":
                return num_actual != num_val
        except (ValueError, TypeError):
            pass

    if actual is None:
        actual = ""
    actual_str = str(actual)
    val_str = str(value)

    if op == "==":
        return actual_str == val_str
    elif op == "!=":
        return actual_str != val_str
    elif op == "LIKE":
        import re as _re
        like_re = ''.join(
            '.*' if c in ('%', '*') else
            '.' if c in ('_', '?') else
            '\\' + c if c in r'.^$+{}[]\\|()'
            else c
            for c in val_str
        )
        return bool(_re.match(f"^{like_re}$", actual_str, _re.IGNORECASE))
    elif op == "CONTAINS":
        return val_str.lower() in actual_str.lower()
    elif op == "STARTSWITH":
        return actual_str.lower().startswith(val_str.lower())
    elif op == "ENDSWITH":
        return actual_str.lower().endswith(val_str.lower())
    elif op in ("=~", "MATCHES"):
        import re
        try:
            return bool(re.search(val_str, actual_str, re.IGNORECASE))
        except Exception:
            return False
    elif op in ("!~", "!=~", "NOT MATCHES"):
        import re
        try:
            return not bool(re.search(val_str, actual_str, re.IGNORECASE))
        except Exception:
            return True
    elif op == "NOT LIKE":
        import re as _re
        like_re = ''.join(
            '.*' if c in ('%', '*') else
            '.' if c in ('_', '?') else
            '\\' + c if c in r'.^$+{}[]\\|()'
            else c
            for c in val_str
        )
        return not bool(_re.match(f"^{like_re}$", actual_str, _re.IGNORECASE))
    elif op == "NOT CONTAINS":
        return val_str.lower() not in actual_str.lower()
    elif op == "NOT STARTSWITH":
        return not actual_str.lower().startswith(val_str.lower())
    elif op == "NOT ENDSWITH":
        return not actual_str.lower().endswith(val_str.lower())
    elif op == "NOT IN":
        if isinstance(value, list):
            return actual_str.lower() not in [str(v).lower() for v in value]
        return actual_str.lower() != val_str.lower()
    return False


def _match_condition(meta: dict, cond: Condition) -> bool:
    """Checks whether a node's metadata dictionary satisfies a single condition."""
    field = cond.field.lower()

    # Constant literal predicate (WHERE 1==1 / WHERE true) — no metadata lookup
    if field == "__constant__":
        return bool(cond.value)

    if field.startswith(("relation.", "relation_", "related_")):
        import json
        relations = []
        if "django_relations_json" in meta:
            try:
                relations = json.loads(meta["django_relations_json"])
            except Exception:
                pass
        for rel in relations:
            if field in ("relation.type", "relation_type"):
                actual = rel.get("relation_type", "")
            elif field in ("relation.target", "relation_target", "related_model"):
                actual = rel.get("related_model", "")
            else:
                actual = rel.get(cond.field, "")
            if _match_value(actual, cond.operator, cond.value):
                return True
        return False

    # Map field names to metadata keys & fallback computation
    if field in ("calls_count", "calls"):
        if "calls_count" in meta and meta["calls_count"] is not None:
            actual = meta["calls_count"]
        else:
            calls = meta.get("calls") or []
            actual = len(calls)
    elif field in ("callers_count", "callers"):
        if "callers_count" in meta and meta["callers_count"] is not None:
            actual = meta["callers_count"]
        else:
            callers = meta.get("callers") or []
            actual = len(callers)
    elif field in ("blast_radius_score", "blast_radius", "transitive_callers", "impact_score", "blast_score"):
        if "blast_radius_score" in meta and meta["blast_radius_score"] is not None:
            actual = meta["blast_radius_score"]
        else:
            actual = 0
    elif field == "depth":
        fpath = meta.get("file_path", "")
        actual = fpath.count("/")
    elif field in ("lines_count", "lines", "line_count"):
        if "lines_count" in meta and meta["lines_count"] is not None:
            actual = meta["lines_count"]
        elif "lines" in meta and isinstance(meta["lines"], dict):
            start = meta["lines"].get("start", 0)
            end = meta["lines"].get("end", 0)
            actual = end - start + 1 if end >= start else 0
        else:
            actual = 0
    elif field in ("is_async", "async"):
        actual = meta.get("is_async", False)
        if actual is None:
            actual = False
    elif field in ("is_generator", "generator"):
        actual = meta.get("is_generator", False)
        if actual is None:
            actual = False
    elif field in ("param_count", "params_count", "params", "args_count", "args", "param_cnt"):
        actual = meta.get("param_count", 0)
        if actual is None:
            actual = 0
    elif field in ("is_exported", "is_public", "exported", "public"):
        actual = meta.get("is_exported", True)
        if actual is None:
            actual = True
    else:
        field_key = {
            "name": "name",
            "type": "type",
            "file_path": "file_path",
            "file": "file_path",
            "path": "file_path",
            "signature": "signature",
            "docstring": "docstring",
        }.get(field, field)
        actual = meta.get(field_key, "")

    # For file_path fields, try pattern variants with workspace prefix stripped
    if field in ("file_path", "file") and isinstance(cond.value, str):
        variants = _pattern_variants(cond.value)
        for v in variants:
            if _match_value(actual, cond.operator, v):
                return True
        return False

    return _match_value(actual, cond.operator, cond.value)


def resolve_node_id(client: Any, query_id: str) -> str | None:
    """Resolves a node ID query string to an exact node ID, supporting shorthands."""
    if client.contains(query_id):
        return query_id

    # Try case-insensitive exact match
    all_meta = client.get_all_metadata()
    for nid in all_meta:
        if nid.lower() == query_id.lower():
            return nid

    # Try matching end of node_id (e.g. "api.py:create_sale" matches "src/modules/sales/api.py:create_sale")
    suffix = "/" + query_id.replace("\\", "/").lstrip("/")
    for nid in all_meta:
        normalized_nid = nid.replace("\\", "/")
        if normalized_nid.endswith(query_id.replace("\\", "/")) or (":" in query_id and normalized_nid.endswith(suffix)):
            return nid

    # Try searching by name (if no colon)
    if ":" not in query_id and "/" not in query_id:
        matches = []
        for nid, meta in all_meta.items():
            meta_name = meta.get("name", "")
            if meta_name.lower() == query_id.lower():
                matches.append(nid)
        if len(matches) == 1:
            return matches[0]

    return None


def find_did_you_mean_suggestion(client: Any, query_id: str) -> str:
    """Find a close candidate match for a misspelled node_id."""
    import difflib
    all_meta = client.get_all_metadata()
    all_keys = list(all_meta.keys())
    matches = difflib.get_close_matches(query_id, all_keys, n=1, cutoff=0.5)
    if matches:
        return f". Did you mean '{matches[0]}'?"
    return ""


def _expand_to_functions(client: Any, node_id: str, all_meta: dict) -> list[str]:
    """Expand a class or file node ID to its function/method children.

    For a Function node, returns [node_id].
    For a Class node, returns its methods (children with '.' separator).
    For a File node, returns functions defined in that file.
    """
    meta = all_meta.get(node_id, {})
    ntype = meta.get("type", "")

    if ntype == "Function":
        return [node_id]

    results = []
    prefix = node_id + "."
    for nid, m in all_meta.items():
        if nid.startswith(prefix) and m.get("type") == "Function":
            results.append(nid)

    # File nodes also have direct colon-prefixed functions
    if ntype == "File":
        file_prefix = node_id + ":"
        for nid, m in all_meta.items():
            if nid.startswith(file_prefix) and m.get("type") == "Function":
                results.append(nid)

    return results


def find_shortest_path(client: Any, start: str, end: str) -> list[str] | None:
    """Finds the shortest dependency path between two nodes using
    multi-source Bidirectional BFS. Class/file nodes are expanded to their
    function/method children for graph traversal."""
    if start == end:
        return [start]

    if not client.contains(start) or not client.contains(end):
        return None

    all_meta = client.get_all_metadata()

    start_fns = _expand_to_functions(client, start, all_meta)
    end_fns = _expand_to_functions(client, end, all_meta)

    if not start_fns or not end_fns:
        return None

    # Multi-source bidirectional BFS
    forward_queue = list(start_fns)
    backward_queue = list(end_fns)

    forward_parent = {s: None for s in start_fns}
    backward_child = {e: None for e in end_fns}

    forward_index = 0
    backward_index = 0
    get_dependencies = getattr(client, "get_dependencies", client.get_callees)
    get_dependents = getattr(client, "get_dependents", client.get_callers)

    while forward_index < len(forward_queue) and backward_index < len(backward_queue):
        # Step forward
        curr_f = forward_queue[forward_index]
        forward_index += 1

        if curr_f in backward_child:
            return _reconstruct_path(forward_parent, backward_child, curr_f)

        callees = get_dependencies(curr_f)
        for nxt in callees:
            if nxt not in forward_parent:
                forward_parent[nxt] = curr_f
                forward_queue.append(nxt)

        # Step backward
        curr_b = backward_queue[backward_index]
        backward_index += 1

        if curr_b in forward_parent:
            return _reconstruct_path(forward_parent, backward_child, curr_b)

        callers = get_dependents(curr_b)
        for prev in callers:
            if prev not in backward_child:
                backward_child[prev] = curr_b
                backward_queue.append(prev)

    return None


def _reconstruct_path(forward_parent: dict, backward_child: dict, intersection: str) -> list[str]:
    path = []
    # Trace back to start
    curr = intersection
    while curr is not None:
        path.append(curr)
        curr = forward_parent[curr]
    path.reverse()

    # Trace forward to end
    curr = backward_child[intersection]
    while curr is not None:
        path.append(curr)
        curr = backward_child[curr]

    return path


def _evaluate_bool_expr(expr: Any, meta_dict: dict) -> bool:
    from src.query.parser import Condition, AndExpr, OrExpr, NotExpr
    if isinstance(expr, Condition):
        return _match_condition(meta_dict, expr)
    elif isinstance(expr, NotExpr):
        return not _evaluate_bool_expr(expr.expr, meta_dict)
    elif isinstance(expr, AndExpr):
        return _evaluate_bool_expr(expr.left, meta_dict) and _evaluate_bool_expr(expr.right, meta_dict)
    elif isinstance(expr, OrExpr):
        return _evaluate_bool_expr(expr.left, meta_dict) or _evaluate_bool_expr(expr.right, meta_dict)
    return True


def _module_root(file_path: str) -> str | None:
    """Map a file path to its module root directory.

    A module is a business container: the directory immediately under a
    'modules' segment (src/modules/sales → src/modules/sales), else the
    top-level directory that owns the file (engram_core/... → engram_core).
    Files at the workspace root (no directory) belong to no module.
    """
    parts = file_path.replace("\\", "/").split("/")
    dir_parts = parts[:-1]
    if not dir_parts:
        return None
    if "modules" in dir_parts:
        idx = dir_parts.index("modules")
        root = dir_parts[: idx + 2]
    else:
        root = dir_parts[:1]
    return "/".join(root) or None


def _collect_modules(all_meta: dict) -> dict[str, dict]:
    """Synthesize Module nodes with per-module aggregate stats.

    Aggregates every File/Function/Class whose file_path lives under the
    module root: file count, function/class counts, and total LOC. Only
    indexed source content defines a module — bare Folder nodes (dirs with
    no indexed files) are excluded.
    """
    modules: dict[str, dict] = {}
    for node_id, meta in all_meta.items():
        ntype = meta.get("type", "")
        if ntype not in ("File", "Function", "Class"):
            continue
        fp = str(meta.get("file_path", "") or "")
        if not fp:
            continue
        root = _module_root(fp)
        if not root:
            continue
        mod = modules.setdefault(root, {
            "node_id": root,
            "name": root.split("/")[-1],
            "type": "Module",
            "file_path": root,
            "files": 0,
            "functions": 0,
            "classes": 0,
            "lines_count": 0,
        })
        if ntype == "File":
            mod["files"] += 1
            mod["lines_count"] += int(meta.get("lines_count", 0) or 0)
        elif ntype == "Function":
            mod["functions"] += 1
        elif ntype == "Class":
            mod["classes"] += 1
    return modules


def _collect_packages(all_meta: dict) -> dict[str, dict]:
    """Synthesize Package nodes from directories that contain __init__.py."""
    package_dirs: set[str] = set()
    for node_id, meta in all_meta.items():
        fp = str(meta.get("file_path", "") or "")
        if meta.get("type") == "File" and os.path.basename(fp) == "__init__.py":
            pkg_dir = os.path.dirname(fp).replace("\\", "/")
            if pkg_dir:
                package_dirs.add(pkg_dir)

    packages: dict[str, dict] = {}
    for pkg in sorted(package_dirs):
        packages[pkg] = {
            "node_id": pkg,
            "name": pkg.split("/")[-1],
            "type": "Package",
            "file_path": pkg,
            "files": 0,
            "functions": 0,
            "classes": 0,
            "lines_count": 0,
        }

    for node_id, meta in all_meta.items():
        fp = str(meta.get("file_path", "") or "")
        if not fp:
            continue
        for pkg, agg in packages.items():
            if fp == pkg or fp.startswith(pkg + "/"):
                ntype = meta.get("type", "")
                if ntype == "File":
                    agg["files"] += 1
                    agg["lines_count"] += int(meta.get("lines_count", 0) or 0)
                elif ntype == "Function":
                    agg["functions"] += 1
                elif ntype == "Class":
                    agg["classes"] += 1
    return packages


def _collect_virtual_nodes(all_meta: dict, vtype: str) -> dict[str, dict]:
    """Synthesize virtual node types (module/package) that have no direct
    representation in the graph metadata, so GET/SEARCH can target them."""
    if vtype == "module":
        return _collect_modules(all_meta)
    if vtype == "package":
        return _collect_packages(all_meta)
    return {}


def _collect_results(
    client: Any,
    query: GetQuery,
) -> tuple[list[dict], list[dict], int, int, int, int, bool]:
    """Collect and filter nodes matching a GetQuery.

    Returns:
        (sliced_results, all_results, total_matched, offset, returned_count, remaining_count, truncated)
        all_results is the full filtered list before slicing (used by DISTINCT).
    """
    all_meta: dict[str, dict] = client.get_all_metadata()

    # Virtual types (module/package) are computed from raw metadata on demand.
    if query.type_filter in ("module", "package"):
        all_meta = _collect_virtual_nodes(all_meta, query.type_filter)

    # Separate relation conditions from standard conditions
    relation_conds = []
    standard_conds = []
    for c in query.conditions:
        if c.field.startswith(("relation.", "relation_", "related_")):
            relation_conds.append(c)
        else:
            standard_conds.append(c)

    results = []
    for node_id, meta in all_meta.items():
        meta_dict = dict(meta.items()) if hasattr(meta, "items") else meta
        meta_dict["node_id"] = node_id

        # Filter by type
        if query.type_filter and meta_dict.get("type", "").lower() != query.type_filter.lower():
            continue

        # Filter by boolean expression tree
        if query.where_expr:
            if not _evaluate_bool_expr(query.where_expr, meta_dict):
                continue
        elif standard_conds:
            if not all(_match_condition(meta_dict, c) for c in standard_conds):
                continue

        # Filter by relation conditions
        if relation_conds:
            import json
            relations = []
            if "django_relations_json" in meta_dict:
                try:
                    relations = json.loads(meta_dict["django_relations_json"])
                except Exception:
                    pass

            matched_any_relation = False
            for rel in relations:
                rel_match = True
                for rc in relation_conds:
                    field_name = rc.field.lower()
                    if field_name in ("relation.type", "relation_type"):
                        actual = rel.get("relation_type", "")
                    elif field_name in ("relation.target", "relation_target", "related_model"):
                        actual = rel.get("related_model", "")
                    else:
                        actual = rel.get(rc.field, "")

                    if not _match_value(actual, rc.operator, rc.value):
                        rel_match = False
                        break
                if rel_match:
                    matched_any_relation = True
                    break

            if not matched_any_relation:
                continue

        results.append(meta_dict)

    total_matched = len(results)

    # Sort by order_by field or fallback to (type, name)
    if query.order_by:
        field = query.order_by.lower()
        reverse = (query.order_dir == "desc")

        def _sort_key(node: dict):
            val = None
            if field in ("callers_count", "callers"):
                val = node.get("callers_count", len(node.get("callers", [])) if node.get("callers") else 0)
            elif field in ("blast_radius_score", "blast_radius", "transitive_callers", "impact_score"):
                val = node.get("blast_radius_score", 0)
            elif field in ("lines_count", "lines"):
                val = node.get("lines_count", 0)
            elif field in ("param_count", "params"):
                val = node.get("param_count", 0)
            elif field == "depth":
                val = node.get("file_path", "").count("/")
            else:
                val = node.get(field) or node.get(field.lower())
            if val is None:
                return (1, 0)
            if isinstance(val, (int, float)):
                return (0, val)
            return (0, str(val).lower())

        results.sort(key=_sort_key, reverse=reverse)
    else:
        results.sort(key=lambda r: (r.get("type", ""), r.get("name", "")))

    offset = query.offset if query.offset is not None and query.offset >= 0 else 0
    limit = _resolve_limit(query.limit)

    if limit == UNLIMITED:
        sliced = results[offset:] if offset < total_matched else []
    else:
        sliced = results[offset : offset + limit] if offset < total_matched else []
    returned_count = len(sliced)
    remaining_count = max(0, total_matched - (offset + returned_count))
    truncated = (offset + returned_count) < total_matched

    return sliced, results, total_matched, offset, returned_count, remaining_count, truncated


def _enrich_with_graph(client: Any, results: list[dict], graph_op: str, depth: int) -> list[dict]:
    """Enrich result nodes with callers/callees from the graph."""
    enriched = []
    for node in results:
        node_id = node["node_id"]
        if graph_op == "callers":
            if depth > 0:
                related = client.blast_radius(node_id, depth)
            else:
                related = client.get_callers(node_id)
        elif graph_op == "callees":
            if depth > 0:
                related = client.get_recursive_callees(node_id, depth)
            else:
                related = client.get_callees(node_id)
        else:
            related = []

        related_meta = []
        seen = set()
        for rid in related:
            if rid == node_id or rid in seen:
                continue
            seen.add(rid)
            rm = client.get_node_meta(rid)
            if rm:
                rm_dict = dict(rm.items()) if hasattr(rm, "items") else rm
                rm_dict["node_id"] = rid
                related_meta.append(rm_dict)
            else:
                related_meta.append({"node_id": rid, "type": "Unknown"})

        node["related_nodes"] = related_meta
        enriched.append(node)
    return enriched


_VALID_GROUP_BYS = {"file_path", "type", "folder", "module"}


def _validate_group_by(group_by: str, all_results: list = None) -> str:
    gb = group_by.lower()
    if gb in _VALID_GROUP_BYS:
        return gb
    # Allow grouping by any field that actually exists in the result metadata
    # (e.g. name, decorators, framework) — not just the well-known dimensions.
    if all_results:
        for item in all_results:
            if gb in item:
                return gb
    valid = ", ".join(sorted(_VALID_GROUP_BYS))
    raise ValueError(
        f"Invalid GROUP BY field '{group_by}'. "
        f"Supported fields: {valid}, or any metadata field present in the results"
    )


def _get_group_key(item: dict, group_by: str) -> str:
    """Determine the grouping key for a result item."""
    if group_by == "file_path":
        return item.get("file_path", "")
    elif group_by == "type":
        return (item.get("type") or "unknown").lower()
    elif group_by in ("folder", "module"):
        fp = item.get("file_path", "")
        parts = fp.replace("\\", "/").strip("/").split("/")
        return parts[0] if parts else ""
    val = item.get(group_by)
    if isinstance(val, list):
        return ", ".join(sorted(str(v) for v in val)) if val else "unknown"
    return str(val) if val is not None else "unknown"


def _group_results(results: list[dict], group_by: str) -> dict:
    """Group results by the specified dimension and format compactly."""
    grouped: dict[str, list] = {}
    for item in results:
        key = _get_group_key(item, group_by)
        if item.get("type") == "File":
            lines = item.get("lines", {})
            if isinstance(lines, dict) and "start" in lines:
                grouped.setdefault(key, []).append(f"{lines['start']}-{lines['end']}")
            else:
                grouped.setdefault(key, []).append(str(item.get("lines_count", 0) or 0))
        else:
            type_ = item.get("type", "")
            # Render symbols by their qualified id suffix (e.g. outer.inner,
            # Class.method.helper) so nested definitions sharing a bare name with
            # a sibling/top-level symbol stay distinguishable. Only trust the id
            # suffix when it is name-derived (last segment == bare name).
            symbol_name = item.get("name", "")
            nid = item.get("node_id") or ""
            if (
                nid
                and ":" in nid
                and item.get("type") in ("Function", "Class", "Declaration")
                and nid.rsplit(":", 1)[1].rsplit(".", 1)[-1] == symbol_name
            ):
                symbol_name = nid.rsplit(":", 1)[1]
            display_name = item.get("full_url") or symbol_name
            if type_ == "Middleware":
                line = item.get("line", 0) or 0
                grouped.setdefault(key, []).append(f"{display_name}: {line}" if line else display_name)
            elif type_ == "Route":
                lines = item.get("lines", {})
                if isinstance(lines, dict) and "start" in lines:
                    grouped.setdefault(key, []).append(f"{display_name}: {lines['start']}-{lines['end']}")
                else:
                    grouped.setdefault(key, []).append(display_name)
            else:
                lines = item.get("lines", {})
                if isinstance(lines, dict) and "start" in lines:
                    start = lines.get("start", 0)
                    end = lines.get("end", 0)
                    grouped.setdefault(key, []).append(f"{display_name}: {start}-{end}")
                else:
                    lines_count = item.get("lines_count", 0) or 0
                    grouped.setdefault(key, []).append(f"{display_name}: {lines_count}")
    if grouped:
        grouped = dict(sorted(grouped.items()))
        for key, entries in grouped.items():
            if len(entries) == 1 and isinstance(entries[0], str) and ":" not in entries[0]:
                grouped[key] = entries[0]
    return grouped


_AGG_FUNCS = {"SUM", "COUNT", "AVG", "MIN", "MAX"}


def _parse_agg_spec(proj_item: str) -> tuple[str, str] | None:
    """Parse a projection item like 'SUM(lines_count)' into ('SUM', 'lines_count').
    Returns None if the item is a plain field name (not an aggregation)."""
    proj = proj_item.strip()
    if "(" not in proj or not proj.endswith(")"):
        return None
    func = proj.split("(")[0].upper()
    if func not in _AGG_FUNCS:
        return None
    arg = proj[proj.index("(") + 1 : -1].strip()
    return (func, arg)


def _is_aggregation_query(projection: list[str] | None) -> bool:
    """Check if the projection contains any aggregation function."""
    if not projection:
        return False
    return any(_parse_agg_spec(p) is not None for p in projection)


def _get_numeric(item: dict, field: str) -> int | float | None:
    """Extract a numeric value from an item's metadata for aggregation."""
    f_lower = field.lower()
    if f_lower in ("lines_count", "lines"):
        val = item.get("lines_count")
        if val is not None:
            return float(val)
        lo = item.get("lines", {})
        if isinstance(lo, dict):
            s, e = lo.get("start", 0), lo.get("end", 0)
            if e >= s:
                return float(e - s + 1)
        return 0.0
    elif f_lower in ("callers_count", "callers"):
        val = item.get("callers_count")
        if val is not None:
            return float(val)
        c = item.get("callers") or []
        return float(len(c))
    elif f_lower in ("blast_radius_score", "blast_radius"):
        return float(item.get("blast_radius_score", 0) or 0)
    elif f_lower in ("param_count", "params"):
        return float(item.get("param_count", 0) or 0)
    elif f_lower in ("name", "type", "file_path"):
        return None  # non-numeric
    val = item.get(f_lower, item.get(field, None))
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _compute_aggregations(items: list[dict], agg_specs: list[tuple[str, str]]) -> dict:
    """Compute aggregation values from a list of items.
    agg_specs: list of (func, field) tuples, e.g. [("SUM", "lines_count"), ("COUNT", "*")]
    """
    result: dict[str, int | float] = {}
    for func, field in agg_specs:
        key = f"{func}({field})"
        if func == "COUNT" and field == "*":
            result[key] = len(items)
        elif func == "COUNT":
            count = 0
            for item in items:
                v = _get_numeric(item, field)
                if v is not None:
                    count += 1
            result[key] = count
        else:
            values = []
            for item in items:
                v = _get_numeric(item, field)
                if v is not None:
                    values.append(v)
            if not values:
                result[key] = 0
            elif func == "SUM":
                result[key] = sum(values)
            elif func == "AVG":
                result[key] = sum(values) / len(values)
            elif func == "MIN":
                result[key] = min(values)
            elif func == "MAX":
                result[key] = max(values)
    return result


def compile_get(client: Any, query: GetQuery) -> dict:
    """Compile a GetQuery into engine calls and return structured results."""
    results, all_results, total_matched, offset, returned_count, remaining_count, truncated = _collect_results(client, query)
    if query.graph_op:
        results = _enrich_with_graph(client, results, query.graph_op, query.depth)

    # Handle COUNT(*) backward compat (no GROUP BY → old flat format)
    is_simple_count = (
        query.projection
        and len(query.projection) == 1
        and query.projection[0].lower() in ("count(*)", "count", "count*")
        and not query.group_by_explicit
    )
    if is_simple_count:
        return {
            "meta": {
                "ok": True,
                "query_type": "GET",
                "type": _singularize(query.type_filter),
                "count": total_matched,
                "total": total_matched,
            },
        }

    # Detect aggregation queries (possibly mixed with plain fields)
    if _is_aggregation_query(query.projection):
        agg_specs = []
        plain_fields = []
        for p in (query.projection or []):
            spec = _parse_agg_spec(p)
            if spec:
                agg_specs.append(spec)
            else:
                plain_fields.append(p)
        gb = _validate_group_by(query.group_by, all_results)

        def _first_of(items: list[dict], field: str) -> Any:
            for item in items:
                v = item.get(field) or item.get(field.lower())
                if v is not None:
                    return v
            return None

        def _compute_with_plain(group_items: list[dict], aggs: list, plains: list) -> dict:
            result = _compute_aggregations(group_items, aggs)
            for f in plains:
                result[f] = _first_of(group_items, f)
            return result

        if query.group_by_explicit:
            # GROUP BY aggregation — aggregate per group
            groups: dict[str, list[dict]] = {}
            for item in all_results:
                key = _get_group_key(item, gb)
                groups.setdefault(key, []).append(item)
            grouped_results: dict[str, dict] = {}
            for key in sorted(groups):
                grouped_results[key] = _compute_with_plain(groups[key], agg_specs, plain_fields)
            return {
                "meta": {
                    "ok": True,
                    "query_type": "GET",
                    "type": _singularize(query.type_filter),
                    "aggregation": True,
                    "group_by": gb,
                    "total": total_matched,
                },
                "results": grouped_results,
            }
        else:
            # No GROUP BY — single aggregation over all results
            aggs = _compute_with_plain(all_results, agg_specs, plain_fields)
            return {
                "meta": {
                    "ok": True,
                    "query_type": "GET",
                    "type": _singularize(query.type_filter),
                    "aggregation": True,
                    "total": total_matched,
                },
                "results": aggs,
            }

    # Handle field selection projection (e.g. GET name, lines_count)
    # Returns EXACTLY the requested fields — no auto-added node_id/file_path —
    # to keep output lean and avoid duplicating group-identifying metadata.
    if query.projection:
        proj_fields = set(query.projection)
        projected = []
        for item in results:
            p_item = {}
            for f in proj_fields:
                f_lower = f.lower()
                if f_lower in item:
                    p_item[f] = item[f_lower]
                elif f_lower == "file":
                    p_item[f] = item.get("file_path", "")
                elif f_lower in ("lines_count", "lines"):
                    p_item[f] = item.get("lines_count", 0)
                elif f_lower in ("callers_count", "callers"):
                    p_item[f] = item.get("callers_count", 0)
                elif f_lower in ("blast_radius_score", "blast_radius"):
                    p_item[f] = item.get("blast_radius_score", 0)
                elif f_lower in ("param_count", "params"):
                    p_item[f] = item.get("param_count", 0)
                elif f in item:
                    p_item[f] = item[f]
            projected.append(p_item)
        results = projected

    # Handle DISTINCT (unique values from list fields like frameworks, imports, decorators)
    if query.distinct:
        proj = query.projection or ["*"]
        distinct_values: dict[str, set] = {}
        for item in all_results:
            for field in proj:
                f_lower = field.lower()
                raw = item.get(f_lower) if f_lower in item else item.get(field, None)
                if raw is None:
                    continue
                if isinstance(raw, list):
                    vals = raw
                elif isinstance(raw, str):
                    import json as _json
                    # Try to parse JSON-encoded arrays (e.g. '["vue","react"]')
                    try:
                        parsed = _json.loads(raw)
                        if isinstance(parsed, list):
                            vals = [str(v) for v in parsed]
                        else:
                            vals = [raw]
                    except Exception:
                        vals = [raw]
                else:
                    vals = [str(raw)]
                for v in vals:
                    if v and v.strip():
                        distinct_values.setdefault(field, set()).add(v.strip())

        values_out = {}
        for field in proj:
            vals = sorted(distinct_values.get(field, set()))
            values_out[field] = vals

        return {
            "meta": {
                "ok": True,
                "query_type": "GET",
                "type": _singularize(query.type_filter),
                "distinct": True,
                "projection": proj,
                "values": values_out,
                "values_count": {f: len(values_out[f]) for f in proj},
            },
        }

    if query.graph_op:
        return {
            "meta": {
                "ok": True,
                "query_type": "GET",
                "type": _singularize(query.type_filter),
                "offset": offset,
                "count": returned_count,
                "total": total_matched,
                "truncated": truncated,
            },
            "results": results,
        }

    gb = _validate_group_by(query.group_by, all_results)

    meta = {
        "ok": True,
        "query_type": "GET",
        "type": _singularize(query.type_filter),
        "offset": offset,
        "count": returned_count,
        "total": total_matched,
        "truncated": truncated,
    }
    if truncated:
        next_offset = offset + returned_count
        remaining = total_matched - next_offset
        next_limit = min(_cap_limit(query.limit or DEFAULT_PAGE_SIZE), remaining)
        next_range_end = next_offset + next_limit
        meta["hint"] = (
            f"Use 'OFFSET {next_offset} LIMIT {next_limit}' "
            f"or 'RANGE {next_offset}:{next_range_end}' to fetch next page."
        )

    # File-like types (file, folder, route, middleware, module, package) have a
    # unique group key per node (group key == file_path == node_id), so a
    # {path: [entry]} wrapper would just duplicate data. Return a flat list
    # unless GROUP BY was explicit.
    flat_types = {"file", "folder", "route", "middleware", "module", "package"}
    if not query.group_by_explicit and query.type_filter in flat_types:
        return {"meta": meta, "results": results}

    if query.projection:
        grouped: dict[str, list] = {}
        for item in results:
            key = _get_group_key(item, gb)
            entry = {k: v for k, v in item.items() if k not in ("node_id", "file_path")}
            grouped.setdefault(key, []).append(entry)
        if grouped:
            grouped = dict(sorted(grouped.items()))
    else:
        # No explicit projection → compact grouped format:
        # {file_path: ["Name: start-end", ...]} — one line per symbol for token
        # safety. Full detail stays available via projections or METADATA FOR.
        grouped = _group_results(results, gb)

    return {"meta": meta, "results": grouped}


def _looks_like_filename(pat: str) -> bool:
    """Heuristic: pattern ends with a dotted extension or contains path separators."""
    dot = pat.rfind('.')
    if dot > 0 and dot > len(pat) - 8 and '/' not in pat[dot:]:
        return True
    return '/' in pat or '\\' in pat


def _looks_like_path_subject(subject: str) -> bool:
    """True if a rule subject is a file path / layer path rather than a bare word."""
    if not subject:
        return False
    if "." in subject and _looks_like_filename(subject):
        return True
    return "/" in subject or "\\" in subject


def words_mention_type(words: list[str]) -> bool:
    """True if the rule text mentions a node type (classes/functions/class/function)."""
    return any(w.lower().strip("\"'") in ("classes", "class", "functions", "function") for w in words)


def compile_search(client: Any, query: SearchQuery) -> dict:
    """Compile a SearchQuery into engine text/regex search calls."""
    import re

    # When the user searches a bare filename without an explicit IN <type>, assume
    # they want the File node, not every function/class that happens to live in that
    # file. An explicit 'IN *' / 'IN ALL' opts into searching ALL types, so it must
    # NOT be narrowed by the filename heuristic.
    type_filter = query.target_type
    if type_filter in ("all", "*") and query.scope is None:
        for pat in query.patterns:
            if _looks_like_filename(pat):
                type_filter = "file"
                break

    get_q = GetQuery(
        type_filter=None if type_filter in ("all", "*") else type_filter,
        where_expr=query.where_expr,
        conditions=query.conditions,
        limit=query.limit,
        offset=query.offset,
        order_by=query.order_by,
        order_dir=query.order_dir,
    )

    all_meta = client.get_all_metadata()
    patterns = query.patterns
    is_regex = getattr(query, "is_regex", False)
    flags_str = getattr(query, "flags", "")

    # Virtual types (module/package) have no raw metadata — synthesize them so
    # SEARCH "sales" IN modules finds the module by name/path.
    if get_q.type_filter in ("module", "package"):
        all_meta = _collect_virtual_nodes(all_meta, get_q.type_filter)

    if is_regex:
        re_flags = 0
        if "i" in flags_str.lower():
            re_flags |= re.IGNORECASE
        if "m" in flags_str.lower():
            re_flags |= re.MULTILINE
        if "s" in flags_str.lower():
            re_flags |= re.DOTALL
        compiled = []
        for pat in patterns:
            try:
                compiled.append(re.compile(pat, re_flags))
            except Exception as e:
                return {"ok": False, "error": f"Invalid regex pattern /{pat}/: {e}"}

        def match_fn(text: str) -> bool:
            for r in compiled:
                if r.search(text):
                    return True
            return False

        def rank_fn(text: str, name: str) -> int:
            """Regex mode: score by which field matched. 100=name, 10=body/path."""
            score = 0
            for r in compiled:
                if r.search(name):
                    score = max(score, 100)
                elif r.search(text):
                    score = max(score, 10)
            return score
    else:
        pattern_lower_list = [p.lower() for p in patterns]
        def match_fn(text: str) -> bool:
            text_lower = text.lower()
            for pat in pattern_lower_list:
                if pat in text_lower:
                    return True
            return False

        def rank_fn(text: str, name: str) -> int:
            """Substring mode: 120 exact-name, 100 name-prefix, 80 name-contains,
            10 body/path — so precise hits float up and bulk body matches sink below
            them (bodies are still searched by default)."""
            t = text.lower()
            score = 0
            for pat in pattern_lower_list:
                n = name.lower()
                if n == pat:
                    score = max(score, 120)
                elif n.startswith(pat):
                    score = max(score, 100)
                elif pat in n:
                    score = max(score, 80)
                elif pat in t:
                    score = max(score, 10)
            return score

    matched = []
    for node_id, meta in all_meta.items():
        meta_dict = dict(meta.items()) if hasattr(meta, "items") else meta
        meta_dict["node_id"] = node_id

        # Filter by node_type
        if get_q.type_filter and meta_dict.get("type", "").lower() != get_q.type_filter.lower():
            continue

        # Evaluate where expression tree if present
        if get_q.where_expr:
            if not _evaluate_bool_expr(get_q.where_expr, meta_dict):
                continue

        # Search over text content (body, name, docstring, signature, file_path)
        # Route nodes carry view_name/url/methods in top-level fields — include them
        # so `SEARCH "add_item" IN routes` finds the handler without body text.
        if query.bodies_only:
            searchable_text = str(meta_dict.get("body", ""))
            searchable_name = str(meta_dict.get("name", ""))
        else:
            route_fields = [
                str(meta_dict.get("view_name", "")),
                str(meta_dict.get("url", "")),
                str(meta_dict.get("methods", "")),
                str(meta_dict.get("framework", "")),
            ]
            searchable_text = " ".join([
                str(meta_dict.get("body", "")),
                str(meta_dict.get("name", "")),
                str(meta_dict.get("docstring", "")),
                str(meta_dict.get("signature", "")),
                str(meta_dict.get("file_path", "")),
                *route_fields,
            ])
            searchable_name = str(meta_dict.get("name", ""))

        if match_fn(searchable_text):
            matched.append((meta_dict, rank_fn(searchable_text, searchable_name)))

    total_matched = len(matched)

    # Sort by relevance score (desc), then (type, name)
    if get_q.order_by:
        field = get_q.order_by.lower()
        reverse = (get_q.order_dir == "desc")
        matched.sort(key=lambda pair: str(pair[0].get(field, "")).lower(), reverse=reverse)
    else:
        matched.sort(key=lambda pair: (-pair[1], pair[0].get("type", ""), pair[0].get("name", "")))

    matched = [m for m, _score in matched]

    offset = get_q.offset if get_q.offset is not None and get_q.offset >= 0 else 0
    limit = _resolve_limit(get_q.limit)

    if limit == UNLIMITED:
        sliced = matched[offset:] if offset < total_matched else []
    else:
        sliced = matched[offset : offset + limit] if offset < total_matched else []
    returned_count = len(sliced)

    meta = _build_page_meta(
        "SEARCH", type_filter, offset, returned_count, total_matched, limit,
        search_mode="regex" if is_regex else "substring",
        patterns=patterns,
        bodies_only=query.bodies_only,
    )
    if not query.bodies_only:
        meta["note"] = "Bodies are searched by default. Name matches rank above body/path matches. Use SEARCH BODIES to restrict the search to body text only."
    if type_filter != query.target_type:
        meta["note"] = f"Auto-limited to type='{type_filter}' (pattern looks like a filename). Use IN <type> to search other types."
    return {"meta": meta, "results": _group_results(sliced, "file_path")}


def compile_glob(client: Any, query: GlobQuery) -> dict:
    """Compile a GlobQuery into engine glob path search calls."""
    get_q = GetQuery(
        type_filter=None,
        where_expr=query.where_expr,
        conditions=query.conditions,
        limit=query.limit,
        offset=query.offset,
        order_by=query.order_by,
        order_dir=query.order_dir,
    )

    all_meta = client.get_all_metadata()
    pattern = query.pattern

    matched = []
    for node_id, meta in all_meta.items():
        meta_dict = dict(meta.items()) if hasattr(meta, "items") else meta
        meta_dict["node_id"] = node_id

        # GLOB is a *path* search: only File/Folder nodes carry the full path as
        # their identity. Symbols (functions/classes) share their file's path and
        # would flood results (one entry per symbol per matching file) — exclude
        # them by default so `GLOB src/modules/*/services.py` yields the ~11 files.
        ntype = meta_dict.get("type", "")
        if ntype not in ("File", "Folder"):
            continue

        # Match glob pattern against node_id or file_path
        fpath = str(meta_dict.get("file_path", node_id))
        variants = _pattern_variants(pattern)
        matches = any(_glob_match(fpath, v) or _glob_match(node_id, v) for v in variants)
        if not matches:
            continue

        # Evaluate where expression tree if present
        if get_q.where_expr:
            if not _evaluate_bool_expr(get_q.where_expr, meta_dict):
                continue

        matched.append(meta_dict)

    total_matched = len(matched)

    # Sort
    if get_q.order_by:
        field = get_q.order_by.lower()
        reverse = (get_q.order_dir == "desc")
        matched.sort(key=lambda r: str(r.get(field, "")).lower(), reverse=reverse)
    else:
        matched.sort(key=lambda r: (r.get("type", ""), r.get("file_path", "")))

    offset = get_q.offset if get_q.offset is not None and get_q.offset >= 0 else 0
    limit = _resolve_limit(get_q.limit)

    if limit == UNLIMITED:
        sliced = matched[offset:] if offset < total_matched else []
    else:
        sliced = matched[offset : offset + limit] if offset < total_matched else []
    returned_count = len(sliced)

    meta = _build_page_meta("GLOB", "file", offset, returned_count, total_matched, limit)
    meta["note"] = ("Matches File/Folder paths only. "
                    "Symbols inside matching files are excluded; use SEARCH or GET functions WHERE file_path CONTAINS '...'.")
    return {"meta": meta, "results": _group_results(sliced, "file_path")}


def compile_metadata(client: Any, query: MetadataQuery) -> dict:
    """Compile a MetadataQuery into engine calls."""
    node_id = resolve_node_id(client, query.node_id)
    if not node_id:
        suggestion = find_did_you_mean_suggestion(client, query.node_id)
        return {"ok": False, "error": f"Node '{query.node_id}' not found{suggestion}"}

    meta = client.get_node_meta(node_id)
    meta_dict = dict(meta.items()) if hasattr(meta, "items") else meta
    meta_dict["node_id"] = node_id

    # Include callers/callees for context
    callers = client.get_callers(node_id)
    callees = client.get_callees(node_id)
    all_dependents = getattr(client, "get_dependents", client.get_callers)(node_id)
    all_dependencies = getattr(client, "get_dependencies", client.get_callees)(node_id)
    caller_ids = set(callers)
    callee_ids = set(callees)
    structural_dependents = [item for item in all_dependents if item not in caller_ids]
    structural_dependencies = [item for item in all_dependencies if item not in callee_ids]

    # External symbols (Decimal, ValidationError, pytest.raises, ORM chains) appear
    # in the node's raw `calls` list but have no graph node — they are dead ends.
    # Surface them explicitly so the boundary is visible instead of a silent stop.
    external = _classify_external_calls(client, meta_dict, callees)

    return {
        "meta": {
            "ok": True,
            "query_type": "METADATA",
            "node_id": node_id,
            "callers_count": len(callers),
            "callees_count": len(callees),
            "structural_dependents_count": len(structural_dependents),
            "structural_dependencies_count": len(structural_dependencies),
            "external_callees_count": len(external),
        },
        "results": {
            "node": meta_dict,
            "direct_callers": callers,
            "direct_callees": callees,
            "structural_dependents": structural_dependents,
            "structural_dependencies": structural_dependencies,
            "external_callees": external,
        },
    }


def _classify_external_calls(client: Any, meta_dict: dict, resolved_callees: list) -> list:
    """Identify call targets in a node's `calls` list that are not in the graph.

    External symbols are dead ends in the call graph (third-party / stdlib /
    unresolved). Returns a list of {name, kind} where kind is one of:
      stdlib, third_party, attribute_chain, unresolved.
    """
    import json

    raw_calls = meta_dict.get("calls") or []
    if isinstance(raw_calls, str):
        try:
            raw_calls = json.loads(raw_calls)
        except Exception:
            raw_calls = [raw_calls]

    if not raw_calls:
        return []

    all_meta = client.get_all_metadata()
    all_ids = set(all_meta.keys())
    # Bare call names resolve to a node in ANY file (e.g. "persist_position" ->
    # "persistence.py:persist_position"); these are NOT external dead ends.
    all_names = {str(m.get("name", "")) for m in all_meta.values()}
    resolved = set(resolved_callees)
    node_file = str(meta_dict.get("file_path", ""))
    external = []
    seen = set()

    for call in raw_calls:
        call = str(call).strip()
        if not call or call in seen:
            continue
        seen.add(call)
        # Resolved in-graph (exact node id, edge, or bare name in any file)
        if call in all_ids or call in resolved or call in all_names or ":" in call:
            continue
        if f"{node_file}:{call}" in all_ids:
            continue

        base = call.split(".", 1)[0]
        if "." in call:
            kind = "attribute_chain"
        elif base in STDLIB_MODULES or base in _STDLIB_BASE_NAMES:
            kind = "stdlib"
        else:
            kind = "third_party"
        external.append({"name": call, "kind": kind})

    return external


def compile_impact(client: Any, query: ImpactQuery) -> dict:
    """Compile an ImpactQuery into engine calls with full enrichment."""
    node_id = resolve_node_id(client, query.node_id)
    if not node_id:
        suggestion = find_did_you_mean_suggestion(client, query.node_id)
        return {"ok": False, "error": f"Node '{query.node_id}' not found{suggestion}"}

    from src.services.graph_service import analyse_impact_data
    res = analyse_impact_data(client, node_id, query.direction, query.depth)
    if "error" in res:
        return {"ok": False, "query_type": "IMPACT", "target_node_id": node_id, "error": res["error"]}

    impact = res.get("impact", {})
    affected = impact.get("affected_nodes", [])
    mode = (getattr(query, "mode", "detailed") or "detailed").lower()

    meta = {
        "ok": True,
        "query_type": "IMPACT",
        "target_node_id": node_id,
        "direction": query.direction,
        "depth": query.depth,
        "mode": mode,
        "count": len(affected),
    }

    if mode == "count":
        by_type: dict = {}
        by_module: dict = {}
        for n in affected:
            t = n.get("type") or "Unknown"
            by_type[t] = by_type.get(t, 0) + 1
            fp = n.get("file") or n.get("node_id") or ""
            module = fp.split("/")[0] if "/" in fp else "(root)"
            by_module[module] = by_module.get(module, 0) + 1
        if query.direction == "callers":
            meta["callers_count"] = len(client.get_callers(node_id))
        else:
            meta["callees_count"] = len(client.get_callees(node_id))
        return {
            "meta": meta,
            "results": {
                "total": len(affected),
                "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
                "by_module": dict(sorted(by_module.items(), key=lambda kv: -kv[1])[:15]),
                "hint": "Use MODE summary for grouped detail, MODE detailed for full listings.",
            },
        }

    if mode == "summary":
        groups: dict = {}
        for n in affected:
            t = n.get("type") or "Unknown"
            fp = n.get("file") or n.get("node_id") or ""
            module = fp.split("/")[0] if "/" in fp else "(root)"
            g = groups.setdefault(t, {})
            g[module] = g.get(module, 0) + 1
        summary_types = {
            t: {"count": sum(mods.values()),
                "modules": dict(sorted(mods.items(), key=lambda kv: -kv[1])[:8])}
            for t, mods in groups.items()
        }
        direct = client.get_callers(node_id) if query.direction == "callers" \
            else client.get_callees(node_id)
        if query.direction == "callers":
            meta["callers_count"] = len(direct)
        else:
            meta["callees_count"] = len(direct)
        direct_key = "direct_callers" if query.direction == "callers" else "direct_callees"
        return {
            "meta": meta,
            "results": {
                "total": len(affected),
                "by_type": summary_types,
                direct_key: [
                    d.get("node_id") if isinstance(d, dict) else str(d) for d in direct
                ][:50],
                "hint": "MODE detailed returns the full per-file listing.",
            },
        }

    compact_affected = _group_results(
        [
            {
                "type": n.get("type", ""),
                "name": n.get("name") or n.get("node_id", ""),
                "file_path": n.get("file", n.get("node_id", "")),
                "lines": n.get("defined_at_lines", {}),
            }
            for n in affected
        ],
        "file_path",
    )
    payload = {k: v for k, v in impact.items() if k not in ("target", "direction", "depth")}
    payload["affected_nodes"] = compact_affected

    # Direct counts reconcile this query with METADATA's callers_count/callees_count
    # (same graph API), so a transitive blast radius is never mistaken for the
    # direct caller/callee set. At depth 1, count == direct count for a pure graph.
    if query.direction == "callers":
        meta["callers_count"] = len(client.get_callers(node_id))
    else:
        meta["callees_count"] = len(client.get_callees(node_id))
    return {
        "meta": meta,
        "results": payload,
    }


def compile_path(client: Any, query: PathQuery) -> dict:
    """Compile a PathQuery into shortest path engine calls with monorepo enrichment."""
    start_id = resolve_node_id(client, query.start_node)
    end_id = resolve_node_id(client, query.end_node)

    if not start_id:
        suggestion = find_did_you_mean_suggestion(client, query.start_node)
        return {"ok": False, "error": f"Start node '{query.start_node}' not found{suggestion}"}
    if not end_id:
        suggestion = find_did_you_mean_suggestion(client, query.end_node)
        return {"ok": False, "error": f"End node '{query.end_node}' not found{suggestion}"}

    path = find_shortest_path(client, start_id, end_id)
    if path is None:
        return {
            "meta": {
                "ok": True,
                "query_type": "PATH",
                "start_node": start_id,
                "end_node": end_id,
                "found": False,
                "length": 0,
            },
            "results": [],
        }

    # Build enriched path with edge relationships
    path_nodes = []
    for idx, nid in enumerate(path):
        meta = client.get_node_meta(nid)
        meta_dict = dict(meta.items()) if meta and hasattr(meta, "items") else (meta or {})
        node_entry = {
            "node_id": nid,
            "type": meta_dict.get("type", "Unknown"),
            "file_path": meta_dict.get("file_path", ""),
        }
        line_info = meta_dict.get("lines", {})
        if isinstance(line_info, dict) and line_info.get("start"):
            node_entry["line"] = line_info["start"]

        # Edge relationship to the next node in the path
        if idx < len(path) - 1:
            next_nid = path[idx + 1]
            callees = client.get_callees(nid)
            if next_nid in callees:
                node_entry["edge_to_next"] = "CALLS"
            else:
                node_entry["edge_to_next"] = "DEPENDS_ON"

        path_nodes.append(node_entry)

    return {
        "meta": {
            "ok": True,
            "query_type": "PATH",
            "start_node": start_id,
            "end_node": end_id,
            "found": True,
            "length": len(path) - 1,
        },
        "results": path_nodes,
    }


def _resolve_middleware_pipeline(
    all_meta: dict, route_url: str
) -> tuple[list[dict], dict | None]:
    """Build middleware chain for a route URL.

    Global middleware (source_var = "MIDDLEWARE" / "app") applies to ALL
    routes regardless of file.  Scoped middleware matches only when the
    route's source_var matches and it is in the same file.

    Returns (middleware_steps, handler_node).
    """
    route_node: dict | None = None
    route_id: str = ""
    for nid, meta in all_meta.items():
        if meta.get("type") != "Route":
            continue
        url = meta.get("full_url") or meta.get("url") or ""
        if not url:
            continue
        if url.rstrip("/") == route_url.rstrip("/") or url.endswith(route_url):
            route_node = meta
            route_id = nid
            break

    if route_node is None:
        return [], None

    source_var = route_node.get("source_var", "")
    route_file = route_node.get("file_path", "")

    mw_candidates: list[dict] = []
    for nid, meta in all_meta.items():
        if meta.get("type") != "Middleware":
            continue
        mw_sv = meta.get("source_var", "")
        # Global middleware applies regardless of file
        if mw_sv in ("MIDDLEWARE", "app"):
            pass
        # Scoped middleware must be in same file and share source_var
        elif mw_sv == source_var and meta.get("file_path", "") == route_file:
            pass
        else:
            continue

        mw_file = meta.get("file_path", "")
        mw_line = meta.get("line", 0) or 0
        mw_candidates.append({
            "name": meta.get("name", ""),
            "file": f"{mw_file}:{mw_line}" if mw_line else mw_file,
            "line": mw_line,
            "scope": "global" if mw_sv in ("MIDDLEWARE", "app") else "scoped",
            "middleware_type": meta.get("middleware_type", ""),
        })

    mw_candidates.sort(key=lambda x: x["line"])

    # Find the handler view — search edges from route to Function/Class nodes
    handler = None
    view_name = route_node.get("view_name", "")
    if view_name:
        # Try same-file first, then scan all nodes for a name match
        func_id = f"{route_file}:{view_name}"
        handler_meta = all_meta.get(func_id)
        if handler_meta is None:
            for nid, meta in all_meta.items():
                if meta.get("name") == view_name and meta.get("type") in ("Function", "Class"):
                    handler_meta = meta
                    func_id = nid
                    break
        if handler_meta:
            h_file = handler_meta.get("file_path", "")
            h_line = (handler_meta.get("lines") or {}).get("start", 0) or 0
            handler = {
                "name": handler_meta.get("name", ""),
                "file": f"{h_file}:{h_line}" if h_line else h_file,
            }

    return mw_candidates, handler


def compile_flow(client: Any, query: FlowQuery) -> dict:
    """Compile a FlowQuery into trace_business_flow or flow-through-pipeline calls."""
    if query.filter_type or query.route_url:
        return _compile_flow_through(client, query)

    import yaml
    from src.services.graph_service import trace_business_flow
    res_yaml = trace_business_flow(query.start_node, max_depth=query.depth)
    try:
        res = yaml.safe_load(res_yaml)
        if isinstance(res, dict):
            if "error" in res:
                return {"ok": False, "query_type": "FLOW", "error": res["error"]}
            meta = {"ok": res.get("ok", True), "query_type": "FLOW"}
            if "workflow" in res:
                meta["workflow"] = res["workflow"]
            if "nodes_traced" in res:
                meta["nodes_traced"] = res["nodes_traced"]
            if "duplicates_traced" in res:
                meta["duplicates_traced"] = res["duplicates_traced"]
            payload = {k: v for k, v in res.items() if k not in ("ok", "workflow", "duplicates_traced")}
            return {"meta": meta, "results": payload}
    except Exception:
        pass
    return {"meta": {"ok": True, "query_type": "FLOW"}, "results": {"raw": res_yaml}}


def _compile_flow_through(client: Any, query: FlowQuery) -> dict:
    """Compile a FLOW FROM route THROUGH middleware pipeline."""
    all_meta = client.get_all_metadata()
    route_url = query.route_url

    mw_steps, handler = _resolve_middleware_pipeline(all_meta, route_url)

    pipeline: list[dict] = []
    for i, mw in enumerate(mw_steps, 1):
        pipeline.append({
            "step": i,
            "kind": "middleware",
            "name": mw["name"],
            "scope": mw["scope"],
            "file": mw["file"],
        })
    if handler:
        pipeline.append({
            "step": len(pipeline) + 1,
            "kind": "handler",
            "name": handler["name"],
            "file": handler["file"],
        })

    resolution = {
        "route": "resolved" if len(pipeline) else "unresolved",
        "middleware": "resolved" if mw_steps else "empty",
        "handler": "resolved" if handler else "empty",
        "complete": len(pipeline) > 0,
    }

    return {
        "meta": {
            "ok": True,
            "query_type": "FLOW_PIPELINE",
            "route": route_url,
            "total_steps": len(pipeline),
            "resolution": resolution,
        },
        "results": {"pipeline": pipeline},
    }


def compile_stack(client: Any, query: StackQuery) -> dict:
    """Compile a StackQuery into trace_frontend_backend calls."""
    import yaml
    from src.services.graph_service import trace_frontend_backend
    res_yaml = trace_frontend_backend(query.api_endpoint)
    try:
        res = yaml.safe_load(res_yaml)
        if isinstance(res, dict):
            if "error" in res:
                return {"ok": False, "query_type": "STACK", "endpoint": query.api_endpoint, "error": res["error"]}
            meta = {"ok": res.get("ok", True), "query_type": "STACK"}
            if "endpoint" in res:
                meta["endpoint"] = res["endpoint"]
            if isinstance(res.get("resolution"), dict):
                meta["resolution"] = res["resolution"]
                meta["complete"] = bool(res["resolution"].get("complete", False))
            payload = {k: v for k, v in res.items() if k != "ok"}
            return {"meta": meta, "results": payload}
    except Exception:
        pass
    return {"meta": {"ok": True, "query_type": "STACK", "endpoint": query.api_endpoint}, "results": {"raw": res_yaml}}


def compile_audit(client: Any, query: AuditQuery) -> dict:
    """Compile an AuditQuery into tenant isolation security audit."""
    all_meta = client.get_all_metadata()
    module = query.module.lower()
    audited = []
    for nid, meta in all_meta.items():
        meta_dict = dict(meta.items()) if hasattr(meta, "items") else meta
        fpath = meta_dict.get("file_path", "").lower()
        if module != "all" and module not in fpath:
            continue
        inherits = meta_dict.get("inherits", []) or meta_dict.get("base_classes", [])
        sig = meta_dict.get("signature", "")
        body = meta_dict.get("body", "")

        status = "SECURE"
        msg = "Proper tenant isolation confirmed"
        if meta_dict.get("type") == "Class" and "TenantAwareModel" not in str(inherits) and "Model" in sig:
            status = "RISK"
            msg = "Model class does not inherit from TenantAwareModel"
        elif meta_dict.get("type") == "Function" and "shop" not in sig and ("objects." in body or "filter(" in body):
            status = "CRITICAL"
            msg = "DB query function missing shop parameter"

        if status != "SECURE" or module != "all":
            audited.append({
                "node_id": nid,
                "name": meta_dict.get("name"),
                "file_path": meta_dict.get("file_path"),
                "status": status,
                "message": msg
            })

    total = len(audited)
    sliced = audited[:50]
    return {
        "meta": {
            "ok": True,
            "query_type": "AUDIT",
            "module": query.module,
            "count": len(sliced),
            "total": total,
        },
        "results": sliced,
    }


STDLIB_MODULES = {
    'os', 'sys', 're', 'json', 'math', 'time', 'datetime', 'collections',
    'itertools', 'functools', 'pathlib', 'typing', 'dataclasses', 'enum',
    'abc', 'copy', 'hashlib', 'base64', 'uuid', 'random', 'statistics',
    'decimal', 'fractions', 'sqlite3', 'csv', 'io', 'pickle', 'socket',
    'http', 'urllib', 'xml', 'html', 'textwrap', 'string', 'logging',
    'warnings', 'traceback', 'pprint', 'inspect', 'ast', 'tokenize',
    'argparse', 'configparser', 'subprocess', 'multiprocessing', 'threading',
    'asyncio', 'unittest', 'doctest', 'tempfile', 'shutil', 'glob', 'fnmatch',
    'distutils', 'importlib', 'pkgutil', 'zipfile', 'tarfile', 'gzip',
    'bz2', 'lzma', 'struct', 'array', 'ctypes', 'curses', 'turtle', 'tkinter',
}

# Builtins / very common names that resolve to stdlib symbols even though the
# module isn't the first segment of the call name (Decimal, timedelta, ...).
_STDLIB_BASE_NAMES = {
    'Decimal', 'timedelta', 'timezone', 'localdate',
    'date', 'datetime', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set',
    'tuple', 'len', 'min', 'max', 'sum', 'abs', 'sorted', 'reversed', 'all',
    'any', 'enumerate', 'zip', 'range', 'filter', 'map', 'next', 'iter',
    'open', 'print', 'isinstance', 'issubclass', 'getattr', 'setattr', 'hasattr',
    'super', 'property', 'classmethod', 'staticmethod', 'type', 'object',
}


def _file_in_layer(fpath: str, layer: str) -> bool:
    """Check if a file path belongs to the given layer.
    
    Matches flexibly:
    - fpath starts with layer (full prefix match)
    - fpath ends with '/' + layer (suffix match, e.g. 'domain/' matches 'pos_caisse/domain/')
    - fpath == layer
    """
    fpath_norm = fpath.replace("\\", "/").strip("/")
    layer_norm = layer.strip("/")
    if fpath_norm == layer_norm:
        return True
    if fpath_norm.startswith(layer_norm + "/"):
        return True
    if fpath_norm.endswith("/" + layer_norm):
        return True
    if "/" + layer_norm + "/" in fpath_norm:
        return True
    return False


def _import_violates_layer(import_name: str, against: str, layer: str = "") -> bool:
    """Check if a dotted import name resolves to a path inside the forbidden layer.

    Handles both 'from X import Y' statements and bare module paths.
    An import within the same *layer* (e.g. achat/models imported by achat/views)
    is an internal dependency, not a violation — skip it when *layer* is provided.
    """
    against_norm = _strip_layer_ref_ext(against.strip("/"))
    layer_norm = _strip_layer_ref_ext(layer.strip("/"))

    def _hits(import_path: str) -> bool:
        if not import_path:
            return False
        # Internal import within the same layer is never a violation
        if layer_norm and (
            import_path.startswith(layer_norm)
            or import_path.startswith(layer_norm + "/")
            or import_path.startswith("src/" + layer_norm)
            or import_path.startswith("src/" + layer_norm + "/")
        ):
            return False
        if import_path.startswith(against_norm) or import_path.startswith(against_norm + "/"):
            return True
        if import_path.startswith("src/" + against_norm) or import_path.startswith("src/" + against_norm + "/"):
            return True
        if import_path.endswith("/" + against_norm):
            return True
        if "/" + against_norm + "/" in import_path:
            return True
        return False

    for module in _import_module_candidates(import_name):
        import_path = module.replace(".", "/")
        if _hits(import_path):
            return True
    return False


def _strip_layer_ref_ext(ref: str) -> str:
    """Normalize a layer reference for import matching.

    CHECK LAYERS may receive a file path (e.g. 'comptabilite/services.py') while
    imports are stored as module paths ('comptabilite.services' → 'comptabilite/services').
    Strip a trailing source-file extension so the two match, but never a dotted
    *folder* segment (e.g. 'pos_caisse.domain.entities' must stay intact).
    """
    root, ext = os.path.splitext(ref)
    if ext and 1 < len(ext) <= 5 and ext[1:].isalpha() and root:
        return root
    return ref


def _is_method_node(node_id: str) -> bool:
    """Check if a node_id represents a method (contains 'ClassName.method' pattern)."""
    if ":" not in node_id:
        return False
    after_colon = node_id.split(":", 1)[1]
    return "." in after_colon


def _resolve_import_to_symbols(imp: str, all_meta: dict) -> dict:
    """Resolve a dotted import to specific symbols (classes, top-level functions).
    
    Returns separate lists for imported_symbols and folder_ancestors, deduplicated.
    """
    imp_path = imp.replace(".", "/")

    # Find the best-matching file node(s)
    candidate_files = []
    for nid, m in all_meta.items():
        if m.get("type") != "File":
            continue
        m_fp = m.get("file_path", "").replace("\\", "/").lstrip("/")
        dotted_fp = m_fp.replace("/", ".")
        # Strip extension for matching
        fp_no_ext = dotted_fp.rsplit(".", 1)[0] if "." in dotted_fp else dotted_fp
        if imp == fp_no_ext or imp.startswith(fp_no_ext + ".") or imp_path.startswith(m_fp):
            candidate_files.append(m_fp)

    if not candidate_files:
        return {"imported_symbols": [], "folder_ancestors": []}

    # Collect all nodes from matched files
    seen_symbols = set()
    seen_folders = set()
    imported_symbols = []
    folder_ancestors = []

    for match_fp in candidate_files:
        for nid, m in all_meta.items():
            m_fp = m.get("file_path", "").replace("\\", "/").lstrip("/")
            if m_fp != match_fp:
                continue
            ntype = m.get("type", "")
            name = m.get("name", "")
            if nid in seen_symbols or nid in seen_folders:
                continue
            if ntype in ("Folder",):
                seen_folders.add(nid)
                folder_ancestors.append({"node_id": nid, "name": name, "type": ntype})
            elif ntype in ("Class",):
                seen_symbols.add(nid)
                imported_symbols.append({"node_id": nid, "name": name, "type": ntype})
            elif ntype == "Function" and not _is_method_node(nid):
                seen_symbols.add(nid)
                imported_symbols.append({"node_id": nid, "name": name, "type": ntype})

    return {
        "imported_symbols": imported_symbols[:10],
        "folder_ancestors": folder_ancestors[:5],
    }


def _extract_sub_layer(import_name: str, resolved_path: str) -> str:
    """Extract the meaningful sub-layer from an import path.
    
    E.g. 'pos_caisse.domain.entities.menu_item' -> 'domain.entities'
         'pos_caisse.application.commands.create_order' -> 'application.commands'
    """
    dotted = import_name
    parts = dotted.split(".")
    # Find where the project-internal path starts: skip the first segment(s)
    # that match the file structure prefix, then take meaningful sub-paths
    if resolved_path:
        rp_norm = resolved_path.replace("\\", "/").lstrip("/")
        rp_dotted = rp_norm.replace("/", ".")
        rp_parts = rp_dotted.split(".")
        # Find common prefix length
        i = 0
        while i < len(parts) and i < len(rp_parts) and parts[i] == rp_parts[i]:
            i += 1
        # The sub-layer is the parts between the common prefix and the last segment
        # Actually, the resolved path includes the file extension, so rp_parts has an extra element
        # Better: the sub-layer is parts[1:-1] (skip first segment and last segment)
        # But we need to handle variable depth
        if len(parts) >= 3:
            # parts[0] might be a top-level project dir, take the rest
            return ".".join(parts[1:-1])
        if len(parts) == 2:
            return parts[0]
    if len(parts) >= 2:
        return ".".join(parts[1:-1]) or parts[0]
    return "unknown"


def _normalize_import_module(imp: str) -> str:
    """Reduce an import string to a plain dotted module path.

    Handles every format the parser stores:
        'from src.modules.comptabilite import selectors' → 'src.modules.comptabilite'
        'from django.core.exceptions import ValidationError' → 'django.core.exceptions'
        'from typing import (Optional, Any)' → 'typing'
        'import os' → 'os'
        'django.db.models' → 'django.db.models'
    """
    imp = (imp or "").strip()
    if imp.startswith("from "):
        body = imp[5:].strip()
        if " import " in body:
            return body.split(" import ", 1)[0].strip().strip("()")
        return body.split(",")[0].strip()
    if imp.startswith("import "):
        return imp[7:].strip().split(",")[0].strip()
    return imp


def _import_module_candidates(imp: str) -> list[str]:
    """Expand an import string into candidate dotted module paths.

    'from A.B import C' → ['A.B', 'A.B.C']
    'from A.B import C, D' → ['A.B', 'A.B.C', 'A.B.D']
    'from A.B import C as X' → ['A.B', 'A.B.C']
    'import A.B' → ['A.B']
    'A.B' → ['A.B']
    """
    imp = (imp or "").strip()
    candidates: list[str] = []

    def _clean(name: str) -> str:
        name = name.strip()
        if " as " in name:
            name = name.split(" as ", 1)[0].strip()
        return name

    if imp.startswith("from "):
        body = imp[5:].strip()
        if " import " in body:
            base, _, names = body.partition(" import ")
            base = _clean(base).strip("()")
            if base:
                candidates.append(base)
            for n in names.strip("()").split(","):
                n = _clean(n)
                if n:
                    candidates.append(f"{base}.{n}")
        else:
            base = _clean(body).strip("()")
            if base:
                candidates.append(base)
    elif imp.startswith("import "):
        for n in imp[7:].split(","):
            n = _clean(n)
            if n:
                candidates.append(n)
    else:
        candidates.append(imp)
    return [c for c in candidates if c]


def _build_import_index(all_file_paths: set) -> dict:
    """Precompute an import→file resolution index (O(N) once instead of O(N×M)).

    by_dotted maps a file's dotted no-extension module path to its normalized
    path; extless lists extension-less normalized paths for rare path-style
    imports. Longest-prefix wins, so results are deterministic.
    """
    by_dotted: dict[str, str] = {}
    extless = []
    for fp in all_file_paths:
        fp_norm = fp.replace("\\", "/").lstrip("/")
        if "." not in fp_norm.rsplit("/", 1)[-1]:
            extless.append(fp_norm)
        dotted_fp = fp_norm.replace("/", ".")
        no_ext = dotted_fp.rsplit(".", 1)[0] if "." in dotted_fp else dotted_fp
        existing = by_dotted.get(no_ext)
        if existing is None or len(fp_norm) < len(existing):
            by_dotted[no_ext] = fp_norm
    return {"by_dotted": by_dotted, "extless": tuple(extless)}


def _categorize_import(import_name: str, all_file_paths: set, index: dict | None = None) -> dict:
    """Categorize an import as stdlib, third-party, or project-internal with sub-layer info."""
    candidates = _import_module_candidates(import_name)
    if not candidates:
        return {"category": "third_party", "import": import_name}

    top = candidates[0].split(".")[0]
    if top in STDLIB_MODULES:
        return {"category": "stdlib", "import": import_name}

    if index is None:
        index = _build_import_index(all_file_paths)
    by_dotted = index["by_dotted"]
    extless = index["extless"]

    # A candidate that resolves to a real project file → project (with sub-layer).
    for module in candidates:
        if not module:
            continue
        parts = module.split(".")
        for k in range(len(parts), 0, -1):
            key = ".".join(parts[:k])
            fp_norm = by_dotted.get(key)
            if fp_norm is not None:
                sub_layer = _extract_sub_layer(module, fp_norm)
                return {"category": "project", "import": import_name, "sub_layer": sub_layer,
                        "resolved_path": fp_norm, "resolved_paths": [fp_norm]}
        import_path = module.replace(".", "/")
        for fp_norm in extless:
            if import_path.startswith(fp_norm):
                sub_layer = _extract_sub_layer(module, fp_norm)
                return {"category": "project", "import": import_name, "sub_layer": sub_layer,
                        "resolved_path": fp_norm, "resolved_paths": [fp_norm]}

    for module in candidates:
        if not module:
            continue
        import_path = module.replace(".", "/")
        top_module = module.split(".")[0]
        if import_path.startswith("src/") or top_module in ("frontend", "backend", "apps", "lib", "modules"):
            sub_layer = _extract_sub_layer(module, "")
            return {"category": "project", "import": import_name, "sub_layer": sub_layer}

    return {"category": "third_party", "import": import_name}


def _build_path_hint(all_file_paths: set, given_layer: str) -> str:
    """Suggest actual paths in the project that resemble the given layer."""
    import difflib
    all_parts = set()
    for fp in all_file_paths:
        parts = fp.replace("\\", "/").strip("/").split("/")
        for p in parts:
            all_parts.add(p)
    matches = difflib.get_close_matches(given_layer, all_parts, n=3, cutoff=0.4)
    if matches:
        return f"No files found in layer '{given_layer}'. Did you mean one of: {matches}? Try a full path like 'pos_caisse/{given_layer}'."
    return f"No files found in layer '{given_layer}'. Check your path prefix (e.g. 'domain/' vs 'pos_caisse/domain/')."


def compile_check_layers(client: Any, query: CheckLayersQuery) -> dict:
    """Check if files in one layer import from a forbidden layer (Clean Architecture violations)."""
    all_meta = client.get_all_metadata()
    layer = query.layer.strip("/")
    against = query.against.strip("/")

    all_file_paths = set()
    for node_id, meta in all_meta.items():
        fp = meta.get("file_path", "")
        if fp:
            all_file_paths.add(fp)

    violations = []
    files_checked = 0
    for node_id, meta in all_meta.items():
        if meta.get("type") != "File":
            continue
        fpath = meta.get("file_path", "")
        if not _file_in_layer(fpath, layer):
            continue
        files_checked += 1
        imports = _dedupe_import_entries(meta.get("imports") or [])
        import_lines = meta.get("import_lines") or {}
        violating_imports = []
        for imp in imports:
            if _import_violates_layer(imp, against, layer):
                resolved = _resolve_import_to_symbols(imp, all_meta)
                entry = {"import": _render_import_statement(imp),
                         "imported_symbols": resolved["imported_symbols"]}
                if imp in import_lines:
                    entry["line"] = import_lines[imp]
                if resolved["folder_ancestors"]:
                    entry["folder_ancestors"] = resolved["folder_ancestors"]
                violating_imports.append(entry)
        if violating_imports:
            violations.append({
                "file": fpath,
                "node_id": node_id,
                "violating_imports": violating_imports,
                "violation_count": len(violating_imports),
            })

    if files_checked == 0:
        hint = _build_path_hint(all_file_paths, layer)
        return {"ok": False, "error": hint, "query_type": "CHECK_LAYERS", "layer": layer, "against": against}

    return {
        "meta": {
            "ok": True,
            "query_type": "CHECK_LAYERS",
            "layer": layer,
            "against": against,
            "files_checked": files_checked,
            "count": len(violations),
            "total": sum(v["violation_count"] for v in violations),
            "clean": len(violations) == 0,
        },
        "results": violations,
    }


def compile_layers_of(client: Any, query: LayersOfQuery) -> dict:
    """Show all external dependencies of a given layer, categorized by target."""
    all_meta = client.get_all_metadata()
    layer = query.layer.strip("/")

    all_file_paths = set()
    for node_id, meta in all_meta.items():
        fp = meta.get("file_path", "")
        if fp:
            all_file_paths.add(fp)

    files_analyzed = 0
    dep_map = {}
    import_index = _build_import_index(all_file_paths)
    for node_id, meta in all_meta.items():
        if meta.get("type") != "File":
            continue
        fpath = meta.get("file_path", "")
        if not _file_in_layer(fpath, layer):
            continue
        files_analyzed += 1
        imports = meta.get("imports") or []
        for imp in imports:
            categorized = _categorize_import(imp, all_file_paths, index=import_index)
            cat = categorized["category"]
            dep_map.setdefault(cat, {}).setdefault("imports", set()).add(imp)
            if cat == "project":
                sub = categorized.get("sub_layer", "unknown")
                dep_map[cat].setdefault("by_sub_layer", {}).setdefault(sub, set()).add(imp)

    if files_analyzed == 0:
        hint = _build_path_hint(all_file_paths, layer)
        return {"ok": False, "error": hint, "query_type": "LAYERS_OF", "layer": layer}

    # Convert sets to sorted lists
    result = {"files_analyzed": files_analyzed, "dependencies": {}}
    for cat, data in dep_map.items():
        entry = {"import_count": len(data["imports"]), "imports": sorted(data["imports"])}
        if "by_sub_layer" in data:
            entry["by_sub_layer"] = {sub: sorted(imps) for sub, imps in sorted(data["by_sub_layer"].items())}
        result["dependencies"][cat] = entry

    return {
        "meta": {
            "ok": True,
            "query_type": "LAYERS_OF",
            "layer": layer,
            "files_analyzed": files_analyzed,
            "total_unique_imports": sum(d["import_count"] for d in result["dependencies"].values()),
        },
        "results": result["dependencies"],
    }


def _normalize_type_filter(raw: str | None) -> str | None:
    """Normalize a type filter string to match metadata 'type' field."""
    if raw is None:
        return None
    raw = raw.lower()
    mapping = {
        "functions": "Function", "function": "Function",
        "classes": "Class", "class": "Class",
    }
    return mapping.get(raw, raw.capitalize())


def _normalize_base(name: str) -> str:
    """Strip whitespace and trailing type arguments so 'Repository<User>' matches 'Repository'."""
    name = name.strip()
    idx = name.find('<')
    if idx != -1:
        name = name[:idx].strip()
    return name


def compile_find_implements(client: Any, query: FindImplementsQuery) -> dict:
    """Find all classes that implement a given interface (base class / protocol)."""
    all_meta = client.get_all_metadata()
    interface = query.interface.strip()
    type_filter = _normalize_type_filter(query.target_type)

    results = []
    for node_id, meta in all_meta.items():
        ntype = meta.get("type", "")
        if type_filter and ntype != type_filter:
            continue
        if ntype != "Class":
            continue
        base_classes = meta.get("base_classes") or meta.get("inherits") or []
        if isinstance(base_classes, str):
            base_classes = [base_classes]
        matches = [b for b in base_classes if _normalize_base(b) == _normalize_base(interface)]
        if not matches:
            continue
        decorators = meta.get("decorators", [])
        methods = _count_methods_for_file(client, node_id, meta.get("file_path", ""))
        results.append({
            "node_id": node_id,
            "name": meta.get("name", ""),
            "type": "Class",
            "file_path": meta.get("file_path", ""),
            "lines": meta.get("lines", {}),
        })

    return {
        "meta": {
            "ok": True,
            "query_type": "FIND_IMPLEMENTS",
            "interface": interface,
            "type": (type_filter or "Class").lower(),
            "count": len(results),
            "total": len(results),
        },
        "results": _group_results(results, "file_path"),
    }


def _match_decorator(stored: str, query: str) -> bool:
    """Match a stored decorator name against a user query.

    Exact match:  "router.get" == "router.get"
    Prefix match: "router.get" starts_with "router" (user typed @router)
    '@' prefixes are ignored on both sides: '@dataclass' == 'dataclass'.
    """
    stored = stored.strip().lstrip("@")
    query = query.strip().lstrip("@")
    if stored == query:
        return True
    if '.' not in query and stored.startswith(query + '.'):
        return True
    return False


def compile_find_decorated(client: Any, query: FindDecoratedQuery) -> dict:
    """Find all classes and functions decorated with a specific decorator."""
    all_meta = client.get_all_metadata()
    raw_decorator = query.decorator.strip()
    decorator = raw_decorator.lstrip("@")
    type_filter = _normalize_type_filter(query.target_type)
    available_types = ("Class", "Function")

    results = []
    for node_id, meta in all_meta.items():
        ntype = meta.get("type", "")
        if ntype not in available_types:
            continue
        if type_filter and ntype != type_filter:
            continue
        node_decorators = meta.get("decorators") or []
        if isinstance(node_decorators, str):
            node_decorators = [node_decorators]
        matches = [d for d in node_decorators if _match_decorator(d, decorator)]
        if not matches:
            continue
        if query.where_expr:
            if not _evaluate_bool_expr(query.where_expr, meta):
                continue
        elif query.conditions:
            if not all(_match_condition(meta, c) for c in query.conditions):
                continue
        results.append({
            "node_id": node_id,
            "name": meta.get("name", ""),
            "type": ntype,
            "file_path": meta.get("file_path", ""),
            "lines": meta.get("lines", {}),
        })

    total = len(results)
    offset = query.offset if query.offset is not None and query.offset >= 0 else 0
    limit = _resolve_limit(query.limit)
    if limit == UNLIMITED:
        sliced = results[offset:] if offset < total else []
    else:
        sliced = results[offset : offset + limit] if offset < total else []

    meta = _build_page_meta(
        "FIND_DECORATED", (type_filter or "all").lower(), offset, len(sliced), total, limit,
        decorator=decorator,
    )
    return {"meta": meta, "results": _group_results(sliced, "file_path")}


def _parse_enforce_rule(rule_str: str) -> dict:
    """Parse an ENFORCE rule string into a structured rule descriptor.

    Supported rule syntaxes:
        "<layer> MUST_NOT_IMPORT <layer>"
        "<a> <- <b> <- <c>"  (dependency direction chain)
        "NO_CIRCULAR_DEPENDENCIES"  (or "NO_CIRCULAR")
        "MUST_BE decorated_with '<decorator>'"  (with optional IN scope)
    """
    rule = rule_str.strip()

    # MUST_NOT_IMPORT: "domain MUST_NOT_IMPORT infrastructure"
    if " MUST_NOT_IMPORT " in rule:
        parts = rule.split(" MUST_NOT_IMPORT ", 1)
        layer = parts[0].strip("\"'")
        against = parts[1].strip("\"'")
        return {"type": "must_not_import", "layer": layer, "against": against}

    # Dependency direction: "domain <- application <- infrastructure"
    if " <- " in rule:
        chain = [p.strip() for p in rule.split(" <- ")]
        return {"type": "dependency_direction", "chain": chain}

    # MUST_BE decorated_with syntax:
    # "classes IN 'domain/entities' MUST_BE decorated_with 'dataclass'"
    # or "<file.py> MUST_BE decorated_with 'transaction.atomic'"  (path-scoped)
    # or "MUST_BE decorated_with '@dataclass'" or "MUST_BE decorated_with dataclass"
    if "decorated_with" in rule and (" MUST_BE " in rule or rule.startswith("MUST_BE ")):
        import re
        decorator_match = re.search(r"decorated_with\s+'([^']+)'", rule)
        if decorator_match:
            decorator = decorator_match.group(1)
        else:
            # Fallback: take the last word in the rule string
            parts = rule.split("decorated_with", 1)
            decorator = parts[-1].strip().strip("'\"") if len(parts) > 1 else ""
        in_match = re.search(r"IN\s+'([^']+)'", rule)
        path = in_match.group(1) if in_match else ""
        # No IN clause but the rule starts with a file/path subject (e.g.
        # "src/modules/sales/services.py MUST_BE ...")? Use it as the path scope.
        if not path:
            subject = rule.split(" MUST_BE", 1)[0].strip("\"'")
            if subject and subject != rule and _looks_like_path_subject(subject):
                path = subject
        words = rule.split()
        node_type = "Class"
        for w in words:
            wl = w.lower().strip("\"'")
            if wl in ("classes", "class"):
                node_type = "Class"
            elif wl in ("functions", "function"):
                node_type = "Function"
        # A bare file subject targets every callable in that file (classes + functions).
        if path and not words_mention_type(words):
            node_type = "ClassAndFunction"
        return {"type": "must_be_decorated", "node_type": node_type, "path": path, "decorator": decorator, "rule": rule}

    # NO_CIRCULAR_DEPENDENCIES (or just NO_CIRCULAR)
    if rule.upper().replace(" ", "_").startswith("NO_CIRCULAR"):
        return {"type": "no_circular"}

    raise ValueError(f"Unknown ENFORCE rule syntax: {rule}")


def _build_file_import_graph(all_meta: dict) -> dict:
    """Build a file-level adjacency list of resolved imports.

    Returns {file_path: [resolved_file_path, ...]} showing which files
    each file depends on via project-internal imports.
    """
    all_file_paths = {meta.get("file_path", "") for meta in all_meta.values() if meta.get("file_path")}

    graph: dict[str, set] = {}
    index = _build_import_index(all_file_paths)
    for node_id, meta in all_meta.items():
        if meta.get("type") != "File":
            continue
        fpath = meta.get("file_path", "")
        if not fpath:
            continue
        if fpath not in graph:
            graph[fpath] = set()
        imports = meta.get("imports") or []
        for imp in imports:
            categorized = _categorize_import(imp, all_file_paths, index=index)
            if categorized["category"] != "project":
                continue
            resolved = categorized.get("resolved_path", "")
            if resolved and resolved != fpath:
                graph[fpath].add(resolved)
    return {k: sorted(v) for k, v in graph.items()}


def _detect_cycles(adj: dict) -> list[list[str]]:
    """Detect cycles in a directed graph via iterative colored DFS.

    Each back-edge is reported once (standard WHITE/GRAY/BLACK discipline), so
    runtime is O(V + E). Path state is maintained incrementally (dict positions
    + append/pop) — never copied per step — which keeps large dense graphs
    (thousands of files) well under a second.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in adj}
    pos: dict[str, int] = {}
    path: list[str] = []
    cycles: list[list[str]] = []
    seen: set[frozenset] = set()

    for root in adj:
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        pos[root] = len(path)
        path.append(root)
        stack = [(root, iter(adj.get(root, ())))]
        while stack:
            u, neighbors = stack[-1]
            pushed = False
            for v in neighbors:
                if color.get(v, WHITE) == GRAY:
                    start = pos.get(v)
                    if start is not None:
                        cyc = path[start:] + [v]
                        key = frozenset(cyc)
                        if key not in seen:
                            seen.add(key)
                            cycles.append(cyc)
                elif color.get(v, WHITE) == WHITE:
                    if v not in color:
                        color[v] = WHITE
                    color[v] = GRAY
                    pos[v] = len(path)
                    path.append(v)
                    stack.append((v, iter(adj.get(v, ()))))
                    pushed = True
                    break
            if not pushed:
                stack.pop()
                color[u] = BLACK
                pos.pop(u, None)
                path.pop()

    return cycles


def compile_enforce(client: Any, query: EnforceQuery) -> dict:
    """Enforce architectural rules — returns PASSED or VIOLATED with details.

    Sub-rules:
        MUST_NOT_IMPORT  — same as CHECK LAYERS but rule-oriented
        Dependency direction (<- chain) — verify layer ordering
        NO_CIRCULAR_DEPENDENCIES — cycle detection in import graph
        MUST_BE decorated_with — structural decorator constraint
    """
    try:
        rule = _parse_enforce_rule(query.rule_str)
    except ValueError as e:
        return {"ok": False, "query_type": "ENFORCE", "rule": query.rule_str, "error": str(e)}

    rule_type = rule["type"]
    scope = query.scope
    # '.'/'/'/' mean "everywhere" — a literal path-match against them would
    # filter out every file and silently produce vacuous results.
    if scope in (".", "/", ""):
        scope = None

    all_meta = client.get_all_metadata()

    if rule_type == "must_not_import":
        return _enforce_must_not_import(all_meta, rule, scope)
    elif rule_type == "dependency_direction":
        return _enforce_dependency_direction(all_meta, rule, scope)
    elif rule_type == "no_circular":
        return _enforce_no_circular(all_meta, scope)
    elif rule_type == "must_be_decorated":
        return _enforce_must_be_decorated(all_meta, rule, scope)
    else:
        return {"ok": False, "query_type": "ENFORCE", "rule": query.rule_str, "error": f"Unknown rule type: {rule_type}"}


def _has_glob_chars(pattern: str) -> bool:
    """True if a layer reference contains glob metacharacters (* ? [ { !)."""
    return any(ch in pattern for ch in "*?[!{")


def _expand_layer_pattern(pattern: str, all_file_paths: set) -> list[str]:
    """Expand a glob layer reference to concrete file paths (empty if none match).

    Non-glob references return [pattern] unchanged (matched later via _file_in_layer).
    """
    if not _has_glob_chars(pattern):
        return [pattern]
    matched = []
    for fp in all_file_paths:
        if any(_glob_match(fp, v) for v in _pattern_variants(pattern)):
            matched.append(fp)
    return sorted(matched)


def _import_hits_any_layer(imp: str, targets: list[str]) -> bool:
    """True if an import resolves into any of the given target layers/paths."""
    for t in targets:
        if _import_violates_layer(imp, t):
            return True
    return False


def _import_module_of(imp: str) -> str:
    """Extract the module path an import entry refers to.

    Handles the mixed formats stored by _extract_imports:
      'from a.b import C' -> 'a.b'   'import a.b as x' -> 'a.b'   'a.b' -> 'a.b'
    """
    text = imp.strip()
    if text.startswith("from "):
        return text[5:].split(" import ", 1)[0].strip()
    if text.startswith("import "):
        return text[7:].split()[0].rstrip(",").strip()
    return text


def _dedupe_import_entries(imports: list) -> list:
    """Collapse duplicate representations of the same import statement.

    The parser stores BOTH the full statement and the bare module path
    ('from a.b import C' AND 'a.b'). Keep full statements; drop bare entries
    whose module is already covered by one. Order preserved.
    """
    seen = set()
    fulls = []
    bares = []
    for imp in imports:
        if not imp:
            continue
        if imp.startswith(("from ", "import ")):
            if imp not in seen:
                seen.add(imp)
                fulls.append(imp)
        else:
            bares.append(imp)
    full_modules = {_import_module_of(f).lstrip(".") for f in fulls}
    kept = list(fulls)
    seen_bare = set()
    for b in bares:
        if b in seen_bare:
            continue
        seen_bare.add(b)
        if b.lstrip(".") not in full_modules:
            kept.append(b)
    return kept


def _render_import_statement(imp: str) -> str:
    """Render an import entry as a readable statement (never 'from from ...')."""
    text = (imp or "").strip()
    if text.startswith(("from ", "import ")):
        return text
    return f"import {text}"


def _enforce_unchecked(rule_label: str, reason: str) -> dict:
    """ENFORCE result when a rule can't be verified (no files/entities matched).

    A vacuous PASSED (zero files inspected) is a silent false-confidence trap —
    instead the verdict is UNCHECKED with an explicit reason so callers never
    mistake "nothing checked" for "everything passed".
    """
    return {
        "meta": {
            "ok": True,
            "query_type": "ENFORCE",
            "rule": rule_label,
            "status": "UNCHECKED",
            "files_checked": 0,
            "entities_checked": 0,
            "count": 0,
            "reason": reason,
        },
        "results": [],
    }


def _enforce_must_not_import(all_meta: dict, rule: dict, scope: str | None) -> dict:
    """ENFORCE '<layer> MUST_NOT_IMPORT <forbidden>'"""
    layer = rule["layer"]
    against = rule["against"]

    all_file_paths = set()
    for node_id, meta in all_meta.items():
        fp = meta.get("file_path", "")
        if fp:
            all_file_paths.add(fp)

    layer_targets = _expand_layer_pattern(layer, all_file_paths)
    against_targets = _expand_layer_pattern(against, all_file_paths)

    # A glob layer that expands to nothing would silently false-pass — report UNCHECKED.
    if _has_glob_chars(layer) and not layer_targets:
        return _enforce_unchecked(
            f"{layer} MUST_NOT_IMPORT {against}",
            f"ENFORCE glob layer '{layer}' matched 0 files. Check the pattern.",
        )
    if _has_glob_chars(against) and not against_targets:
        return _enforce_unchecked(
            f"{layer} MUST_NOT_IMPORT {against}",
            f"ENFORCE glob target '{against}' matched 0 files. Check the pattern.",
        )

    violations = []
    files_checked = 0
    for node_id, meta in all_meta.items():
        if meta.get("type") != "File":
            continue
        fpath = meta.get("file_path", "")
        if not any(_file_in_layer(fpath, t) for t in layer_targets):
            continue
        if scope and not _file_in_layer(fpath, scope):
            continue
        files_checked += 1
        imports = _dedupe_import_entries(meta.get("imports") or [])
        import_lines = meta.get("import_lines") or {}
        for imp in imports:
            if _import_hits_any_layer(imp, against_targets):
                resolved = _resolve_import_to_symbols(imp, all_meta)
                violations.append({
                    "file": fpath,
                    "line": import_lines.get(imp),
                    "statement": _render_import_statement(imp),
                    "forbidden_target": against,
                    "hint": f"{layer.capitalize()} layer must only depend on Abstractions/Ports, not {against} implementations.",
                })

    # Nothing inspected => can't be a verified pass; report UNCHECKED instead.
    if files_checked == 0:
        return _enforce_unchecked(
            f"{layer} MUST_NOT_IMPORT {against}",
            f"Layer '{layer}' matched 0 indexed files — nothing to verify.",
        )

    passed = len(violations) == 0
    meta = {
        "ok": True,
        "query_type": "ENFORCE",
        "rule": f"{layer} MUST_NOT_IMPORT {against}",
        "status": "PASSED" if passed else "VIOLATED",
        "files_checked": files_checked,
        "count": len(violations),
    }
    return {"meta": meta, "results": violations}


def _enforce_dependency_direction(all_meta: dict, rule: dict, scope: str | None) -> dict:
    """ENFORCE '<a> <- <b> <- <c>' — verify dependency direction chain.

    Checks that files in layer b do NOT import from layer a,
    files in layer c do NOT import from layer b or a, etc.
    Supports glob layer references ('src/modules/*/api.py').
    """
    chain = rule["chain"]
    violations = []

    all_file_paths = set()
    for node_id, meta in all_meta.items():
        fp = meta.get("file_path", "")
        if fp:
            all_file_paths.add(fp)

    expanded_chain = [_expand_layer_pattern(part, all_file_paths) for part in chain]
    files_checked = 0

    # For each pair (parent, child) in chain, verify child does not import from parent
    for i in range(len(chain)):
        for j in range(i + 1, len(chain)):
            layer_child_targets = expanded_chain[j]
            layer_forbidden_targets = expanded_chain[i]

            for node_id, meta in all_meta.items():
                if meta.get("type") != "File":
                    continue
                fpath = meta.get("file_path", "")
                if not any(_file_in_layer(fpath, t) for t in layer_child_targets):
                    continue
                if scope and not _file_in_layer(fpath, scope):
                    continue
                files_checked += 1
                imports = _dedupe_import_entries(meta.get("imports") or [])
                import_lines = meta.get("import_lines") or {}
                for imp in imports:
                    if _import_hits_any_layer(imp, layer_forbidden_targets):
                        violations.append({
                            "file": fpath,
                            "line": import_lines.get(imp),
                            "statement": _render_import_statement(imp),
                            "violation": f"Layer '{chain[j]}' imports from '{chain[i]}', violating direction {chain[j-1]} <- {chain[j]}",
                            "hint": f"Dependencies must flow: {' <- '.join(chain)}",
                        })

    # Any chain layer that matches no files makes the whole rule unverifiable —
    # never a vacuous pass. Report UNCHECKED (glob or plain path).
    missing = [p for p, t in zip(chain, expanded_chain) if not t]
    if missing:
        return _enforce_unchecked(
            " <- ".join(chain),
            f"ENFORCE chain layer(s) matched 0 files: {', '.join(missing)}. Check the patterns.",
        )

    if files_checked == 0:
        return _enforce_unchecked(
            " <- ".join(chain),
            "No files matched the chain layers — nothing to verify.",
        )

    passed = len(violations) == 0
    meta = {
        "ok": True,
        "query_type": "ENFORCE",
        "rule": " <- ".join(chain),
        "status": "PASSED" if passed else "VIOLATED",
        "files_checked": files_checked,
        "count": len(violations),
    }
    return {"meta": meta, "results": violations}


def _enforce_no_circular(all_meta: dict, scope: str | None) -> dict:
    """ENFORCE NO_CIRCULAR_DEPENDENCIES — detect cycles in import graph."""
    adj = _build_file_import_graph(all_meta)

    # Filter by scope if specified
    if scope:
        filtered = {}
        for fpath, deps in adj.items():
            if _file_in_layer(fpath, scope):
                filtered[fpath] = [d for d in deps if _file_in_layer(d, scope)]
        adj = filtered

    cycles = _detect_cycles(adj)
    if len(adj) == 0:
        return _enforce_unchecked(
            "NO_CIRCULAR_DEPENDENCIES",
            f"No files analyzed in scope '{scope or 'all'}' — nothing to verify.",
        )
    passed = len(cycles) == 0

    meta = {
        "ok": True,
        "query_type": "ENFORCE",
        "rule": "NO_CIRCULAR_DEPENDENCIES",
        "status": "PASSED" if passed else "VIOLATED",
        "scope": scope or "all",
        "files_analyzed": len(adj),
        "count": len(cycles),
    }
    return {"meta": meta, "results": cycles}


def _enforce_must_be_decorated(all_meta: dict, rule: dict, scope: str | None) -> dict:
    """ENFORCE "<subject> MUST_BE decorated_with '<decorator>'"

    subject may be a node type (classes/functions), optionally IN a path, or a bare
    file path (e.g. "src/modules/sales/services.py") which targets every callable
    (class + function) in that file.
    """
    node_type = rule.get("node_type", "Class")
    both = node_type == "ClassAndFunction"
    path = rule.get("path", "")
    decorator = rule["decorator"].lstrip("@")

    violations = []
    files_checked = 0
    for node_id, meta in all_meta.items():
        ntype = meta.get("type", "")
        if both:
            if ntype not in ("Class", "Function"):
                continue
        elif ntype != node_type:
            continue
        fpath = meta.get("file_path", "")
        if path and not _file_in_layer(fpath, path):
            continue
        if scope and not _file_in_layer(fpath, scope):
            continue
        files_checked += 1
        node_decorators = meta.get("decorators") or []
        if isinstance(node_decorators, str):
            node_decorators = [node_decorators]
        has_decorator = any(d.strip().lstrip("@") == decorator for d in node_decorators)
        if not has_decorator:
            violations.append({
                "node_id": node_id,
                "name": meta.get("name", ""),
                "file_path": fpath,
                "missing_decorator": decorator,
                "hint": f"Add @{decorator} decorator to this {ntype.lower()}.",
            })

    target_label = "Classes & Functions" if both else f"{node_type}s"
    if files_checked == 0:
        return _enforce_unchecked(
            f"{target_label} in '{path}' MUST_BE decorated_with '{decorator}'" if path
            else f"{target_label} MUST_BE decorated_with '{decorator}'",
            f"No {target_label.lower()} matched" + (f" under '{path}'" if path else "") + " — nothing to verify.",
        )

    passed = len(violations) == 0
    rule_label = f"{target_label} in '{path}'" if path else target_label
    meta = {
        "ok": True,
        "query_type": "ENFORCE",
        "rule": f"{rule_label} MUST_BE decorated_with '{decorator}'",
        "status": "PASSED" if passed else "VIOLATED",
        "entities_checked": files_checked,
        "count": len(violations),
    }
    return {"meta": meta, "results": violations}


def _count_methods_for_file(client: Any, class_node_id: str, file_path: str) -> int:
    """Count methods belonging to a class by scanning sibling nodes."""
    count = 0
    prefix = class_node_id + "."
    all_meta = client.get_all_metadata()
    for nid, meta in all_meta.items():
        if nid.startswith(prefix) and meta.get("type") == "Function":
            count += 1
    return count


def _module_name(file_path: str) -> str:
    """Extract the immediate module name from a file path under a root.

    A file counts as part of a module only when it lives INSIDE a directory —
    bare root-level files (README.md, requirements.txt, *.json, ...) are not
    modules and return "".
    """
    parts = file_path.replace("\\", "/").split("/")
    for i, p in enumerate(parts):
        if p == "modules":
            return parts[i + 1] if i + 1 < len(parts) else ""
    parts = [p for p in parts if p and p != "src"]
    if len(parts) > 1:
        return parts[0]
    return ""


_KNOWN_SERVICE_FILES = {"services.py", "service.py"}
_KNOWN_API_FILES = {"api.py", "views.py"}
_KNOWN_SELECTOR_FILES = {"selectors.py"}
_KNOWN_SCHEMA_FILES = {"schema.py", "schemas.py", "serializer.py", "serializers.py"}
_KNOWN_MODEL_FILES = {"models.py", "model.py"}

_FILE_ROLE_MAP: dict[str, str] = {}
for _f in _KNOWN_SERVICE_FILES:
    _FILE_ROLE_MAP[_f] = "services_loc"
for _f in _KNOWN_API_FILES:
    _FILE_ROLE_MAP[_f] = "api_loc"
for _f in _KNOWN_SELECTOR_FILES:
    _FILE_ROLE_MAP[_f] = "selector_loc"
for _f in _KNOWN_SCHEMA_FILES:
    _FILE_ROLE_MAP[_f] = "schema_loc"
for _f in _KNOWN_MODEL_FILES:
    _FILE_ROLE_MAP[_f] = "models_loc"


_TEST_FILE_PATTERNS = (
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx",
)
_TEST_FRAMEWORKS = {"pytest", "unittest", "jest", "vitest", "bun:test", "mocha", "jasmine"}

# JS/TS test-call detection. Word boundaries prevent Python false positives like
# commit(, fit(, exit(, quit(, split( — none have a boundary before "it"/"test".
_JS_TEST_CALL_RE = re.compile(r"\b(?:it|test|describe)(?:\.each)?\s*\(")
_JS_TEST_CASE_RE = re.compile(r"\b(?:it|test)(?:\.each)?\s*\(")
_JS_TS_EXT = (".js", ".jsx", ".ts", ".tsx")


def _is_js_ts_file(fpath: str) -> bool:
    """The it()/test()/describe() body heuristic is JS/TS-only; other languages
    (e.g. Python) detect tests by naming (test_*, *_test.py) and frameworks."""
    return fpath.lower().endswith(_JS_TS_EXT)


def _is_test_file(base: str, meta: dict) -> bool:
    """Determine if a file is a test file by name or framework metadata."""
    if base.startswith("test_") or base.endswith("_test.py") or base.endswith("tests.py"):
        return True
    if base.endswith(_TEST_FILE_PATTERNS):
        return True
    fw = meta.get("frameworks") or []
    if isinstance(fw, str):
        fw = [fw]
    if any(f.lower() in _TEST_FRAMEWORKS for f in fw):
        return True
    return False


def _count_test_calls_in_body(body: str) -> int:
    """Count JS/TS individual test cases (it/test) in a file body.
    describe() is a suite, not a test — excluded from the count.
    """
    if not body:
        return 0
    return len(_JS_TEST_CASE_RE.findall(body))


def compile_stats(client: Any, query: StatsQuery) -> dict:
    """Project / module stats — a one-shot onboarding summary."""
    import os

    all_meta = client.get_all_metadata()
    root = query.path.strip()
    workspace = getattr(client, "workspace_path", "")
    if not workspace and hasattr(client, "_client"):
        workspace = getattr(client._client, "workspace_path", "")

    # Indexed file paths are workspace-relative. Accept an absolute workspace
    # path too, which is what agents often have in their execution context.
    if root and os.path.isabs(root) and workspace:
        try:
            relative = os.path.relpath(root, workspace)
            if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
                root = relative
        except ValueError:
            pass

    root = root.replace("\\", "/").lstrip("./").lstrip("/").rstrip("/")
    if root in ("", ".", "./"):
        root = ""

    files = functions = classes = declarations = loc = 0
    test_functions = test_files = 0
    decorator_counts: dict[str, int] = {}
    module_map: dict[str, dict] = {}
    test_modules: set[str] = set()

    # Per-file test-function tallies, so a file containing test_* functions is
    # always counted as a test file (keeps test_functions / test_files consistent).
    test_fn_by_file: dict[str, int] = {}
    file_info: dict[str, dict] = {}

    for node_id, meta in all_meta.items():
        meta = dict(meta) if hasattr(meta, "items") else meta
        fpath = meta.get("file_path", "")
        if root and fpath != root and not fpath.startswith(root + "/"):
            continue

        ntype = meta.get("type", "")

        if ntype == "File":
            files += 1
            fc = meta.get("lines_count", 0) or 0
            loc += fc
            mn = _module_name(fpath)
            base = fpath.split("/")[-1]
            file_info[fpath] = {
                "node_id": node_id, "lines": fc, "module": mn, "base": base, "meta": meta,
            }
            if mn:
                module_map.setdefault(mn, {
                    "files": 0, "services_loc": 0, "models_loc": 0,
                    "api_loc": 0, "selector_loc": 0, "schema_loc": 0,
                    "test_files": 0,
                })
                module_map[mn]["files"] += 1
                role = _FILE_ROLE_MAP.get(base)
                if role:
                    module_map[mn][role] += fc

        elif ntype == "Function":
            functions += 1
            name = meta.get("name", "")
            is_test_fn = False
            if name.startswith("test_"):
                is_test_fn = True
            else:
                body = meta.get("body", "")
                if _is_js_ts_file(fpath) and _JS_TEST_CALL_RE.search(body or ""):
                    is_test_fn = True
            if is_test_fn:
                test_functions += 1
                test_fn_by_file[fpath] = test_fn_by_file.get(fpath, 0) + 1

        elif ntype == "Class":
            classes += 1

        elif ntype == "Declaration":
            declarations += 1

        # Decorator frequency
        decos = meta.get("decorators") or []
        if isinstance(decos, str):
            decos = [decos]
        for d in decos:
            d = d.strip()
            if d:
                decorator_counts[d] = decorator_counts.get(d, 0) + 1

    # Second pass over files: a file is a test file if its name/framework says so
    # OR it actually contains test functions. This keeps the two counters coherent.
    for fpath, info in file_info.items():
        base = info["base"]
        mn = info["module"]
        is_test = _is_test_file(base, info["meta"]) or test_fn_by_file.get(fpath, 0) > 0
        if not is_test:
            # JS/TS: test files detected by name already; also catch files whose
            # body contains it()/test() calls even without a test_* prefix.
            if _is_js_ts_file(fpath) and _count_test_calls_in_body(info["meta"].get("body", "")) > 0:
                is_test = True
        if is_test:
            test_files += 1
            body = info["meta"].get("body", "")
            test_functions += _count_test_calls_in_body(body)
            if mn:
                test_modules.add(mn)
                module_map[mn]["test_files"] += 1

    modules_out = []
    for mn in sorted(module_map):
        m = module_map[mn]
        modules_out.append({
            "name": mn,
            "files": m["files"],
            "services_loc": m["services_loc"],
            "api_loc": m["api_loc"],
            "selector_loc": m["selector_loc"],
            "schema_loc": m["schema_loc"],
            "models_loc": m["models_loc"],
            "test_files": m["test_files"],
        })

    test_file_ratio = round(test_files / files, 3) if files else 0.0
    tested_functions = min(test_functions, functions)
    function_cover_ratio = round(tested_functions / functions, 3) if functions else 0.0

    return {
        "meta": {
            "ok": True,
            "query_type": "STATS",
            "path": root,
            "files": files,
            "functions": functions,
            "classes": classes,
            "declarations": declarations,
            "test_functions": test_functions,
            "lines_of_code": loc,
            "test_file_ratio": test_file_ratio,
            "function_coverage_ratio": function_cover_ratio,
        },
        "results": {
            "modules": modules_out,
            "decorators": dict(sorted(decorator_counts.items(), key=lambda x: -x[1])),
            "test_coverage": {
                "test_files": test_files,
                "test_functions": test_functions,
                "test_file_ratio": test_file_ratio,
                "function_coverage_ratio": function_cover_ratio,
                "modules_with_tests": sorted(test_modules),
            },
        },
    }
