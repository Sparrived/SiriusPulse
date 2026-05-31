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

        chain = EvolutionChain(db_path, read_only=True)
        all_subjects = chain._store.get_all_subjects()
        all_records = []
        for s in all_subjects:
            all_records.extend(chain.get_all_by_subject(s))
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

        sit_store = SituationStore(db_path, read_only=True)
        all_situations = sit_store.get_all(limit=500)
        result["situation_stats"] = {
            "total_situations": len(all_situations),
            "today_count": len([s for s in all_situations if s.created_at[:10] == __import__("datetime").datetime.now().strftime("%Y-%m-%d")]),
        }
        # 话题频率
        topic_freq: dict[str, int] = {}
        for sit in all_situations:
            for t in sit.topics:
                topic_freq[t] = topic_freq.get(t, 0) + 1
        result["top_topics"] = sorted(topic_freq.items(), key=lambda x: x[1], reverse=True)[:15]
    except Exception as exc:
        LOG.debug("读取情景统计失败: %s", exc)
        result["situation_stats"] = {"total_situations": 0, "today_count": 0}

    # 日记统计
    try:
        diary_dir = paths.dir / "diary"
        total_entries = 0
        if diary_dir.exists():
            for f in diary_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    total_entries += len(data.get("entries", []))
                except (OSError, json.JSONDecodeError):
                    continue
        slices_dir = diary_dir / "slices"
        total_slices = 0
        if slices_dir.exists():
            for f in slices_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    total_slices += len(data.get("slices", []))
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
    db_path, _ = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.evolution.chain import EvolutionChain

    chain = EvolutionChain(db_path, read_only=True)
    subject = request.query.get("subject", "").strip()
    status_filter = request.query.get("status", "").strip()
    limit = min(int(request.query.get("limit", "200")), 500)
    offset = max(int(request.query.get("offset", "0")), 0)

    if subject:
        records = chain.get_all_by_subject(subject)
    else:
        all_subjects = chain._store.get_all_subjects()
        records = []
        for s in all_subjects:
            records.extend(chain.get_all_by_subject(s))

    # 状态过滤
    if status_filter:
        records = [r for r in records if r.status == status_filter]

    total = len(records)
    records.sort(key=lambda r: r.extracted_at or "", reverse=True)
    records = records[offset:offset + limit]

    return _json_response({
        "records": [_record_to_dict(r) for r in records],
        "total": total,
        "offset": offset,
        "limit": limit,
    })


@handle_api_errors
async def api_evolution_history(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/evolution/{record_id}/history — 单条记录的演化历史。"""
    name = _get_name(request)
    record_id = str(request.match_info.get("record_id", "")).strip()
    if not record_id:
        return _json_response({"error": "缺少 record_id"}, 400)

    db_path, _ = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.evolution.chain import EvolutionChain

    chain = EvolutionChain(db_path, read_only=True)
    history = chain.get_history(record_id)
    return _json_response({
        "history": [_record_to_dict(r) for r in history],
    })


@handle_api_errors
async def api_evolution_uncertain(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/evolution/uncertain — 待验证记录。"""
    name = _get_name(request)
    db_path, _ = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.evolution.chain import EvolutionChain

    chain = EvolutionChain(db_path, read_only=True)
    limit = min(int(request.query.get("limit", "50")), 200)
    records = chain.get_uncertain_records(limit=limit)
    return _json_response({
        "records": [_record_to_dict(r) for r in records],
        "total": len(records),
    })


def _record_to_dict(r: Any) -> dict[str, Any]:
    """将 EvolutionRecord 转为 JSON 字典。"""
    return {
        "record_id": r.record_id,
        "subject": r.subject,
        "subject_user_id": r.subject_user_id,
        "predicate": r.predicate,
        "obj": r.obj,
        "status": r.status,
        "confidence": r.confidence,
        "initial_confidence": r.initial_confidence,
        "supersedes": r.supersedes,
        "superseded_by": r.superseded_by,
        "source_type": r.source_type,
        "source_situation_id": r.source_situation_id,
        "source_group_id": r.source_group_id,
        "source_message_ids": r.source_message_ids,
        "extracted_at": r.extracted_at,
        "extracted_by_model": r.extracted_by_model,
        "verifications": r.verifications,
        "corrections": r.corrections,
    }


# ─── 情景时间线 ──────────────────────────────────────────


@handle_api_errors
async def api_situations_list(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/situations — 情景列表。"""
    name = _get_name(request)
    db_path, _ = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.situation.store import SituationStore

    store = SituationStore(db_path, read_only=True)
    group_id = request.query.get("group_id", "").strip()
    limit = min(int(request.query.get("limit", "100")), 500)

    situations = store.get_by_group(group_id, limit=limit) if group_id else store.get_all(limit=limit)

    return _json_response({
        "situations": [_situation_to_dict(s) for s in situations],
        "total": len(situations),
    })


def _situation_to_dict(s: Any) -> dict[str, Any]:
    """将 Situation 转为 JSON 字典。"""
    triples = []
    for t in s.triples:
        triples.append({
            "subject": t.subject,
            "predicate": t.predicate,
            "obj": t.obj,
            "confidence": t.confidence,
            "meta_tag": t.meta_tag,
        })
    return {
        "situation_id": s.situation_id,
        "group_id": s.group_id,
        "created_at": s.created_at,
        "triples": triples,
        "participants": s.participants,
        "topics": s.topics,
        "summary": s.summary,
        "source_entry_ids": s.source_entry_ids,
        "time_range_start": s.time_range_start,
        "time_range_end": s.time_range_end,
        "validated_triple_count": s.validated_triple_count,
        "rejected_triple_count": s.rejected_triple_count,
    }


# ─── 日记切片 ────────────────────────────────────────────


@handle_api_errors
async def api_diary_slices(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/diary-slices — 日记切片列表。"""
    name = _get_name(request)
    _, paths = _open_db(persona_manager, name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    slices_dir = paths.dir / "diary" / "slices"
    if not slices_dir.exists():
        return _json_response({"slices": [], "total": 0})

    limit = min(int(request.query.get("limit", "100")), 500)
    offset = max(int(request.query.get("offset", "0")), 0)
    search = request.query.get("search", "").strip().lower()

    slices: list[dict[str, Any]] = []
    for slice_file in slices_dir.glob("*.json"):
        try:
            data = json.loads(slice_file.read_text(encoding="utf-8"))
            g_id = data.get("group_id", "")
            for slice_data in data.get("slices", []):
                if not isinstance(slice_data, dict):
                    continue
                slice_data["_group_id"] = g_id
                if search:
                    content = (slice_data.get("content", "") + slice_data.get("summary", "")).lower()
                    if search not in content:
                        continue
                slices.append(slice_data)
        except (OSError, json.JSONDecodeError):
            continue

    slices.sort(key=lambda s: s.get("time_range_start", ""), reverse=True)
    total = len(slices)
    slices = slices[offset:offset + limit]

    return _json_response({
        "slices": slices,
        "total": total,
        "offset": offset,
        "limit": limit,
    })


# ─── 传记面板 ────────────────────────────────────────────


@handle_api_errors
async def api_biography_view(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/biography/{user_id} — 传记视图。"""
    name = _get_name(request)
    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少 user_id"}, 400)

    db_path, _ = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.evolution.chain import EvolutionChain
    from sirius_pulse.memory.biography.view import BiographyView

    chain = EvolutionChain(db_path, read_only=True)
    bio_view = BiographyView(chain)
    bio = bio_view.get_biography(user_id)

    return _json_response({
        "biography": {
            "user_id": bio.user_id,
            "name": bio.name,
            "identity_anchors": bio.identity_anchors,
            "relationships": bio.relationships,
            "short_bio": bio.short_bio,
            "active_fact_count": bio.active_fact_count,
            "superseded_fact_count": bio.superseded_fact_count,
            "uncertain_fact_count": bio.uncertain_fact_count,
            "source_record_ids": bio.source_record_ids,
            "generated_at": bio.generated_at,
        }
    })


@handle_api_errors
async def api_biography_list_all(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/biographies — 所有用户传记列表。"""
    name = _get_name(request)
    db_path, _ = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.evolution.chain import EvolutionChain
    from sirius_pulse.memory.biography.view import BiographyView

    chain = EvolutionChain(db_path, read_only=True)
    bio_view = BiographyView(chain)

    # 收集所有有记录的 user_id
    all_subjects = chain._store.get_all_subjects()
    all_records = []
    for s in all_subjects:
        all_records.extend(chain.get_all_by_subject(s))
    user_ids: set[str] = set()
    for r in all_records:
        if r.subject_user_id:
            user_ids.add(r.subject_user_id)

    bios: list[dict[str, Any]] = []
    for uid in user_ids:
        bio = bio_view.get_biography(uid)
        bios.append({
            "user_id": bio.user_id,
            "name": bio.name,
            "identity_anchors": bio.identity_anchors,
            "relationships": bio.relationships,
            "short_bio": bio.short_bio,
            "active_fact_count": bio.active_fact_count,
            "superseded_fact_count": bio.superseded_fact_count,
            "uncertain_fact_count": bio.uncertain_fact_count,
        })

    bios.sort(key=lambda b: b["active_fact_count"], reverse=True)
    return _json_response({"biographies": bios, "total": len(bios)})


# ─── 知识缺口与行为模式 ─────────────────────────────────


@handle_api_errors
async def api_knowledge_gaps(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory/gaps/{user_id} — 知识缺口检测。"""
    name = _get_name(request)
    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少 user_id"}, 400)

    db_path, _ = _open_db(persona_manager, name)
    if not db_path:
        return _json_response({"error": "人格不存在或数据库不存在"}, 404)

    from sirius_pulse.memory.evolution.chain import EvolutionChain
    from sirius_pulse.memory.biography.view import BiographyView

    chain = EvolutionChain(db_path, read_only=True)
    bio_view = BiographyView(chain)
    bio = bio_view.get_biography(user_id)

    # 简单的知识缺口分析
    gaps: list[dict[str, Any]] = []
    if not bio.identity_anchors:
        gaps.append({"gap_type": "identity", "domain": "身份", "description": f"用户 {bio.name} 的身份信息不足", "importance": "high"})
    if not bio.relationships:
        gaps.append({"gap_type": "social", "domain": "社交", "description": f"用户 {bio.name} 的社交关系未知", "importance": "medium"})
    if bio.uncertain_fact_count > 0:
        gaps.append({"gap_type": "uncertain", "domain": "待确认", "description": f"有 {bio.uncertain_fact_count} 条待确认的事实", "importance": "low"})
    if bio.active_fact_count < 3:
        gaps.append({"gap_type": "sparse", "domain": "稀疏", "description": f"用户 {bio.name} 的已知信息太少（仅 {bio.active_fact_count} 条）", "importance": "medium"})

    return _json_response({"gaps": gaps, "total": len(gaps)})
