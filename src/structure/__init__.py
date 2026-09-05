"""Structural code understanding for agents: map, lookup, impact.

    from src.structure import CodeStructure
    structure = CodeStructure(db.client, workspace_path)
    structure.map("src/services")
    structure.lookup("create_sale")
    structure.impact("services.py:create_sale", depth=2)
"""
from .service import CodeStructure, CALLERS, CALLEES, DEFAULT_IMPACT_DEPTH
from .resolver import Resolution, resolve_symbol, RESOLVED, AMBIGUOUS, NOT_FOUND
from .index_view import IndexView, normalize_path

__all__ = [
    "CodeStructure", "CALLERS", "CALLEES", "DEFAULT_IMPACT_DEPTH",
    "Resolution", "resolve_symbol", "RESOLVED", "AMBIGUOUS", "NOT_FOUND",
    "IndexView", "normalize_path",
]
