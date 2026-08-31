import os
import logging
from tree_sitter import Parser

from .language_adapter import LANGUAGE_ADAPTERS, get_lang_config, build_parser, DEFAULT_CONFIG

logger = logging.getLogger(__name__)

# Fallback node-type defaults (see language_adapter.py) so a missing key in a
# language config never breaks extraction.
DEFAULT_NODE_TYPES = DEFAULT_CONFIG["node_types"]

# Map of extension → flattened config dict, driven entirely by language config
# files in languages/*.yaml (see language_adapter.py).
LANGUAGE_MAP = {ext: get_lang_config(ext) for ext in LANGUAGE_ADAPTERS}

class UniversalCodeParser:
    """
    محرك التحليل الهيكلي الشامل الذكي.
    يدعم الإضافة الديناميكية للغات عبر قاموس LANGUAGE_MAP المتوفر أعلاه.
    """
    def __init__(self):
        self.parsers = {}
        for ext, config in LANGUAGE_MAP.items():
            try:
                parser = build_parser(ext)
                if parser is None:
                    continue
                self.parsers[ext] = parser
            except Exception as e:
                logger.warning(f"Failed to initialize parser for '{ext}': {e}")

    def get_parser(self, file_path: str) -> Parser:
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        if ext in self.parsers:
            return self.parsers[ext]
        raise ValueError(f"Unsupported file extension: {ext}")

    def _get_lang_config(self, file_path: str) -> dict:
        _, ext = os.path.splitext(file_path)
        return get_lang_config(ext)

    @staticmethod
    def _pick_framework(file_frameworks: list, *preferred: str) -> str:
        for p in preferred:
            if p in file_frameworks:
                return p
        return file_frameworks[0] if file_frameworks else ''

    @staticmethod
    def _framework_for_route(route: dict, file_frameworks: list) -> str:
        """Pick the most likely framework for a route based on decorator style and file frameworks."""
        url = route.get("url", "")
        source_var = route.get("source_var", "")
        methods = route.get("methods", [])
        # @api_view(['GET']) — only Django REST uses this
        if not source_var and not url:
            if any(f in file_frameworks for f in ('django', 'django-rest-framework')):
                return "django"
            return "django"
        # api_operation is django-ninja specific
        if 'api_operation' in str(methods).lower() or any(
            'api_operation' in str(m) for m in methods
        ):
            fw = UniversalCodeParser._pick_framework(file_frameworks, 'django-ninja', 'fastapi')
            if fw:
                return fw
            return "django-ninja"
        # .route() -> Flask (most common)
        if url and not methods:
            pass  # will fall through
        # Use file-level detection
        if 'flask' in file_frameworks:
            return "flask"
        if 'django-ninja' in file_frameworks:
            return "django-ninja"
        if 'fastapi' in file_frameworks:
            return "fastapi"
        if 'django' in file_frameworks or 'django-rest-framework' in file_frameworks:
            return "django"
        # Default heuristic: HTTP method decorators are most often FastAPI in Python
        if source_var and methods:
            return "fastapi"
        return ""

    def parse_file(self, file_path: str) -> dict:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        non_code_exts = {'.json', '.md', '.html', '.css', '.yml', '.yaml', '.toml', '.txt', '.sql'}
        if ext not in self.parsers:
            if ext not in non_code_exts:
                raise ValueError(f"Unsupported file extension: {ext}")
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
            except Exception:
                content = ""
            return {
                "file_path": file_path,
                "imports": [],
                "exports": [],
                "classes": [],
                "functions": [],
                "declarations": [],
                "file_calls": [],
                "frameworks": [],
                "endpoints": [],
                "http_calls": [],
                "url_patterns": [],
                "middleware": [],
                "file_body": content
            }

        parser = self.get_parser(file_path)
        lang_config = self._get_lang_config(file_path)

        with open(file_path, 'r', encoding='utf-8') as f:
            code_bytes = f.read().encode('utf-8')

        tree = parser.parse(code_bytes)
        root_node = tree.root_node

        classes = []
        functions = []

        def walk_ast(node, is_top_level=True, scope=""):
            processed = False
            
            is_class = False
            is_func = False
            
            decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
            
            if node.type == decorated_definition:
                for child in node.children:
                    if child.type in lang_config.get("class_nodes", []):
                        is_class = True
                        break
                    elif child.type in lang_config.get("function_nodes", []):
                        is_func = True
                        break
            else:
                if node.type in lang_config.get("class_nodes", []):
                    is_class = True
                elif node.type in lang_config.get("function_nodes", []):
                    is_func = True

            # Classes — top-level and any found at the module scope
            if is_class:
                class_info = self._extract_node_info(node, code_bytes, lang_config)
                class_info['methods'] = self._extract_methods(node, code_bytes, lang_config)
                if lang_config.get("django_relations", False):
                    class_info['django_relations'] = self._extract_django_relations(node, code_bytes, lang_config)
                class_scope = f"{scope}.{class_info['name']}" if scope else class_info['name']
                class_info['node_name'] = class_scope
                for m in class_info['methods']:
                    m['node_name'] = f"{class_scope}.{m['name']}"
                classes.append(class_info)
                processed = True
                # Nested definitions below this class (inner classes, methods'
                # closures) become first-class nodes too.
                nested_fns, nested_cls = self._collect_nested(node, code_bytes, lang_config, class_scope)
                functions.extend(nested_fns)
                classes.extend(nested_cls)
                
            # Explicit functions
            elif is_func:
                func_info = self._extract_node_info(node, code_bytes, lang_config)
                func_info['calls'] = self._extract_calls(node, code_bytes, lang_config)
                func_info['returns'] = self._extract_returns(node, code_bytes, lang_config)
                func_scope = f"{scope}.{func_info['name']}" if scope else func_info['name']
                func_info['node_name'] = func_scope
                functions.append(func_info)
                processed = True
                nested_fns, nested_cls = self._collect_nested(node, code_bytes, lang_config, func_scope)
                functions.extend(nested_fns)
                classes.extend(nested_cls)
                
            # Arrow functions (JS/TS lexical_declaration / variable_declaration)
            elif node.type in lang_config.get("arrow_nodes", []):
                variable_declarator_nodes = lang_config.get("variable_declarator_nodes", ["variable_declarator"])
                arrow_function_nodes = lang_config.get("arrow_function_nodes", ["arrow_function"])
                function_expression_nodes = lang_config.get("function_expression_nodes", ["function_expression"])
                call_nodes_cfg = set(lang_config.get("call_nodes", ["call_expression"]))
                wrapper_names = set(lang_config.get("arrow_wrapper_functions", []))

                def _find_wrapped_fn(declarator):
                    """forwardRef((...) => ...), memo((...) => ...): the component
                    function hides inside a wrapper CALL, not directly under the
                    declarator. Return it when the callee is a known wrapper."""
                    call_node = next(
                        (c for c in declarator.children if c.type in call_nodes_cfg), None
                    )
                    if call_node is None:
                        return None
                    callee = call_node.child_by_field_name("function")
                    if callee is None:
                        return None
                    callee_text = callee.text.decode('utf-8', errors='replace')
                    bare = callee_text.split(".")[-1].split("<")[0].strip()
                    if bare not in wrapper_names:
                        return None
                    args_node = call_node.child_by_field_name("arguments")
                    stack = [args_node] if args_node is not None else []
                    while stack:
                        cur = stack.pop()
                        if cur.type in (arrow_function_nodes + function_expression_nodes):
                            return cur
                        stack.extend(getattr(cur, "children", []))
                    return None

                for child in node.children:
                    if child.type in variable_declarator_nodes:
                        has_arrow = any(n.type in arrow_function_nodes for n in child.children)
                        wrapped_fn = None if has_arrow else _find_wrapped_fn(child)
                        if has_arrow or wrapped_fn is not None:
                            func_info = self._extract_node_info(child, code_bytes, lang_config, override_name=True)
                            func_info['calls'] = self._extract_calls(child, code_bytes, lang_config)
                            func_info['returns'] = self._extract_returns(child, code_bytes, lang_config)
                            func_scope = f"{scope}.{func_info['name']}" if scope else func_info['name']
                            func_info['node_name'] = func_scope
                            functions.append(func_info)
                            processed = True
                            nested_fns, nested_cls = self._collect_nested(child, code_bytes, lang_config, func_scope)
                            functions.extend(nested_fns)
                            classes.extend(nested_cls)

            # TS namespaces/modules (namespace Foo {}, module Bar {}): their
            # members are top-level entities scoped under the namespace name.
            elif node.type in set(lang_config.get("namespace_nodes", [])):
                ident_types = set(lang_config.get("identifier_nodes", ["identifier"]))
                ns_name_node = next(
                    (c for c in node.children if c.type in ident_types), None
                )
                ns_name = ns_name_node.text.decode('utf-8', errors='replace') if ns_name_node else ""
                ns_scope = f"{scope}.{ns_name}" if scope and ns_name else (ns_name or scope)
                for child in node.children:
                    walk_ast(child, is_top_level=True, scope=ns_scope)

            # If we processed this node as a top-level entity, we usually don't want to 
            # walk its immediate children for more top-level entities (like decorated funcs)
            # unless it's a specific container like 'program' or 'export'
            container_nodes = set(lang_config.get("export_statement_nodes", ["export_statement"]))
            container_nodes.add(lang_config.get("export_default_statement_nodes", ["export_default_statement"])[0])
            container_nodes.add(lang_config.get("program_root", "program"))
            container_nodes.update(lang_config.get("namespace_nodes", []))
            if is_top_level or node.type in container_nodes:
                for child in node.children:
                    ignore_nodes = lang_config.get("class_nodes", []) + lang_config.get("function_nodes", []) + lang_config.get("arrow_nodes", [])
                    # If we already processed the current node, we skip children that are part of its own definition
                    if processed and child.type in ignore_nodes:
                        continue
                    if child.type not in ignore_nodes:
                        walk_ast(child, is_top_level=False, scope=scope)
                    else:
                        walk_ast(child, is_top_level=True, scope=scope)

        for node in root_node.children:
            walk_ast(node, is_top_level=True)

        imports, import_lines = self._extract_imports(root_node, code_bytes, lang_config)
        file_level_calls = self._extract_calls(root_node, code_bytes, lang_config)
        exports = self._extract_exports(root_node, code_bytes, lang_config)
        declarations = self._extract_declarations(root_node, code_bytes, lang_config) if lang_config.get("features", {}).get("declarations", False) else []
        frameworks = self._detect_framework(root_node, file_path, lang_config)
        all_routes, inline_handlers = self._extract_routes(root_node, code_bytes, lang_config)
        functions.extend(inline_handlers)
        http_calls = self._extract_http_calls(root_node, code_bytes, lang_config) if lang_config.get("http_calls", False) else []
        url_patterns = self._extract_url_patterns(root_node, code_bytes, lang_config) if lang_config.get("url_patterns", False) else []
        middleware = self._extract_middleware(root_node, code_bytes,
                                              file_path=os.path.basename(file_path),
                                              lang_config=lang_config)

        seen_urls = {p["url"] for p in url_patterns}
        seen_ep_keys = set()  # (url, method) for endpoint dedup

        # Split unified routes: decorators → endpoints, mount/call → url_patterns
        file_endpoints = []
        for r in all_routes:
            if r["type"] == "decorator":
                # Label framework based on file-level detection
                framework = self._framework_for_route(r, frameworks)
                ep = {
                    "function": r["function"],
                    "methods": r["methods"],
                    "url": r["url"],
                    "framework": framework,
                    "source_var": r.get("source_var", ""),
                }
                if not ep["methods"] and not ep["url"]:
                    ep["url"] = r["url"]
                    ep["methods"] = r["methods"]
                ep_key = (ep["url"], tuple(ep["methods"]), ep["function"])
                if ep_key not in seen_ep_keys:
                    seen_ep_keys.add(ep_key)
                    file_endpoints.append(ep)
            elif r["type"] == "mount":
                url_patterns.append({
                    "url": r["url"],
                    "view_name": r["handler_var"],
                    "name": "",
                    "is_include": True,
                    "func": "add_router",
                    "parent_var": r["source_var"],
                    "sub_router_var": r["handler_var"],
                    "methods": r.get("methods", []),
                })
            elif r["type"] == "endpoint_call":
                # Express/Koa/Fastify-style router verbs: usersRouter.get('/x', h)
                ep = {
                    "function": r.get("function") or r.get("handler_var", ""),
                    "methods": r["methods"],
                    "url": r["url"],
                    "framework": self._framework_for_route(r, frameworks),
                    "source_var": r.get("source_var", ""),
                }
                ep_key = (ep["url"], tuple(ep["methods"]), ep["function"])
                if ep_key not in seen_ep_keys:
                    seen_ep_keys.add(ep_key)
                    file_endpoints.append(ep)
            elif r["type"] == "call":
                # Bare path()/re_path() calls — deduplicate against _extract_url_patterns
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    url_patterns.append({
                        "url": r["url"],
                        "view_name": '',
                        "name": '',
                        "is_include": False,
                        "func": "path",
                    })
            elif r["type"] == "jsx_route":
                # React Router <Route> or createBrowserRouter entry
                ep = {
                    "function": r["function"],
                    "methods": r["methods"],
                    "url": r["url"],
                    "framework": "react-router",
                }
                ep_key = (ep["url"], tuple(ep["methods"]), ep["function"])
                if ep_key not in seen_ep_keys:
                    seen_ep_keys.add(ep_key)
                    file_endpoints.append(ep)

        # File-based routing (Next.js, Nuxt, SvelteKit) — derive routes from file path
        file_based_routes = self._extract_file_based_routes(file_path, functions, exports) if lang_config.get("file_based_routes", False) else []
        for fbr in file_based_routes:
            ep_key = (fbr["url"], tuple(fbr["methods"]))
            if ep_key not in seen_ep_keys:
                seen_ep_keys.add(ep_key)
                file_endpoints.append(fbr)

        # Attach endpoint info to function/method dicts
        endpoint_by_func = {ep["function"]: ep for ep in file_endpoints}
        for func in functions:
            func_name = func["name"]
            if func_name in endpoint_by_func:
                func["api_endpoint"] = endpoint_by_func[func_name]
        for cls in classes:
            for method in cls.get("methods", []):
                method_name = method["name"]
                if method_name in endpoint_by_func:
                    method["api_endpoint"] = endpoint_by_func[method_name]

        # Cross-reference exports (enabled per language via config)
        if lang_config.get("exports", False):
            exported_names = set()
            for exp in exports:
                for n in exp.get("names", []):
                    if n.get("name"):
                        exported_names.add(n["name"])
                    if n.get("alias"):
                        exported_names.add(n["alias"])
            for func in functions:
                if func.get("_is_inline_export") or func["name"] in exported_names:
                    func["is_exported"] = True
                else:
                    func["is_exported"] = False
            for cls in classes:
                if cls.get("_is_inline_export") or cls["name"] in exported_names:
                    cls["is_exported"] = True
                else:
                    cls["is_exported"] = False

        return {
            "file_path": file_path,
            "imports": imports,
            "import_lines": import_lines,
            "exports": exports,
            "classes": classes,
            "functions": functions,
            "declarations": declarations,
            "file_calls": file_level_calls,
            "frameworks": frameworks,
            "endpoints": file_endpoints,
            "http_calls": http_calls,
            "url_patterns": url_patterns,
            "middleware": middleware,
            "file_body": code_bytes.decode('utf-8', errors='ignore')
        }

    def get_node_location(self, file_path: str, node_name: str, node_type: str = "function") -> dict:
        parsed_data = self.parse_file(file_path)
        if node_type == 'class':
            for item in parsed_data['classes']:
                if item['name'] == node_name:
                    return item
        else:
            # Check standalone functions
            for func in parsed_data['functions']:
                if func['name'] == node_name:
                    return func
            # Check methods inside classes
            for cls in parsed_data['classes']:
                for method in cls.get('methods', []):
                    if method['name'] == node_name:
                        return method
        return None

    def _extract_node_info(self, node, code_bytes: bytes, lang_config: dict, override_name=False) -> dict:
        node_name = "anonymous"

        decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
        class_nodes = lang_config.get("class_nodes", ["class_definition"])
        function_nodes = lang_config.get("function_nodes", ["function_definition", "async_function_definition"])
        method_nodes = lang_config.get("method_nodes", function_nodes)
        name_identifiers = lang_config.get("name_identifiers", ["identifier"])
        body_nodes = lang_config.get("body_nodes", ["block", "class_body", "statement_block"])

        # Helper to find identifier in node or its children recursively (shallow)
        def find_name(n):
            # Check direct children first
            name_node = next((c for c in n.children if c.type in name_identifiers), None)
            if name_node:
                return name_node.text.decode('utf-8')
            # If decorated_definition, look inside for the actual definition
            if n.type == decorated_definition:
                def_child = next((c for c in n.children if c.type in (class_nodes + function_nodes)), None)
                if def_child:
                    return find_name(def_child)
            return None

        if override_name and node.type in lang_config.get("variable_declarator_nodes", ["variable_declarator"]):
             name_node = next((n for n in node.children if n.type in name_identifiers), None)
             if name_node:
                 node_name = name_node.text.decode('utf-8')
        else:
             extracted_name = find_name(node)
             if extracted_name:
                 node_name = extracted_name
        
        # Ensure anonymity doesn't cause primary key collisions
        if node_name == "anonymous":
            node_name = f"anonymous_L{node.start_point[0] + 1}"

        # Get actual definition node (skipping decorators)
        def_node = node
        if node.type == decorated_definition:
            def_child = next((c for c in node.children if c.type in (class_nodes + function_nodes + method_nodes)), None)
            if def_child:
                def_node = def_child

        body_node = next((c for c in def_node.children if c.type in body_nodes), None)

        docstring = ""
        # Python docstring
        if lang_config.get("docstrings", False) and body_node and body_node.type == lang_config.get("class_body", "block") and body_node.children:
            first_stmt = body_node.children[0]
            if first_stmt.type in lang_config.get("statement_nodes", ["expression_statement"]):
                string_node = next((c for c in first_stmt.children if c.type in lang_config.get("string_nodes", ["string"])), None)
                if string_node:
                    raw_doc = string_node.text.decode('utf-8').strip('\'" \n\r\t')
                    # Get first non-empty line
                    lines = [l.strip() for l in raw_doc.split('\n') if l.strip()]
                    if lines:
                        docstring = lines[0]

        signature = ""
        if body_node:
            try:
                sig_bytes = code_bytes[def_node.start_byte:body_node.start_byte]
                signature = sig_bytes.decode('utf-8').strip(' \n\r\t:')
                # Collapse multiple spaces and newlines
                signature = " ".join(signature.split())
            except Exception:
                pass
        
        if not signature:
            # Fallback: first line of def_node
            try:
                first_line = def_node.text.decode('utf-8').split('\n')[0].strip(' \n\r\t:')
                signature = first_line
            except Exception:
                pass

        try:
            body = code_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
        except Exception:
            body = ""

        is_async = False
        is_generator = False

        if def_node.type in lang_config.get("async_function_nodes", ["async_function_definition", "async_function"]):
            is_async = True
        elif any(c.type in lang_config.get("async_keywords", ["async"]) for c in def_node.children) or any(c.type in lang_config.get("async_keywords", ["async"]) for c in node.children):
            is_async = True
        elif signature.startswith('async ') or signature.startswith('async('):
            is_async = True

        if def_node.type in lang_config.get("generator_function_nodes", ["generator_function", "generator_function_declaration"]):
            is_generator = True
        elif body_node:
            generator_keywords = lang_config.get("generator_keywords", ["yield", "yield_statement", "yield_expression"])
            prune_nodes = lang_config.get("prune_walk_nodes", [])
            def _has_yield(n):
                if n.type in generator_keywords:
                    return True
                for c in n.children:
                    if c.type not in prune_nodes:
                        if _has_yield(c):
                            return True
                return False
            if _has_yield(body_node):
                is_generator = True

        param_count = 0
        parameters_nodes = lang_config.get("parameters_nodes", ["parameters", "formal_parameters"])
        param_recurse_nodes = (
            lang_config.get("arrow_function_nodes", ["arrow_function"])
            + lang_config.get("function_expression_nodes", ["function_expression"])
            + function_nodes
            + lang_config.get("generator_function_nodes", ["generator_function", "generator_function_declaration"])
            + lang_config.get("variable_declarator_nodes", ["variable_declarator"])
        )
        def _find_params_node(n):
            for c in n.children:
                if c.type in parameters_nodes:
                    return c
                if c.type in param_recurse_nodes:
                    res = _find_params_node(c)
                    if res:
                        return res
            return None

        param_node = _find_params_node(def_node) or _find_params_node(node)
        if param_node:
            ignore_types = set(lang_config.get("param_ignore_types", ['(', ')', ',', ':', ';', 'comment']))
            raw_params = [c for c in param_node.children if c.type not in ignore_types]
            if raw_params and raw_params[0].text.decode('utf-8', errors='ignore').strip() in ('self', 'cls'):
                param_count = max(0, len(raw_params) - 1)
            else:
                param_count = len(raw_params)

        # Determine is_exported
        export_nodes = lang_config.get("export_statement_nodes", ["export_statement"]) + lang_config.get("export_default_statement_nodes", ["export_default_statement"])
        curr = node
        found_export = False
        while curr:
            if curr.type in export_nodes:
                found_export = True
                break
            curr = curr.parent
        if not found_export and def_node != node:
            curr = def_node
            while curr:
                if curr.type in export_nodes:
                    found_export = True
                    break
                curr = curr.parent

        if found_export:
            is_exported = True
        elif node_name.startswith('_') and not (node_name.startswith('__') and node_name.endswith('__')):
            is_exported = False
        else:
            is_exported = True

        decorators = self._extract_decorators(node, code_bytes, lang_config)
        base_classes = []
        if def_node.type in class_nodes:
            base_classes = self._extract_base_classes(node, code_bytes, lang_config)

        return {
            "name": node_name,
            "lines": {
                "start": node.start_point[0] + 1,
                "end": node.end_point[0] + 1
            },
            "docstring": docstring,
            "signature": signature,
            "body": body,
            "is_async": is_async,
            "is_generator": is_generator,
            "param_count": param_count,
            "is_exported": is_exported,
            "decorators": decorators,
            "base_classes": base_classes,
            "inherits": base_classes,
            "_is_inline_export": found_export
        }

    def _extract_decorators(self, node, code_bytes: bytes, lang_config: dict) -> list:
        decorators = []
        import re

        def _extract(text: str):
            m = re.search(r'@([a-zA-Z0-9_\.]+)', text)
            if m:
                return m.group(1)
            if text.startswith('@'):
                return text[1:].split('(')[0].strip()
            return None

        decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
        decorator_nodes = lang_config.get("decorator_nodes", ["decorator"])
        export_prefix_skips = lang_config.get("export_prefix_skips", ["export"])

        # Python: decorators are children of the decorated_definition wrapper
        if node.type == decorated_definition:
            for child in node.children:
                if child.type in decorator_nodes:
                    text = child.text.decode('utf-8', errors='ignore').strip()
                    name = _extract(text)
                    if name:
                        decorators.append(name)

        # TypeScript/JS/C#/Java: decorators are consecutive previous siblings
        # separated only by 'export'/'default'/'abstract' keywords
        prev = node.prev_sibling
        while prev is not None:
            if prev.type in decorator_nodes:
                text = prev.text.decode('utf-8', errors='ignore').strip()
                name = _extract(text)
                if name:
                    decorators.append(name)
            elif prev.type not in export_prefix_skips:
                break
            prev = prev.prev_sibling

        return decorators

    def _extract_base_classes(self, node, code_bytes: bytes, lang_config: dict) -> list:
        bases = []
        def_node = node
        decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
        if node.type == decorated_definition:
            def_node = next((c for c in node.children if c.type in lang_config.get("class_nodes", [])), node)

        base_class_nodes = lang_config.get("base_class_nodes", ["argument_list", "class_heritage"])
        extends_keyword = lang_config.get("base_class_extends_keyword", "extends")
        prefix_keywords = [extends_keyword] + list(lang_config.get("base_class_implements_keywords", []))
        ignore_bases = lang_config.get("base_class_ignores", ["object"])
        for child in def_node.children:
            if child.type in base_class_nodes:
                text = child.text.decode('utf-8', errors='ignore').strip('() \n\r\t')
                parts = [p.strip() for p in text.split(',') if p.strip()]
                for p in parts:
                    for kw in prefix_keywords:
                        if kw and p.startswith(kw):
                            p = p[len(kw):].strip()
                            break
                    if p and p not in ignore_bases:
                        bases.append(p)
        return bases

    def _extract_methods(self, class_node, code_bytes: bytes, lang_config: dict) -> list:
        methods = []
        
        decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
        class_body = lang_config.get("class_body", "block")

        # If the class itself is decorated, get the actual class_definition child
        if class_node.type == decorated_definition:
            actual_class = next((n for n in class_node.children if n.type in lang_config.get("class_nodes", [])), None)
            if actual_class:
                class_node = actual_class

        body_node = next((n for n in class_node.children if n.type == class_body), None)
        if body_node:
            method_nodes = lang_config.get("method_nodes", [])
            for child in body_node.children:
                is_method = False
                if child.type == decorated_definition:
                    if any(c.type in method_nodes for c in child.children):
                        is_method = True
                elif child.type in method_nodes:
                    is_method = True

                if is_method:
                    meth_info = self._extract_node_info(child, code_bytes, lang_config)
                    meth_info['calls'] = self._extract_calls(child, code_bytes, lang_config)
                    meth_info['returns'] = self._extract_returns(child, code_bytes, lang_config)
                    methods.append(meth_info)
        return methods

    def _collect_nested(self, node, code_bytes: bytes, lang_config: dict, scope: str) -> tuple:
        """Collect function/class definitions nested inside a top-level def.

        Catches closures, inner (schema/Meta) classes, decorator-wrapped helpers
        and defs living inside if/for/try blocks — anything the flatter top-level
        walk skips. Each entry mirrors ``_extract_node_info`` output, keeping its
        bare ``name`` (so the Rust name index still resolves calls) and adding a
        dotted ``node_name`` that encodes the full qualification path:

            class Outer:                    -> node_name "Outer"
                class Inner:                -> node_name "Outer.Inner"
                    def m(self): ...        -> method node_name "Outer.Inner.m"
                def method(self):           -> method node_name "Outer.method"

        Returns a ``(nested_functions, nested_classes)`` tuple of lists.
        """
        func_nodes = self._node_types(lang_config, "function_nodes")
        class_nodes = self._node_types(lang_config, "class_nodes")
        method_nodes = self._node_types(lang_config, "method_nodes")
        def_nodes = class_nodes | func_nodes
        arrow_containers = self._node_types(lang_config, "arrow_nodes")
        variable_declarators = self._node_types(lang_config, "variable_declarator_nodes")
        arrow_function_nodes = self._node_types(lang_config, "arrow_function_nodes")
        name_ids = self._node_types(lang_config, "name_identifiers")
        body_nodes = self._node_types(lang_config, "body_nodes")
        decorated = lang_config.get("decorated_definition", "decorated_definition")

        functions = []
        classes = []

        def _unwrap(n):
            """Return the actual definition node behind a decorated_definition."""
            if n.type != decorated:
                return n
            for c in n.children:
                if c.type in def_nodes or c.type in method_nodes:
                    return c
            return n

        def _classify(n):
            """Classify a node as class/function (or (None, None) for statements)."""
            if n.type == decorated:
                inner = _unwrap(n)
                if inner is not n:
                    return ("class", n) if inner.type in class_nodes else ("function", n)
                return None, None
            if n.type in class_nodes:
                return "class", n
            if n.type in func_nodes or n.type in method_nodes:
                return "function", n
            if n.type in arrow_containers:
                for d in n.children:
                    if d.type in variable_declarators and any(
                        c.type in arrow_function_nodes for c in d.children
                    ):
                        return "function", d
            return None, None

        def _name_of(n):
            inner = _unwrap(n)
            nid = next((c for c in inner.children if c.type in name_ids), None)
            return nid.text.decode('utf-8', errors='replace') if nid is not None else None

        def _scan_body_target(n):
            """Locate the body node of a def, unwrapping JS arrow declarator chains."""
            inner = _unwrap(n)
            stack = list(inner.children)
            while stack:
                c = stack.pop()
                if c.type in body_nodes:
                    return c
                if c.type in def_nodes or c.type in method_nodes:
                    continue
                stack.extend(c.children)
            return None

        def _scan(n, cur_scope, in_class_body):
            """Recursively visit children, registering defs below the current scope."""
            for child in n.children:
                kind, wrap = _classify(child)
                if kind is None:
                    # Statement/block — dig deeper (if/for/try ... may hold defs).
                    _scan(child, cur_scope, False)
                    continue
                bare = _name_of(wrap)
                if bare is None:
                    _scan(child, cur_scope, False)
                    continue
                child_scope = f"{cur_scope}.{bare}" if cur_scope else bare
                body = _scan_body_target(wrap)

                if kind == "class":
                    c_info = self._extract_node_info(wrap, code_bytes, lang_config)
                    c_info["node_name"] = child_scope
                    c_methods = self._extract_methods(wrap, code_bytes, lang_config)
                    for m in c_methods:
                        m["node_name"] = f"{child_scope}.{m['name']}"
                    c_info["methods"] = c_methods
                    if lang_config.get("django_relations", False):
                        c_info["django_relations"] = self._extract_django_relations(
                            wrap, code_bytes, lang_config
                        )
                    classes.append(c_info)
                    if body is not None:
                        _scan(body, child_scope, True)
                    continue

                # function / method / arrow
                if in_class_body:
                    # Direct method of the class being scanned — already indexed
                    # in its `methods` list; only descend for deeper nesting.
                    if body is not None:
                        _scan(body, child_scope, False)
                    continue

                override = wrap.type in arrow_containers
                f_info = self._extract_node_info(wrap, code_bytes, lang_config, override_name=override)
                f_info["node_name"] = child_scope
                f_info["calls"] = self._extract_calls(wrap, code_bytes, lang_config)
                f_info["returns"] = self._extract_returns(wrap, code_bytes, lang_config)
                functions.append(f_info)
                if body is not None:
                    _scan(body, child_scope, False)

        inner = _unwrap(node)
        body = _scan_body_target(node)
        if body is not None:
            _scan(body, scope, inner.type in class_nodes)
        return functions, classes

    def _extract_django_relations(self, class_node, code_bytes: bytes, lang_config: dict) -> list:
        relations = []
        decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
        class_nodes = lang_config.get("class_nodes", ["class_definition"])
        class_body = lang_config.get("class_body", "block")
        statement_nodes = lang_config.get("statement_nodes", ["expression_statement"])
        if class_node.type == decorated_definition:
            actual_class = next((n for n in class_node.children if n.type in class_nodes), None)
            if actual_class:
                class_node = actual_class
                
        body_node = next((n for n in class_node.children if n.type == class_body), None)
        if not body_node:
            return relations
            
        import re
        for child in body_node.children:
            if child.type in statement_nodes:
                text = child.text.decode('utf-8')
                if any(f in text for f in ('ForeignKey', 'OneToOneField', 'ManyToManyField')):
                    match = re.search(r'(ForeignKey|OneToOneField|ManyToManyField)\s*\(\s*[\'"]?([a-zA-Z0-9_]+)[\'"]?', text)
                    if match:
                        related_model = match.group(2)
                        relations.append({
                            "type": match.group(1),
                            "related_model": related_model
                        })
        return relations

    def _extract_returns(self, node, code_bytes: bytes, lang_config: dict) -> list:
        returns = []
        return_statement_nodes = lang_config.get("return_statement_nodes", ["return_statement"])
        prune_nodes = lang_config.get("prune_walk_nodes", [])
        
        def walk(n):
            if n.type in return_statement_nodes:
                try:
                    ret_text = n.text.decode('utf-8').strip()
                    # Collapse multiple spaces and newlines
                    ret_text = " ".join(ret_text.split())
                    
                    # PRUNING LOGIC: Prevent massive JSX/TSX blocks from bloating the index
                    if len(ret_text) > 120:
                        if "<" in ret_text and ">" in ret_text:
                            # It's likely a JSX block
                            ret_text = "return <JSX.Element /> (pruned)"
                        else:
                            ret_text = ret_text[:117] + "..."
                            
                    returns.append(ret_text)
                except:
                    pass
            for child in n.children:
                # Don't walk into nested functions or classes
                if child.type not in prune_nodes:
                    walk(child)
        
        walk(node)
        return list(dict.fromkeys(returns))

    def _extract_imports(self, root_node, code_bytes: bytes, lang_config: dict) -> tuple:
        imports = set()
        import_lines = {}

        def _record(value, line_no):
            if not value:
                return
            imports.add(value)
            import_lines.setdefault(value, line_no)

        import_statement_nodes = lang_config.get("import_statement_nodes", ["import_statement"])
        import_from_statement_nodes = lang_config.get("import_from_statement_nodes", ["import_from_statement"])
        dotted_name_nodes = lang_config.get("dotted_name_nodes", ["dotted_name"])
        string_nodes = lang_config.get("string_nodes", ["string"])
        module_name_field = lang_config.get("module_name_field", "module_name")
        enable_import_statement = lang_config.get("enable_import_statement", True)
        enable_import_from = lang_config.get("enable_import_from", True)
        enable_require = lang_config.get("enable_require", False)
        require_nodes = lang_config.get("require_nodes", ["lexical_declaration", "variable_declaration"])
        variable_declarator_nodes = lang_config.get("variable_declarator_nodes", ["variable_declarator"])
        call_nodes = lang_config.get("call_nodes", ["call"])
        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"])
        argument_list_nodes = lang_config.get("argument_list_nodes", ["argument_list"])

        def walk(node):
            # 1. import X / import { X } from 'Y'
            if enable_import_statement and node.type in import_statement_nodes:
                line_no = node.start_point[0] + 1
                raw_text = node.text.decode('utf-8', errors='replace').strip()
                if raw_text:
                    _record(raw_text, line_no)
                for child in node.children:
                    # 'dotted_name' للبايثون، و 'string' للـ JS/TS
                    if child.type in (dotted_name_nodes + string_nodes):
                         val = child.text.decode('utf-8', errors='replace').strip("'").strip('"')
                         _record(val, line_no)

            # 2. from X import Y
            elif enable_import_from and node.type in import_from_statement_nodes:
                line_no = node.start_point[0] + 1
                raw_text = node.text.decode('utf-8', errors='replace').strip()
                if raw_text:
                    _record(raw_text, line_no)
                # الطريقة الأصح والأكثر أماناً في Tree-sitter
                module_node = node.child_by_field_name(module_name_field)
                if module_node:
                    _record(module_node.text.decode('utf-8', errors='replace'), line_no)
                else:
                    # Fallback للاستيراد النسبي (from . import X)
                    for child in node.children:
                        if child.type in dotted_name_nodes:
                            _record(child.text.decode('utf-8', errors='replace'), line_no)
                            break

            # 3. CommonJS (const X = require('Y'))
            elif enable_require and node.type in require_nodes:
                 for child in node.children:
                     if child.type in variable_declarator_nodes:
                         call_expr = next((n for n in child.children if n.type in call_nodes), None)
                         if call_expr:
                             ident = next((n for n in call_expr.children if n.type in identifier_nodes), None)
                             # التأكد من اسم الدالة بأمان
                             if ident and ident.text.decode('utf-8') == 'require':
                                 args = next((n for n in call_expr.children if n.type in argument_list_nodes), None)
                                 if args:
                                     str_node = next((n for n in args.children if n.type in string_nodes), None)
                                     if str_node:
                                        _record(str_node.text.decode('utf-8').strip("'").strip('"'),
                                                node.start_point[0] + 1)

            # مواصلة البحث في كل فروع الشجرة (لاستخراج الـ Local Imports)
            for child in node.children:
                 walk(child)

        # انطلاق المسح الشامل من الجذر
        walk(root_node)

        return list(imports), import_lines

    def _extract_exports(self, root_node, code_bytes: bytes, lang_config: dict = None) -> list:
        exports = []
        lang_config = lang_config or {}
        name_ids = lang_config.get("name_identifiers", ["identifier"])
        string_nodes = lang_config.get("string_nodes", ["string"])
        export_statement_nodes = lang_config.get("export_statement_nodes", ["export_statement"])
        export_default_statement_nodes = lang_config.get("export_default_statement_nodes", ["export_default_statement"])
        default_keyword_nodes = lang_config.get("default_keyword_nodes", ["default"])
        export_clause_nodes = lang_config.get("export_clause_nodes", ["export_clause"])
        export_specifier_nodes = lang_config.get("export_specifier_nodes", ["export_specifier"])
        function_declaration_nodes = lang_config.get("function_declaration_nodes", ["function_declaration"])
        class_declaration_nodes = lang_config.get("class_declaration_nodes", ["class_declaration"])
        function_expression_nodes = lang_config.get("function_expression_nodes", ["function_expression"])
        require_nodes = lang_config.get("require_nodes", ["lexical_declaration", "variable_declaration"])
        variable_declarator_nodes = lang_config.get("variable_declarator_nodes", ["variable_declarator"])
        arrow_function_nodes = lang_config.get("arrow_function_nodes", ["arrow_function"])
        default_value_types = ('function_expression', 'class', 'number', 'string', 'identifier', 'arrow_function', 'object', 'array')

        def _find_name(n):
            return next((c for c in n.children if c.type in name_ids), None)

        def walk(node):
            if node.type in export_statement_nodes:
                info = {"default": False, "names": [], "source": None}
                has_default = False

                for child in node.children:
                    if child.type in default_keyword_nodes:
                        has_default = True
                        info["default"] = True
                    elif child.type in export_clause_nodes:
                        for spec in child.children:
                            if spec.type in export_specifier_nodes:
                                ids = [c.text.decode('utf-8') for c in spec.children if c.type in name_ids or c.type in default_keyword_nodes]
                                name = ids[0] if ids else None
                                alias = ids[1] if len(ids) > 1 else None
                                if name:
                                    info["names"].append({"name": name, "alias": alias})
                    elif child.type in string_nodes:
                        info["source"] = child.text.decode('utf-8').strip("'\"")
                    else:
                        # python 'from X import Y' — not an export, ignored
                        pass

                if not info["names"]:
                    for child in node.children:
                        if child.type in (function_declaration_nodes + class_declaration_nodes + function_expression_nodes):
                            n = _find_name(child)
                            info["names"].append({"name": n.text.decode('utf-8') if n else "default", "alias": None})
                        elif child.type in require_nodes:
                            for d in child.children:
                                if d.type in variable_declarator_nodes:
                                    n = next((c for c in d.children if c.type in name_ids), None)
                                    if n:
                                        info["names"].append({"name": n.text.decode('utf-8'), "alias": None})
                        elif has_default and child.type in default_value_types:
                            info["names"].append({"name": "default", "alias": None})

                if info["names"]:
                    exports.append(info)

            elif node.type in export_default_statement_nodes:
                exports.append({"default": True, "names": [{"name": "default", "alias": None}], "source": None})

            for child in node.children:
                walk(child)

        walk(root_node)
        return exports

    def _extract_declarations(self, root_node, code_bytes: bytes, lang_config: dict = None) -> list:
        """Extract module-level variable declarations assigned to a call expression.

        Covers patterns like `export const usersTable = pgTable('users', {...})` or
        `const api = fetch('/api/users/')` that tree-sitter represents as a
        lexical/variable declaration whose declarator value is a call — these were
        previously invisible to the index. Arrow functions are already indexed as
        functions and are skipped here.
        """
        lang_config = lang_config or {}
        containers = lang_config.get("arrow_nodes", [])
        if not containers:
            return []
        declarators = lang_config.get("variable_declarator_nodes", ["variable_declarator"])
        call_nodes = set(lang_config.get("call_nodes", ["call_expression"]))
        arrow_fn = set(lang_config.get("arrow_function_nodes", ["arrow_function"]))
        name_ids = lang_config.get("name_identifiers", ["identifier"])
        member_nodes = set(lang_config.get("member_nodes", ["member_expression"]))
        exports = set(lang_config.get("export_statement_nodes", ["export_statement"]))
        exports.add(lang_config.get("export_default_statement_nodes", ["export_default_statement"])[0])
        program_root = lang_config.get("program_root", "program")

        declarations = []
        seen = set()

        def _callee_name(call_node):
            for c in call_node.children:
                if c.type in name_ids or c.type in member_nodes:
                    return c.text.decode("utf-8", errors="replace")
            return ""

        def _visit_container(container, exported):
            for d in container.children:
                if d.type not in declarators:
                    continue

                def _contains_fn(node):
                    # Wrapper components (`const C = forwardRef((...) => ...)`)
                    # are FUNCTIONS handled by the main extraction pass — never
                    # data declarations, even though their top-level value is a
                    # call expression.
                    stack = [node]
                    fn_types = arrow_fn | set(
                        lang_config.get("function_expression_nodes", ["function_expression"])
                    )
                    while stack:
                        cur = stack.pop()
                        if cur.type in fn_types:
                            return True
                        stack.extend(getattr(cur, "children", []))
                    return False

                if any(c.type in arrow_fn for c in d.children) or _contains_fn(d):
                    continue
                value = next((c for c in d.children if c.type in call_nodes), None)
                if value is None:
                    continue
                name_node = next((c for c in d.children if c.type in name_ids), None)
                if name_node is None:
                    continue
                name = name_node.text.decode("utf-8", errors="replace")
                if name in seen:
                    continue
                seen.add(name)
                try:
                    body = code_bytes[d.start_byte:d.end_byte].decode("utf-8", errors="ignore")
                except Exception:
                    body = ""
                declarations.append({
                    "name": name,
                    "call": _callee_name(value),
                    "lines": {"start": d.start_point[0] + 1, "end": d.end_point[0] + 1},
                    "body": body,
                    "is_exported": exported,
                })

        def walk(node, exported=False):
            if node.type in exports:
                for c in node.children:
                    if c.type in containers:
                        _visit_container(c, True)
                    elif c.type != program_root:
                        walk(c, True)
            elif node.type in containers:
                _visit_container(node, exported)
            elif node.type == program_root:
                for c in node.children:
                    walk(c, exported)

        walk(root_node)
        return declarations

    def _extract_calls(self, root_node, code_bytes: bytes, lang_config: dict) -> list:
        calls = []
        seen = set()

        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"]) + lang_config.get("jsx_identifier_nodes", ["jsx_identifier"])
        member_nodes = lang_config.get("member_nodes", ["attribute", "member_expression"])
        call_nodes = lang_config.get("call_nodes", ["call"])
        jsx_nodes = lang_config.get("jsx_nodes", ["jsx_self_closing_element", "jsx_opening_element"])
        prune_nodes = set(lang_config.get("prune_walk_nodes", []))

        def _add_call(name: str):
            if name and name not in seen:
                seen.add(name)
                calls.append(name)

        def _qualified_name(func_node) -> str:
            """Extract full dotted qualifier from a call node.
            re.search(...) → \"re.search\",  db.client.search(...) → \"db.client.search\",
            bare search(...) → \"search\", <Header /> → \"Header\"
            """
            if func_node.type in identifier_nodes:
                return func_node.text.decode('utf-8', errors='ignore')
            parts = []
            n = func_node
            while n.type in member_nodes:
                names = [c for c in n.children if c.type in identifier_nodes]
                if names:
                    parts.append(names[-1].text.decode('utf-8', errors='ignore'))
                obj = next(iter(n.children), None)
                if obj and obj.type in member_nodes:
                    n = obj
                elif obj and obj.type in identifier_nodes:
                    parts.append(obj.text.decode('utf-8', errors='ignore'))
                    break
                elif obj and lang_config.get("language") == "python" and obj.type in call_nodes:
                    # Preserve the only receiver-call form we can resolve
                    # statically without type inference: super().method().
                    inner = obj.child_by_field_name("function")
                    if inner is not None:
                        receiver = _qualified_name(inner)
                        if receiver == "super":
                            parts.append(receiver)
                    break
                else:
                    break
            return '.'.join(reversed(parts))

        scan_root = root_node
        if lang_config.get("language") == "python":
            decorated = lang_config.get("decorated_definition", "decorated_definition")
            function_nodes = set(lang_config.get("function_nodes", []))
            method_nodes = set(lang_config.get("method_nodes", []))

            # Calls in decorators and default arguments execute when a function is
            # defined, not when its body runs. Start at the body for Python call
            # graph nodes, and skip nested definitions so their calls are owned by
            # their own qualified nodes instead of leaking into the parent.
            definition = root_node
            if root_node.type == decorated:
                definition = next(
                    (child for child in root_node.children
                     if child.type in function_nodes | method_nodes),
                    root_node,
                )
            if definition.type in function_nodes | method_nodes:
                body = definition.child_by_field_name("body")
                if body is None:
                    body_types = set(lang_config.get("body_nodes", []))
                    body = next(
                        (child for child in definition.children if child.type in body_types),
                        None,
                    )
                if body is not None:
                    scan_root = body

        def walk(node):
            if node.type in call_nodes:
                func_node = None
                for n in node.children:
                     if getattr(n, "field_name", None) == "function" or n.type in (identifier_nodes + member_nodes):
                          func_node = n
                          break
                if func_node:
                    _add_call(_qualified_name(func_node))
            elif node.type in jsx_nodes:
                tag_node = None
                for n in node.children:
                    if n.type in identifier_nodes:
                        tag_node = n
                        break
                if tag_node:
                    tag_name = _qualified_name(tag_node)
                    if tag_name and (tag_name[0].isupper() or '.' in tag_name):
                        _add_call(tag_name)
            for child in node.children:
                if (lang_config.get("language") == "python"
                        and child.type in prune_nodes):
                    continue
                walk(child)

        walk(scan_root)
        return calls

    @staticmethod
    def _node_types(lang_config: dict, key: str) -> set:
        """Resolve a node-type config key into a set (falling back to defaults)."""
        value = lang_config.get(key, DEFAULT_NODE_TYPES.get(key, []))
        if isinstance(value, str):
            return {value}
        return set(value or [])

    @classmethod
    def _is_call_node(cls, node, lang_config: dict) -> bool:
        return getattr(node, 'type', None) in cls._node_types(lang_config, "call_nodes")

    @classmethod
    def _is_string_node(cls, node, lang_config: dict) -> bool:
        return getattr(node, 'type', None) in cls._node_types(lang_config, "string_nodes")

    @staticmethod
    def _make_synthetic_name(kind: str, file_path: str, line: int) -> str:
        """Generate a synthetic node ID for anonymous inline handlers (lambdas, arrow fns).
        
        Example: _make_synthetic_name('middleware', 'api.py', 42)
          → '__anon__middleware__api.py__L42'
        """
        file_stem = file_path.replace('/', '_').replace('\\', '_').replace('.', '_')
        return f"__anon__{kind}__{file_stem}__L{line}"

    def _extract_string_arg(self, node, code_bytes: bytes) -> str:
        """Extract a string literal value from an AST node."""
        import re as _re
        text = node.text.decode('utf-8', errors='replace')
        # Template/f-string with interpolation children
        if node.type in ('template_string', 'interpreted_string_literal', 'formatted_string'):
            raw = _re.sub(r'^[bfruBFURu]*["\'`]', '', text)
            raw = _re.split(r'\$\{|{', raw)[0]
            raw = raw.rstrip('"\'`')
            return raw
        # Python f-string (tree-sitter represents as `string` with string_content/interpolation children)
        if node.type == 'string' and any(c.type == 'interpolation' for c in node.children):
            parts = []
            for c in node.children:
                if c.type == 'string_content':
                    parts.append(c.text.decode('utf-8', errors='replace'))
                elif c.type == 'interpolation' and not parts:
                    continue  # interpolation before any content: skip, keep looking
                elif c.type == 'interpolation':
                    break  # stop at first interpolation after content
            return ''.join(parts)
        m = _re.match(r'[rRfFbBuU]*(?:["\'])(.*?)(?:["\'])', text)
        if not m:
            m = _re.match(r'["\'](.*?)["\']', text)
        return m.group(1) if m else ''

    def _collect_router_prefixes(self, root_node, lang_config: dict = None) -> dict:
        """Pre-pass: collect router variable → prefix mappings.
        
        Detects: router = APIRouter(prefix="/api/v1")
                 router = Router(prefix="/api/v1")
        Returns dict like {"router": "/api/v1"}.
        """
        import re as _re
        lang_config = lang_config or {}
        prefixes = {}
        assignment_nodes = lang_config.get("assignment_nodes", ["assignment"])
        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"])

        def walk(n):
            if n.type in assignment_nodes:
                children = list(n.children)
                if len(children) >= 2 and children[0].type in identifier_nodes:
                    var_name = children[0].text.decode('utf-8', errors='replace')
                    rhs = children[-1]
                    if self._is_call_node(rhs, lang_config):
                        rhs_text = rhs.text.decode('utf-8', errors='replace')
                        # Match APIRouter(prefix=...) or Router(prefix=...) or similar
                        m = _re.match(r'(?:APIRouter|Router|DefaultRouter)\s*\(', rhs_text)
                        if m:
                            # Extract prefix= keyword arg
                            p_m = _re.search(r'prefix\s*=\s*["\']([^"\']+)["\']', rhs_text)
                            if p_m:
                                prefixes[var_name] = p_m.group(1)
            for c in n.children:
                walk(c)

        walk(root_node)
        return prefixes

    def _extract_middleware(self, root_node, code_bytes: bytes, file_path: str = '', lang_config: dict = None) -> list:
        """Extract middleware registrations from the AST.

        Three universal patterns:
          1. Call-based:  obj.use(handler) — no URL, or obj.add_middleware(handler)
          2. Decorator-based: @obj.middleware("http"), @obj.before_request
          3. Declarative list: MIDDLEWARE = ["path.to.Middleware", ...]

        Returns a list of middleware dicts with fields:
          name, type, handler_var, source_var, url (scope), middleware_type, line
        """
        import re as _re
        lang_config = lang_config or {}
        middleware = []

        argument_list_nodes = lang_config.get("argument_list_nodes", ["argument_list"])
        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"])
        comment_nodes = lang_config.get("comment_nodes", ["comment"])
        anonymous_fn_nodes = (
            lang_config.get("arrow_function_nodes", ["arrow_function"])
            + lang_config.get("function_expression_nodes", ["function_expression"])
            + lang_config.get("lambda_nodes", ["lambda"])
        )
        middleware_methods = set(lang_config.get("middleware_methods", ["add_middleware"]))
        middleware_decorator_prefixes = set(lang_config.get("middleware_decorator_prefixes", ["middleware", "before_request", "after_request"]))
        decorator_nodes = lang_config.get("decorator_nodes", ["decorator"])
        decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
        assignment_nodes = lang_config.get("assignment_nodes", ["assignment"])
        declarative_middleware_var = lang_config.get("declarative_middleware_var", "MIDDLEWARE")
        list_nodes = lang_config.get("list_nodes", ["list"])
        definition_nodes = (
            lang_config.get("function_nodes", ["function_definition"])
            + lang_config.get("class_nodes", ["class_definition"])
            + lang_config.get("function_declaration_nodes", ["function_declaration"])
            + lang_config.get("method_nodes", ["method_definition"])
        )

        def _arg_list_children(call_node):
            for c in call_node.children:
                if c.type in argument_list_nodes:
                    return [a for a in c.children
                            if a.type not in (',', '(', ')') + tuple(comment_nodes)]
            return []

        def _first_identifier_arg(call_node) -> str:
            """Return the name of the first identifier/arrow-function positional arg."""
            args = _arg_list_children(call_node)
            for a in args:
                if a.type in identifier_nodes:
                    return a.text.decode('utf-8', errors='replace')
                # Anonymous inline function — generate synthetic name
                if a.type in anonymous_fn_nodes:
                    line = a.start_point[0] + 1
                    return self._make_synthetic_name('middleware', file_path, line)
                # Nested call (e.g. middleware() returning a handler)
                if self._is_call_node(a, lang_config):
                    return a.text.decode('utf-8', errors='replace').split('(')[0]
            return ''

        def _is_declarative_middleware_list(node) -> bool:
            """Check if node is an assignment with a MIDDLEWARE-like list."""
            if node.type not in assignment_nodes:
                return False
            children = list(node.children)
            if len(children) < 2:
                return False
            lhs = children[0]
            if lhs.type not in ('identifier',) or not _re.match(
                    r'^' + declarative_middleware_var, lhs.text.decode('utf-8', errors='replace'), _re.I):
                return False
            rhs = children[-1]
            return rhs.type in list_nodes

        def walk(node):
            # ── Pattern 1: Call-based middleware (obj.use, obj.add_middleware) ──
            if self._is_call_node(node, lang_config):
                parent = node.parent
                # Skip decorators (handled in Pattern 2) and assignments
                if parent and parent.type in decorator_nodes + assignment_nodes + lang_config.get("variable_declarator_nodes", ["variable_declarator"]):
                    pass
                else:
                    call_text = node.text.decode('utf-8', errors='replace')
                    dot_m = _re.match(r'([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(',
                                       call_text)
                    if dot_m:
                        var_name = dot_m.group(1)
                        method_name = dot_m.group(2).lower()
                        if method_name in middleware_methods:
                            # Only catch url-less .use() — .add_middleware always
                            if method_name == 'use':
                                s_node = None
                                for c in node.children:
                                    if c.type in argument_list_nodes:
                                        for a in c.children:
                                            if self._is_string_node(a, lang_config):
                                                s_node = a
                                                break
                                        break
                                if s_node is not None:
                                    url = self._extract_string_arg(s_node, code_bytes)
                                    if url.startswith('/'):
                                        return  # has a URL path → mount, not middleware
                            handler = _first_identifier_arg(node)
                            if handler:
                                middleware.append({
                                    "name": handler,
                                    "type": "middleware",
                                    "handler_var": handler,
                                    "source_var": var_name,
                                    "url": '',
                                    "middleware_type": "call",
                                    "line": node.start_point[0] + 1,
                                })

            # ── Pattern 2: Decorator-based middleware (@obj.middleware, @obj.before_request) ──
            if node.type == decorated_definition:
                for child in node.children:
                    if child.type in decorator_nodes:
                        dec_text = child.text.decode('utf-8', errors='replace')
                        dot_m = _re.match(
                            r'@([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(',
                            dec_text
                        )
                        if dot_m:
                            var_name = dot_m.group(1)
                            method_name = dot_m.group(2).lower()
                            if method_name in middleware_decorator_prefixes:
                                func_name = None
                                for c2 in node.children:
                                    if c2.type in definition_nodes:
                                        for x in c2.children:
                                            if x.type in identifier_nodes:
                                                func_name = x.text.decode(
                                                    'utf-8', errors='replace')
                                                break
                                        break
                                if func_name:
                                    middleware.append({
                                        "name": func_name,
                                        "type": "middleware",
                                        "handler_var": func_name,
                                        "source_var": var_name,
                                        "url": '',
                                        "middleware_type": "decorator",
                                        "line": node.start_point[0] + 1,
                                    })

            # ── Pattern 3: Declarative list (MIDDLEWARE = [...]) ──
            if _is_declarative_middleware_list(node):
                rhs = list(node.children)[-1]
                for item_node in rhs.children:
                    if self._is_string_node(item_node, lang_config):
                        mw_path = self._extract_string_arg(item_node, code_bytes)
                        if mw_path:
                            middleware.append({
                                "name": mw_path,
                                "type": "middleware",
                                "handler_var": mw_path,
                                "source_var": declarative_middleware_var,
                                "url": '',
                                "middleware_type": "declarative",
                                "line": item_node.start_point[0] + 1,
                            })

            for child in node.children:
                walk(child)

        walk(root_node)
        return middleware

    def _extract_routes(self, root_node, code_bytes: bytes, lang_config: dict = None) -> list:
        """Unified route extractor — driven by per-language config.

        Finds both decorator-based (@app.get, @router.post) and call-based
        (router.add_router, app.use, app.get) route registrations.

        Pre-resolves router prefixes so @router.get("/{id}") on a router
        instantiated as APIRouter(prefix="/api/v1") yields "/api/v1/{id}".
        """
        import re as _re
        lang_config = lang_config or {}
        router_prefixes = self._collect_router_prefixes(root_node, lang_config)
        controller_prefixes = {}  # class_name → prefix from @Controller('/prefix')
        routes = []
        inline_handlers = []  # anonymous arrow/function route handlers → traceable Function nodes

        route_methods = set(lang_config.get("route_methods", ["get", "post", "put", "delete", "patch", "head", "options", "route", "add_route", "add_url_rule", "api_operation", "add_router", "include_router", "mount", "use"]))
        route_functions = set(lang_config.get("route_functions", ["path", "re_path"]))
        http_methods = set(lang_config.get("http_methods", ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]))
        mount_methods = {'add_router', 'include_router', 'mount', 'use', 'register_blueprint'}
        controller_decorators = set(lang_config.get("controller_decorators", ["Controller"]))
        method_decorators = set(lang_config.get("method_decorators", ["Get", "Post", "Put", "Delete", "Patch", "Head", "Options"]))
        jsx_route_tag = lang_config.get("jsx_route_tag", "Route")
        jsx_browser_router_fn = lang_config.get("jsx_browser_router_fn", "createBrowserRouter")
        enable_jsx_routes = lang_config.get("jsx_routes", False)
        enable_trpc_routes = lang_config.get("trpc_routes", False)
        decorated_definition = lang_config.get("decorated_definition", "decorated_definition")
        decorator_nodes = lang_config.get("decorator_nodes", ["decorator"])
        class_nodes = lang_config.get("class_nodes", ["class_definition"])
        class_declaration_nodes = lang_config.get("class_declaration_nodes", ["class_declaration"])
        function_nodes = lang_config.get("function_nodes", ["function_definition"])
        function_declaration_nodes = lang_config.get("function_declaration_nodes", ["function_declaration"])
        method_nodes = lang_config.get("method_nodes", ["method_definition"])
        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"])
        member_nodes = lang_config.get("member_nodes", ["attribute", "member_expression"])
        argument_list_nodes = lang_config.get("argument_list_nodes", ["argument_list"])
        call_nodes = lang_config.get("call_nodes", ["call"])
        string_nodes = lang_config.get("string_nodes", ["string"])
        comment_nodes = lang_config.get("comment_nodes", ["comment"])
        keyword_argument_nodes = lang_config.get("keyword_argument_nodes", ["keyword_argument"])
        export_prefix_skips = lang_config.get("export_prefix_skips", ["export"])
        jsx_nodes = lang_config.get("jsx_nodes", ["jsx_self_closing_element", "jsx_opening_element"])
        jsx_self_closing_nodes = {"jsx_self_closing_element"}

        def _find_controller_prefix(node) -> str:
            """Walk up AST parent chain to find a class-level @Controller prefix."""
            p = getattr(node, 'parent', None)
            while p is not None:
                if p.type == decorated_definition:
                    for c in p.children:
                        if c.type in (class_nodes + class_declaration_nodes):
                            for name_child in c.children:
                                if name_child.type in identifier_nodes:
                                    cn = name_child.text.decode('utf-8', errors='replace')
                                    if cn in controller_prefixes:
                                        return controller_prefixes[cn]
                elif p.type in class_declaration_nodes:
                    # TypeScript: class_declaration is the parent of class_body
                    for name_child in p.children:
                        if name_child.type in identifier_nodes:
                            cn = name_child.text.decode('utf-8', errors='replace')
                            if cn in controller_prefixes:
                                return controller_prefixes[cn]
                p = getattr(p, 'parent', None)
            return ''

        def _apply_prefix(var_name: str, url: str) -> str:
            """Prepend router prefix if var_name has one registered."""
            prefix = router_prefixes.get(var_name, '')
            if prefix:
                combined = prefix.rstrip('/') + '/' + url.lstrip('/')
                return combined
            return url

        def _func_name_under_decorator(node):
            for c2 in node.children:
                if c2.type in (function_nodes + class_nodes + function_declaration_nodes + method_nodes):
                    for x in c2.children:
                        if x.type in identifier_nodes:
                            return x.text.decode('utf-8', errors='replace')
            return None

        def _arg_list_children(call_node):
            """Get children of the enclosing argument/argument_list node."""
            for c in call_node.children:
                if c.type in argument_list_nodes:
                    return [a for a in c.children
                            if a.type not in (',', '(', ')') + tuple(comment_nodes)]
            return []

        def _first_string_child(call_node):
            """Find the first positional string argument in a call."""
            args = _arg_list_children(call_node)
            for a in args:
                if self._is_string_node(a, lang_config):
                    return a
                # Stop at first non-keyword non-string positional arg
                if a.type not in keyword_argument_nodes:
                    break
            return None

        def _kwarg_string_value(call_node, names):
            """Return the string value of the first keyword argument whose name
            is in `names` (e.g. FastAPI include_router(router, prefix="/api/v1"))."""
            for a in _arg_list_children(call_node):
                if a.type not in keyword_argument_nodes:
                    continue
                kids = [k for k in a.children
                        if k.type not in (',', '=', ':') + tuple(comment_nodes)]
                if len(kids) >= 2 and kids[0].type in identifier_nodes:
                    kw_name = kids[0].text.decode('utf-8', errors='replace')
                    if kw_name in names and self._is_string_node(kids[1], lang_config):
                        return self._extract_string_arg(kids[1], code_bytes)
            return None

        def _get_call_func_name(call_node) -> str:
            """Get the bare function name of a call (e.g. 'path' from path(...))."""
            call_text = call_node.text.decode('utf-8', errors='replace')
            m = _re.match(r'([a-zA-Z_]\w*)\s*\(', call_text)
            if m:
                return m.group(1)
            return ''

        def _extract_url(node):
            """Extract URL from first string arg.
            
            Returns URL if:
              - first string arg starts with '/' (most frameworks), OR
              - call function name is one of route_functions (Django path/re_path)
            Falls back to regex search on full call text for chained calls
            like app.route('/path').get(handler).
            Empty strings from path()/re_path() → '/' (root route).
            """
            s_node = _first_string_child(node)
            if s_node is not None:
                url = self._extract_string_arg(s_node, code_bytes)
                if url.startswith('/'):
                    return url
                if _get_call_func_name(node) in route_functions:
                    return url if url else '/'
            # Keyword-arg URLs: include_router(router, prefix="/api/v1"),
            # register_blueprint(bp, url_prefix="/api"), add_url_rule("/x", rule=...), etc.
            kw_url = _kwarg_string_value(node, {'prefix', 'url_prefix', 'url', 'path', 'rule'})
            if kw_url and kw_url.startswith('/'):
                return kw_url
            # Fallback for chained calls: check if function part has nested call
            # e.g. app.route('/path').get(handler) — app.route('/path') is nested
            func_part = next((c for c in node.children if c.type in member_nodes), None)
            if func_part:
                for fc in func_part.children:
                    if self._is_call_node(fc, lang_config):
                        inner_url = _extract_url(fc)
                        if inner_url:
                            return inner_url
            return ''

        def _extract_methods(dec_text: str, method_name: str, call_node) -> list:
            """Extract HTTP methods from a decorator/call."""
            if method_name == 'api_operation':
                return [m.upper() for m in
                        _re.findall(r"[\"'](\w+)[\"']", dec_text)
                        if m.upper() in http_methods]
            if method_name in ('route', 'add_url_rule', 'add_route'):
                # Flask @app.route('/path', methods=['GET', "POST"])
                if 'methods=' in dec_text:
                    return [m.upper() for m in
                            _re.findall(r"[\"'](\w+)[\"']", dec_text.split('methods=')[-1])
                            if m.upper() in http_methods] or ['GET']
                return ['GET']
            # For http method names (get, post, put, ...): use the method itself
            if method_name in ('get', 'post', 'put', 'delete', 'patch', 'head', 'options'):
                return [method_name.upper()]
            return []

        def _extract_second_arg(call_node):
            """For mount calls: extract the second positional arg (sub-router var name)."""
            args = _arg_list_children(call_node)
            if len(args) >= 2:
                a2 = args[1]
                if a2.type in (identifier_nodes + member_nodes):
                    return a2.text.decode('utf-8', errors='replace')
            return ''

        def _first_positional_var(call_node):
            """First non-keyword identifier/attribute arg (e.g. `items.router` of
            include_router(items.router, prefix=...))."""
            for a in _arg_list_children(call_node):
                if a.type in keyword_argument_nodes:
                    continue
                if a.type in (identifier_nodes + member_nodes):
                    return a.text.decode('utf-8', errors='replace')
                break
            return ''

        def _extract_inline_handler(call_node, method_name):
            """If the handler argument of a route call is an anonymous arrow /
            function expression, extract it as a pseudo-function so the route's
            view_name resolves to an indexed, traceable Function node.

            Name format: handler_{method}_L{line} — unique per file, and the
            route's handler_var/view_name points at it, so resolve_url_patterns
            and _resolve_middleware_pipeline can link Route -> handler.
            """
            fn_types = (
                set(lang_config.get("arrow_function_nodes", ["arrow_function"]))
                | set(lang_config.get("function_expression_nodes", ["function_expression"]))
            )
            args = _arg_list_children(call_node)
            handler_node = None
            seen_url = False
            for a in args:
                if self._is_string_node(a, lang_config):
                    seen_url = True
                    continue
                if a.type in keyword_argument_nodes:
                    continue
                if not seen_url:
                    continue
                handler_node = a
                break
            if handler_node is None or handler_node.type not in fn_types:
                return None
            line = handler_node.start_point[0] + 1
            info = self._extract_node_info(handler_node, code_bytes, lang_config)
            info["name"] = f"handler_{method_name}_L{line}"
            info["calls"] = self._extract_calls(handler_node, code_bytes, lang_config)
            info["returns"] = self._extract_returns(handler_node, code_bytes, lang_config)
            return info

        def walk(node):
            if node.type == decorated_definition:
                for child in node.children:
                    if child.type in decorator_nodes:
                        dec_text = child.text.decode('utf-8', errors='replace')
                        # Django @api_view(['GET', "POST"])
                        if 'api_view' in dec_text:
                            methods = [m.upper() for m in
                                       _re.findall(r"[\"'](\w+)[\"']", dec_text)
                                       if m.upper() in http_methods]
                            func_name = _func_name_under_decorator(node)
                            if func_name:
                                routes.append({
                                    "function": func_name,
                                    "methods": methods,
                                    "url": '',
                                    "type": "decorator",
                                    "source_var": '',
                                    "handler_var": '',
                                })
                            continue
                        # NestJS bare decorators: @Get(':id'), @Controller('/users')
                        bare_m = _re.match(
                            r'@(Get|Post|Put|Delete|Patch|Head|Options|Controller)\s*\(',
                            dec_text
                        )
                        if bare_m:
                            bare_name = bare_m.group(1).lower()
                            if bare_name == 'controller':
                                # @Controller('/prefix') — store prefix for this class
                                call_node = next((c for c in child.children if c.type in call_nodes), child)
                                s_node = _first_string_child(call_node)
                                if s_node:
                                    ctrl_url = self._extract_string_arg(s_node, code_bytes)
                                    if ctrl_url:
                                        class_name = _func_name_under_decorator(node)
                                        if class_name:
                                            controller_prefixes[class_name] = ctrl_url.lstrip('/')
                                continue
                            if bare_name in ('get', 'post', 'put', 'delete', 'patch', 'head', 'options'):
                                call_node = next((c for c in child.children if c.type in call_nodes), child)
                                s_node = _first_string_child(call_node)
                                url = self._extract_string_arg(s_node, code_bytes) if s_node else ''
                                methods = [bare_name.upper()]
                                func_name = _func_name_under_decorator(node)
                                if func_name:
                                    # Check if parent class has a controller prefix
                                    ctrl_prefix = _find_controller_prefix(node)
                                    full_url = '/' + '/'.join(
                                        p for p in (ctrl_prefix.strip('/'), url.strip('/')) if p
                                    ) if (ctrl_prefix or url) else '/'
                                    routes.append({
                                        "function": func_name,
                                        "methods": methods,
                                        "url": full_url,
                                        "type": "decorator",
                                        "source_var": '',
                                        "handler_var": '',
                                    })
                                continue
                        # @<var>.<method>(<url>, ...)
                        dot_m = _re.match(
                            r'@([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(',
                            dec_text
                        )
                        if dot_m:
                            var_name = dot_m.group(1)
                            method_name = dot_m.group(2).lower()
                            if method_name in route_methods:
                                call_node = next((c for c in child.children if c.type in call_nodes), child)
                                # api_operation uses keyword arg url=, others use positional
                                if method_name == 'api_operation':
                                    url_m = _re.search(r"url\s*=\s*['\"]([^'\"]+)['\"]", dec_text)
                                    url = url_m.group(1) if url_m else ''
                                else:
                                    url = _extract_url(call_node)
                                methods = _extract_methods(dec_text, method_name, call_node)
                                func_name = _func_name_under_decorator(node)
                                if func_name:
                                    routes.append({
                                        "function": func_name,
                                        "methods": methods,
                                        "url": _apply_prefix(var_name, url),
                                        "type": "decorator",
                                        "source_var": var_name,
                                        "handler_var": '',
                                    })
                            continue

            elif node.type in decorator_nodes:
                # TypeScript/JavaScript decorators (NestJS @Get, @Controller, etc.)
                # These are NOT wrapped in decorated_definition (that's Python-specific)
                dec_text = node.text.decode('utf-8', errors='replace')
                bare_m = _re.match(
                    r'@(Get|Post|Put|Delete|Patch|Head|Options|Controller)\s*\(',
                    dec_text
                )
                if bare_m:
                    bare_name = bare_m.group(1).lower()
                    if bare_name == 'controller':
                        call_node = next((c for c in node.children if c.type in call_nodes), node)
                        s_node = _first_string_child(call_node)
                        if s_node:
                            ctrl_url = self._extract_string_arg(s_node, code_bytes)
                            if ctrl_url:
                                parent = node.parent
                                if parent:
                                    siblings = list(parent.children)
                                    idx = siblings.index(node) if node in siblings else -1
                                    for sib in siblings[idx+1:]:
                                        if sib.type in class_declaration_nodes:
                                            for x in sib.children:
                                                if x.type in identifier_nodes:
                                                    controller_prefixes[x.text.decode('utf-8', errors='replace')] = ctrl_url.lstrip('/')
                                                    break
                                        elif sib.type in export_prefix_skips:
                                            continue
                                        break
                    if bare_name in ('get', 'post', 'put', 'delete', 'patch', 'head', 'options'):
                        call_node = next((c for c in node.children if c.type in call_nodes), node)
                        s_node = _first_string_child(call_node)
                        url = self._extract_string_arg(s_node, code_bytes) if s_node else ''
                        methods = [bare_name.upper()]
                        func_name = ''
                        parent = node.parent
                        if parent:
                            siblings = list(parent.children)
                            idx = siblings.index(node) if node in siblings else -1
                            for sib in siblings[idx+1:]:
                                if sib.type in (method_nodes + function_declaration_nodes + function_nodes):
                                    for x in sib.children:
                                        if x.type in identifier_nodes:
                                            func_name = x.text.decode('utf-8', errors='replace')
                                            break
                                elif sib.type in export_prefix_skips:
                                    continue
                                break
                        if func_name:
                            ctrl_prefix = _find_controller_prefix(node)
                            full_url = '/' + '/'.join(
                                        p for p in (ctrl_prefix.strip('/'), url.strip('/')) if p
                                    ) if (ctrl_prefix or url) else '/'
                            routes.append({
                                "function": func_name,
                                "methods": methods,
                                "url": full_url,
                                "type": "decorator",
                                "source_var": '',
                                "handler_var": '',
                            })

            elif self._is_call_node(node, lang_config):
                parent = node.parent
                if parent and parent.type in decorator_nodes:
                    pass  # handled above
                # Skip context where result is assigned to a variable (false positive risk)
                # But allow tRPC t.router({...}) through
                elif parent and parent.type in lang_config.get("variable_declarator_nodes", ["variable_declarator"]):
                    call_text = node.text.decode('utf-8', errors='replace')
                    dot_m = _re.match(
                        r'([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(',
                        call_text
                    )
                    if enable_trpc_routes and dot_m and dot_m.group(2).lower() == 'router':
                        # tRPC router call assigned to variable — still process
                        trpc_routes = _extract_trpc_routes(node)
                        routes.extend(trpc_routes)
                    else:
                        pass
                elif parent and parent.type in lang_config.get("assignment_nodes", ["assignment"]):
                    pass
                else:
                    call_text = node.text.decode('utf-8', errors='replace')
                    dot_m = _re.match(
                        r'([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)\s*\(',
                        call_text
                    )
                    if dot_m:
                        var_name = dot_m.group(1)
                        method_name = dot_m.group(2).lower()
                        if method_name in route_methods:
                            if enable_trpc_routes and method_name == 'router':
                                # tRPC: t.router({key: procedure.query()/.mutation()})
                                trpc_routes = _extract_trpc_routes(node)
                                routes.extend(trpc_routes)
                            else:
                                url = _extract_url(node)
                                if url:
                                    methods = _extract_methods(call_text, method_name, node)
                                    handler_var = ''
                                    inline_handler = _extract_inline_handler(node, method_name)
                                    if inline_handler:
                                        handler_var = inline_handler["name"]
                                        inline_handlers.append(inline_handler)
                                    elif method_name in mount_methods:
                                        handler_var = _extract_second_arg(node)
                                        if not handler_var:
                                            handler_var = _first_positional_var(node)
                                    # HTTP verbs on a router/app object are ENDPOINTS
                                    # (router.get('/x', h)); true mounts (use, mount,
                                    # add_router, include_router, register_blueprint)
                                    # attach sub-routers and get prefix-combined later.
                                    if method_name in mount_methods:
                                        routes.append({
                                            "function": '',
                                            "methods": methods,
                                            "url": _apply_prefix(var_name, url),
                                            "type": "mount",
                                            "source_var": var_name,
                                            "handler_var": handler_var,
                                        })
                                    else:
                                        routes.append({
                                            "function": '',
                                            "methods": methods,
                                            "url": _apply_prefix(var_name, url),
                                            "type": "endpoint_call",
                                            "source_var": var_name,
                                            "handler_var": handler_var,
                                        })
                    else:
                        # Bare function call: path(...), re_path(...)
                        bare_m = _re.match(r'([a-zA-Z_]\w*)\s*\(', call_text)
                        if bare_m:
                            func_name = bare_m.group(1)
                            if func_name in route_functions:
                                url = _extract_url(node)
                                if url:
                                    routes.append({
                                        "function": '',
                                        "methods": ['GET'],
                                        "url": url,
                                        "type": "call",
                                        "source_var": '',
                                        "handler_var": '',
                                    })

            # JSX <Route path="..." element={<Comp />} /> (React Router v5/v6)
            if enable_jsx_routes and node.type in jsx_nodes:
                children_list = node.children if node.type in jsx_self_closing_nodes else node.children
                tag = next((c for c in children_list if c.type in identifier_nodes), None)
                if tag and tag.text.decode('utf-8', errors='replace') == jsx_route_tag:
                    path_val = ''
                    comp_val = ''
                    for attr in children_list:
                        if attr.type == 'jsx_attribute':
                            attr_name = next((c for c in (attr.children if hasattr(attr, 'children') else [])
                                              if c.type in identifier_nodes), None)
                            if attr_name:
                                an = attr_name.text.decode('utf-8', errors='replace')
                                if an == 'path':
                                    val_node = next((c for c in attr.children if c.type in (string_nodes + ['jsx_expression'])), None)
                                    if val_node:
                                        if val_node.type in string_nodes:
                                            path_val = self._extract_string_arg(val_node, code_bytes)
                                        elif val_node.type == 'jsx_expression':
                                            inner = val_node.text.decode('utf-8', errors='replace').strip('{} ')
                                            m = _re.match(r"""[`'"]([^`'"]+)[`'"]""", inner)
                                            if m:
                                                path_val = m.group(1)
                                elif an == 'element':
                                    comp_val = _extract_element_component(attr)
                    if path_val:
                        routes.append({
                            "function": comp_val,
                            "methods": ['GET'],
                            "url": path_val if path_val.startswith('/') else '/' + path_val,
                            "type": "jsx_route",
                            "source_var": '',
                            "handler_var": '',
                        })

            # createBrowserRouter([{path, element}, ...]) — React Router v6.4+
            if enable_jsx_routes and self._is_call_node(node, lang_config) and jsx_browser_router_fn in node.text.decode('utf-8', errors='replace'):
                router_configs = _extract_router_configs(node)
                for rc in router_configs:
                    routes.append({
                        "function": rc.get("element", ''),
                        "methods": ['GET'],
                        "url": rc.get("path", ''),
                        "type": "jsx_route",
                        "source_var": '',
                        "handler_var": '',
                    })

            for child in node.children:
                walk(child)

        def _extract_element_component(node):
            """Extract component name from element={<Comp />} attribute value."""
            search_nodes = []
            if node.type == 'jsx_expression':
                search_nodes.append(node)
            else:
                search_nodes.extend(getattr(node, 'children', []))
            for n in search_nodes:
                if n.type == 'jsx_expression':
                    for inner in n.children:
                        if inner.type in ('jsx_self_closing_element', 'jsx_opening_element'):
                            tag = next((x for x in getattr(inner, 'children', [])
                                        if x.type == 'identifier'), None)
                            if tag:
                                return tag.text.decode('utf-8', errors='replace')
            return ''

        def _extract_router_configs(call_node):
            """Extract route configs from createBrowserRouter([{path, element}, ...])."""
            configs = []
            for c in call_node.children:
                if c.type == 'arguments':
                    for arg in c.children:
                        if arg.type == 'array':
                            for item in arg.children:
                                if item.type == 'object':
                                    config = {}
                                    for prop in item.children:
                                        if prop.type == 'pair':
                                            key = next((x for x in prop.children if x.type == 'property_identifier'), None)
                                            val = next((x for x in prop.children if x.type in ('string', 'jsx_expression')), None)
                                            if key and val:
                                                k = key.text.decode('utf-8', errors='replace')
                                                if k == 'path' and val.type == 'string':
                                                    config['path'] = _re.sub(r'^["\']|["\']$', '', val.text.decode('utf-8', errors='replace'))
                                                elif k == 'element' and val.type == 'jsx_expression':
                                                    config['element'] = _extract_element_component(val)
                                    if 'path' in config:
                                        configs.append(config)
                                    # Handle nested children
                                    for prop in item.children:
                                        if prop.type == 'pair':
                                            key = next((x for x in prop.children if x.type == 'property_identifier'), None)
                                            val = next((x for x in prop.children if x.type == 'array'), None)
                                            if key and val and key.text.decode('utf-8', errors='replace') == 'children':
                                                for child_item in val.children:
                                                    if child_item.type == 'object':
                                                        child_config = {}
                                                        for cprop in child_item.children:
                                                            if cprop.type == 'pair':
                                                                ck = next((x for x in cprop.children if x.type == 'property_identifier'), None)
                                                                cv = next((x for x in cprop.children if x.type in ('string', 'jsx_expression')), None)
                                                                if ck and cv:
                                                                    kn = ck.text.decode('utf-8', errors='replace')
                                                                    if kn == 'path' and cv.type == 'string':
                                                                        child_config['path'] = (config.get('path', '') + '/' + 
                                                                            _re.sub(r'^["\']|["\']$', '', cv.text.decode('utf-8', errors='replace'))).rstrip('/')
                                                                    elif kn == 'element' and cv.type == 'jsx_expression':
                                                                        child_config['element'] = _extract_element_component(cv)
                                                        if 'path' in child_config:
                                                            configs.append(child_config)
            return configs

        def _extract_trpc_routes(call_node):
            """Extract tRPC routes from t.router({key: procedure.query()/.mutation()})."""
            trpc_routes = []
            for c in call_node.children:
                if c.type in ('argument_list', 'arguments'):
                    for arg in c.children:
                        if arg.type == 'object':
                            for item in arg.children:
                                if item.type == 'pair':
                                    key_node = next((x for x in item.children if x.type in ('identifier', 'property_identifier', 'string')), None)
                                    val_node = next((x for x in item.children if x.type == 'call_expression'), None)
                                    if key_node and val_node:
                                        key = key_node.text.decode('utf-8', errors='replace').strip('"\'` ')
                                        val_text = val_node.text.decode('utf-8', errors='replace')
                                        method = 'POST' if '.mutation' in val_text else 'GET'
                                        url = '/' + key
                                        trpc_routes.append({
                                            "function": key,
                                            "methods": [method],
                                            "url": url,
                                            "type": "jsx_route",
                                            "source_var": '',
                                            "handler_var": '',
                                        })
            return trpc_routes

        walk(root_node)
        return routes, inline_handlers

    def _extract_http_calls(self, root_node, code_bytes: bytes, lang_config: dict = None) -> list:
        """Detect frontend HTTP calls (fetch, axios) and return call info."""
        import re
        lang_config = lang_config or {}
        http_calls = []

        call_nodes = lang_config.get("call_nodes", ["call"])
        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"])
        member_nodes = lang_config.get("member_nodes", ["attribute", "member_expression"])
        decorator_nodes = lang_config.get("decorator_nodes", ["decorator"])

        def clean_url(u: str) -> str:
            # Replace JS template literals like ${id} with {id} for clean path normalization
            return re.sub(r"\$\{[^}]+\}", "{id}", u).strip()

        # Pre-scan for URL builder functions emitted by generated API clients
        # (orval / openapi-typescript): `const getReturnRentalUrl = (id) =>
        # ` + '`/api/rentals/${id}/return`' + ``.  Custom wrappers call these
        # indirection functions instead of literal URLs, so we map builder name
        # -> URL to resolve them below.
        try:
            builder_text = code_bytes.decode('utf-8', errors='ignore')
            url_builders: dict[str, str] = {}
            for m in re.finditer(
                r"const\s+(get\w*(?:[Uu]rl|[Pp]ath))\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>\s*(?:\{\s*return\s*)?[`'\"]\s*([^`'\"]+?)\s*[`'\"]",
                builder_text,
            ):
                url_builders[m.group(1)] = clean_url(m.group(2))
        except Exception:
            url_builders = {}

        def walk(node):
            if node.type in call_nodes:
                # Skip decorator-style calls (@api.get, @app.post, etc.)
                parent = node.parent
                if parent and parent.type in decorator_nodes:
                    return
                
                call_text = node.text.decode('utf-8', errors='ignore')
                func_node = None
                for n in node.children:
                    if getattr(n, "field_name", None) == "function" or n.type in (identifier_nodes + member_nodes):
                        func_node = n
                        break
                
                if func_node:
                    func_text = func_node.text.decode('utf-8', errors='ignore')
                    line_no = node.start_point[0] + 1
                    
                    # fetch('/api/...') or fetch("https://...") or fetch(`...`)
                    if func_text == 'fetch' or func_text.endswith('.fetch'):
                        url_match = re.search(r"fetch\s*\(\s*[`'\"]([^`'\"]+)[`'\"]", call_text)
                        if url_match:
                            url = clean_url(url_match.group(1))
                            method = 'GET'
                            if 'method:' in call_text or 'method :' in call_text:
                                method_match = re.search(r"method['\"]?\s*:\s*['\"](\w+)['\"]", call_text)
                                if method_match:
                                    method = method_match.group(1).upper()
                            http_calls.append({
                                "url": url,
                                "method": method,
                                "lib": "fetch",
                                "line": line_no
                            })
                    
                    # axios.get('/api/...'), axios.post('/api/...'), etc.
                    elif func_text.startswith('axios.') or '.axios.' in func_text:
                        parts = func_text.split('.')
                        method_part = parts[-1].lower()
                        if method_part in ('get', 'post', 'put', 'delete', 'patch', 'head', 'options'):
                            url_match = re.search(rf"\.{method_part}\s*\(\s*[`'\"]([^`'\"]+)[`'\"]", call_text)
                            if url_match:
                                http_calls.append({
                                    "url": clean_url(url_match.group(1)),
                                    "method": method_part.upper(),
                                    "lib": "axios",
                                    "line": line_no
                                })
                    
                    # axios({method: 'GET', url: '/api/...'}) - axios config form
                    elif func_text == 'axios':
                        url_match = re.search(r"url\s*:\s*([`'\"]) ([^`'\"]+) \1", call_text, re.VERBOSE)
                        if not url_match:
                            url_match = re.search(r"url\s*:\s*[`'\"]([^`'\"]+)[`'\"]", call_text)
                        method_match = re.search(r"method\s*:\s*['\"](\w+)['\"]", call_text)
                        if url_match:
                            http_calls.append({
                                "url": clean_url(url_match.group(1)),
                                "method": method_match.group(1).upper() if method_match else 'GET',
                                "lib": "axios",
                                "line": line_no
                            })
                    
                    # Custom API wrappers like apiFetch('/api/...'), apiClient.get('/api/...')
                    elif any(kw in func_text.lower() for kw in ['api', 'fetch', 'request', 'http']):
                        # Match first arg that looks like a URL path (starts with /),
                        # or an indirect URL builder: customFetch<Rental>(getXUrl(id), ...)
                        url = ''
                        name_re = re.escape(func_text)
                        first_arg_match = (
                            re.search(rf"{name_re}\s*<[^>]*>\s*\(\s*([^,)]+)", call_text)
                            or re.search(rf"{name_re}\s*\(\s*([^,)]+)", call_text)
                        )
                        if first_arg_match:
                            arg0 = first_arg_match.group(1).strip()
                            if arg0.startswith(("'", '"', "`")):
                                m = re.match(r"([`'\"])(.*?)\1", arg0)
                                if m:
                                    url = clean_url(m.group(2))
                            else:
                                # Builder indirection: getReturnRentalUrl(id) -> '/api/...'
                                bm = re.match(r"([a-zA-Z_]\w*)\s*\(", arg0)
                                if bm:
                                    url = url_builders.get(bm.group(1), '')
                        if url.startswith('/'):
                            # Determine method from function name
                            method = 'GET'
                            name_parts = func_text.lower().split('.')
                            last_part = name_parts[-1]
                            if last_part in ('post', 'put', 'patch', 'delete', 'del'):
                                method = last_part.upper() if last_part != 'del' else 'DELETE'
                            elif 'method:' in call_text or 'method :' in call_text:
                                method_match = re.search(r"method['\"]?\s*:\s*['\"](\w+)['\"]", call_text)
                                if method_match:
                                    method = method_match.group(1).upper()
                            http_calls.append({
                                "url": url,
                                "method": method,
                                "lib": func_text,
                                "line": line_no
                            })
                
            for child in node.children:
                walk(child)
        
        walk(root_node)
        return http_calls

    def _extract_url_patterns(self, root_node, code_bytes: bytes, lang_config: dict = None) -> list:
        """Extract Django URL patterns from urls.py files.
        
        Detects ``path()`` and ``re_path()`` calls inside ``urlpatterns = [...]``
        and returns structured route info.
        """
        import re as _re
        lang_config = lang_config or {}
        url_patterns = []

        assignment_nodes = lang_config.get("assignment_nodes", ["assignment"])
        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"])
        list_nodes = lang_config.get("list_nodes", ["list"])
        call_nodes = lang_config.get("call_nodes", ["call"])
        comment_nodes = lang_config.get("comment_nodes", ["comment"])
        url_patterns_var = lang_config.get("url_patterns_var", "urlpatterns")

        def _get_text(n):
            try:
                return n.text.decode('utf-8')
            except Exception:
                return ''
        
        def _walk(n):
            # Look for assignment: urlpatterns = [ ... ]
            if n.type in assignment_nodes:
                children = list(n.children)
                if len(children) >= 2 and children[0].type in identifier_nodes:
                    if _get_text(children[0]) == url_patterns_var:
                        rhs = children[-1]
                        if rhs.type in list_nodes:
                            for item in rhs.children:
                                if item.type in call_nodes:
                                    self._extract_path_call(item, url_patterns, code_bytes, lang_config)
                                elif item.type in comment_nodes:
                                    continue
            # Also handle augmented assignment or type-annotated: urlpatterns: list[...] = [...]
            for c in n.children:
                _walk(c)
        
        _walk(root_node)
        return url_patterns
    
    def _extract_path_call(self, call_node, url_patterns: list, code_bytes: bytes, lang_config: dict = None):
        """Extract a single ``path()`` or ``re_path()`` call and append to url_patterns."""
        import re as _re
        lang_config = lang_config or {}

        argument_list_nodes = lang_config.get("argument_list_nodes", ["argument_list"])
        string_nodes = lang_config.get("string_nodes", ["string"])
        call_nodes = lang_config.get("call_nodes", ["call"])
        comment_nodes = lang_config.get("comment_nodes", ["comment"])
        identifier_nodes = lang_config.get("identifier_nodes", ["identifier"])
        member_nodes = lang_config.get("member_nodes", ["attribute", "member_expression"])
        keyword_argument_nodes = lang_config.get("keyword_argument_nodes", ["keyword_argument"])
        route_functions = lang_config.get("route_functions", ["path", "re_path"])
        include_function = lang_config.get("include_function", "include")

        def _get_text(n):
            try:
                return n.text.decode('utf-8')
            except Exception:
                return ''
        
        children = list(call_node.children)
        func_node = children[0] if children else None
        if not func_node:
            return
        
        func_text = _get_text(func_node)
        # Must be path() or re_path()
        if func_text not in route_functions:
            return
        
        arg_list = None
        for c in children:
            if c.type in argument_list_nodes:
                arg_list = c
                break
        if not arg_list:
            return
        
        args = [a for a in arg_list.children if a.type not in (',',) + tuple(comment_nodes) + ('(', ')')]
        
        # First positional arg = URL pattern (string)
        url = ''
        if args and args[0].type in string_nodes:
            raw = _get_text(args[0])
            # Handle Python string prefixes: r"", f"", b"", u"", rf"", etc.
            m = _re.match(r'[rRfFbBuU]*(?:["\'])(.+?)(?:["\'])', raw)
            if not m:
                m = _re.match(r'["\'](.+?)["\']', raw)
            if m:
                url = m.group(1)
        
        if not url:
            # Empty string path → root route "/"
            # Still process for include() detection
            pass
        
        # Second positional arg = view (identifier/attribute)
        view_name = ''
        is_include = False
        if len(args) > 1:
            a2 = args[1]
            # Check for include(...) as second arg
            if a2.type in call_nodes:
                inc_func = a2.children[0] if a2.children else None
                if inc_func and _get_text(inc_func) == include_function:
                    is_include = True
                    # Extract the module string from include('module.urls')
                    inc_args = [x for x in (list(a2.children) if hasattr(a2, 'children') else []) 
                                if x.type in argument_list_nodes]
                    if inc_args:
                        first_arg = [x for x in inc_args[0].children if x.type in string_nodes]
                        if first_arg:
                            raw = _get_text(first_arg[0])
                            m = _re.match(r'["\'](.+?)["\']', raw)
                            if m:
                                view_name = f'include:{m.group(1)}'
            elif a2.type in (identifier_nodes + member_nodes):
                view_name = _get_text(a2)
        
        # Named argument: name='something'
        route_name = ''
        for kw in args:
            if kw.type in keyword_argument_nodes:
                kw_children = list(kw.children) if hasattr(kw, 'children') else []
                kw_id = None
                kw_val = None
                for i, kc in enumerate(kw_children):
                    if hasattr(kc, 'type') and kc.type in identifier_nodes:
                        kw_id = _get_text(kc)
                    elif hasattr(kc, 'type') and kc.type in string_nodes:
                        kw_val = _get_text(kc)
                if kw_id == 'name' and kw_val:
                    m = _re.match(r'[rRfFbBuU]*(?:["\'])(.+?)(?:["\'])', kw_val)
                    if not m:
                        m = _re.match(r'["\'](.+?)["\']', kw_val)
                    if m:
                        route_name = m.group(1)
        
        # Normalize empty string to "/" for root route
        normalized_url = url if url else "/"
        url_patterns.append({
            "url": normalized_url,
            "view_name": view_name,
            "name": route_name,
            "is_include": is_include,
            "func": func_text,
        })
    
    def _extract_file_based_routes(self, file_path: str, functions: list, exports: list) -> list:
        """Extract routes from file path for file-based routing frameworks.
        
        Supports:
          - Next.js App Router: app/api/users/[id]/route.ts → GET /api/users/:id
          - Next.js Pages Router: pages/api/users.ts → GET /api/users (default handler)
          - Nuxt 3: pages/users/[id].vue → /users/:id
        """
        import re as _re
        routes = []
        rel = file_path.replace('\\', '/')

        def _path_to_url(path_part: str, strip_segments: str = '') -> str:
            """Convert a file path segment to a URL route pattern."""
            p = _re.sub(r'\.[^/.]+$', '', path_part)
            p = _re.sub(r'/\([^/]+\)', '', p)
            p = _re.sub(r'\[([^\]]+)\]', r':\1', p)
            p = _re.sub(r'\[\.\.\.([^\]]+)\]', r':\1*', p)
            p = _re.sub(r'\[\[\.\.\.([^\]]+)\]\]', r':\1?', p)
            if strip_segments:
                p = _re.sub(r'/?(?:' + strip_segments + r')(?:$|(?=/))', '', p)
            p = p.strip('/')
            return '/' + p if p else '/'

        def _has_http_exported_func(functions, exports) -> bool:
            """Check if file has named HTTP-method exports (GET, POST, etc.)."""
            for func in functions:
                if func["name"].upper() in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'):
                    return True
            return False

        def _has_default_handler(functions, exports) -> bool:
            """Check if file has a default export function handler (Pages Router)."""
            for exp in exports:
                if exp.get('default'):
                    return True
            for func in functions:
                if func.get('is_exported') and func.get('name') == 'handler':
                    return True
            return False

        # ---- Next.js App Router (/app/) ----
        app_idx = rel.find('/app/')
        if app_idx >= 0:
            route_part = rel[app_idx + 5:]
            url = _path_to_url(route_part, strip_segments='page|route|layout|loading|error|not-found')
            for func in functions:
                if func["name"].upper() in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'):
                    routes.append({
                        "function": func["name"],
                        "methods": [func["name"].upper()],
                        "url": url,
                        "framework": "nextjs",
                    })
            if not routes and 'page' in rel:
                routes.append({
                    "function": '',
                    "methods": ['GET'],
                    "url": url,
                    "framework": "nextjs",
                })
            return routes

        # ---- Next.js Pages Router (/pages/) or Nuxt ----
        pages_idx = rel.find('/pages/')
        if pages_idx >= 0:
            route_part = rel[pages_idx + 7:]
            is_nextjs = _has_http_exported_func(functions, exports) or _has_default_handler(functions, exports)
            if is_nextjs:
                url = _path_to_url(route_part, strip_segments='index')
                for func in functions:
                    if func["name"].upper() in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'):
                        routes.append({
                            "function": func["name"],
                            "methods": [func["name"].upper()],
                            "url": url,
                            "framework": "nextjs",
                        })
                # Default handler: handles all HTTP methods
                if _has_default_handler(functions, exports):
                    routes.append({
                        "function": 'handler',
                        "methods": ['GET', 'POST', 'PUT', 'DELETE', 'PATCH'],
                        "url": url,
                        "framework": "nextjs",
                    })
                if not routes:
                    # No HTTP exports, no default handler — still register as GET
                    routes.append({
                        "function": '',
                        "methods": ['GET'],
                        "url": url,
                        "framework": "nextjs",
                    })
                return routes
            # Nuxt 3
            url = _path_to_url(route_part, strip_segments='index')
            routes.append({
                "function": '',
                "methods": ['GET'],
                "url": url,
                "framework": "nuxt",
            })
            return routes

        return routes

    def _detect_framework(self, root_node, file_path: str, lang_config: dict = None) -> list:
        """Detect which framework(s) a file uses based on imports and patterns.

        Rules are loaded from the language config's `frameworks` / `test_frameworks`
        lists (each entry: {name, signals}) so new languages/frameworks are
        config-only additions.
        """
        lang_config = lang_config or {}
        frameworks = set()

        try:
            code = root_node.text.decode('utf-8')
        except Exception:
            code = ''

        framework_rules = lang_config.get("frameworks", [])
        test_framework_rules = lang_config.get("test_frameworks", [])

        for rule in framework_rules:
            name = rule.get("name", '')
            signals = rule.get("signals", [])
            for sig in signals:
                if sig in code:
                    frameworks.add(name)
                    break

        if lang_config.get("test_framework_detection", False):
            for rule in test_framework_rules:
                name = rule.get("name", '')
                signals = rule.get("signals", [])
                for sig in signals:
                    if sig in code:
                        frameworks.add(name)
                        break
            # Generic test pattern detection (describe/it/test globals)
            if lang_config.get("generic_test_patterns", False):
                has_describe = 'describe(' in code or 'describe.each(' in code
                has_it = 'it(' in code or 'it.each(' in code
                has_test_call = 'test(' in code or 'test.each(' in code
                if has_describe or has_it or has_test_call:
                    if not frameworks & {t['name'] for t in test_framework_rules}:
                        frameworks.add('jest')

        return list(frameworks)
