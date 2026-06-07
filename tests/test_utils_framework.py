from __future__ import annotations

import json
import sqlite3

import pytest

from sirius_pulse.utils.json_io import atomic_write_json, read_json
from sirius_pulse.utils.query_builder import QueryBuilder
from sirius_pulse.utils.sqlite_base import (
    BaseSqliteStore,
    configure_sqlite_connection,
    open_sqlite_connection,
)


class ExampleStore(BaseSqliteStore):
    def _create_tables(self) -> None:
        self.ensure_meta_table()
        self.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "name TEXT NOT NULL"
            ")"
        )
        self._ensure_columns("items", {"kind": "TEXT NOT NULL DEFAULT 'generic'"})
        self.set_schema_version(2)

    def add_item(self, name: str) -> None:
        self.execute("INSERT INTO items(name) VALUES (?)", (name,))
        self.commit()


def test_json_io_when_atomic_write_creates_parent_then_file_round_trips(tmp_path):
    target = tmp_path / "nested" / "data.json"

    atomic_write_json(target, {"name": "alpha", "values": [1, 2]}, indent=None)

    assert json.loads(target.read_text(encoding="utf-8")) == {"name": "alpha", "values": [1, 2]}
    assert read_json(target) == {"name": "alpha", "values": [1, 2]}
    assert not target.with_suffix(".json.tmp").exists()


def test_json_io_when_file_is_missing_or_invalid_then_default_is_returned(tmp_path):
    missing_default = {"fallback": True}
    invalid_path = tmp_path / "broken.json"
    invalid_path.write_text("{broken", encoding="utf-8")

    assert read_json(tmp_path / "missing.json", missing_default) is missing_default
    assert read_json(invalid_path, default=[]) == []


def test_query_builder_when_conditions_are_added_then_builds_select_delete_and_count():
    builder = QueryBuilder().where("group_id = ?", "g1").where_optional("actor_id = ?", "u1")

    assert builder.build_where() == (" WHERE group_id = ? AND actor_id = ?", ["g1", "u1"])
    assert builder.build_select(
        "token_usage", "id, total_tokens", order_by="id DESC", limit=5, offset=2
    ) == (
        "SELECT id, total_tokens FROM token_usage WHERE group_id = ? AND actor_id = ? ORDER BY id DESC LIMIT 5 OFFSET 2",
        ["g1", "u1"],
    )
    assert builder.build_count("token_usage") == (
        "SELECT COUNT(*) as cnt FROM token_usage WHERE group_id = ? AND actor_id = ?",
        ["g1", "u1"],
    )
    assert builder.build_delete("token_usage") == (
        "DELETE FROM token_usage WHERE group_id = ? AND actor_id = ?",
        ["g1", "u1"],
    )

    assert builder.reset().where_optional("ignored = ?", None).build_select("items") == (
        "SELECT * FROM items",
        [],
    )


def test_sqlite_connection_when_invalid_journal_mode_then_raises():
    conn = sqlite3.connect(":memory:")

    with pytest.raises(ValueError, match="journal_mode"):
        configure_sqlite_connection(conn, journal_mode="WAL;DROP")

    conn.close()


def test_base_sqlite_store_when_created_then_initializes_schema_and_helpers(tmp_path):
    store = ExampleStore(tmp_path / "store.db")

    store.add_item("alpha")

    assert store.db_path == tmp_path / "store.db"
    assert store.get_schema_version() == 2
    assert store.fetchone("SELECT name, kind FROM items") == {"name": "alpha", "kind": "generic"}
    assert store.fetchall("SELECT name FROM items") == [{"name": "alpha"}]

    store.close()


def test_base_sqlite_store_when_shared_connection_is_used_then_close_does_not_close_owner():
    conn = sqlite3.connect(":memory:")
    store = ExampleStore(conn=conn)

    store.add_item("alpha")
    store.close()

    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    conn.close()


def test_base_sqlite_store_when_read_only_then_skips_create_tables_and_rejects_writes(tmp_path):
    writable = ExampleStore(tmp_path / "store.db")
    writable.add_item("alpha")
    writable.close()

    read_only = ExampleStore(tmp_path / "store.db", read_only=True)

    assert read_only.fetchone("SELECT name FROM items") == {"name": "alpha"}
    with pytest.raises(sqlite3.OperationalError):
        read_only.execute("INSERT INTO items(name) VALUES ('beta')")
    read_only.close()


def test_open_sqlite_connection_when_parent_is_missing_then_creates_database(tmp_path):
    db_path = tmp_path / "nested" / "open.db"

    conn = open_sqlite_connection(db_path)
    conn.execute("CREATE TABLE t(id INTEGER)")
    conn.close()

    assert db_path.exists()
