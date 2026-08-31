import pytest

pytestmark = pytest.mark.unit


def _yaml():
    from src.services.yaml_utils import to_yaml
    return to_yaml


class TestToYaml:
    def test_basic_dict(self):
        data = {"ok": True, "results": []}
        out = _yaml()(data)
        assert "ok: true" in out
        assert "results: []" in out

    def test_nested_structure(self):
        data = {
            "impact": {
                "target": "app.py:run",
                "affected_nodes": [
                    {"node_id": "app.py:start", "type": "Function"}
                ]
            }
        }
        out = _yaml()(data)
        assert "target:" in out
        assert "app.py:run" in out
        assert "affected_nodes:" in out
        assert "node_id:" in out

    def test_list_of_strings(self):
        data = ["alpha", "beta", "gamma"]
        out = _yaml()(data)
        assert "- alpha" in out
        assert "- beta" in out
        assert "- gamma" in out

    def test_empty_dict(self):
        out = _yaml()({})
        assert out.strip() == "{}"

    def test_empty_list(self):
        out = _yaml()([])
        assert out.strip() == "[]"

    def test_unicode(self):
        data = {"message": "مرحبا بالعالم"}
        out = _yaml()(data)
        assert "مرحبا بالعالم" in out

    def test_sort_keys_false(self):
        data = {"z": 1, "a": 2, "m": 3}
        out = _yaml()(data)
        z_pos = out.index("z:")
        a_pos = out.index("a:")
        assert z_pos < a_pos, "sort_keys=False should preserve insertion order"

    def test_none_value(self):
        data = {"key": None}
        out = _yaml()(data)
        assert "null" in out or "~" in out or "key:" in out

    def test_boolean_values(self):
        data = {"flag": False, "enabled": True}
        out = _yaml()(data)
        assert "false" in out
        assert "true" in out

    def test_mixed_types(self):
        data = {
            "name": "test",
            "count": 42,
            "ratio": 3.14,
            "tags": ["a", "b"],
            "meta": {"key": "val"}
        }
        out = _yaml()(data)
        assert "name: test" in out
        assert "count: 42" in out
        assert "tags:" in out
        assert "meta:" in out
