"""WebUI API endpoints for the new unified memory system.

暴露演化链、情景、日记切片、传记、知识缺口、行为模式等接口。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_utils import _get_name, _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")


def _open_db(persona_manager: Any, name: str) -> tuple[Any, Any]:
    """获取人格的 db_path 和 paths，失败返回 (None, None)。"""
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return None, None
    db_path = paths.dir / "persona.db"
    if not db_path.exists():
        return None, paths
    return str(db_path), paths


# ─── 记忆系统仪表盘 ──────────────────────────────────────


@handle_api_errors
async def api_memory_dashboard(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/dashboard — 记忆系统综合仪表盘。"""
    name = _get_name(request)
    db_path, paths = _open_db(persona_manager, name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    result: dict[str, Any] = {"has_data": False}
    if not db_path:
        return _json_response(result)

    result["has_data"] = True

    # 演化链统计
    try:
        from sirius_pulse.memory.evolution.chain import EvolutionChain

        chain = EvolutionChain(db_path)
        all_records = chain.get_all_by_subject("")
        active = [r for r in all_records if r.status == "active"]
        superseded = [r for r in all_records if r.status == "superseded"]
        uncertain = [r for r in all_records if r.status == "uncertain"]
        rejected = [r for r in all_records if r.status == "rejected"]
        result["evolution_stats"] = {
            "total_records": len(all_records),
            "active_records": len(active),
            "superseded_records": len(superseded),
            "uncertain_records": len(uncertain),
            "rejected_records": len(rejected),
        }
        # 计算置信度分布
        conf_buckets: dict[str, int] = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "0.9-1.0": 0}
        for r in all_records:
            c = r.confidence
            if c < 0.3:
                conf_buckets["0.0-0.3"] += 1
            elif c < 0.5:
                conf_buckets["0.3-0.5"] += 1
            elif c < 0.7:
                conf_buckets["0.5-0.7"] += 1
            elif c < 0.9:
                conf_buckets["0.7-0.9"] += 1
            else:
                conf_buckets["0.9-1.0"] += 1
        result["confidence_distribution"] = conf_buckets

        # 谓语频率
        predicate_freq: dict[str, int] = {}
        for r in active:
            predicate_freq[r.predicate] = predicate_freq.get(r.predicate, 0) + 1
        result["top_predicates"] = sorted(predicate_freq.items(), key=lambda x: x[1], reverse=True)[:15]
    except Exception as exc:
        LOG.debug("读取演化链统计失败: %s", exc)
        result["evolution_stats"] = {"total_records": 0, "active_records": 0, "superseded_records": 0, "uncertain_records": 0, "rejected_records": 0}

    # 情景统计
    try:
        from sirius_pulse.memory.situation.store import SituationStore

        sit_store = SituationStore(db_path)
        all_situations = sit_store.get_by_group("", limit=500)
        result["situation_stats"] = {
            "total_situations": len(all_situations),
            "today_count": len([s for s in all_situations if s.created_at[:10] == __import__("datetime").datetime.now().strftime("%Y-%m-%d")]),
        }
        # 话题频率
        topic_freq: dict[str, int] = {}
        for s in all_situations:
            for t in s.topics:
                topic_freq[t] = topic_freq.get(t, 0) + 1
        result["top_topics"] = sorted(topic_freq.items(), key=lambda x: x[1], reverse=True)[:15]
    except Exception as exc:
        LOG.debug("读取情景统计失败: %s", exc)
        result["situation_stats"] = {"total_situations": 0, "today_count": 0}

    # 日记统计
    try:
        diary_dir = paths.dir / "diary"
        total_slices = 0
        total_entries = 0
        if diary_dir.exists():
            for f in diary_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    entries = data.get("entries", [])
                    total_entries += len(entries)
                    for e in entries:
                        total_slices += len(e.get("slices", []))
                except (OSError, json.JSONDecodeError):
                    continue
        result["diary_stats"] = {"total_entries": total_entries, "total_slices": total_slices}
    except Exception as exc:
        LOG.debug("读取日记统计失败: %s", exc)
        result["diary_stats"] = {"total_entries": 0, "total_slices": 0}

    # 用户统计
    try:
        semantic_base = paths.dir / "memory" / "semantic"
        user_count = 0
        if semantic_base.exists():
            users_dir = semantic_base / "users"
            if users_dir.exists():
                seen: set[str] = set()
                for g_dir in users_dir.iterdir():
                    if g_dir.is_dir():
                        for f in g_dir.glob("*.json"):
                            uid = f.stem
                            if uid not in seen:
                                seen.add(uid)
                                user_count += 1
        result["user_count"] = user_count
    except Exception as exc:
        LOG.debug("读取用户统计失败: %s", exc)
        result["user_count"] = 0

    return _json_response(result)


# ─── 演化链浏览器 ────────────────────────────────────────


@handle_api_errors
async def api_evolution_records(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/evolution — 演化链记录列表。"""
    name = _get_name(request)
    db_path, paths = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.evolution.chain import EvolutionChain

    chain = EvolutionChain(db_path)
    subject = request.query.get("subject", "").strip()
    status_filter = request.query.get("status", "").strip()
    limit = min(int(request.query.get("limit", "200")), 500)
    offset = max(int(request.query.get("offset", "0")), 0)

    if subject:
        records = chain.get_all_by_subject(subject)