"""业务场景测试共享夹具。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


@pytest.fixture
def tmp_skill_dir(tmp_path: Path) -> Path:
    """模拟用户在工作区安装的本地技能目录。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_file = skills_dir / "test_hello.py"
    skill_file.write_text(
        """
SKILL_META = {
    "name": "test_hello",
    "description": "给群友发送问候",
    "version": "1.0",
    "parameters": {
        "name": {
            "type": "str",
            "description": "要问候的群友",
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
""",
        encoding="utf-8",
    )
    return skills_dir
