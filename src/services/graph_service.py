"""
Graph Service — Discovery & Analysis tools.
Powered by EngramDB's CSR engine instead of KuzuDB Cypher.
"""
import os
import logging
from .yaml_utils import to_yaml

logger = logging.getLogger(__name__)


def _enclosing_class(node_id: str, symbol_name: str) -> str:
    """Extract the immediately enclosing class name from a node id like
    'file.py:Outer.Inner.method' -> 'Inner' ('' for module-level symbols)."""
    if not node_id or ":" not in node_id:
        return ""
    rest = node_id.split(":", 1)[1]
    if symbol_name and rest.endswith(symbol_name):
        rest = rest[: -len(symbol_name)].rstrip(".")
    segments = [s for s in rest.split(".") if s]
    return segments[-1] if segments else ""


def _analyze_architecture(node: dict, node_id: str = "") -> dict:
    """
    Analyzes the node and returns a dict with architectural validation and metadata.
    """
    file_path = node.get("file_path", "") or node.get("file", "") or ""
    # Normalize paths
    file_path = file_path.replace('\\', '/')
    
    # Determine the module: GSM layout first, then fall back to the top-level dir
    module = None
    if "src/modules/" in file_path:
        parts = file_path.split("src/modules/")
        if len(parts) > 1:
            module = parts[1].split("/")[0]
    elif "frontend/" in file_path:
        module = "frontend"
    elif "a_main_app" in file_path:
        module = "a_main_app"
    if not module:
        stripped = file_path.strip("/")
        module = stripped.split("/")[0] if stripped else None

    name = node.get("name", "")
    container = _enclosing_class(node_id, name)
    arch_role = _classify_arch_role(file_path, name, container)

    # Compile the workspace metadata dictionary
    meta = {
        "module": module,
        "architecture_role": arch_role,
    }
    return meta


def _classify_arch_role(file_path: str, symbol_name: str = "", container_name: str = "") -> str:
    """Generic, case-insensitive architectural-role classification.

    Uses directory hints first, then common filename stems/suffixes, then a
    symbol-name keyword fallback, and finally the enclosing class name so that
    methods like `_now` inside a `LogWriter` class inherit its Utility role.
    """
    lower_path = str(file_path).replace("\\", "/").lower()
    file_name = os.path.basename(lower_path)
    stem = file_name.rsplit(".", 1)[0] if file_name else ""

    # Directory hints
    if "tests" in lower_path or stem.startswith("test_"):
        return "Test"
    if "frontend/" in lower_path:
        if file_name.startswith("use-") or "hooks" in lower_path:
            return "Frontend Hook"
        if file_name.endswith((".tsx", ".ts")) or "components" in lower_path:
            return "Frontend Component"
        return "Frontend"
    if any(p in lower_path for p in ("/models/", "/entities/", "/model/", "/entity/")):
        return "Model"
    if any(p in lower_path for p in ("/schemas/", "/serializers/", "/dto/")):
        return "Schema"
    if any(p in lower_path for p in ("/selectors/", "/repositories/", "/repos/", "/repository/")):
        return "Selector"
    if any(p in lower_path for p in ("/services/", "/handlers/", "/usecases/", "/workflows/", "/engines/", "/processors/", "/builders/", "/strategies/")):
        return "Service"
    if any(p in lower_path for p in ("/api/", "/controllers/", "/routes/", "/endpoints/", "/brokers/", "/clients/", "/views/")):
        return "API Router"

    # Filename stems (case-insensitive)
    if stem in ("model", "models", "entity", "entities"):
        return "Model"
    if stem in ("schema", "schemas", "serializer", "serializers", "dto"):
        return "Schema"
    if stem in ("selector", "selectors", "query", "queries"):
        return "Selector"
    if stem in ("utils", "helpers", "helper", "common", "shared", "constants", "config"):
        return "Utility"
    if stem in ("calibration", "validation", "cross_validation", "splitter", "splitters",
                "metrics", "metric", "indicators", "signals", "signal_processing",
                "features", "feature_engineering", "preprocessing", "transforms",
                "transform", "scoring", "dataset", "datasets", "samplers", "kernels") or stem.endswith(
                    ("_features", "_indicators", "_signals", "_metrics", "_validation",
                     "_calibration", "_preprocessing")):
        return "Service"
    if stem in ("persistence", "database", "db", "store", "stores", "storage") or stem.endswith(
            ("_store", "_persistence", "_repo", "_repository")):
        return "Data Access"
    if stem in ("repository", "repositories", "repo", "repos"):
        return "Data Access"
    if stem in ("service", "services", "handler", "handlers", "usecase", "usecases",
                "workflow", "workflows", "engine", "engines", "processor", "processors",
                "builder", "builders", "factory", "factories", "strategy", "strategies",
                "manager", "managers", "classifier", "broker", "brokers", "learner",
                "estimator", "pipeline", "trainer") or stem.endswith(
                    ("_service", "_handler", "_engine", "_processor", "_builder",
                     "_factory", "_strategy", "_strategies", "_manager", "_classifier",
                     "_broker", "_learner", "_estimator", "_pipeline", "_model",
                     "_utils", "_helpers")):
        return "Service"
    if stem in ("api", "view", "views", "controller", "controllers", "route", "routes",
                "endpoint", "endpoints") or stem.endswith(("_api", "_controller", "_routes", "_endpoints", "_views")):
        return "API Router"
    if stem in ("main", "app", "server", "application", "manage", "cli", "entrypoint") or stem.endswith(("_app", "_main")):
        return "Entry Point"

    # Symbol-name keyword fallback: covers helpers in generic files whose names
    # carry the layer signal (ML/data helpers, API/view handlers, UI widgets).
    if symbol_name:
        sn = symbol_name.lower().replace("_", " ")
        if any(k in sn for k in (
                "model", "entity", "schema", "dto", "aggregate", "record")):
            return "Model"
        if any(k in sn for k in (
                "api", "controller", "route", "endpoint", "webhook",
                "broker", "listener", "view", "handler")):
            return "API Router"
        if any(k in sn for k in (
                "tooltip", "widget", "dialog", "panel", "frame", "toolbar",
                "popup", "screen", "page", "component")):
            return "UI Component"
        if any(k in sn for k in (
                "feature", "signal", "indicator", "metric", "calibrat", "validat",
                "kfold", "cv split", "split", "sampl", "estim", "predict", "train",
                "fit ", "infer", "score", "preprocess", "normalize", "standardi",
                "transform", "rolling", "backtest", "regime", "label", "target")):
            return "Service"
        if any(k in sn for k in (
                "service", "handler", "usecase", "workflow", "command",
                "facade", "manager", "coordinator", "pipeline", "factory")):
            return "Service"
        if any(k in sn for k in (
                "util", "helper", "canonical", "serialize", "deserialize",
                "parse", "convert", "format", "encode", "decode", "mapping",
                "write", "read", "load", "save", "fetch", "config")):
            return "Utility"

    # Enclosing-class fallback: a method inherits its class's role when the
    # method name itself carries no layer signal (e.g. LogWriter._now -> Utility,
    # Tooltip.show_tip -> UI Component).
    if container_name:
        cn = container_name.lower().replace("_", " ")
        if any(k in cn for k in (
                "tooltip", "widget", "dialog", "panel", "frame", "toolbar",
                "popup", "screen", "page", "component")):
            return "UI Component"
        if any(k in cn for k in (
                "log", "logger", "writter", "writer", "util", "helper",
                "config", "manager", "loader", "serializer", "cache")):
            return "Utility"
        if any(k in cn for k in (
                "api", "controller", "route", "endpoint", "webhook", "broker",
                "listener", "handler", "service", "repository", "repo",
                "engine", "processor", "builder", "classifier", "pipeline",
                "model", "schema", "entity", "strategy")):
            return "Model" if cn in ("model", "schema", "entity") else "Service"
    return "Other"


def analyse_impact_data(client, node_id: str, direction: str = "callers", depth: int = 0) -> dict:
    """
    Performs core blast radius analysis with full GSMOS monorepo enrichment.
    """
    meta = client.get_node_meta(node_id)
    if not meta:
        return {"error": "Node not found.", "node_id": node_id}

    if direction == "callees":
        callee_ids = client.get_recursive_callees(node_id, depth)
        impact = {
            "target": node_id,
            "direction": "callees",
            "type": meta.get("type", "Unknown"),
            "defined_in": meta.get("file_path"),
            "depth": depth,
            "affected_nodes": []
        }
        for cid in callee_ids:
            cid_meta = client.get_node_meta(cid)
            if cid_meta:
                node_data = {
                    "node_id": cid,
                    "type": cid_meta.get("type", "Unknown"),
                    "name": cid_meta.get("name", ""),
                    "file": cid_meta.get("file_path", ""),
                    "defined_at_lines": cid_meta.get("lines", {})
                }
                node_data["architecture"] = _analyze_architecture(node_data, cid)
                impact["affected_nodes"].append(node_data)
            else:
                impact["affected_nodes"].append({"node_id": cid, "type": "Unknown"})
        return {"ok": True, "impact": impact}

    # direction == "callers" — blast radius BFS
    impact = {
        "target": node_id,
        "direction": "callers",
        "type": meta.get("type", "Unknown"),
        "defined_in": meta.get("file_path"),
        "depth": depth,
        "affected_nodes": []
    }

    affected_ids = client.blast_radius(node_id, depth)
    target_file = meta.get("file_path")

    for aid in affected_ids:
        if aid == node_id:
            continue
        aid_meta = client.get_node_meta(aid)
        if aid_meta:
            node_type = aid_meta.get("type", "Unknown")
            if node_type == "Folder":
                continue
            if node_type == "File" and aid == target_file:
                continue
            node_data = {
                "node_id": aid,
                "type": node_type,
                "name": aid_meta.get("name", ""),
                "file": aid_meta.get("file_path", "")
            }
            if node_type == "Class":
                django_relations = aid_meta.get("django_relations", [])
                if django_relations:
                    node_data["django_relations"] = django_relations
            lines = aid_meta.get("lines", {})
            if lines:
                node_data["defined_at_lines"] = lines
            node_data["architecture"] = _analyze_architecture(node_data, aid)
            impact["affected_nodes"].append(node_data)
        else:
            impact["affected_nodes"].append({"node_id": aid, "type": "Unknown"})

    # Include URL patterns and template refs for the target node itself
    target_meta = client.get_node_meta(node_id)
    url_patterns = target_meta.get("url_patterns", [])
    if url_patterns:
        impact["url_patterns"] = url_patterns
    template_refs = target_meta.get("template_refs", [])
    if template_refs:
        impact["templates"] = template_refs

    # Include API endpoint info for the target
    api_endpoint = target_meta.get("api_endpoint", {})
    if api_endpoint:
        impact["api_endpoint"] = api_endpoint

    # Include HTTP call info (frontend calling backend) for the target
    http_calls = target_meta.get("http_calls", [])
    if http_calls:
        impact["http_calls"] = http_calls

    # Include resolved API dependencies (links from frontend callers to backend endpoints)
    api_deps = target_meta.get("api_dependencies", [])
    if api_deps:
        impact["api_dependencies"] = api_deps

    # For API endpoints, find frontend callers
    if direction == "callers":
        frontend_callers = []
        for aid in affected_ids:
            if aid == node_id:
                continue
            aid_extra = client._extra_meta.get(aid, {})
            aid_calls = aid_extra.get("http_calls", [])
            if aid_calls:
                frontend_callers.append({
                    "node_id": aid,
                    "http_calls": aid_calls
                })
        if frontend_callers:
            impact["frontend_callers"] = frontend_callers

    impact["architecture"] = _analyze_architecture({
        "type": meta.get("type", "Unknown"),
        "name": meta.get("name", ""),
        "file_path": meta.get("file_path", ""),
        "signature": meta.get("signature", ""),
        "docstring": meta.get("docstring", "")
    })

    return {"ok": True, "impact": impact}


def is_framework_noise(node_id: str) -> bool:
    """True if a node is framework/ORM plumbing rather than business logic.

    GSM-OS-specific noise patterns only apply inside the GSM `src/modules/`
    layout; for any other workspace only clear Django-ORM manager chaining
    (e.g. `.objects.`) is treated as noise, so user-defined methods like
    `.save()` are preserved.
    """
    if node_id.startswith("src/modules/"):
        clean_id = node_id.replace("src/modules/", "")
        noise_patterns = [
            "core/models.py:save",
            "core/models.py:_generate_shop_code",
            "core/models.py:User",
            "core/models.py:Shop",
            "core/models.py:TenantAwareModel",
            "ShopMembership",
            "SaaSPayment",
        ]
        for pattern in noise_patterns:
            if pattern in clean_id:
                return True
        if clean_id.endswith(":save") or ".objects." in clean_id:
            return True
        return False
    return ".objects." in node_id


def is_business_only(node_id: str) -> bool:
    """Generic business-layer heuristic: frontend files or well-known backend
    business-layer filenames (services/api/selectors/controllers/handlers/...)."""
    if node_id.startswith("frontend/"):
        return True
    business_filenames = (
        "/services.py:", "/service.py:", "/api.py:", "/controllers.py:",
        "/handlers.py:", "/usecases.py:", "/selectors.py:", "/repositories.py:",
        "/views.py:",
    )
    return any(p in node_id for p in business_filenames)


def get_criticality_prefix(name: str) -> str:
    """Generic visual marker for a node: ⚙️ for private/internal functions.

    Domain-specific criticality keywords are intentionally not hardcoded here so
    the flow rendering stays project-agnostic.
    """
    if name.startswith("_"):
        return "⚙️ "
    return ""


def _resolve_flow_start(db, start_node: str):
    """Resolve a flow start node to a real node id + metadata.

    Accepts exact node ids, `file:name`, `file:Class.method`, and bare names.
    """
    exact = db.client.get_node_meta(start_node)
    if exact:
        return start_node, exact

    file_part = None
    sym = start_node
    if ":" in start_node:
        file_part, _, sym = start_node.rpartition(":")
    last_seg = sym.split(".")[-1]  # Class.method -> method

    for nid, m in db.client.get_all_metadata().items():
        if m.get("type") in ("File", "Folder"):
            continue
        if m.get("name") != last_seg:
            continue
        if file_part and m.get("file_path") != file_part:
            continue
        return nid, m

    # Fallback: search by bare symbol, prefer a non-container match
    results = db.client.search(last_seg)
    for r in results:
        nid = r.get("id", "")
        if not nid:
            continue
        cand = db.client.get_node_meta(nid)
        if cand and cand.get("type") not in ("File", "Folder"):
            return nid, cand
    return None, None


def trace_business_flow(
    start_node: str,
    workflow: str = "Business Flow",
    exclude_framework: bool = True,
    business_only: bool = False,
    max_depth: int = 5,
    show_module_boundaries: bool = True,
    deduplicate_paths: bool = True
) -> str:
    """Traces and visualizes end-to-end business workflows from a starting API or service node.
    
    Traverses call graph descendants, filtering for custom workspace modules and building
    a structured flow path.
    """
    try:
        from src.database import get_graph_db
        db = get_graph_db()

        start_node, meta = _resolve_flow_start(db, start_node)
        if not meta:
            return to_yaml({"error": f"Starting node '{start_node}' not found."})

        traced_nodes = set()
        duplicate_count = 0

        def build_tree(node_id, visited=None, depth=0, parent_module=None):
            nonlocal duplicate_count
            if visited is None:
                visited = set()
            if node_id in visited or depth > max_depth:
                return None
            
            node_meta = db.client.get_node_meta(node_id)
            if not node_meta:
                return None
                
            # Skip file and folder container nodes from business logic tree
            if node_meta.get("type") in ("File", "Folder") or ":" not in node_id:
                return None

            # Exclude framework noise
            if depth > 0 and exclude_framework and is_framework_noise(node_id):
                return None
                
            # Filter for business logic only
            if depth > 0 and business_only and not is_business_only(node_id):
                return None

            visited.add(node_id)
            extra_gsm = _analyze_architecture(node_meta, node_id)
            current_module = extra_gsm.get("module", "")
            if not current_module:
                # Generic workspace: fall back to the top-level directory of the
                # node's file path so module boundaries still render.
                file_path = node_meta.get("file_path", "") or node_meta.get("file", "")
                file_path = str(file_path).replace("\\", "/").strip("/")
                current_module = file_path.split("/")[0] if file_path else ""

            is_duplicate = False
            if deduplicate_paths and node_id in traced_nodes:
                is_duplicate = True
                duplicate_count += 1

            children = []
            if not is_duplicate:
                traced_nodes.add(node_id)
                callees = db.client.get_callees(node_id)
                for callee_id in callees:
                    child_tree = build_tree(callee_id, visited.copy(), depth + 1, current_module)
                    if child_tree:
                        children.append(child_tree)

                # Surface external dead-end calls (stdlib / third-party / attribute
                # chains with no graph node) so leaf nodes don't look empty.
                try:
                    from src.query.compiler import _classify_external_calls
                    external = _classify_external_calls(db.client, dict(node_meta.items()), callees)
                except Exception:
                    external = []
                for ex in external:
                    children.append({
                        "node_id": ex["name"],
                        "name": ex["name"],
                        "file_path": "",
                        "architecture_role": "",
                        "module": current_module,
                        "parent_module": current_module,
                        "children": [],
                        "already_traced": False,
                        "external": ex["kind"],
                    })

            return {
                "node_id": node_id,
                "name": node_meta.get("name", node_id),
                "file_path": node_meta.get("file_path", ""),
                "architecture_role": extra_gsm.get("architecture_role", ""),
                "module": current_module,
                "parent_module": parent_module,
                "children": children,
                "already_traced": is_duplicate
            }
            
        tree = build_tree(start_node)
        if not tree:
            return to_yaml({"error": "Failed to trace any call graph for starting node."})
            
        MODULE_FRIENDLY_NAMES = {
            'sales': 'Sales',
            'comptabilite': 'Accounting',
            'treasury': 'Treasury',
            'inventory': 'Inventory',
            'catalog': 'Catalog',
            'achat': 'Purchases',
            'facilite': 'Facilite',
            'sav': 'SAV',
            'core': 'Auth/Core',
            'reports': 'Reports'
        }
        
        sequence = []
        def get_sequence(t):
            if t.get("external"):
                return
            role = t.get("architecture_role")
            mod = t.get("module")
            friendly = None
            if role == "API Router":
                friendly = "API"
            elif mod in MODULE_FRIENDLY_NAMES:
                friendly = MODULE_FRIENDLY_NAMES[mod]
            elif mod:
                friendly = mod.replace("-", " ").title()
            if friendly and (not sequence or sequence[-1] != friendly):
                sequence.append(friendly)
            for c in t.get("children", []):
                get_sequence(c)
                
        get_sequence(tree)
        flow_seq = " → ".join(sequence)

        # Cross-module marker is anchored to the flow ROOT module so the same node
        # is always rendered consistently, regardless of which path reached it.
        root_module = tree.get("module", "")

        def format_tree(t, prefix="", is_last=True, is_root=False):
            if not t:
                return ""

            external_kind = t.get("external")
            file_name = t['file_path']
            if external_kind:
                node_str = f"⚡ {t['name']} (external · {external_kind})"
            else:
                if file_name.startswith("src/modules/"):
                    file_name = file_name[12:]
                elif file_name.startswith("frontend/"):
                    file_name = file_name[len("frontend/"):]
                elif file_name.startswith("src/"):
                    file_name = file_name[4:]

                node_str = f"{file_name}:{t['name']}"
                if t['architecture_role']:
                    node_str += f" ({t['architecture_role']})"

                # Prepend criticality tag if available
                crit_prefix = get_criticality_prefix(t['name'])
                node_str = crit_prefix + node_str

                # Prepend module boundary if module differs from the flow root
                if show_module_boundaries and root_module and t.get("module") and t["module"] != root_module:
                    friendly_mod = MODULE_FRIENDLY_NAMES.get(t["module"], t["module"].capitalize())
                    node_str = f"🔄 [{friendly_mod}] {node_str}"

            if t.get("already_traced"):
                node_str += " (already traced — see above)"

            if is_root:
                line = f"{node_str}\n"
                new_prefix = ""
            else:
                line = f"{prefix}{'└─ ' if is_last else '├─ '}{node_str}\n"
                new_prefix = prefix + ("   " if is_last else "│  ")

            children_str = ""
            for idx, child in enumerate(t["children"]):
                child_is_last = (idx == len(t["children"]) - 1)
                children_str += format_tree(child, new_prefix, child_is_last, is_root=False)

            return line + children_str

        tree_visualization = format_tree(tree, "", True, is_root=True)
        
        outcome = f"Flow mapped — {len(traced_nodes)} nodes"
        if duplicate_count:
            outcome += f", {duplicate_count} duplicated"
            
        return to_yaml({
            "ok": True,
            "workflow": workflow,
            "sequence": flow_seq,
            "visualization": f"\n{tree_visualization}",
            "outcome": f"Result: {outcome}",
            "nodes_traced": len(traced_nodes),
            "duplicates_traced": duplicate_count,
        })
        
    except Exception as e:
        logger.exception("trace_business_flow failed")
        return to_yaml({"error": str(e)})


def _pick_backend_handler(client, route_id: str) -> str:
    """Pick the handler node for a Route from its callees.

    Prefers Function/Class nodes (actual handlers) and skips structural nodes
    (File, Folder, Route, Middleware, Declaration) that may appear first in the
    edge list. Returns the best handler node id, or '' if none.
    """
    preferred = []
    fallback = None
    for c in client.get_callees(route_id):
        c_meta = client.get_node_meta(c)
        if not c_meta:
            continue
        ctype = c_meta.get("type", "")
        if ctype in ("Function", "Class"):
            preferred.append(c)
        elif ctype in ("File", "Folder", "Route", "Middleware", "Declaration"):
            continue
        elif fallback is None:
            fallback = c
    return preferred[0] if preferred else (fallback or "")


def trace_frontend_backend(api_endpoint: str, include_components: bool = True) -> str:
    """
    Traces the cross-stack flow from frontend components, through hooks,
    to the backend API router, and finally to the backend service layer.
    """
    try:
        from src.database import get_graph_db
        db = get_graph_db()
        from .yaml_utils import to_yaml
        import re
        import os

        # Resolve from code symbols or node IDs first
        node_id = api_endpoint.strip()
        node_meta = None
        if ":" in node_id:
            node_meta = db.client.get_node_meta(node_id)
        if not node_meta:
            symbol_name = node_id.split(":")[-1]
            all_meta = db.client.get_all_metadata()
            for nid, meta in all_meta.items():
                if meta.get("name") == symbol_name and meta.get("type") in ("Function", "Class"):
                    if "api.py" in nid or "views.py" in nid:
                        node_id = nid
                        node_meta = meta
                        break
            else:
                for nid, meta in all_meta.items():
                    if meta.get("name") == symbol_name and meta.get("type") in ("Function", "Class"):
                        node_id = nid
                        node_meta = meta
                        break
        if node_meta:
            extra = db.client._extra_meta.get(node_id, {}) if hasattr(db.client, "_extra_meta") else {}
            api_ep = extra.get("api_endpoint") or node_meta.get("api_endpoint")
            if api_ep and api_ep.get("url"):
                api_endpoint = api_ep["url"]
            else:
                url_patterns = extra.get("url_patterns", [])
                if url_patterns:
                    api_endpoint = url_patterns[0].get("url", "") or url_patterns[0].get("pattern", "")
                else:
                    # Search Route nodes calling this function
                    all_meta = db.client.get_all_metadata()
                    for r_nid, r_meta in all_meta.items():
                        if r_meta.get("type") == "Route":
                            callees = db.client.get_callees(r_nid)
                            if node_id in callees:
                                api_endpoint = r_meta.get("name", "")
                                break

        # 1. Normalize input endpoint
        input_clean = api_endpoint.strip("/").lower()
        if input_clean.startswith("api/"):
            input_clean = input_clean[4:]

        # Helper to match candidate URLs strictly
        def match_endpoint_strict(candidate_url: str) -> bool:
            cand_clean = candidate_url.strip("/").lower()
            if cand_clean.startswith("api/"):
                cand_clean = cand_clean[4:]
                
            # Replace param patterns
            cand_norm = re.sub(r'\{[^}]+\}', '*', cand_clean)
            cand_norm = re.sub(r'<[^>]+>', '*', cand_norm)
            cand_norm = re.sub(r':[a-zA-Z0-9_]+', '*', cand_norm)
            
            input_norm = re.sub(r'[0-9]+', '*', input_clean)
            
            input_segs = [s for s in input_norm.split("/") if s]
            cand_segs = [s for s in cand_norm.split("/") if s]
            
            if len(input_segs) != len(cand_segs):
                return False
                
            for iseg, cseg in zip(input_segs, cand_segs):
                if iseg == '*' or cseg == '*':
                    continue
                if iseg != cseg:
                    return False
            return True

        # Helper to match candidate URLs loosely (fallback)
        def match_endpoint_loose(candidate_url: str) -> bool:
            cand_clean = candidate_url.strip("/").lower()
            if cand_clean.startswith("api/"):
                cand_clean = cand_clean[4:]
                
            # Replace param patterns
            cand_norm = re.sub(r'\{[^}]+\}', '*', cand_clean)
            cand_norm = re.sub(r'<[^>]+>', '*', cand_norm)
            cand_norm = re.sub(r':[a-zA-Z0-9_]+', '*', cand_norm)
            
            input_norm = re.sub(r'[0-9]+', '*', input_clean)
            
            input_segs = [s for s in input_norm.split("/") if s]
            cand_segs = [s for s in cand_norm.split("/") if s]
            
            input_words = [w for w in input_segs if w != '*']
            cand_words = [w for w in cand_segs if w != '*']
            if input_words and all(w in cand_words for w in input_words):
                return True
            return False

        # 2. Find backend handler node & endpoint details
        backend_node = None
        backend_meta = None
        matched_url = None
        matched_method = "GET"  # default

        all_meta = db.client.get_all_metadata()

        # Phase A: Try to find a strict match on Route nodes
        for nid, meta in all_meta.items():
            if meta.get("type") == "Route":
                route_url = meta.get("name", "")
                if route_url and match_endpoint_strict(route_url):
                    handler_id = _pick_backend_handler(db.client, nid)
                    if handler_id:
                        backend_node = handler_id
                        backend_meta = db.client.get_node_meta(backend_node)
                        matched_url = route_url
                        extra_route = db.client._extra_meta.get(nid, {}) if hasattr(db.client, "_extra_meta") else {}
                        methods = extra_route.get("methods", [])
                        matched_method = methods[0].upper() if methods else meta.get("method", "GET").upper()
                        break

        # Phase B: Try to find a strict match on FastAPI/Ninja decorated functions
        if not backend_node:
            for nid, meta in all_meta.items():
                if (nid.startswith("src/modules/") or "views.py" in nid or "api.py" in nid) and meta.get("type") in ("Function", "Class"):
                    api_ep = meta.get("api_endpoint")
                    if api_ep and api_ep.get("url"):
                        if match_endpoint_strict(api_ep["url"]):
                            backend_node = nid
                            backend_meta = meta
                            matched_url = api_ep["url"]
                            if api_ep.get("methods"):
                                matched_method = api_ep["methods"][0].upper()
                            break

        # Phase C: Try to find a loose match on Route nodes
        if not backend_node:
            for nid, meta in all_meta.items():
                if meta.get("type") == "Route":
                    route_url = meta.get("name", "")
                    if route_url and match_endpoint_loose(route_url):
                        handler_id = _pick_backend_handler(db.client, nid)
                        if handler_id:
                            backend_node = handler_id
                            backend_meta = db.client.get_node_meta(backend_node)
                            matched_url = route_url
                            extra_route = db.client._extra_meta.get(nid, {}) if hasattr(db.client, "_extra_meta") else {}
                            methods = extra_route.get("methods", [])
                            matched_method = methods[0].upper() if methods else meta.get("method", "GET").upper()
                            break

        # Phase D: Try to find a loose match on FastAPI/Ninja decorated functions
        if not backend_node:
            for nid, meta in all_meta.items():
                if (nid.startswith("src/modules/") or "views.py" in nid or "api.py" in nid) and meta.get("type") in ("Function", "Class"):
                    api_ep = meta.get("api_endpoint")
                    if api_ep and api_ep.get("url"):
                        if match_endpoint_loose(api_ep["url"]):
                            backend_node = nid
                            backend_meta = meta
                            matched_url = api_ep["url"]
                            if api_ep.get("methods"):
                                matched_method = api_ep["methods"][0].upper()
                            break

        # Fallback to name search
        if not backend_node:
            search_query = input_clean.split("/")[-1]
            for nid, meta in all_meta.items():
                if meta.get("type") in ("Function", "Class") and ("api.py" in nid or "views.py" in nid):
                    if search_query in meta.get("name", "").lower():
                        backend_node = nid
                        backend_meta = meta
                        break

        if not backend_node:
            # Fallback to any node matching name
            results = db.client.search(api_endpoint)
            if results:
                for r in results:
                    nid = r.get("id", "")
                    if "api.py" in nid or "views.py" in nid:
                        backend_node = nid
                        backend_meta = db.client.get_node_meta(backend_node)
                        break

        if not backend_node:
            return to_yaml({"error": f"Backend API handler for '{api_endpoint}' not found."})

        # 3. Get backend callees (Services / Selectors) via graph edges
        backend_callees = []
        callees = db.client.get_callees(backend_node)
        for c in callees:
            if not c.endswith((".tsx", ".ts", ".jsx", ".js")) and ":" in c:
                c_meta = db.client.get_node_meta(c)
                if c_meta:
                    role = "Service" if "/services.py:" in c else "Selector" if "/selectors.py:" in c else "Logic"
                    backend_callees.append({
                        "id": c,
                        "name": c_meta.get("name", ""),
                        "file": c_meta.get("file_path", ""),
                        "role": role
                    })

        # 4. Find frontend callers via graph traversal (edges created by resolve_api_calls)
        frontend_hooks = []
        frontend_components = []

        # Backend/API-server files share the .ts/.tsx suffix with real frontend
        # code, so they must be excluded from frontend hook classification.
        backend_markers = ('/api-server/', '/backend/', '/server/', 'backend/', 'api-server/')

        def _is_frontend_path(fpath: str) -> bool:
            if not (fpath.startswith("frontend/") or fpath.endswith((".tsx", ".ts", ".jsx", ".js"))):
                return False
            return not any(m in fpath for m in backend_markers)

        def _collect_frontend_callers(targets: list) -> set:
            """Map callers of graph targets to frontend hook Function nodes.

            resolve_api_calls wires frontend HTTP calls as File -> Route edges,
            so a File caller is expanded to the functions that live in that file
            (they are the actual API hooks / components). Route, Middleware, and
            Declaration nodes are never frontend hooks.
            """
            ids = set()
            for target in targets:
                for caller in db.client.get_callers(target):
                    c_meta = db.client.get_node_meta(caller)
                    if not c_meta:
                        continue
                    ctype = c_meta.get("type", "")
                    if ctype in ("Route", "Middleware", "Declaration"):
                        continue
                    fpath = c_meta.get("file_path", "")
                    if not _is_frontend_path(fpath):
                        continue
                    if ctype == "Function":
                        ids.add(caller)
                    elif ctype == "File":
                        for nid2, m2 in all_meta.items():
                            if m2.get("type") == "Function" and m2.get("file_path") == fpath:
                                ids.add(nid2)
            return ids

        hook_ids = _collect_frontend_callers([backend_node])

        # Also collect callers of Route nodes that connect to this backend handler
        route_ids = [
            nid for nid, meta in all_meta.items()
            if meta.get("type") == "Route" and backend_node in db.client.get_callees(nid)
        ]
        hook_ids |= _collect_frontend_callers(route_ids)

        api_url = matched_url or (backend_meta.get("api_endpoint") or {}).get("url", api_endpoint)
        api_method = matched_method or "POST"

        for hook_nid in hook_ids:
            h_meta = db.client.get_node_meta(hook_nid)
            if h_meta:
                frontend_hooks.append({
                    "id": hook_nid,
                    "name": h_meta.get("name", ""),
                    "file": h_meta.get("file_path", ""),
                    "role": "Hook"
                })
                if include_components:
                    for hc in db.client.get_callers(hook_nid):
                        hc_meta = db.client.get_node_meta(hc)
                        if hc_meta:
                            fpath = hc.split(":")[0]
                            if fpath.startswith("frontend/") or fpath.endswith((".tsx", ".ts", ".jsx", ".js")):
                                role = "Component" if "/components/" in hc else "Page" if "/pages/" in hc else "Frontend Module"
                                comp_entry = {
                                    "id": hc,
                                    "name": hc_meta.get("name", ""),
                                    "file": hc_meta.get("file_path", ""),
                                    "role": role
                                }
                                if comp_entry not in frontend_components:
                                    frontend_components.append(comp_entry)

        # 5. Build ASCII visualization
        vis_lines = ["\nFrontend ➔ Backend Integration Flow:"]
        
        # Components
        for comp in frontend_components:
            vis_lines.append(f"├─ 💻 [{comp['role']}] {comp['file']}:{comp['name']}")
            
        # Hooks
        for hook in frontend_hooks:
            vis_lines.append(f"├─ ⚓ [Hook] {hook['file']}:{hook['name']}")
            
        # API Endpoint
        clean_url = api_url if api_url.startswith("/") else f"/{api_url}"
        vis_lines.append(f"├─ 🌐 [Endpoint] {api_method} /api{clean_url}")
        
        # Backend handler
        h_file = backend_meta.get("file_path", "")
        h_name = backend_meta.get("name", "")
        vis_lines.append(f"├─ 🔥 [API Router] {h_file}:{h_name}")
        
        # Backend callees
        for idx, callee in enumerate(backend_callees):
            is_last = (idx == len(backend_callees) - 1)
            prefix = "└─" if is_last else "├─"
            vis_lines.append(f"{prefix} 🔥 [{callee['role']}] {callee['file']}:{callee['name']}")

        visualization = "\n".join(vis_lines)

        # 6. Explicit resolution coverage so a partial trace is never mistaken
        #    for a complete one (e.g. backend resolved but no frontend hop found).
        resolution = {
            "backend_handler": "resolved",
            "backend_logic": "resolved" if backend_callees else "empty",
            "frontend_hooks": "resolved" if frontend_hooks else "empty",
            "frontend_components": (
                "resolved" if frontend_components
                else ("skipped" if not include_components else "empty")
            ),
        }
        resolution["complete"] = all(
            v in ("resolved", "skipped") for v in resolution.values()
        )

        return to_yaml({
            "ok": True,
            "endpoint": f"{api_method} /api{clean_url}",
            "frontend_components": frontend_components,
            "frontend_hooks": frontend_hooks,
            "backend_handler": {
                "id": backend_node,
                "name": h_name,
                "file": h_file,
                "role": "API Router"
            },
            "backend_logic": backend_callees,
            "resolution": resolution,
            "visualization": visualization
        })

    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception("trace_frontend_backend failed")
        from .yaml_utils import to_yaml
        return to_yaml({"error": str(e)})
