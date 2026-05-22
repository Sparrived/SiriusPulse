"""Shared serialisation mixins for dataclasses in Sirius Chat.

Public module — import from here rather than the private ``_mixin`` shim.

Usage::

    from sirius_pulse.mixins import JsonSerializable

    @dataclass(slots=True)
    class MyModel(JsonSerializable):
        x: int
        y: str = "hello"

Any ``@dataclass(slots=True)`` class that inherits from ``JsonSerializable``
gets automatic ``to_dict()`` / ``from_dict()`` methods backed by
``dataclasses.asdict`` and ``dataclasses.fields`` reflection.  New fields
added with default values are serialised and deserialised without any manual
code changes.

``__slots__ = ()`` on the mixin ensures the derived class can remain purely
slot-based (no ``__dict__`` overhead).
"""
from __future__ import annotations

from dataclasses import asdict, fields, MISSING
from typing import Any, TypeVar

_T = TypeVar("_T", bound="JsonSerializable")


class JsonSerializable:
    """Reflection-based JSON serialisation mixin for leaf dataclasses.

    Subclasses gain two methods:

    * ``to_dict()`` — serialise to a plain :class:`dict` via
      :func:`dataclasses.asdict` (recursive, works for nested dataclasses
      whose fields are also JSON-compatible primitives / lists / dicts).

    * ``from_dict(data)`` — deserialise from a plain :class:`dict`.
      Fields present in *data* are used as-is; fields absent from *data*
      fall back to their declared ``default`` or ``default_factory``.
      Required fields without defaults raise :class:`TypeError` if absent,
      which is the correct behaviour (data is corrupt or incomplete).

    This design means: adding a new optional field with a default value to
    a dataclass requires *zero* changes to serialisation code, and existing
    persisted files load transparently.
    """

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (recursively via ``dataclasses.asdict``)."""
        return asdict(self)

    @classmethod
    def from_dict(cls: type[_T], data: dict[str, Any]) -> _T:
        """Deserialise from a plain dict, applying declared defaults for missing keys."""
        kwargs: dict[str, Any] = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name in data:
                kwargs[f.name] = data[f.name]
            elif f.default is not MISSING:
                kwargs[f.name] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                kwargs[f.name] = f.default_factory()  # type: ignore[misc]
        return cls(**kwargs)
