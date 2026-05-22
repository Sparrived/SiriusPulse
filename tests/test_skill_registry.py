"""技能注册中心关键路径测试。"""
from __future__ import annotations

from pathlib import Path

from sirius_pulse.skills import SkillRegistry, SkillDefinition


def _make_dummy_skill(name: str) -> SkillDefinition:
    def dummy_run(**kwargs):
        return {"success": True}

    return SkillDefinition(
        name=name,
        description=f"Test skill {name}",
        parameters=[],
        source_path=None,
        _run_func=dummy_run,
    )


def test_load_skills_from_directory(tmp_skill_dir: Path):
    """从目录加载技能并验证查找。"""
    registry = SkillRegistry()
    count = registry.load_from_directory(
        tmp_skill_dir, auto_install_deps=False, include_builtin=False
    )
    assert count >= 1

    skill = registry.get("test_hello")
    assert skill is not None
    assert skill.name == "test_hello"
    assert skill.description == "测试用打招呼技能"
    assert len(skill.parameters) == 1
    assert skill.parameters[0].name == "name"


def test_manual_register():
    """手动注册技能。"""
    registry = SkillRegistry()
    skill = _make_dummy_skill("test_manual")
    registry.register(skill)
    assert registry.get("test_manual") is skill
    assert "test_manual" in registry.skill_names


def test_build_tool_descriptions():
    """构建 LLM 工具描述文本。"""
    registry = SkillRegistry()
    registry.register(_make_dummy_skill("test_search"))
    registry.register(_make_dummy_skill("test_read"))

    desc = registry.build_tool_descriptions(adapter_type="napcat")
    assert isinstance(desc, str)
    assert "test_search" in desc
    assert "test_read" in desc


def test_replace_all():
    """原子替换所有技能。"""
    registry = SkillRegistry()
    registry.replace_all([
        _make_dummy_skill("a"),
        _make_dummy_skill("b"),
    ])
    assert len(registry.skill_names) == 2
    assert registry.get("a") is not None
    assert registry.get("b") is not None

    registry.replace_all([
        _make_dummy_skill("c"),
    ])
    assert len(registry.skill_names) == 1
    assert registry.get("c") is not None
    assert registry.get("a") is None
