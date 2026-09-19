"""记忆单元检索质量与失效/冲突硬闩。

这些用例保护的是「哪些单元会被注入提示词」这一行为，而不是管道能否读写。
检索排序改动没有观测口径时无法判断改好还是改坏，因此这里固定三件事：

1. 召回基线：一条查询应当命中哪条记忆。
2. 过期事实不能进入候选（valid_until 已过）。
3. 冲突对里旧的那条，在两条同时被召回时让位。
"""

from __future__ import annotations

import pytest

from sirius_pulse.memory.units import MemoryUnit, MemoryUnitIndexer

_PAST = "2020-01-01T00:00:00+00:00"
_FUTURE = "2099-01-01T00:00:00+00:00"


def _unit(unit_id: str, summary: str, **changes) -> MemoryUnit:
    values = {
        "unit_id": unit_id,
        "group_id": "group-a",
        "created_at": "2026-07-12T00:00:00+00:00",
        "unit_type": "event",
        "scope": "group",
        "scope_id": "",
        "summary": summary,
        "keywords": ["部署"],
        "salience": 0.6,
        "confidence": 0.7,
        "source_ids": [f"src-{unit_id}"],
    }
    values.update(changes)
    return MemoryUnit(**values)


def _ids(results) -> list[str]:
    return [unit.unit_id for unit, _score in results]


# ----------------------------------------------------------------------
# 召回基线
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("上次部署到哪一步了", "mem-deploy"),
        ("Alice 喜欢什么样的回复风格", "mem-style"),
        ("服务器迁移的事情定了吗", "mem-migrate"),
    ],
)
def test_recall_hits_expected_unit(query, expected):
    indexer = MemoryUnitIndexer()
    indexer.add(_unit("mem-deploy", "已完成服务重新部署。", keywords=["部署", "上线"]))
    indexer.add(_unit("mem-style", "Alice 偏好简洁的回复风格。", keywords=["简洁", "回复风格"]))
    indexer.add(_unit("mem-migrate", "服务器迁移计划尚未确认。", keywords=["迁移", "服务器"]))

    found = _ids(indexer.search(query, group_id="group-a", top_k=3))

    assert expected in found


def test_recall_respects_top_k_budget():
    indexer = MemoryUnitIndexer()
    for index in range(6):
        indexer.add(_unit(f"mem-{index}", f"第 {index} 次部署记录。", keywords=["部署"]))

    assert len(indexer.search("部署", group_id="group-a", top_k=2)) == 2


# ----------------------------------------------------------------------
# 生命周期闩：过期事实不再注入
# ----------------------------------------------------------------------


def test_expired_unit_is_not_recalled():
    indexer = MemoryUnitIndexer()
    indexer.add(_unit("mem-dead", "下周三要开评审会。", valid_until=_PAST, keywords=["评审会"]))
    indexer.add(_unit("mem-live", "评审会已改期到明年。", valid_until=_FUTURE, keywords=["评审会"]))

    found = _ids(indexer.search("评审会什么时候", group_id="group-a", top_k=5))

    assert "mem-dead" not in found
    assert "mem-live" in found


def test_unparseable_valid_until_is_treated_as_not_expired():
    """脏数据不能把有效记忆判死：解析失败一律视为未过期。"""
    indexer = MemoryUnitIndexer()
    indexer.add(_unit("mem-dirty", "部署窗口待定。", valid_until="下周三", keywords=["部署窗口"]))

    assert "mem-dirty" in _ids(indexer.search("部署窗口", group_id="group-a", top_k=5))


# ----------------------------------------------------------------------
# 生命周期闩：冲突对不让新旧值同时注入
# ----------------------------------------------------------------------


def test_conflict_pair_injects_only_the_newer_value():
    indexer = MemoryUnitIndexer()
    old = _unit(
        "mem-old",
        "Alice 偏好详细解释。",
        keywords=["偏好"],
        event_time="2026-01-01T00:00:00+00:00",
        metadata={"conflicts_with": ["mem-new"]},
    )
    new = _unit(
        "mem-new",
        "Alice 现在偏好简洁回复。",
        keywords=["偏好"],
        event_time="2026-06-01T00:00:00+00:00",
        metadata={"conflicts_with": ["mem-old"]},
    )
    indexer.add(old)
    indexer.add(new)

    found = _ids(indexer.search("Alice 偏好什么", group_id="group-a", top_k=5))

    assert "mem-new" in found
    assert "mem-old" not in found


def test_conflicting_unit_is_kept_when_its_counterpart_is_not_recalled():
    """只有两条都被召回时才让位；对手不在候选里就保留本条。"""
    indexer = MemoryUnitIndexer()
    lonely = _unit(
        "mem-lonely",
        "Alice 偏好详细解释。",
        keywords=["偏好"],
        event_time="2026-01-01T00:00:00+00:00",
        metadata={"conflicts_with": ["mem-absent"]},
    )
    indexer.add(lonely)

    assert "mem-lonely" in _ids(indexer.search("Alice 偏好什么", group_id="group-a", top_k=5))


def test_conflict_check_survives_non_dict_metadata():
    """metadata 直接来自 JSON，被手改成 null 时不能让检索崩掉。"""
    indexer = MemoryUnitIndexer()
    indexer.add(_unit("mem-bad", "Alice 偏好详细解释。", keywords=["偏好"], metadata=None))

    assert "mem-bad" in _ids(indexer.search("Alice 偏好什么", group_id="group-a", top_k=5))
