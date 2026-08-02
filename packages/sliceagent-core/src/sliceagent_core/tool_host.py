"""Dependency-light ToolHost contract and schema utilities.

Concrete dispatch and coding-tool execution belong to injected CLI implementations.
This module only exposes the core protocol and pure schema construction/normalization.
"""
from __future__ import annotations

from .interfaces import ToolHost as ToolHost


def function_schema(
    name: str,
    description: str,
    properties: dict,
    required: list[str],
) -> dict:
    """Build one provider-neutral function-tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


NOTE_PROP = {
    "note": {
        "type": "string",
        "description": (
            "Optional — usually leave EMPTY. Fill ONLY when this call established a NEW durable FACT "
            "(root cause, a confirmed fix, a ruled-out hypothesis, or 'task done'), in <=15 words — a "
            "conclusion, NOT the action you're taking. Saved across turns so you never re-derive it; "
            "routine reads/edits need no note."
        ),
    }
}


def with_note(schema: dict) -> dict:
    """Inject the optional durable-finding ``note`` argument into a tool schema."""
    fn = schema.get("function") or {}
    params = fn.get("parameters") or {"type": "object", "properties": {}, "required": []}
    props = {**NOTE_PROP, **(params.get("properties") or {})}
    required = [item for item in (params.get("required") or []) if item != "note"]
    return {
        **schema,
        "function": {
            **fn,
            "parameters": {
                **params,
                "properties": props,
                "required": required,
            },
        },
    }


__all__ = ["NOTE_PROP", "ToolHost", "function_schema", "with_note"]
