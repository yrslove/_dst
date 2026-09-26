from __future__ import annotations

import os
from collections.abc import Mapping

_SENSITIVE_EXACT = {"DATABASE_URL", "DST_FARM_DB"}
_SENSITIVE_SUFFIXES = (
    "_PASSWORD",
    "_TOKEN",
    "_SECRET",
    "_SECRET_KEY",
    "_PRIVATE_KEY",
    "_ACCESS_KEY",
    "_API_KEY",
)


def is_sensitive_environment_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in _SENSITIVE_EXACT or normalized.endswith(
        _SENSITIVE_SUFFIXES
    )


def sanitized_subprocess_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    result = {
        name: value
        for name, value in os.environ.items()
        if not is_sensitive_environment_name(name)
    }
    result.update(overrides or {})
    return result


def purge_sensitive_environment() -> None:
    for name in tuple(os.environ):
        if is_sensitive_environment_name(name):
            os.environ.pop(name, None)
