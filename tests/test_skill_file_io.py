"""Tests for file_read, file_write and file_list built-in skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.skills.data_store import SkillDataStore

from sirius_pulse.skills.builtin import file_read, file_write, file_list


class TestFileReadSkill:
    """Tests for the file_read built-in skill."""

    @staticmethod
    def _make_store(tmp_path: Path) -> SkillDataStore:
        store_path = tmp_path / "skill_data" / "file_read.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        return SkillDataStore(store_path)

    def test_read_text_file(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        target = tmp_path / "docs" / "readme.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# Hello\nWorld", encoding="utf-8")

        result = file_read.run(path="docs/readme.md", data_store=store)
        assert result["success"] is True
        assert "# Hello" in result["text_blocks"][0]
        assert result["internal_metadata"]["line_count"] == 2

    def test_read_directory_lists_entries(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        target = tmp_path / "src"
        target.mkdir()
        (target / "main.py").write_text("x", encoding="utf-8")
        (target / "utils").mkdir()

        result = file_read.run(path="src", data_store=store)
        assert result["success"] is True
        assert result["internal_metadata"]["is_directory"] is True
        listing = result["text_blocks"][0]
        assert "main.py" in listing
        assert "utils/" in listing

    def test_read_nonexistent_file(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_read.run(path="missing.txt", data_store=store)
        assert result["success"] is False
        assert "不存在" in result["error"]

    def test_read_empty_path(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_read.run(path="", data_store=store)
        assert result["success"] is False
        assert "不能为空" in result["error"]

    def test_read_binary_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        target = tmp_path / "data.bin"
        target.write_bytes(b"\x00\x01\x02\x03")

        result = file_read.run(path="data.bin", data_store=store)
        assert result["success"] is False
        assert result["error"]

    def test_read_outside_allowed_dir_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_read.run(path="../etc/hosts", data_store=store)
        assert result["success"] is False
        assert "非法字符" in result["error"]

    def test_read_traversal_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_read.run(path="../../secret.txt", data_store=store)
        assert result["success"] is False
        assert "非法字符" in result["error"]

    def test_read_large_file_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_read, "_ALLOWED_READ_DIR", tmp_path)
        store = self._make_store(tmp_path)
        target = tmp_path / "big.txt"
        target.write_text("hello world", encoding="utf-8")
        monkeypatch.setattr(file_read, "_MAX_SIZE_BYTES", 5)

        result = file_read.run(path="big.txt", data_store=store)
        assert result["success"] is False
        assert result["error"]


class TestFileWriteSkill:
    """Tests for the file_write built-in skill."""

    @staticmethod
    def _make_store(tmp_path: Path) -> SkillDataStore:
        store_path = tmp_path / "skill_data" / "file_write.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        return SkillDataStore(store_path)

    def test_write_new_file(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_write.run(
            path="src/utils.py",
            content="def helper(): pass\n",
            mode="write",
            data_store=store,
        )
        assert result["success"] is True
        assert (tmp_path / "src" / "utils.py").read_text(encoding="utf-8") == "def helper(): pass\n"

    def test_append_to_existing(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        target = tmp_path / "log.txt"
        target.write_text("line1\n", encoding="utf-8")

        result = file_write.run(
            path="log.txt",
            content="line2\n",
            mode="append",
            data_store=store,
        )
        assert result["success"] is True
        assert target.read_text(encoding="utf-8") == "line1\nline2\n"
        assert "追加" in result["summary"]

    def test_write_empty_path(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_write.run(path="", content="x", data_store=store)
        assert result["success"] is False
        assert "不能为空" in result["error"]

    def test_write_outside_allowed_dir_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_write.run(
            path="../etc/hosts",
            content="hack\n",
            data_store=store,
        )
        assert result["success"] is False
        assert "非法字符" in result["error"]

    def test_write_to_directory_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        folder = tmp_path / "folder"
        folder.mkdir()
        result = file_write.run(path="folder", content="x", data_store=store)
        assert result["success"] is False
        assert "目录" in result["error"]

    def test_write_overwrite_large_file_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        target = tmp_path / "big.py"
        target.write_text("x" * 100, encoding="utf-8")
        monkeypatch.setattr(file_write, "_MAX_FILE_SIZE_BYTES", 50)

        result = file_write.run(
            path="big.py", content="new", mode="write", data_store=store
        )
        assert result["success"] is False
        assert "过大" in result["error"]

    def test_write_to_binary_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        target = tmp_path / "image.png"
        target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00")

        result = file_write.run(
            path="image.png", content="new text", mode="write", data_store=store
        )
        assert result["success"] is False
        assert "二进制" in result["error"]

    def test_write_large_content_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        monkeypatch.setattr(file_write, "_MAX_WRITE_SIZE_BYTES", 5)

        result = file_write.run(
            path="small.py", content="hello world", data_store=store
        )
        assert result["success"] is False
        assert "过大" in result["error"]

    def test_write_traversal_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_write, "_ALLOWED_WRITE_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_write.run(
            path="../../secret.txt", content="hack\n", data_store=store
        )
        assert result["success"] is False
        assert "非法字符" in result["error"]


class TestFileListSkill:
    """Tests for the file_list built-in skill."""

    @staticmethod
    def _make_store(tmp_path: Path) -> SkillDataStore:
        store_path = tmp_path / "skill_data" / "file_list.json"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        return SkillDataStore(store_path)

    def test_list_root_shows_files_and_dirs(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_list, "_ALLOWED_LIST_DIR", tmp_path)
        store = self._make_store(tmp_path)
        (tmp_path / "main.py").write_text("x", encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "readme.md").write_text("x", encoding="utf-8")

        result = file_list.run(path=".", recursive=False, data_store=store)
        assert result["success"] is True
        assert result["internal_metadata"]["count"] >= 2
        text = result["text_blocks"][0]
        assert "main.py" in text
        assert "docs" in text

    def test_list_recursive(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_list, "_ALLOWED_LIST_DIR", tmp_path)
        store = self._make_store(tmp_path)
        (tmp_path / "src" / "utils").mkdir(parents=True)
        (tmp_path / "src" / "utils" / "helpers.py").write_text("x", encoding="utf-8")

        result = file_list.run(path="src", recursive=True, data_store=store)
        assert result["success"] is True
        assert result["internal_metadata"]["recursive"] is True
        text = result["text_blocks"][0]
        assert "helpers.py" in text

    def test_list_with_glob_pattern(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_list, "_ALLOWED_LIST_DIR", tmp_path)
        store = self._make_store(tmp_path)
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        (tmp_path / "c.py").write_text("x", encoding="utf-8")

        result = file_list.run(path=".", pattern="*.py", data_store=store)
        assert result["success"] is True
        assert result["internal_metadata"]["count"] == 2
        text = result["text_blocks"][0]
        assert "a.py" in text
        assert "c.py" in text
        assert "b.txt" not in text

    def test_list_single_file(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_list, "_ALLOWED_LIST_DIR", tmp_path)
        store = self._make_store(tmp_path)
        (tmp_path / "note.md").write_text("hello", encoding="utf-8")

        result = file_list.run(path="note.md", data_store=store)
        assert result["success"] is True
        assert result["internal_metadata"]["count"] == 1
        text = result["text_blocks"][0]
        assert "note.md" in text

    def test_list_nonexistent_path(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_list, "_ALLOWED_LIST_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_list.run(path="missing", data_store=store)
        assert result["success"] is False
        assert result["error"]

    def test_list_outside_allowed_dir_rejected(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_list, "_ALLOWED_LIST_DIR", tmp_path)
        store = self._make_store(tmp_path)
        result = file_list.run(path="../secret", data_store=store)
        assert result["success"] is False
        assert "非法字符" in result["error"]

    def test_list_truncates_large_results(self, tmp_path: Path, monkeypatch: Any):
        monkeypatch.setattr(file_list, "_ALLOWED_LIST_DIR", tmp_path)
        store = self._make_store(tmp_path)
        for i in range(10):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(file_list, "_MAX_RESULTS", 3)

        result = file_list.run(path=".", data_store=store)
        assert result["success"] is True
        assert result["internal_metadata"]["truncated"] is True
        assert result["internal_metadata"]["count"] == 3
