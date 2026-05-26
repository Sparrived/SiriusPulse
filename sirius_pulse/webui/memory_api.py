"""WebUI API endpoints for memory, tokens, cognition, diary, and user profiles."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from aiohttp import web

from sirius_pulse.webui.server_core import _get_name, _json_response, LOG


async def api_tokens_get(request: web.Request, persona_manager: Any) -> web.Response:
    """Return aggregated token usage across all personas."""
    from sirius_pulse.token.token_store import TokenUsageStore
    from sirius_pulse.token import analytics as token_analytics

    total_summary = {
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
    }
    persona_breakdown: list[dict[str, Any]] = []

    for persona_info in persona_manager.list_personas():
        name = persona_info.get("name")
        if not name:
            continue
        paths = persona_manager.get_persona_paths(name)
        if paths is None:
            continue
        db_path = paths.dir / "token" / "token_usage.db"
        if not db_path.exists():
            continue
        try:
            store = TokenUsageStore(str(db_path))
            baseline = token_analytics.compute_baseline(store)
            total_summary["total_calls"] += baseline.get("total_calls", 0)
            total_summary["total_prompt_tokens"] += baseline.get("total_prompt_tokens", 0)
            total_summary["total_completion_tokens"] += baseline.get("total_completion_tokens", 0)
            total_summary["total_tokens"] += baseline.get("total_tokens", 0)
            persona_breakdown.append({
                "name": name,
                "calls": baseline.get("total_calls", 0),
                "prompt_tokens": baseline.get("total_prompt_tokens", 0),
                "completion_tokens": baseline.get("total_completion_tokens", 0),
                "total_tokens": baseline.get("total_tokens", 0),
            })
        except Exception as exc:
            LOG.warning("读取 Token 统计失败 %s: %s", name, exc)

    response_avg: dict[str, Any] = {"total_calls": 0, "avg_total_tokens": 0, "avg_prompt_tokens": 0, "avg_completion_tokens": 0}
    if total_summary["total_calls"]:
        response_avg = {
            "total_calls": total_summary["total_calls"],
            "avg_total_tokens": round(total_summary["total_tokens"] / total_summary["total_calls"], 1),
            "avg_prompt_tokens": round(total_summary["total_prompt_tokens"] / total_summary["total_calls"], 1),
            "avg_completion_tokens": round(total_summary["total_completion_tokens"] / total_summary["total_calls"], 1),
        }

    return _json_response({
        "summary": total_summary,
        "response_avg": response_avg,
        "personas": persona_breakdown,
    })


async def api_telemetry_get(request: web.Request, persona_manager: Any) -> web.Response:
    """Return global skill usage telemetry aggregated across all personas."""
    all_summaries: dict[str, dict[str, Any]] = {}
    total_calls = 0

    for persona_info in persona_manager.list_personas():
        name = persona_info.get("name")
        if not name:
            continue
        paths = persona_manager.get_persona_paths(name)
        if paths is None:
            continue
        telemetry_path = paths.dir / "skill_data" / ".telemetry.jsonl"
        if not telemetry_path.exists():
            continue
        try:
            with open(telemetry_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    skill_name = record.get("skill_name", "unknown")
                    if skill_name not in all_summaries:
                        all_summaries[skill_name] = {
                            "calls": 0,
                            "successes": 0,
                            "failures": 0,
                            "total_ms": 0.0,
                        }
                    agg = all_summaries[skill_name]
                    agg["calls"] += 1
                    total_calls += 1
                    if record.get("success"):
                        agg["successes"] += 1
                    else:
                        agg["failures"] += 1
                    agg["total_ms"] += record.get("duration_ms", 0)
        except Exception as exc:
            LOG.warning("读取 Telemetry 失败 %s: %s", name, exc)

    skills: dict[str, Any] = {}
    for skill_name, stats in all_summaries.items():
        calls = stats["calls"]
        skills[skill_name] = {
            "calls": calls,
            "success_rate": round(stats["successes"] / calls * 100, 1) if calls else 0,
            "avg_ms": round(stats["total_ms"] / calls, 1) if calls else 0,
        }

    return _json_response({
        "total_calls": total_calls,
        "skills": skills,
    })


async def api_persona_tokens_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    from sirius_pulse.token.token_store import TokenUsageStore
    from sirius_pulse.token import analytics as token_analytics

    db_path = paths.dir / "token" / "token_usage.db"
    if not db_path.exists():
        return _json_response({"total": 0, "daily": [], "models": []})

    # Parse optional time range from query params
    start_ts: float | None = None
    end_ts: float | None = None
    try:
        if request.query.get("start"):
            start_ts = float(request.query["start"])
        if request.query.get("end"):
            end_ts = float(request.query["end"])
    except ValueError:
        LOG.warning("解析时间范围查询参数失败", exc_info=True)
        pass

    try:
        store = TokenUsageStore(str(db_path))
        baseline = token_analytics.compute_baseline(store, start_ts=start_ts, end_ts=end_ts)
        by_model = token_analytics.group_by_model(store, start_ts=start_ts, end_ts=end_ts)
        time_series = token_analytics.time_series(store, bucket_seconds=3600, start_ts=start_ts, end_ts=end_ts)

        # 转换为前端期望的格式
        summary = {
            "total_calls": baseline.get("total_calls", 0),
            "total_prompt_tokens": baseline.get("total_prompt_tokens", 0),
            "total_completion_tokens": baseline.get("total_completion_tokens", 0),
            "total_tokens": baseline.get("total_tokens", 0),
        }
        response_avg = {}
        if summary["total_calls"]:
            response_avg = {
                "total_calls": summary["total_calls"],
                "avg_total_tokens": round(summary["total_tokens"] / summary["total_calls"], 1),
                "avg_prompt_tokens": round(summary["total_prompt_tokens"] / summary["total_calls"], 1),
                "avg_completion_tokens": round(summary["total_completion_tokens"] / summary["total_calls"], 1),
            }

        # hourly 数据（按小时聚合，用于时间序列图）
        hourly = []
        for ts in time_series:
            try:
                dt = datetime.fromisoformat(ts["time_bucket"])
                hour_ts = int(dt.timestamp())
            except Exception:
                LOG.warning("读取 token 文件失败", exc_info=True)
                continue
            hourly.append({
                "hour_ts": hour_ts,
                "hour": dt.hour,
                "calls": ts.get("calls", 0),
                "prompt_tokens": ts.get("prompt_tokens", 0),
                "completion_tokens": ts.get("completion_tokens", 0),
                "total_tokens": ts.get("total_tokens", 0),
            })

        # hourly_distribution: 按小时聚合的调用分布
        hourly_distribution: dict[int, int] = {}
        for h in hourly:
            hour = h["hour"]
            hourly_distribution[hour] = hourly_distribution.get(hour, 0) + h["calls"]
        hourly_distribution_list = [
            {"hour": h, "calls": c}
            for h, c in sorted(hourly_distribution.items())
        ]

        # by_model 转换为前端期望的格式
        by_model_list = [
            {
                "name": m,
                "calls": v.get("calls", 0),
                "prompt_tokens": v.get("prompt_tokens", 0),
                "completion_tokens": v.get("completion_tokens", 0),
                "total_tokens": v.get("total_tokens", 0),
            }
            for m, v in by_model.items()
        ]

        # 查询各维度 breakdown 数据
        by_group = store.get_breakdown_by("group_id", start_ts=start_ts, end_ts=end_ts)
        by_provider = store.get_breakdown_by("provider_name", start_ts=start_ts, end_ts=end_ts)
        by_task = store.get_breakdown_by("task_name", start_ts=start_ts, end_ts=end_ts)
        section_breakdown = store.get_section_breakdown(start_ts=start_ts, end_ts=end_ts)
        section_breakdown_by_task = store.get_section_breakdown_by_task(start_ts=start_ts, end_ts=end_ts)
        recent_with_breakdown = store.get_recent_records_with_breakdown(limit=100, start_ts=start_ts, end_ts=end_ts)

        # 统计指标
        total_tokens = summary["total_tokens"]
        prompt_tokens = summary["total_prompt_tokens"]
        completion_tokens = summary["total_completion_tokens"]
        ratio = {}
        if total_tokens:
            ratio = {
                "prompt_pct": round(prompt_tokens * 100.0 / total_tokens, 1),
                "completion_pct": round(completion_tokens * 100.0 / total_tokens, 1),
            }

        return _json_response({
            "summary": summary,
            "response_avg": response_avg,
            "hourly": hourly,
            "hourly_distribution": hourly_distribution_list,
            "by_model": by_model_list,
            "by_group": by_group,
            "by_provider": by_provider,
            "by_task": by_task,
            "section_breakdown": section_breakdown,
            "section_breakdown_by_task": section_breakdown_by_task,
            "recent_with_breakdown": recent_with_breakdown,
            "ratio": ratio,
            "efficiency_stats": store.get_efficiency_stats(start_ts=start_ts, end_ts=end_ts),
            "retry_stats": store.get_retry_stats(start_ts=start_ts, end_ts=end_ts),
            "duration_stats": store.get_duration_stats(start_ts=start_ts, end_ts=end_ts),
            "empty_reply_stats": store.get_empty_reply_stats(start_ts=start_ts, end_ts=end_ts),
            "failure_stats": store.get_failure_stats(start_ts=start_ts, end_ts=end_ts),
            "depth_stats": store.get_conversation_depth_stats(start_ts=start_ts, end_ts=end_ts),
            "period_comparison": store.get_period_comparison(start_ts=start_ts, end_ts=end_ts),
        })
    except Exception as exc:
        LOG.warning("读取 Token 统计失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_cognition_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    db_path = paths.dir / "cognition_events.db"
    if not db_path.exists():
        return _json_response({"events": [], "emotion_distribution": {}})

    try:
        from sirius_pulse.memory.cognition_store import CognitionEventStore
        store = CognitionEventStore(str(db_path))
        limit = int(request.query.get("limit", "50"))
        events = store.get_recent(limit=limit)
        group_id = request.query.get("group_id", None)
        emotion_distribution = store.get_emotion_distribution(group_id=group_id if group_id else None)
        store.close()
        return _json_response({"events": events, "emotion_distribution": emotion_distribution})
    except Exception as exc:
        LOG.warning("读取认知事件失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_diary_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    diary_dir = paths.dir / "diary"
    if not diary_dir.exists():
        return _json_response({"entries": [], "stats": {}, "groups": []})

    try:
        limit = int(request.query.get("limit", "500"))
        group_id = request.query.get("group_id", "")

        entries: list[dict[str, Any]] = []
        groups: set[str] = set()
        keyword_counts: dict[str, int] = {}

        for path in diary_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                g_id = data.get("group_id", "")
                if g_id:
                    groups.add(g_id)
                if group_id and g_id != group_id:
                    continue
                for item in data.get("entries", []):
                    if isinstance(item, dict):
                        entries.append(item)
                        for kw in item.get("keywords", []):
                            keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
            except (OSError, json.JSONDecodeError):
                continue

        total_count = len(entries)
        entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        if limit > 0:
            entries = entries[:limit]

        stats = {
            "total": total_count,
            "groups": len(groups),
            "top_keywords": sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)[:20],
        }

        return _json_response({
            "entries": entries,
            "stats": stats,
            "groups": sorted(groups),
        })
    except Exception as exc:
        LOG.warning("读取日记失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_vector_store_status_get(request: web.Request, persona_manager: Any) -> web.Response:
    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    from sirius_pulse.memory.diary.vector_store import DiaryVectorStore

    vector_db_dir = paths.dir / "diary" / "vector_db"
    try:
        vs = DiaryVectorStore(vector_db_dir)
        stats = vs.get_stats()
        return _json_response(stats)
    except Exception as exc:
        LOG.warning("读取向量存储状态失败 %s: %s", name, exc)
        return _json_response({
            "available": False,
            "total_entries": 0,
            "groups": [],
            "model": DiaryVectorStore.MODEL_NAME,
            "error": str(exc),
        })


async def api_persona_users_get(request: web.Request, persona_manager: Any) -> web.Response:
    """Return user semantic profiles for a single persona."""
    from sirius_pulse.memory.semantic.store import SemanticProfileStore

    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    # SemanticProfileStore expects persona_dir and appends memory/semantic itself
    semantic_base = paths.dir / "memory" / "semantic"
    if not semantic_base.exists():
        return _json_response({"users": [], "groups": []})

    try:
        group_id = request.query.get("group_id", "")
        store = SemanticProfileStore(paths.dir)

        users: list[dict[str, Any]] = []
        groups: set[str] = set()
        seen_user_ids: set[str] = set()

        # Collect available group IDs from directory structure
        users_dir = semantic_base / "users"
        if users_dir.exists():
            for g_dir in users_dir.iterdir():
                if g_dir.is_dir():
                    groups.add(g_dir.name)

        if group_id:
            # Group-scoped query
            for profile in store.list_group_user_profiles(group_id):
                if profile.user_id and profile.user_id not in seen_user_ids:
                    seen_user_ids.add(profile.user_id)
                    users.append(profile.to_dict())
        else:
            # Cross-group query: collect all group-local profiles
            for g in groups:
                for profile in store.list_group_user_profiles(g):
                    if profile.user_id and profile.user_id not in seen_user_ids:
                        seen_user_ids.add(profile.user_id)
                        users.append(profile.to_dict())

        return _json_response({"users": users, "groups": sorted(groups)})
    except Exception as exc:
        LOG.warning("读取用户画像失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_user_get(request: web.Request, persona_manager: Any) -> web.Response:
    """Return a single user semantic profile for a persona."""
    from sirius_pulse.memory.semantic.store import SemanticProfileStore

    name = _get_name(request)
    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少用户ID"}, 400)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    # SemanticProfileStore expects persona_dir and appends memory/semantic itself
    semantic_base = paths.dir / "memory" / "semantic"
    if not semantic_base.exists():
        return _json_response({"error": "用户不存在"}, 404)

    try:
        group_id = request.query.get("group_id", "")
        store = SemanticProfileStore(paths.dir)

        profile = None
        if group_id:
            profile = store.load_user_profile(group_id, user_id)
        if profile is None:
            # Fallback: scan all groups
            users_dir = semantic_base / "users"
            if users_dir.exists():
                for g_dir in users_dir.iterdir():
                    if g_dir.is_dir():
                        p = store.load_user_profile(g_dir.name, user_id)
                        if p is not None:
                            profile = p
                            break

        if profile is None:
            return _json_response({"error": "用户不存在"}, 404)

        return _json_response({"user": profile.to_dict()})
    except Exception as exc:
        LOG.warning("读取用户画像失败 %s/%s: %s", name, user_id, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_glossary_get(request: web.Request, persona_manager: Any) -> web.Response:
    """Return glossary terms for a persona.

    Query params:
      - search: text search (optional)
      - limit: max terms (default 200)
    """
    from sirius_pulse.memory.glossary.manager import GlossaryManager

    name = _get_name(request)
    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    glossary_dir = paths.dir / "glossary"
    if not glossary_dir.exists():
        return _json_response({"terms": [], "stats": {}})

    try:
        search = request.query.get("search", "")
        limit = int(request.query.get("limit", "200"))

        manager = GlossaryManager(paths.dir, persona_name=name)

        terms: list[dict[str, Any]] = []
        all_terms = manager._load()
        for term in all_terms.values():
            term_dict = term.to_dict()
            terms.append(term_dict)

        if search:
            search_lower = search.lower()
            terms = [
                t for t in terms
                if search_lower in t.get("term", "").lower()
                or search_lower in t.get("definition", "").lower()
            ]

        terms.sort(key=lambda t: t.get("confidence", 0) * t.get("usage_count", 1), reverse=True)
        terms = terms[:limit]

        stats = {
            "total": len(terms),
        }

        return _json_response({"terms": terms, "stats": stats})
    except Exception as exc:
        LOG.warning("读取名词解释失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)


async def api_persona_memory_viz(request: web.Request, persona_manager: Any) -> web.Response:
    """GET /api/personas/{name}/memory-viz — 记忆可视化数据聚合接口。

    Query params:
        group_id     : 按群过滤（为空则全部）
        basic_limit  : 基础记忆条数上限（默认 500，最大 2000）
        diary_limit  : 日记条数上限（默认 200，最大 500）
    """
    name = _get_name(request)
    group_filter = request.query.get("group_id", "").strip()
    limit_basic = min(int(request.query.get("basic_limit", "500")), 2000)
    limit_diary = min(int(request.query.get("diary_limit", "200")), 500)

    paths = persona_manager.get_persona_paths(name)
    if paths is None:
        return _json_response({"error": "人格不存在"}, 404)

    try:
        # ── 1. 基础记忆：按群+天聚合为柱状图数据 ──
        archive_dir = paths.dir / "archive"
        all_groups: set[str] = set()
        # day_bucket[date][group_id] = {human: N, assistant: N, system: N}
        day_bucket: dict[str, dict[str, dict[str, int]]] = {}
        # 最近 N 条明细（仅用于 tooltip 展示）
        recent_entries: list[dict[str, Any]] = []

        if archive_dir.exists():
            for path in archive_dir.glob("*.jsonl"):
                gid = path.stem
                all_groups.add(gid)
                if group_filter and gid != group_filter:
                    continue
                try:
                    with path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                                ts = data.get("timestamp", "")
                                role = data.get("role", "human")
                                day = ts[:10] if ts else "unknown"
                                if day not in day_bucket:
                                    day_bucket[day] = {}
                                if gid not in day_bucket[day]:
                                    day_bucket[day][gid] = {"human": 0, "assistant": 0, "system": 0}
                                day_bucket[day][gid][role] = day_bucket[day][gid].get(role, 0) + 1

                                recent_entries.append({
                                    "group_id": gid,
                                    "speaker_name": data.get("speaker_name", ""),
                                    "role": role,
                                    "content": data.get("content", "")[:120],
                                    "timestamp": ts,
                                })
                            except (json.JSONDecodeError, TypeError):
                                continue
                except OSError:
                    continue

        recent_entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        recent_entries = recent_entries[:limit_basic]

        days_sorted = sorted(day_bucket.keys())
        groups_in_data = sorted({g for bucket in day_bucket.values() for g in bucket})

        # ── 2. 日记聚类：embedding + 关键词频率 ──
        diary_dir = paths.dir / "diary"
        diary_entries: list[dict[str, Any]] = []
        keyword_freq: dict[str, int] = {}
        if diary_dir.exists():
            for path in diary_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    g_id = data.get("group_id", "")
                    if group_filter and g_id != group_filter:
                        continue
                    for item in data.get("entries", []):
                        if not isinstance(item, dict):
                            continue
                        emb = item.get("embedding")
                        diary_entries.append({
                            "entry_id": item.get("entry_id", ""),
                            "group_id": g_id,
                            "created_at": item.get("created_at", ""),
                            "summary": item.get("summary", ""),
                            "content": item.get("content", "")[:300],
                            "keywords": item.get("keywords", []),
                            "embedding": emb,
                        })
                        for kw in item.get("keywords", []):
                            keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
                except (OSError, json.JSONDecodeError):
                    continue
        diary_entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
        diary_entries = diary_entries[:limit_diary]
        top_keywords = sorted(keyword_freq.items(), key=lambda x: x[1], reverse=True)[:20]

        # ── 3. 用户-话题二部图 ──
        semantic_base = paths.dir / "memory" / "semantic"
        user_nodes: list[dict[str, Any]] = []
        topic_nodes: list[dict[str, str]] = []  # {id, name}
        user_topic_links: list[dict[str, Any]] = []
        if semantic_base.exists():
            users_dir = semantic_base / "users"
            if users_dir.exists():
                seen: set[str] = set()
                topic_set: set[str] = set()
                for g_dir in users_dir.iterdir():
                    if not g_dir.is_dir():
                        continue
                    if group_filter and g_dir.name != group_filter:
                        continue
                    for u_file in g_dir.glob("*.json"):
                        try:
                            u_data = json.loads(u_file.read_text(encoding="utf-8"))
                            uid = u_data.get("user_id", "")
                            if not uid or uid in seen:
                                continue
                            seen.add(uid)
                            engagement = u_data.get("engagement_rate", 0)
                            count = u_data.get("interaction_count", 0)
                            user_nodes.append({
                                "user_id": uid,
                                "name": u_data.get("name", uid),
                                "engagement_rate": engagement,
                                "interaction_count": count,
                            })
                        except (OSError, json.JSONDecodeError, TypeError):
                            continue
                for t in sorted(topic_set):
                    topic_nodes.append({"id": t, "name": t})

        return _json_response({
            "groups": sorted(all_groups),
            "basic_timeline": {
                "days": days_sorted,
                "groups": groups_in_data,
                "buckets": day_bucket,
                "recent": recent_entries,
            },
            "diary_entries": diary_entries,
            "diary_top_keywords": top_keywords,
            "user_nodes": user_nodes,
            "topic_nodes": topic_nodes,
            "user_topic_links": user_topic_links,
        })
    except Exception as exc:
        LOG.warning("读取记忆可视化数据失败 %s: %s", name, exc)
        return _json_response({"error": str(exc)}, 500)
