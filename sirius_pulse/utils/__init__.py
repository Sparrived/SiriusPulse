"""Utility modules shared across sirius_pulse."""

from __future__ import annotations

from sirius_pulse.utils.image_bytes import (
    MAX_INLINE_IMAGE_BYTES,
    MAX_INLINE_IMAGE_EDGE,
    downscale_image_bytes,
)
from sirius_pulse.utils.json_io import atomic_write_json, read_json, replace_with_retry
from sirius_pulse.utils.layout import WorkspaceLayout
from sirius_pulse.utils.sqlite_base import (
    BaseSqliteStore,
    configure_sqlite_connection,
    open_sqlite_connection,
)

__all__ = [
    "WorkspaceLayout",
    "atomic_write_json",
    "read_json",
    "replace_with_retry",
    "BaseSqliteStore",
    "configure_sqlite_connection",
    "open_sqlite_connection",
    "MAX_INLINE_IMAGE_BYTES",
    "MAX_INLINE_IMAGE_EDGE",
    "downscale_image_bytes",
]
