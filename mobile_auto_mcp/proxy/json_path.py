"""Shared JSON path traversal primitives for patch and mutation engines."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


def parse_path(path: str) -> list[str | int]:
    """Parse dotted keys and numeric bracket indexes into traversal tokens."""
    tokens: list[str | int] = []
    for part in path.split("."):
        if not part:
            continue
        match = re.match(r"^([^\[]+)", part)
        if match:
            tokens.append(match.group(1))
        tokens.extend(int(index) for index in re.findall(r"\[(\d+)\]", part))
    return tokens


def get_value(container: Any, token: str | int) -> Any:
    """Read one dict key or in-range list index, returning None when traversal cannot continue."""
    if isinstance(token, int):
        return container[token] if isinstance(container, list) and 0 <= token < len(container) else None
    return container.get(token) if isinstance(container, dict) else None


def set_value(container: Any, token: str | int | None, value: Any, *, create_missing: bool = False) -> None:
    """Set one dict key or list index, optionally extending lists for patch upserts."""
    if token is None:
        return
    if isinstance(token, int):
        if isinstance(container, list):
            while create_missing and len(container) <= token:
                container.append(None)
            if 0 <= token < len(container):
                container[token] = value
        return
    if isinstance(container, dict):
        container[token] = value


def delete_value(container: Any, token: str | int | None) -> None:
    """Delete one dict key or in-range list item without failing on an absent value."""
    if isinstance(token, int) and isinstance(container, list) and 0 <= token < len(container):
        container.pop(token)
    elif isinstance(token, str) and isinstance(container, dict):
        container.pop(token, None)


def resolve_parent(root: Any, path: str, *, create_missing: bool = False) -> tuple[Any | None, str | int | None]:
    """Return the parent container and final token for a path, creating intermediates when requested."""
    tokens = parse_path(path)
    if not tokens:
        return None, None
    current = root
    for index, token in enumerate(tokens[:-1]):
        next_token = tokens[index + 1]
        next_value = get_value(current, token)
        if next_value is None and create_missing:
            next_value = [] if isinstance(next_token, int) else {}
            set_value(current, token, next_value, create_missing=True)
        if next_value is None:
            return None, None
        current = next_value
    return current, tokens[-1]


def read_value(container: Any, token: str | int | None) -> tuple[bool, Any]:
    """Read one child and return an existence flag plus a defensive copy."""
    if token is None:
        return False, None
    value = get_value(container, token)
    return (value is not None), deepcopy(value)


def read_path(root: Any, path: str) -> tuple[bool, Any]:
    """Read a complete path and return a defensive copy of the resolved value."""
    if not path:
        return True, deepcopy(root)
    current = root
    for token in parse_path(path):
        current = get_value(current, token)
        if current is None:
            return False, None
    return True, deepcopy(current)
