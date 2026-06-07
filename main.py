"""Compatibility wrapper for the Sirius Pulse command line interface."""

from __future__ import annotations

from sirius_pulse.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
