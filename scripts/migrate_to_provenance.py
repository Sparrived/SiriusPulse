"""Populate the evidence-first memory ledger for an existing persona.

Usage:
    python scripts/migrate_to_provenance.py data/personas/sirius
    python scripts/migrate_to_provenance.py data/personas/sirius/persona.db
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sirius_pulse.memory.provenance.store import ProvenanceStore

LOG = logging.getLogger(__name__)


def _resolve_db_path(path: Path) -> Path:
    if path.is_dir():
        return path / "persona.db"
    return path


def migrate(path: Path) -> dict[str, object]:
    db_path = _resolve_db_path(path)
    if not db_path.exists():
        raise FileNotFoundError(f"persona.db not found: {db_path}")

    store = ProvenanceStore(db_path)
    try:
        migrated = store.migrate_from_legacy_tables()
        stats = store.stats()
    finally:
        store.close()
    return {
        "db_path": str(db_path),
        "migrated": migrated,
        "stats": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy memory rows into memory_evidence/memory_claims.",
    )
    parser.add_argument("path", type=Path, help="Persona directory or persona.db path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        result = migrate(args.path)
    except Exception as exc:
        LOG.error("provenance migration failed: %s", exc)
        raise SystemExit(1) from exc

    print(json.dumps(
        result,
        ensure_ascii=False,
        indent=2 if args.pretty else None,
    ))


if __name__ == "__main__":
    main()
