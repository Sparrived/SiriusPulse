"""Developer-role helpers shared by participant and skill security paths."""

from __future__ import annotations

from typing import Any, Mapping

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", ""}


def metadata_declares_developer(metadata: Mapping[str, Any] | None) -> bool:
    """Return whether the metadata explicitly marks a profile as developer."""
    if not isinstance(metadata, Mapping):
        return False

    direct_value = _extract_direct_flag(metadata)
    if direct_value is not None:
        return direct_value

    role = metadata.get("role")
    if _role_value_contains_developer(role):
        return True

    roles = metadata.get("roles")
    if isinstance(roles, (list, tuple, set)):
        return any(_role_value_contains_developer(item) for item in roles)

    return False


def _extract_direct_flag(metadata: Mapping[str, Any]) -> bool | None:
    for key in ("is_developer", "developer"):
        if key not in metadata:
            continue
        return _coerce_explicit_bool(metadata.get(key))
    return None


def _coerce_explicit_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    return False


def _role_value_contains_developer(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_")
    return normalized in {"developer", "dev", "engineer"}