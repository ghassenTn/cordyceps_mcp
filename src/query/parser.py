"""
Query DSL Parser — transforms raw query strings into structured Query objects.
Uses Lark LALR with the grammar defined in grammar.lark.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

from lark import Lark, Transformer, Token

# Sentinel for LIMIT ALL / LIMIT * — return everything from the offset.
UNLIMITED = -1

__all__ = [
    "Query", "GetQuery", "MetadataQuery", "ImpactQuery", "PathQuery",
    "CheckLayersQuery", "LayersOfQuery", "FindImplementsQuery", "FindDecoratedQuery", "EnforceQuery",
    "Condition", "parse_query",
]

# ── AST Node Types ──

@dataclass
class Condition:
    field: str
    operator: str
    value: str | int | float | bool


@dataclass
class AndExpr:
    left: BoolExpr
    right: BoolExpr


@dataclass
class OrExpr:
    left: BoolExpr
    right: BoolExpr


@dataclass
class NotExpr:
    expr: BoolExpr


BoolExpr = Condition | AndExpr | OrExpr | NotExpr


def _extract_flat_conditions(expr: BoolExpr, acc: list[Condition]):
    """Recursively collect Condition objects from AST for backwards compatibility."""
    if isinstance(expr, Condition):
        acc.append(expr)
    elif isinstance(expr, AndExpr):
        _extract_flat_conditions(expr.left, acc)
        _extract_flat_conditions(expr.right, acc)
    elif isinstance(expr, NotExpr):
        _extract_flat_conditions(expr.expr, acc)
    elif isinstance(expr, OrExpr):
        _extract_flat_conditions(expr.left, acc)
        _extract_flat_conditions(expr.right, acc)


@dataclass
class GetQuery:
    type_filter: str | None = None
    projection: list[str] | None = None
    distinct: bool = False
    order_by: str | None = None
    order_dir: str = "asc"
    where_expr: BoolExpr | None = None
    conditions: list[Condition] = field(default_factory=list)
    graph_op: str | None = None
    limit: int | None = None
    offset: int = 0
    depth: int = 0
    group_by: str = "file_path"
    group_by_explicit: bool = False


_REGEX_META = set("()|*+?[]{}^$\\")


def _looks_like_regex(pattern: str) -> bool:
    """Heuristic: pattern contains regex metacharacters beyond simple wildcards."""
    for ch in _REGEX_META:
        if ch in pattern:
            return True
    return False


def _parse_search_pattern(raw_pattern: str) -> tuple[str, bool, str]:
    """
    Parses raw_pattern. If it starts and ends with '/' (with optional flags after the closing '/'),
    treats it as a regex pattern. Returns (pattern, is_regex, flags).
    Otherwise auto-detects regex by metacharacters.
    """
    raw_pattern = raw_pattern.strip()
    if raw_pattern.startswith("/") and "/" in raw_pattern[1:]:
        last_slash_idx = raw_pattern.rfind("/")
        if last_slash_idx > 0:
            pattern = raw_pattern[1:last_slash_idx].replace(r"\/", "/")
            flags = raw_pattern[last_slash_idx + 1 :]
            return pattern, True, flags
    if _looks_like_regex(raw_pattern):
        return raw_pattern, True, ""
    return raw_pattern, False, ""


@dataclass
class SearchQuery:
    patterns: list[str] = field(default_factory=list)
    target_type: str = "all"
    scope: str | None = None  # 'IN <target>' specifier, if explicitly given (e.g. 'all', 'route')
    is_regex: bool = False
    flags: str = ""
    bodies_only: bool = False
    where_expr: BoolExpr | None = None
    conditions: list[Condition] = field(default_factory=list)
    limit: int | None = 20
    offset: int = 0
    order_by: str | None = None
    order_dir: str = "asc"

    @property
    def pattern(self) -> str:
        return self.patterns[0] if self.patterns else ""


@dataclass
class GlobQuery:
    pattern: str
    where_expr: BoolExpr | None = None
    conditions: list[Condition] = field(default_factory=list)
    limit: int | None = 20
    offset: int = 0
    order_by: str | None = None
    order_dir: str = "asc"


@dataclass
class MetadataQuery:
    node_id: str


@dataclass
class ImpactQuery:
    node_id: str
    direction: str = "callers"
    depth: int = 0
    mode: str = "detailed"


@dataclass
class PathQuery:
    start_node: str
    end_node: str


@dataclass
class FlowQuery:
    start_node: str
    depth: int = 3
    route_url: str = ""
    filter_type: str = ""


@dataclass
class StackQuery:
    api_endpoint: str


@dataclass
class AuditQuery:
    module: str = "sales"


@dataclass
class CheckLayersQuery:
    layer: str
    against: str


@dataclass
class LayersOfQuery:
    layer: str


@dataclass
class FindImplementsQuery:
    interface: str
    target_type: str | None = None


@dataclass
class FindDecoratedQuery:
    decorator: str
    target_type: str | None = None
    where_expr: BoolExpr | None = None
    conditions: list[Condition] = field(default_factory=list)
    limit: int | None = None
    offset: int = 0


@dataclass
class EnforceQuery:
    rule_str: str
    scope: str | None = None


@dataclass
class StatsQuery:
    path: str


Query = GetQuery | MetadataQuery | ImpactQuery | PathQuery | StatsQuery


# ── Lark Transformer ──

class _QueryTransformer(Transformer):
    # ── rules ──

    def query(self, items):
        return items[0]

    def agg_func(self, items):
        for item in items:
            if isinstance(item, Token):
                return item.value.upper()
            if isinstance(item, str):
                return item.upper()
        return ""

    def proj_agg(self, items):
        func = ""
        arg = "*"
        for item in items:
            if isinstance(item, Token):
                if item.type == "STAR":
                    arg = "*"
            elif isinstance(item, str):
                if item.upper() in ("SUM", "COUNT", "AVG", "MIN", "MAX"):
                    func = item.upper()
                else:
                    arg = item.lower()
        return f"{func}({arg})"

    def proj_field(self, items):
        for item in items:
            if isinstance(item, Token):
                return item.value
            if isinstance(item, str):
                return item
        return ""

    def proj_items(self, items):
        fields = [i for i in items if isinstance(i, str) and not isinstance(i, Token)]
        return ("projection", fields)

    def proj_count(self, items):
        return ("projection", ["count(*)"])

    def proj_all(self, items):
        return ("projection", None)

    def projection_fields(self, items):
        fields = [i for i in items if isinstance(i, str) and not isinstance(i, Token)]
        return ("projection", fields)

    def projection(self, items):
        for i in items:
            if isinstance(i, tuple) and i[0] == "projection":
                return i
        return ("projection", items)

    def field_name(self, items):
        token = items[0]
        return token.value if isinstance(token, Token) else str(token)

    def order_clause(self, items):
        field = None
        direction = "asc"
        for item in items:
            if isinstance(item, str):
                if item.lower() in ("asc", "desc"):
                    direction = item.lower()
                else:
                    field = item
            elif isinstance(item, Token):
                val = item.value.lower()
                if val in ("asc", "desc"):
                    direction = val
                elif item.type not in ("ORDER_KW", "BY_KW"):
                    field = item.value
        return ("order_by", (field, direction))

    def limit_clause(self, items):
        # LIMIT ALL / LIMIT * → unlimited (UNLIMITED sentinel); LIMIT n → n; LIMIT n, m → (n, offset m)
        if any(isinstance(i, Token) and i.type in ("ALL_KW", "STAR") for i in items):
            return ("limit", UNLIMITED)
        nums = [i for i in items if isinstance(i, int)]
        if len(nums) == 2:
            return ("limit_offset", (nums[1], nums[0]))  # (limit, offset)
        elif len(nums) == 1:
            return ("limit", nums[0])
        return ("limit", 20)

    def offset_clause(self, items):
        nums = [i for i in items if isinstance(i, int)]
        return ("offset", nums[0] if nums else 0)

    def range_clause(self, items):
        nums = [i for i in items if isinstance(i, int)]
        if len(nums) == 2:
            start, end = nums[0], nums[1]
            offset = max(0, start)
            limit = max(1, end - start)
            return ("range", (limit, offset))
        return ("offset", 0)

    def depth_clause(self, items):
        return ("depth", items[1])

    def group_by_field(self, items):
        for item in items:
            if isinstance(item, Token):
                return item.value.lower()
            if isinstance(item, str):
                return item.lower()
        return "file_path"

    def group_by_clause(self, items):
        fields = []
        for item in items:
            if isinstance(item, Token):
                if item.type not in ("GROUP_KW", "BY_KW", "COMMA"):
                    fields.append(item.value.lower())
            elif isinstance(item, str):
                fields.append(item.lower())
        return ("group_by", fields[0] if fields else "file_path")

    def with_clause(self, items):
        return ("graph_op", items[1])

    def or_expr(self, children):
        exprs = [c for c in children if not (isinstance(c, Token) and c.type == "OR_KW")]
        if len(exprs) == 1:
            return exprs[0]
        res = exprs[0]
        for c in exprs[1:]:
            res = OrExpr(res, c)
        return res

    def and_expr(self, children):
        exprs = [c for c in children if not (isinstance(c, Token) and c.type == "AND_KW")]
        if len(exprs) == 1:
            return exprs[0]
        res = exprs[0]
        for c in exprs[1:]:
            res = AndExpr(res, c)
        return res

    def not_expr(self, children):
        exprs = [c for c in children if not (isinstance(c, Token) and c.type == "NOT_KW")]
        return NotExpr(exprs[0])

    def get_query(self, items):
        q = GetQuery()
        has_range = False
        for item in items:
            if isinstance(item, Token):
                if item.type == "DISTINCT_KW":
                    q.distinct = True
                continue
            if isinstance(item, (Condition, AndExpr, OrExpr, NotExpr)):
                q.where_expr = item
                _extract_flat_conditions(item, q.conditions)
            elif isinstance(item, tuple) and len(item) == 2:
                key, val = item
                if key == "range":
                    lim_val, off_val = val
                    q.limit = lim_val
                    q.offset = off_val
                    has_range = True
                elif key == "limit_offset" and not has_range:
                    lim_val, off_val = val
                    q.limit = lim_val
                    q.offset = off_val
                elif key == "limit" and not has_range:
                    q.limit = val
                elif key == "offset" and not has_range:
                    q.offset = val
                elif key == "order_by":
                    f_name, d_name = val
                    q.order_by = f_name
                    q.order_dir = d_name
                elif key == "projection":
                    q.projection = val
                elif key == "depth":
                    q.depth = val
                elif key == "group_by":
                    q.group_by = val
                    q.group_by_explicit = True
                elif key == "graph_op":
                    q.graph_op = val
            elif isinstance(item, str):
                lower = item.lower()
                if lower in ("function", "class", "file", "folder", "middleware", "route", "module", "package", "declaration", "all", "*"):
                    if lower not in ("all", "*"):
                        q.type_filter = lower
        return q

    def search_pattern(self, items):
        for item in items:
            if isinstance(item, str):
                return item.strip("\"'")
        return ""

    def search_query(self, items):
        raw_patterns = []
        target_type = "all"
        scope = None
        where_expr = None
        conditions = []
        limit = 20
        offset = 0
        order_by = None
        order_dir = "asc"
        has_range = False
        after_in = False
        force_regex = False
        bodies_only = False

        for item in items:
            if isinstance(item, Token):
                if item.type == "REGEX_KW":
                    force_regex = True
                if item.type == "IN_KW":
                    after_in = True
                if item.type == "BODIES_KW":
                    bodies_only = True
                if item.type == "STAR" and after_in:
                    target_type = "all"
                    scope = "all"
                    after_in = False
                continue
            if isinstance(item, (Condition, AndExpr, OrExpr, NotExpr)):
                where_expr = item
                _extract_flat_conditions(item, conditions)
            elif isinstance(item, tuple) and len(item) == 2:
                key, val = item
                if key == "range":
                    lim_val, off_val = val
                    limit = lim_val
                    offset = off_val
                    has_range = True
                elif key == "limit_offset" and not has_range:
                    lim_val, off_val = val
                    limit = lim_val
                    offset = off_val
                elif key == "limit" and not has_range:
                    limit = val
                elif key == "offset" and not has_range:
                    offset = val
                elif key == "order_by":
                    order_by, order_dir = val
            elif isinstance(item, str):
                if after_in:
                    target_type = item.lower()
                    scope = target_type
                else:
                    raw_patterns.append(item)

        if not raw_patterns:
            raise ValueError("SEARCH requires at least one search pattern")

        patterns = []
        is_regex = False
        flags = ""
        for raw in raw_patterns:
            if force_regex:
                patterns.append(raw.strip("\"'"))
                is_regex = True
            else:
                pat, ire, fl = _parse_search_pattern(raw)
                patterns.append(pat)
                if ire:
                    is_regex = True
                if fl:
                    flags = fl

        return SearchQuery(
            patterns=patterns,
            target_type=target_type,
            scope=scope,
            is_regex=is_regex,
            flags=flags,
            bodies_only=bodies_only,
            where_expr=where_expr,
            conditions=conditions,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir
        )

    def glob_query(self, items):
        pattern = ""
        where_expr = None
        conditions = []
        limit = 20
        offset = 0
        order_by = None
        order_dir = "asc"
        has_range = False

        non_kw_items = []
        for item in items:
            if isinstance(item, Token) and item.type == "GLOB_KW":
                continue
            if isinstance(item, (Condition, AndExpr, OrExpr, NotExpr)):
                where_expr = item
                _extract_flat_conditions(item, conditions)
            elif isinstance(item, tuple) and len(item) == 2:
                key, val = item
                if key == "range":
                    lim_val, off_val = val
                    limit = lim_val
                    offset = off_val
                    has_range = True
                elif key == "limit_offset" and not has_range:
                    lim_val, off_val = val
                    limit = lim_val
                    offset = off_val
                elif key == "limit" and not has_range:
                    limit = val
                elif key == "offset" and not has_range:
                    offset = val
                elif key == "order_by":
                    order_by, order_dir = val
            elif isinstance(item, str):
                non_kw_items.append(item)

        if non_kw_items:
            pattern = non_kw_items[0].strip("\"'")

        return GlobQuery(
            pattern=pattern,
            where_expr=where_expr,
            conditions=conditions,
            limit=limit,
            offset=offset,
            order_by=order_by,
            order_dir=order_dir
        )

    def REGEX_PATTERN(self, token):
        return token.value

    def metadata_query(self, items):
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                return MetadataQuery(node_id=item.strip("'\""))
        raise ValueError("Missing node_id in METADATA query")

    def impact_mode(self, items):
        return str(items[0]).lower() if items else "detailed"

    def impact_query(self, items):
        node_id = None
        direction = "callers"
        depth = 0
        mode = "detailed"
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                node_id = item.strip("'\"")
            elif isinstance(item, str) and item.lower() == "callees":
                direction = "callees"
            elif isinstance(item, int):
                depth = item
            elif isinstance(item, str) and item.lower() in ("count", "summary", "detailed"):
                mode = item.lower()
        if node_id is None:
            raise ValueError("Missing node_id in IMPACT query")
        return ImpactQuery(node_id=node_id, direction=direction,
                           depth=depth, mode=mode)

    def path_query(self, items):
        start_node = None
        end_node = None
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                if start_node is None:
                    start_node = item.strip("'\"")
                else:
                    end_node = item.strip("'\"")
        if start_node is None or end_node is None:
            raise ValueError("Missing FROM or TO in PATH query")
        return PathQuery(start_node=start_node, end_node=end_node)

    def flow_query(self, items):
        start_node = ""
        depth = 3
        route_url = ""
        filter_type = ""

        # Detect THROUGH variant by checking for THROUGH_KW token
        has_through = any(
            isinstance(item, Token) and item.type == "THROUGH_KW"
            for item in items
        )

        if has_through:
            for item in items:
                if isinstance(item, Token):
                    ttype = item.type
                    if ttype == "THROUGH_KW":
                        continue
                    if ttype == "IDENTIFIER":
                        filter_type = item.value.lower()
                        continue
                    if ttype in ("FLOW_KW", "FROM_KW", "DEPTH_KW"):
                        continue
                if isinstance(item, str) and item.startswith(("'", '"')):
                    route_url = item.strip("'\"")
                elif isinstance(item, tuple) and len(item) == 2 and item[0] == "depth":
                    depth = item[1]
                elif isinstance(item, int):
                    depth = item
            return FlowQuery(start_node=start_node, depth=depth,
                             route_url=route_url, filter_type=filter_type)

        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                start_node = item.strip("'\"")
            elif isinstance(item, str) and start_node == "":
                start_node = item.strip("'\"")
            elif isinstance(item, tuple) and len(item) == 2 and item[0] == "depth":
                depth = item[1]
            elif isinstance(item, int):
                depth = item
        return FlowQuery(start_node=start_node, depth=depth)

    def stack_query(self, items):
        api_endpoint = ""
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                api_endpoint = item.strip("'\"")
            elif isinstance(item, str) and api_endpoint == "":
                api_endpoint = item.strip("'\"")
        return StackQuery(api_endpoint=api_endpoint)

    def audit_query(self, items):
        module = "sales"
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                module = item.strip("'\"")
            elif isinstance(item, str) and item.lower() not in ("audit", "tenant", "isolation", "for", "of"):
                module = item.strip("'\"")
        return AuditQuery(module=module)

    def check_layers_query(self, items):
        layer = ""
        against = ""
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                val = item.strip("'\"")
                if not layer:
                    layer = val
                else:
                    against = val
        if not layer or not against:
            raise ValueError("CHECK LAYERS requires two paths: layer and AGAINST")
        return CheckLayersQuery(layer=layer, against=against)

    def layers_of_query(self, items):
        layer = ""
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                layer = item.strip("'\"")
        if not layer:
            raise ValueError("LAYERS OF requires a layer path")
        return LayersOfQuery(layer=layer)

    def find_implements_query(self, items):
        interface = ""
        target_type = None
        for item in items:
            if isinstance(item, str) and item.startswith(("'", '"')):
                interface = item.strip("'\"")
            elif isinstance(item, str) and item.lower() in ("function", "class", "all", "*"):
                target_type = item.lower()
        if not interface:
            raise ValueError("FIND IMPLEMENTS requires an interface name")
        return FindImplementsQuery(interface=interface, target_type=target_type)

    def find_decorated_query(self, items):
        decorator = ""
        target_type = None
        where_expr = None
        conditions = []
        limit = None
        offset = 0
        has_range = False
        non_kw_items = []
        for item in items:
            if isinstance(item, Token) and item.type in ("FIND_KW", "DECORATED_KW", "WITH_KW", "IN_KW", "WHERE_KW"):
                continue
            if isinstance(item, (Condition, AndExpr, OrExpr, NotExpr)):
                where_expr = item
                _extract_flat_conditions(item, conditions)
                continue
            if isinstance(item, tuple) and len(item) == 2:
                key, val = item
                if key == "range":
                    lim_val, off_val = val
                    limit = lim_val
                    offset = off_val
                    has_range = True
                elif key == "limit_offset" and not has_range:
                    limit, offset = val
                elif key == "limit" and not has_range:
                    limit = val
                elif key == "offset" and not has_range:
                    offset = val
                continue
            if isinstance(item, str):
                lower = item.lower()
                if lower in ("function", "class", "all", "*"):
                    target_type = lower
                else:
                    non_kw_items.append(item)
        if non_kw_items:
            decorator = non_kw_items[0].strip("'\"")
        if not decorator:
            raise ValueError("FIND DECORATED WITH requires a decorator name")
        return FindDecoratedQuery(decorator=decorator, target_type=target_type,
                                  limit=limit, offset=offset,
                                  where_expr=where_expr, conditions=conditions)

    def enforce_query(self, items):
        rule_str = ""
        scope = None
        for item in items:
            if isinstance(item, Token) and item.type in ("ENFORCE_KW", "IN_KW"):
                continue
            if isinstance(item, str) and item.startswith(("'", '"')):
                val = item.strip("'\"")
                if not rule_str:
                    rule_str = val
                else:
                    scope = val
        if not rule_str:
            raise ValueError("ENFORCE requires a rule string")
        return EnforceQuery(rule_str=rule_str, scope=scope)

    def stats_query(self, items):
        path = ""
        for item in items:
            if isinstance(item, Token) and item.type in ("STATS_KW", "FOR_KW", "OF_KW"):
                continue
            if isinstance(item, str) and item.startswith(("'", '"')):
                path = item.strip("'\"")
        if not path:
            raise ValueError("STATS requires a target path")
        return StatsQuery(path=path)

    def node_type(self, items):
        token = items[0]
        return token.value.lower() if isinstance(token, Token) else str(token).lower()

    def list_value(self, items):
        return [i.strip("'\"") for i in items if isinstance(i, str)]

    def field_condition(self, items):
        f = items[0]
        op = items[1]
        v = items[2]
        field_val = f.value.lower() if isinstance(f, Token) else str(f).lower()
        op_val = op.value.upper() if isinstance(op, Token) else str(op).upper()
        import re
        op_val = re.sub(r'\s+', ' ', op_val)

        if isinstance(v, list):
            val = v
        elif isinstance(v, Token) and v.type == "BOOLEAN":
            val = (v.value.lower() == "true")
        elif isinstance(v, Token) and v.type == "INT":
            val = int(v.value)
        elif isinstance(v, Token) and v.type == "FLOAT":
            val = float(v.value)
        elif isinstance(v, bool):
            val = v
        elif isinstance(v, (int, float)):
            val = v
        elif isinstance(v, str):
            clean = v.strip("'\"")
            if clean.lower() == "true":
                val = True
            elif clean.lower() == "false":
                val = False
            elif clean.isdigit():
                val = int(clean)
            else:
                try:
                    val = float(clean)
                except ValueError:
                    val = clean
        else:
            val = str(v).strip("'\"")

        return Condition(field=field_val, operator=op_val, value=val)

    def lit_cond(self, items):
        """Constant comparison like WHERE 1==1 / 1!=0 / true==true — a truthy filter."""
        vals = []
        op = "=="
        for item in items:
            if isinstance(item, Token):
                if item.type == "BOOLEAN":
                    vals.append(item.value.lower() == "true")
                elif item.type in ("OP_EQ", "OP_NE"):
                    op = item.value
            elif isinstance(item, bool):
                vals.append(item)
            elif isinstance(item, (int, float)):
                vals.append(item)
        if len(vals) < 2:
            return Condition(field="__constant__", operator="==", value=True)
        truthy = (vals[0] == vals[1]) if op in ("==", "=") else (vals[0] != vals[1])
        return Condition(field="__constant__", operator="==", value=truthy)

    def lit_bool(self, items):
        """Bare boolean like WHERE true / WHERE false."""
        t = items[0]
        return Condition(field="__constant__", operator="==", value=(t.value.lower() == "true"))

    def cond_literal(self, items):
        return items[0]

    def field(self, items):
        return items[0]

    def operator(self, items):
        return items[0]

    def value(self, items):
        return items[0]

    def graph_op(self, items):
        t = items[0]
        return t.value.lower() if isinstance(t, Token) else str(t).lower()

    def direction(self, items):
        t = items[0]
        return t.value.lower() if isinstance(t, Token) else str(t).lower()

    # ── terminal handlers ──

    def QSTRING(self, token):
        return token.value

    def INT(self, token):
        return int(token.value)

    def TYPE(self, token):
        raw = token.value.lower()
        mapping = {
            "functions": "function", "function": "function",
            "classes": "class", "class": "class",
            "files": "file", "file": "file",
            "folders": "folder", "folder": "folder",
            "middlewares": "middleware", "middleware": "middleware",
            "routes": "route", "route": "route",
            "modules": "module", "module": "module",
            "packages": "package", "package": "package",
            "declarations": "declaration", "declaration": "declaration",
        }
        return mapping.get(raw, raw)

    def __default_token__(self, token):
        return token

    def __default__(self, data, children, meta):
        return children


# ── Public API ──

_GRAMMAR_PATH = Path(__file__).parent / "grammar.lark"
_parser: Lark | None = None


def _get_parser() -> Lark:
    global _parser
    if _parser is None:
        grammar = _GRAMMAR_PATH.read_text()
        _parser = Lark(grammar, parser="lalr", transformer=_QueryTransformer(), maybe_placeholders=False)
    return _parser


def parse_query(raw: str) -> Query:
    """Parse a raw query string into a structured Query object."""
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty query string")
    parser = _get_parser()
    result = parser.parse(raw)
    if result is None:
        raise ValueError(f"Could not parse query: {raw!r}")
    return result
