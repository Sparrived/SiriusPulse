"""Test fixtures for critical runtime nodes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


@pytest.fixture
def tmp_skill_dir(tmp_path: Path) -> Path:
    """临时技能目录，含一个测试技能文件。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_file = skills_dir / "test_hello.py"
    skill_file.write_text("""
SKILL_META = {
    "name": "test_hello",
    "description": "测试用打招呼技能",
    "version": "1.0",
    "parameters": {
        "name": {
            "type": "str",
            "description": "要打招呼的人",
            "required": False,
            "default": "世界",
        },
    },
}

def run(name: str = "世界", **kwargs) -> dict:
    return {
        "success": True,
        "data": {"greeting": f"你好，{name}！"},
        "text": f"你好，{name}！",
    }
""", encoding="utf-8")
    return skills_dir
