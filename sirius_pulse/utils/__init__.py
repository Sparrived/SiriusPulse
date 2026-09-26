"""Utility modules shared across sirius_pulse."""

from __future__ import annotations

from sirius_pulse.utils.image_bytes import (
    MAX_INLINE_IMAGE_BYTES,
    MAX_INLINE_IMAGE_EDGE,
    downscale_image_bytes,
)
from sirius_pulse.utils.json_io import atomic_write_json, read_json, replace_with_retry
from sirius_pulse.utils.layout import WorkspaceLayout
from sirius_pulse.utils.query_builder import QueryBuilder
from sirius_pulse.utils.retry import async_retry, is_transient_error, sync_retry
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
    "async_retry",
    "sync_retry",
    "is_transient_error",
    "BaseSqliteStore",
    "configure_sqlite_connection",
    "open_sqlite_connection",
    "QueryBuilder",
    "MAX_INLINE_IMAGE_BYTES",
    "MAX_INLINE_IMAGE_EDGE",
    "downscale_image_bytes",
]
