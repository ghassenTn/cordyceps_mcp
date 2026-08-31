"""
Query Engine & DSL for Cordyceps Search.

Provides a unified `query()` function that accepts a DSL string,
parses it, compiles it into EngramDB engine calls, and returns
structured YAML output.

Example queries:
    GET functions WHERE name LIKE 'test_*' WITH callers LIMIT 20
    GET classes WHERE file_path LIKE 'src/modules/sales/*'
    GET ALL WHERE name LIKE 'get_*' LIMIT 10
    METADATA FOR 'src/modules/sales/api.py:get_sales'
    IMPACT OF 'src/modules/sales/api.py:get_sales' DIRECTION callers DEPTH 3
    CHECK LAYERS 'domain/' AGAINST 'infrastructure/'
    LAYERS OF 'domain/'
    FIND IMPLEMENTS 'Protocol'
    FIND DECORATED WITH '@dataclass'
    ENFORCE 'domain MUST_NOT_IMPORT infrastructure'
    ENFORCE 'domain <- application <- infrastructure'
    ENFORCE NO_CIRCULAR_DEPENDENCIES IN 'pos_caisse'
    ENFORCE "MUST_BE decorated_with '@dataclass'"
"""

from __future__ import annotations
import logging

from .parser import parse_query, GetQuery, SearchQuery, GlobQuery, MetadataQuery, ImpactQuery, PathQuery, FlowQuery, StackQuery, AuditQuery, CheckLayersQuery, LayersOfQuery, FindImplementsQuery, FindDecoratedQuery, EnforceQuery, StatsQuery
from .compiler import compile_get, compile_search, compile_glob, compile_metadata, compile_impact, compile_path, compile_flow, compile_stack, compile_audit, compile_check_layers, compile_layers_of, compile_find_implements, compile_find_decorated, compile_enforce, compile_stats

logger = logging.getLogger(__name__)

def _truncate_bodies_in_result(data, expand_body: bool = False):
    """
    Recursively scans the returned dictionary/list. If expand_body is False,
    any 'body' key is replaced with 'body_preview' truncated to 150 characters.
    Also handles 'extra_json' which is a JSON string from the Rust engine.
    """
    if isinstance(data, dict):
        # Handle extra_json (stored as JSON string by Rust engine)
        if "extra_json" in data and not expand_body:
            ej = data["extra_json"]
            if isinstance(ej, str):
                try:
                    import json
                    parsed = json.loads(ej)
                    if isinstance(parsed, dict) and "body" in parsed:
                        body = parsed["body"]
                        if isinstance(body, str) and len(body) > 150:
                            parsed["body"] = body[:150] + "..."
                        data["extra_json"] = json.dumps(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass

        if "body" in data and not expand_body:
            body = data["body"]
            if isinstance(body, str):
                if len(body) > 150:
                    data["body_preview"] = body[:150] + "..."
                else:
                    data["body_preview"] = body
            else:
                data["body_preview"] = str(body)
            del data["body"]
        for k, v in list(data.items()):
            _truncate_bodies_in_result(v, expand_body)
    elif isinstance(data, list):
        for item in data:
            _truncate_bodies_in_result(item, expand_body)


def _annotate_index_stale(res: dict, client) -> None:
    """Surface index staleness on query meta when the persisted graph was built
    under a different indexing configuration (e.g. language adapters changed
    without a rescan), so callers never silently trust outdated data."""
    if not isinstance(res, dict) or not isinstance(res.get("meta"), dict):
        return
    try:
        if hasattr(client, "is_index_stale"):
            stale = bool(client.is_index_stale())
        elif hasattr(client, "client") and hasattr(client.client, "is_index_stale"):
            stale = bool(client.client.is_index_stale())
        else:
            return
    except Exception:
        return
    res["meta"]["index_stale"] = stale


def query(client: object, raw: str, expand_body: bool = False) -> dict:
    """
    Unified query interface. Accepts a database client and a DSL string.
    Returns a structured dict that can be serialized to YAML.

    Parameters
    ----------
    client : object
        An EngramClient-like object.
    raw : str
        The query DSL string.
    expand_body : bool
        If True, returns full 'body' in results. If False, returns 'body_preview'.

    Returns
    -------
    dict
        Structured result with ok/error, query_type, and results.
    """
    try:
        parsed = parse_query(raw)
    except Exception as e:
        try:
            err = f"Parse error: {e}"
        except Exception:
            err = f"Parse error: {type(e).__name__}"
        # Client-side syntax mistakes are normal tool-input errors, not server
        # faults: log one concise line (full detail goes back in the response).
        logger.warning("Rejected malformed query (%s): %.200r", type(e).__name__, raw)
        return {"ok": False, "error": err}

    try:
        if isinstance(parsed, GetQuery):
            res = compile_get(client, parsed)
        elif isinstance(parsed, SearchQuery):
            res = compile_search(client, parsed)
        elif isinstance(parsed, GlobQuery):
            res = compile_glob(client, parsed)
        elif isinstance(parsed, MetadataQuery):
            res = compile_metadata(client, parsed)
        elif isinstance(parsed, ImpactQuery):
            res = compile_impact(client, parsed)
        elif isinstance(parsed, PathQuery):
            res = compile_path(client, parsed)
        elif isinstance(parsed, FlowQuery):
            res = compile_flow(client, parsed)
        elif isinstance(parsed, StackQuery):
            res = compile_stack(client, parsed)
        elif isinstance(parsed, AuditQuery):
            res = compile_audit(client, parsed)
        elif isinstance(parsed, CheckLayersQuery):
            res = compile_check_layers(client, parsed)
        elif isinstance(parsed, LayersOfQuery):
            res = compile_layers_of(client, parsed)
        elif isinstance(parsed, FindImplementsQuery):
            res = compile_find_implements(client, parsed)
        elif isinstance(parsed, FindDecoratedQuery):
            res = compile_find_decorated(client, parsed)
        elif isinstance(parsed, EnforceQuery):
            res = compile_enforce(client, parsed)
        elif isinstance(parsed, StatsQuery):
            res = compile_stats(client, parsed)
        else:
            return {"ok": False, "error": f"Unknown query type: {type(parsed).__name__}"}

        _annotate_index_stale(res, client)
        _truncate_bodies_in_result(res, expand_body)
        return res
    except Exception as e:
        logger.exception(f"Failed to compile query: {raw!r}")
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception(f"Failed to execute query: {raw!r}")
        return {"ok": False, "error": f"Execution error: {e}"}
