import os
import pytest

import yaml

from src.database.parser import language_adapter
from src.database.parser.language_adapter import (
    LANGUAGES_DIR,
    LANGUAGE_ADAPTERS,
    NON_CODE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    LanguageAdapter,
    _flatten,
    _deep_merge,
    discover_language_configs,
    get_adapter,
    get_lang_config,
    load_language_config,
)
from src.database.parser.ast_parser import UniversalCodeParser


pytestmark = pytest.mark.unit


@pytest.fixture
def parser():
    return UniversalCodeParser()


class TestLanguageRegistry:
    def test_python_registered_from_config(self):
        """The .py adapter comes from languages/python.yaml — no code registration."""
        assert '.py' in LANGUAGE_ADAPTERS
        adapter = get_adapter('.py')
        assert adapter is not None
        assert adapter.language == 'python'
        assert adapter.display_name == 'Python'
        assert '.py' in adapter.extensions

    def test_config_file_has_all_required_sections(self):
        cfg = get_lang_config('.py')
        assert cfg.get('class_nodes') == ['class_definition']
        assert cfg.get('function_nodes') == ['function_definition', 'async_function_definition']
        assert cfg.get('class_body') == 'block'
        assert cfg.get('decorated_definition') == 'decorated_definition'
        assert cfg.get('call_nodes') == ['call']
        assert cfg.get('enable_import_from') is True
        assert cfg.get('enable_require') is False
        assert cfg.get('django_relations') is True
        assert cfg.get('url_patterns') is True
        assert cfg.get('http_calls') is False
        assert cfg.get('file_based_routes') is False

    def test_framework_rules_in_config(self):
        cfg = get_lang_config('.py')
        names = {r['name'] for r in cfg.get('frameworks', [])}
        assert {'django', 'fastapi', 'flask', 'django-rest-framework', 'django-ninja'} <= names

    def test_unknown_extension_has_empty_config(self):
        assert get_lang_config('.rs') == {}

    def test_discover_loads_only_yaml_files(self, isolated_temp_dir):
        configs = discover_language_configs()
        assert any(c.get('language') == 'python' for c in configs)

    def test_load_config_from_arbitrary_yaml(self, isolated_temp_dir):
        """A brand-new language is a pure config file addition."""
        cfg_path = os.path.join(isolated_temp_dir, "golang.yaml")
        with open(cfg_path, "w") as f:
            yaml.safe_dump({
                "language": "golang",
                "extensions": [".go"],
                "grammar": {"module": "tree_sitter_go", "language_function": "language"},
                "node_types": {"class_nodes": ["type_declaration"]},
                "features": {"http_calls": True},
            }, f)
        cfg = load_language_config(cfg_path)
        assert cfg["language"] == "golang"
        assert ".go" in cfg["extensions"]
        # node_types section is flattened for parser consumption
        assert cfg["class_nodes"] == ["type_declaration"]
        # unspecified keys keep safe defaults
        assert cfg["call_nodes"] == ["call"]
        assert cfg["http_calls"] is True
        assert cfg["framework_detection"] is True


class TestLanguageAdapter:
    def test_adapter_creates_parser(self):
        adapter = get_adapter('.py')
        assert adapter.available
        parser = adapter.create_parser()
        assert parser is not None

    def test_missing_grammar_disables_language(self):
        """A config pointing to an unavailable grammar degrades gracefully."""
        cfg = {
            "language": "fake",
            "extensions": [".zzz"],
            "grammar": {"module": "tree_sitter_nonexistent_lang", "language_function": "language"},
            "node_types": {},
        }
        adapter = LanguageAdapter(_flatten(cfg))
        assert not adapter.available
        assert adapter.create_parser() is None

    def test_merge_override_wins(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"y": 9}, "b": 4}
        merged = _deep_merge(base, override)
        assert merged == {"a": {"x": 1, "y": 9}, "b": 4}


class TestConfigDrivenParsing:
    def test_python_parse_still_works_via_config(self, parser, isolated_temp_dir):
        path = os.path.join(isolated_temp_dir, "mod.py")
        with open(path, "w") as f:
            f.write("""
from django.db import models

@dataclass
class Product:
    name: str

def greet(name):
    return f"Hello {name}"
""")
        result = parser.parse_file(path)
        names = {c["name"] for c in result["classes"]}
        assert "Product" in names
        assert "greet" in {fn["name"] for fn in result["functions"]}
        # framework detection driven by config signals
        assert "django" in result["frameworks"]

    def test_framework_detection_follows_config(self, parser, isolated_temp_dir):
        path = os.path.join(isolated_temp_dir, "svc.py")
        with open(path, "w") as f:
            f.write("import fastapi\n")
        result = parser.parse_file(path)
        assert "fastapi" in result["frameworks"]
        assert "django" not in result["frameworks"]

    def test_parser_uses_only_configured_extensions(self, parser, isolated_temp_dir):
        """Only extensions listed in configs get a parser."""
        assert sorted(parser.parsers.keys()) == ['.js', '.jsx', '.py', '.ts', '.tsx']


class TestSupportedExtensions:
    def test_supported_extensions_are_python_plus_non_code(self):
        assert '.py' in SUPPORTED_EXTENSIONS
        assert '.ts' in SUPPORTED_EXTENSIONS
        assert '.tsx' in SUPPORTED_EXTENSIONS
        assert '.js' in SUPPORTED_EXTENSIONS
        assert '.jsx' in SUPPORTED_EXTENSIONS
        assert '.rs' not in SUPPORTED_EXTENSIONS
        assert set(NON_CODE_EXTENSIONS) <= set(SUPPORTED_EXTENSIONS)

    def test_sync_handler_uses_single_source_of_truth(self, isolated_temp_dir):
        from src.watcher.sync_handler import GraphSyncHandler
        handler = GraphSyncHandler(isolated_temp_dir)
        assert handler.supported_extensions == SUPPORTED_EXTENSIONS

    def test_clean_stale_files_prunes_unsupported_extensions(self, isolated_temp_dir):
        """Stale nodes with unsupported extensions are pruned."""
        from src.database import get_graph_db
        os.makedirs(os.path.join(isolated_temp_dir, "src"))
        with open(os.path.join(isolated_temp_dir, "app.py"), "w") as f:
            f.write("def hello():\n    return 1\n")
        with open(os.path.join(isolated_temp_dir, "src", "Main.rs"), "w") as f:
            f.write("fn main() {}\n")
        db = get_graph_db(isolated_temp_dir)
        db.client.add_node("app.py", "File", "app.py", "app.py")
        db.client.add_node("src/Main.rs", "File", "Main.rs", "src/Main.rs")
        removed = db.client.clean_stale_files()
        assert removed == 1
        all_meta = db.client.get_all_metadata()
        assert "app.py" in all_meta
        assert "src/Main.rs" not in all_meta
