import os
import logging
import importlib

from tree_sitter import Language, Parser

logger = logging.getLogger(__name__)

LANGUAGES_DIR = os.path.join(os.path.dirname(__file__), "languages")

# Fallback defaults mirror the original built-in Python behaviour, so a missing
# key in a language config never crashes the parser. Language config files
# (languages/*.yaml) are authoritative and override these.
DEFAULT_CONFIG = {
    "language": "unknown",
    "display_name": "Unknown",
    "extensions": [],
    "grammar": {"module": None, "language_function": "language"},
    "node_types": {
        "program_root": "program",
        "decorated_definition": "decorated_definition",
        "class_nodes": ["class_definition"],
        "function_nodes": ["function_definition", "async_function_definition"],
        "arrow_nodes": [],
        "method_nodes": ["function_definition", "async_function_definition"],
        "class_body": "block",
        "body_nodes": ["block", "class_body", "statement_block"],
        "name_identifiers": ["identifier"],
        "call_nodes": ["call"],
        "string_nodes": ["string"],
        "decorator_nodes": ["decorator"],
        "identifier_nodes": ["identifier"],
        "member_nodes": ["attribute", "member_expression"],
        "argument_list_nodes": ["argument_list"],
        "parameters_nodes": ["parameters", "formal_parameters"],
        "param_ignore_types": ["(", ")", ",", ":", ";", "comment"],
        "comment_nodes": ["comment"],
        "statement_nodes": ["expression_statement"],
        "assignment_nodes": ["assignment"],
        "list_nodes": ["list"],
        "lambda_nodes": ["lambda"],
        "async_function_nodes": ["async_function_definition", "async_function"],
        "async_keywords": ["async"],
        "generator_function_nodes": ["generator_function", "generator_function_declaration"],
        "generator_keywords": ["yield", "yield_statement", "yield_expression"],
        "prune_walk_nodes": [
            "function_definition", "async_function_definition", "class_definition",
            "method_definition", "arrow_function", "generator_function",
        ],
        "export_statement_nodes": ["export_statement"],
        "export_default_statement_nodes": ["export_default_statement"],
        "export_prefix_skips": ["export"],
        "base_class_nodes": ["argument_list", "class_heritage"],
        "base_class_extends_keyword": "extends",
        "base_class_ignores": ["object"],
        "return_statement_nodes": ["return_statement"],
        "require_nodes": ["lexical_declaration", "variable_declaration"],
        "variable_declarator_nodes": ["variable_declarator"],
        "arrow_function_nodes": ["arrow_function"],
        "function_expression_nodes": ["function_expression"],
        "class_declaration_nodes": ["class_declaration"],
        "function_declaration_nodes": ["function_declaration"],
        "import_statement_nodes": ["import_statement"],
        "import_from_statement_nodes": ["import_from_statement"],
        "dotted_name_nodes": ["dotted_name"],
        "export_clause_nodes": ["export_clause"],
        "export_specifier_nodes": ["export_specifier"],
        "default_keyword_nodes": ["default"],
        "keyword_argument_nodes": ["keyword_argument"],
        "jsx_nodes": ["jsx_self_closing_element", "jsx_opening_element", "jsx_attribute", "jsx_expression"],
        "jsx_identifier_nodes": ["jsx_identifier"],
        "module_name_field": "module_name",
    },
    "imports": {
        "enable_import_statement": True,
        "enable_import_from": True,
        "enable_require": False,
    },
    "routes": {
        "route_methods": [
            "get", "post", "put", "delete", "patch", "head", "options",
            "route", "add_route", "add_url_rule", "api_operation",
            "add_router", "include_router", "mount", "use",
        ],
        "route_functions": ["path", "re_path"],
        "http_methods": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
        "middleware_methods": ["add_middleware"],
        "middleware_decorator_prefixes": ["middleware", "before_request", "after_request"],
        "declarative_middleware_var": "MIDDLEWARE",
        "url_patterns_var": "urlpatterns",
        "include_function": "include",
        "controller_decorators": ["Controller"],
        "method_decorators": ["Get", "Post", "Put", "Delete", "Patch", "Head", "Options"],
        "jsx_route_tag": "Route",
        "jsx_browser_router_fn": "createBrowserRouter",
    },
    "features": {
        "docstrings": True,
        "exports": False,
        "django_relations": True,
        "url_patterns": True,
        "file_based_routes": False,
        "jsx_routes": False,
        "trpc_routes": False,
        "commonjs_imports": False,
        "http_calls": False,
        "framework_detection": True,
        "test_framework_detection": False,
        "generic_test_patterns": False,
    },
    "frameworks": [],
    "test_frameworks": [],
}


def _deep_merge(base, override):
    """Recursively merge override dict into base dict (override wins)."""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _flatten(config):
    """Flatten structured sections (node_types/imports/routes/features) to top level.

    The parser consumes the config as a flat dict via lang_config.get(...), so
    each section's keys are promoted while the sections remain available too.
    """
    flat = dict(config)
    for section in ("node_types", "imports", "routes", "features"):
        for key, value in (config.get(section) or {}).items():
            flat[key] = value
    return flat


def load_language_config(path):
    """Load and merge a single language YAML config file."""
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    merged = _deep_merge(DEFAULT_CONFIG, raw)
    return _flatten(merged)


def discover_language_configs():
    """Load every language config file found in the languages/ directory."""
    configs = []
    if not os.path.isdir(LANGUAGES_DIR):
        return configs
    for filename in sorted(os.listdir(LANGUAGES_DIR)):
        if filename.endswith((".yaml", ".yml")):
            try:
                configs.append(load_language_config(os.path.join(LANGUAGES_DIR, filename)))
            except Exception as e:
                logger.warning("Failed to load language config '%s': %s", filename, e)
    return configs


class LanguageAdapter:
    """Adapts a language config file into a runnable tree-sitter parser.

    Adding a new language means dropping a YAML file into languages/ — no code
    changes required. The adapter resolves the grammar import lazily so that a
    missing optional dependency only disables that language.
    """

    def __init__(self, config):
        self.config = config
        self._grammar_language = None
        self._load_error = None

    @property
    def language(self):
        return self.config.get("language", "unknown")

    @property
    def display_name(self):
        return self.config.get("display_name", self.language)

    @property
    def extensions(self):
        return list(self.config.get("extensions", []))

    def _load_grammar(self):
        if self._grammar_language is not None or self._load_error is not None:
            return
        try:
            grammar = self.config.get("grammar", {})
            module = importlib.import_module(grammar["module"])
            factory_name = grammar.get("language_function", "language")
            factory = getattr(module, factory_name)
            self._grammar_language = Language(factory())
        except Exception as e:
            self._load_error = e
            logger.warning(
                "Skipping %s parser (%s): grammar unavailable — %s",
                self.display_name,
                ", ".join(self.extensions),
                e,
            )

    @property
    def available(self):
        self._load_grammar()
        return self._load_error is None

    def create_parser(self):
        self._load_grammar()
        if self._load_error is not None or self._grammar_language is None:
            return None
        return Parser(self._grammar_language)


# Registry: extension → adapter. Built once at import time.
LANGUAGE_ADAPTERS = {}
for _config in discover_language_configs():
    _adapter = LanguageAdapter(_config)
    for _ext in _adapter.extensions:
        LANGUAGE_ADAPTERS[_ext.lower()] = _adapter

# Non-code extensions that are still indexed (as File nodes with body only).
# They get no tree-sitter parser but are visible to file-level queries.
NON_CODE_EXTENSIONS = ('.json', '.md', '.html', '.css', '.yml', '.yaml', '.toml', '.txt', '.sql')

# Every extension the indexer accepts: code extensions (from language configs)
# plus the non-code passthrough set. Lowercased to match parser normalization.
SUPPORTED_EXTENSIONS = tuple(sorted(set(LANGUAGE_ADAPTERS) | set(NON_CODE_EXTENSIONS)))


# Bump whenever the parser's extraction schema changes (new node kinds, new
# fields, nested-definition indexing, ...). Persisted indexes built by an older
# parser are then detected as stale instead of silently answering from a graph
# that lacks the new nodes.
PARSER_SCHEMA_VERSION = "python-contextual-calls-v3"


def compute_index_fingerprint() -> str:
    """Deterministic fingerprint of the current indexing configuration.

    Changes whenever supported languages/extensions are added or removed (e.g.
    a JS/TS adapter being disabled) or the parser's extraction schema changes,
    so a persisted index can be detected as stale and rebuilt instead of
    silently answering from outdated data.
    """
    import hashlib
    raw = "|".join([PARSER_SCHEMA_VERSION, *SUPPORTED_EXTENSIONS])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_adapter(ext: str):
    """Return the LanguageAdapter registered for a file extension, or None."""
    return LANGUAGE_ADAPTERS.get(str(ext).lower())


def get_lang_config(ext: str) -> dict:
    """Return the flattened config dict for an extension (or {} if unknown)."""
    adapter = get_adapter(ext)
    return adapter.config if adapter is not None else {}


def build_parser(ext: str):
    """Build a tree-sitter Parser for an extension, or None if unavailable."""
    adapter = get_adapter(ext)
    if adapter is None:
        return None
    return adapter.create_parser()
