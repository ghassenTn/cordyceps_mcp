"""
YAML output utilities for tool responses.
"""
import yaml
from typing import Any

def to_yaml(data: Any) -> str:
    """Convert a Python object to YAML string format."""
    return yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
