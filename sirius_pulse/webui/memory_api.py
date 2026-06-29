"""WebUI API endpoints for memory, tokens, cognition, diary, and user profiles."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

from sirius_pulse.persona_config import PersonaConfigPaths
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_memory_name(name: str) -> str:
    import re

    base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
    base = re.sub(r"_+", "_", base).strip("_")
    return base or "default"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _diary_file(paths: PersonaConfigPaths, group_id: str) -> Path:
    return paths.dir / "diary" / f"{_safe_memory_name(group_id)}.json"


def _load_diary_payload(path: Path, group_id: str = "") -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                entries = data.get("entries", [])
                if isinstance(entries, list):
                    return {"group_id": str(data.get("group_id") or group_id), "entries": entries}
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return {"group_id": group_id, "entries": []}


def _find_diary_entry(paths: PersonaConfigPaths, entry_id: str) -> tuple[Path, dict[str, Any], dict[str, Any], int] | None:
    diary_dir = paths.dir / "diary"
    if not diary_dir.exists():
        return None
    for path in diary_dir.glob("*.json"):
        payload = _load_diary_payload(path)
        for idx, item in enumerate(payload.get("entries", [])):
            if isinstance(item, dict) and str(item.get("entry_id") or "") == entry_id:
                return path, payload, item, idx
    return None


def _read_tail_lines(path: Path, n: int) -> list[str]:
    """从文件末尾倒序读取 n 行非空内容，返回倒序列表（最新在前）。"""
    lines: list[str] = []
    with path.open("rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        if file_size == 0:
            return lines

        buf = b""
        pos = file_size
        chunk_size = 8192

        while pos > 0 and len(lines) < n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            buf = chunk + buf

            parts = buf.split(b"\n")
            buf = parts[0]
            for part in reversed(parts[1:]):
                stripped = part.strip()
                if stripped:
                    lines.append(stripped.decode("utf-8", errors="replace"))
                    if len(lines) >= n:
                        break

        if buf and len(lines) < n:
            stripped = buf.strip()
            if stripped:
                lines.append(stripped.decode("utf-8", errors="replace"))

    return lines


def _conversation_entry_key(entry: dict[str, Any]) -> str:
    entry_id = str(entry.get("entry_id") or "").strip()
    if entry_id:
        return f"id:{entry_id}"
    timestamp = str(entry.get("timestamp") or "")
    role = str(entry.get("role") or "")
    user_id = str(entry.get("user_id") or "")
    content = str(entry.get("content") or "")[:120]
    return f"fallback:{timestamp}:{role}:{user_id}:{content}"


def _load_runtime_basic_memory_messages(paths: Any, group_id: str = "") -> list[dict[str, Any]]:
    """Load the active basic-memory window used for prompt assembly.

    The archive files are append-only display history. The active prompt context
    is restored from engine_state/basic_memory.json, so exposing it here lets the
    WebUI inspect the same recent LLM chains that generation uses.
    """
    state_path = paths.engine_state / "basic_memory.json"
    if not state_path.exists():
        return []

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []

    if not isinstance(raw, dict):
        return []

    messages: list[dict[str, Any]] = []
    for gid, entries in raw.items():
        gid_text = str(gid)
        if group_id and gid_text != group_id:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["group_id"] = item.get("group_id") or gid_text
            if not item.get("tags"):
                item["tags"] = []
            messages.append(item)
    return messages


def _merge_conversation_messages(
    archive_messages: list[dict[str, Any]],
    runtime_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for source in (archive_messages, runtime_messages):
        for message in source:
            key = _conversation_entry_key(message)
            if key not in merged:
                order.append(key)
                merged[key] = message
            else:
                merged[key] = {**merged[key], **message}

    return [merged[key] for key in order]


def _conversation_message_matches_filters(
    message: dict[str, Any],
    *,
    search: str,
    speaker: str,
    start_time: str,
    end_time: str,
) -> bool:
    if search and search not in (message.get("content", "") or "").lower():
        return False
    if speaker:
        speaker_name = (message.get("speaker_name", "") or "").lower()
        user_id = (message.get("user_id", "") or "").lower()
        if speaker not in speaker_name and speaker not in user_id:
            return False
    if start_time and message.get("timestamp", "") < start_time:
        return False
    if end_time and message.get("timestamp", "") > end_time:
        return False
    return True


def _conversation_key_from_query(request: web.Request) -> str:
    key = request.query.get("key", "").strip()
    if key:
        return key

    entry_id = request.query.get("entry_id", "").strip()
    if entry_id:
        return f"id:{entry_id}"

    timestamp = request.query.get("timestamp", "")
    role = request.query.get("role", "")
    user_id = request.query.get("user_id", "")
    content = request.query.get("content", "")[:120]
    if timestamp or role or user_id or content:
        return f"fallback:{timestamp}:{role}:{user_id}:{content}"
    return ""


def _rewrite_jsonl_without_conversation_key(path: Path, group_id: str, key: str) -> int:
    if not path.exists():
        return 0

    deleted = 0
    lines: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    lines.append(raw_line)
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    lines.append(raw_line)
                    continue
                if not isinstance(entry, dict):
                    lines.append(raw_line)
                    continue
                entry_with_group = dict(entry)
                entry_with_group["group_id"] = entry_with_group.get("group_id") or group_id
                if _conversation_entry_key(entry_with_group) == key:
                    deleted += 1
                    continue
                lines.append(raw_line)
    except OSError:
        return 0

    if deleted <= 0:
        return 0

    tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return deleted


def _delete_runtime_basic_memory_message(paths: Any, key: str, group_id: str = "") -> int:
    state_path = paths.engine_state / "basic_memory.json"
    if not state_path.exists():
        return 0

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return 0

    if not isinstance(raw, dict):
        return 0

    deleted = 0
    changed = False
    for gid, entries in list(raw.items()):
        gid_text = str(gid)
        if group_id and gid_text != group_id:
            continue
        if not isinstance(entries, list):
            continue
        kept: list[Any] = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            item = dict(entry)
            item["group_id"] = item.get("group_id") or gid_text
            if _conversation_entry_key(item) == key:
                deleted += 1
                changed = True
                continue
            kept.append(entry)
        raw[gid] = kept

    if changed:
        _atomic_write_json(state_path, raw)
    return deleted


async def api_tokens_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return token usage for the current persona."""
    from sirius_pulse.token import analytics as token_analytics
    from sirius_pulse.token.token_store import TokenUsageStore

    db_path = data_dir / "persona.db"
    total_summary = {
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
    }

    if db_path.exists():
        try:
            store = TokenUsageStore(str(db_path), read_only=True)
            baseline = token_analytics.compute_baseline(store)
            total_summary["total_calls"] = baseline.get("total_calls", 0)
            total_summary["total_prompt_tokens"] = baseline.get("total_prompt_tokens", 0)
            total_summary["total_completion_tokens"] = baseline.get("total_completion_tokens", 0)
            total_summary["total_tokens"] = baseline.get("total_tokens", 0)
        except Exception as exc:
            LOG.warning("读取 Token 统计失败: %s", exc)

    response_avg: dict[str, Any] = {
        "total_calls": 0,
        "avg_total_tokens": 0,
        "avg_prompt_tokens": 0,
        "avg_completion_tokens": 0,
    }
    if total_summary["total_calls"]:
        response_avg = {
            "total_calls": total_summary["total_calls"],
            "avg_total_tokens": round(
                total_summary["total_tokens"] / total_summary["total_calls"], 1
            ),
            "avg_prompt_tokens": round(
                total_summary["total_prompt_tokens"] / total_summary["total_calls"], 1
            ),
            "avg_completion_tokens": round(
                total_summary["total_completion_tokens"] / total_summary["total_calls"], 1
            ),
        }

    return _json_response(
        {
            "summary": total_summary,
            "response_avg": response_avg,
        }
    )


async def api_telemetry_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return skill usage telemetry for the current persona."""
    all_summaries: dict[str, dict[str, Any]] = {}
    total_calls = 0

    telemetry_path = data_dir / "skill_data" / ".telemetry.jsonl"
    if telemetry_path.exists():
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
            LOG.warning("读取 Telemetry 失败: %s", exc)

    skills: dict[str, Any] = {}
    for skill_name, stats in all_summaries.items():
        calls = stats["calls"]
        skills[skill_name] = {
            "calls": calls,
            "success_rate": round(stats["successes"] / calls * 100, 1) if calls else 0,
            "avg_ms": round(stats["total_ms"] / calls, 1) if calls else 0,
        }

    return _json_response(
        {
            "total_calls": total_calls,
            "skills": skills,
        }
    )


@handle_api_errors
async def api_persona_tokens_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    from sirius_pulse.token import analytics as token_analytics
    from sirius_pulse.token.token_store import TokenUsageStore

    db_path = paths.dir / "persona.db"
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

    store = TokenUsageStore(str(db_path), read_only=True)
    baseline = token_analytics.compute_baseline(store, start_ts=start_ts, end_ts=end_ts)
    by_model = token_analytics.group_by_model(store, start_ts=start_ts, end_ts=end_ts)
    time_series = token_analytics.time_series(
        store, bucket_seconds=3600, start_ts=start_ts, end_ts=end_ts
    )

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
            "avg_completion_tokens": round(
                summary["total_completion_tokens"] / summary["total_calls"], 1
            ),
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
        hourly.append(
            {
                "hour_ts": hour_ts,
                "hour": dt.hour,
                "calls": ts.get("calls", 0),
                "prompt_tokens": ts.get("prompt_tokens", 0),
                "completion_tokens": ts.get("completion_tokens", 0),
                "total_tokens": ts.get("total_tokens", 0),
            }
        )

    # hourly_distribution: 按小时聚合的调用分布
    hourly_distribution: dict[int, int] = {}
    for h in hourly:
        hour = h["hour"]
        hourly_distribution[hour] = hourly_distribution.get(hour, 0) + h["calls"]
    hourly_distribution_list = [
        {"hour": h, "calls": c} for h, c in sorted(hourly_distribution.items())
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
    section_breakdown_by_task = store.get_section_breakdown_by_task(
        start_ts=start_ts, end_ts=end_ts
    )
    recent_with_breakdown = store.get_recent_records_with_breakdown(
        limit=100, start_ts=start_ts, end_ts=end_ts
    )

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

    return _json_response(
        {
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
        }
    )


@handle_api_errors
async def api_persona_cognition_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    db_path = paths.dir / "persona.db"
    if not db_path.exists():
        return _json_response({"events": [], "emotion_distribution": {}})

    from sirius_pulse.memory.cognition_store import CognitionEventStore

    store = CognitionEventStore(str(db_path), read_only=True)
    limit = int(request.query.get("limit", "50"))
    events = store.get_recent(limit=limit)
    group_id = request.query.get("group_id", None)
    emotion_distribution = store.get_emotion_distribution(group_id=group_id if group_id else None)
    store.close()
    return _json_response({"events": events, "emotion_distribution": emotion_distribution})


@handle_api_errors
async def api_persona_cognition_analysis_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return rich cognition analysis: intent/user/hourly/score distributions + decision stats."""
    paths = PersonaConfigPaths(data_dir)

    db_path = paths.dir / "persona.db"
    if not db_path.exists():
        return _json_response({"has_data": False})

    from sirius_pulse.memory.cognition_store import CognitionEventStore

    store = CognitionEventStore(str(db_path), read_only=True)
    group_id = request.query.get("group_id", None) or None

    result: dict[str, Any] = {"has_data": True}

    # 认知事件聚合
    result["intent_distribution"] = store.get_intent_distribution(group_id=group_id)
    result["user_stats"] = store.get_user_stats(group_id=group_id)
    result["group_summary"] = store.get_group_summary()
    result["hourly_distribution"] = store.get_hourly_distribution(group_id=group_id)

    # 分数分布（只取直方图统计，不传原始数组）
    raw_scores = store.get_score_distributions(group_id=group_id)
    result["score_histograms"] = {
        key: _build_histogram(values, bins=10, range_min=0.0, range_max=1.0)
        for key, values in raw_scores.items()
    }

    # 决策事件聚合
    result["strategy_distribution"] = store.get_strategy_distribution(group_id=group_id)
    result["decision_summary"] = store.get_decision_summary(group_id=group_id)
    result["decision_timeline"] = store.get_decision_timeline(group_id=group_id, limit=50)

    store.close()
    return _json_response(result)


def _build_histogram(
    values: list[float], bins: int = 10, range_min: float = 0.0, range_max: float = 1.0
) -> dict[str, Any]:
    """Build a histogram from raw values for frontend rendering."""
    if not values:
        return {"labels": [], "counts": [], "total": 0}
    step = (range_max - range_min) / bins
    labels: list[str] = []
    counts: list[int] = [0] * bins
    for i in range(bins):
        lo = range_min + i * step
        hi = lo + step
        labels.append(f"{lo:.1f}-{hi:.1f}")
    for v in values:
        idx = min(int((v - range_min) / step), bins - 1)
        if 0 <= idx < bins:
            counts[idx] += 1
    return {"labels": labels, "counts": counts, "total": len(values)}


@handle_api_errors
async def api_persona_diary_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    diary_dir = paths.dir / "diary"
    if not diary_dir.exists():
        return _json_response({"entries": [], "stats": {}, "groups": [], "total": 0})

    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)
    group_id = request.query.get("group_id", "")
    search = request.query.get("search", "").strip().lower()
    keyword = request.query.get("keyword", "").strip()

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
                if not isinstance(item, dict):
                    continue
                # 关键词筛选
                if keyword and keyword not in item.get("keywords", []):
                    continue
                # 全文搜索（匹配内容和摘要）
                if search:
                    content = (item.get("content", "") + item.get("summary", "")).lower()
                    if search not in content:
                        continue
                entries.append(item)
                for kw in item.get("keywords", []):
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
        except (OSError, json.JSONDecodeError):
            continue

    total = len(entries)
    entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    # 从末尾分页：offset=0 → 最新一页
    end = total - offset
    start = max(0, end - limit)
    entries = entries[start:end] if end > 0 else []

    stats = {
        "total": total,
        "groups": len(groups),
        "top_keywords": sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)[:20],
    }

    return _json_response(
        {
            "entries": entries,
            "stats": stats,
            "groups": sorted(groups),
            "total": total,
        }
    )


@handle_api_errors
async def api_persona_diary_post(request: web.Request, data_dir: Path) -> web.Response:
    """Create a diary memory entry."""
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    group_id = str(body.get("group_id") or body.get("group") or "default").strip() or "default"
    content = str(body.get("content") or "").strip()
    summary = str(body.get("summary") or "").strip()
    if not content and not summary:
        return _json_response({"error": "日记内容不能为空"}, 400)

    raw_keywords = body.get("keywords", [])
    if isinstance(raw_keywords, str):
        keywords = [kw.strip() for kw in raw_keywords.replace("，", ",").split(",") if kw.strip()]
    elif isinstance(raw_keywords, list):
        keywords = [str(kw).strip() for kw in raw_keywords if str(kw).strip()]
    else:
        keywords = []

    entry = {
        "entry_id": str(body.get("entry_id") or f"diary_{uuid4().hex}"),
        "group_id": group_id,
        "created_at": str(body.get("created_at") or _now_iso()),
        "source_ids": body.get("source_ids") if isinstance(body.get("source_ids"), list) else [],
        "content": content,
        "keywords": keywords,
        "summary": summary or content[:80],
        "embedding": body.get("embedding") if isinstance(body.get("embedding"), list) else None,
        "merge_count": int(body.get("merge_count") or 0),
        "source_diary_ids": (
            body.get("source_diary_ids") if isinstance(body.get("source_diary_ids"), list) else []
        ),
    }

    paths = PersonaConfigPaths(data_dir)
    path = _diary_file(paths, group_id)
    payload = _load_diary_payload(path, group_id)
    payload["group_id"] = group_id
    payload.setdefault("entries", []).append(entry)
    _atomic_write_json(path, payload)
    return _json_response({"success": True, "entry": entry}, 201)


@handle_api_errors
async def api_persona_diary_put(request: web.Request, data_dir: Path) -> web.Response:
    """Update a diary memory entry."""
    entry_id = str(request.match_info.get("entry_id", "")).strip()
    if not entry_id:
        return _json_response({"error": "缺少日记 ID"}, 400)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    paths = PersonaConfigPaths(data_dir)
    found = _find_diary_entry(paths, entry_id)
    if found is None:
        return _json_response({"error": "日记不存在"}, 404)
    path, payload, entry, idx = found

    old_group_id = str(entry.get("group_id") or payload.get("group_id") or "default")
    new_group_id = str(body.get("group_id") or body.get("group") or old_group_id).strip() or "default"
    for key in ("content", "summary", "created_at"):
        if key in body:
            entry[key] = str(body.get(key) or "")
    if "keywords" in body:
        raw_keywords = body.get("keywords", [])
        if isinstance(raw_keywords, str):
            entry["keywords"] = [
                kw.strip() for kw in raw_keywords.replace("，", ",").split(",") if kw.strip()
            ]
        elif isinstance(raw_keywords, list):
            entry["keywords"] = [str(kw).strip() for kw in raw_keywords if str(kw).strip()]
    if "source_ids" in body and isinstance(body.get("source_ids"), list):
        entry["source_ids"] = body["source_ids"]
    if "source_diary_ids" in body and isinstance(body.get("source_diary_ids"), list):
        entry["source_diary_ids"] = body["source_diary_ids"]
    if "merge_count" in body:
        entry["merge_count"] = int(body.get("merge_count") or 0)
    entry["group_id"] = new_group_id

    if new_group_id != old_group_id:
        payload["entries"].pop(idx)
        _atomic_write_json(path, payload)
        target_path = _diary_file(paths, new_group_id)
        target_payload = _load_diary_payload(target_path, new_group_id)
        target_payload["group_id"] = new_group_id
        target_payload.setdefault("entries", []).append(entry)
        _atomic_write_json(target_path, target_payload)
    else:
        payload["entries"][idx] = entry
        _atomic_write_json(path, payload)

    return _json_response({"success": True, "entry": entry})


@handle_api_errors
async def api_persona_diary_delete(request: web.Request, data_dir: Path) -> web.Response:
    """Delete a diary memory entry."""
    entry_id = str(request.match_info.get("entry_id", "")).strip()
    if not entry_id:
        return _json_response({"error": "缺少日记 ID"}, 400)

    paths = PersonaConfigPaths(data_dir)
    found = _find_diary_entry(paths, entry_id)
    if found is None:
        return _json_response({"error": "日记不存在"}, 404)
    path, payload, _entry, idx = found
    payload["entries"].pop(idx)
    _atomic_write_json(path, payload)
    return _json_response({"success": True})


async def api_persona_vector_store_status_get(request: web.Request, data_dir: Path) -> web.Response:
    paths = PersonaConfigPaths(data_dir)

    from sirius_pulse.memory.diary.vector_store import DiaryVectorStore

    vector_db_dir = paths.dir / "diary" / "vector_db"
    try:
        vs = DiaryVectorStore(vector_db_dir)
        stats = vs.get_stats()
        return _json_response(stats)
    except Exception as exc:
        LOG.warning("读取向量存储状态失败: %s", exc)
        return _json_response(
            {
                "available": False,
                "total_entries": 0,
                "groups": [],
                "model": DiaryVectorStore.MODEL_NAME,
                "error": str(exc),
            }
        )


@handle_api_errors
async def api_persona_users_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return user semantic profiles for the current persona (paginated)."""
    from sirius_pulse.memory.semantic.store import SemanticProfileStore

    paths = PersonaConfigPaths(data_dir)

    semantic_base = paths.dir / "memory" / "semantic"
    if not semantic_base.exists():
        return _json_response({"users": [], "groups": [], "total": 0})

    group_id = request.query.get("group_id", "")
    search = request.query.get("search", "").strip().lower()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)
    store = SemanticProfileStore(paths.dir)

    users: list[dict[str, Any]] = []
    groups: set[str] = set()
    users_dir = semantic_base / "users"
    if users_dir.exists():
        for g_dir in users_dir.iterdir():
            if g_dir.is_dir():
                groups.add(g_dir.name)

    if group_id:
        for profile in store.list_group_user_profiles(group_id):
            if profile.user_id:
                item = profile.to_dict()
                item["group_id"] = group_id
                users.append(item)
    else:
        for g in groups:
            for profile in store.list_group_user_profiles(g):
                if profile.user_id:
                    item = profile.to_dict()
                    item["group_id"] = g
                    users.append(item)

    # 后端搜索：按名称或 user_id 模糊匹配
    if search:
        users = [
            u
            for u in users
            if search in (u.get("name", "") or "").lower()
            or search in (u.get("user_id", "") or "").lower()
        ]

    total = len(users)
    users.sort(key=lambda u: u.get("last_interaction_at", ""), reverse=True)
    # 从末尾分页
    end = total - offset
    start = max(0, end - limit)
    users = users[start:end] if end > 0 else []

    return _json_response({"users": users, "groups": sorted(groups), "total": total})


@handle_api_errors
async def api_persona_user_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return a single user semantic profile for the current persona."""
    from sirius_pulse.memory.semantic.store import SemanticProfileStore

    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少用户ID"}, 400)

    paths = PersonaConfigPaths(data_dir)

    # SemanticProfileStore expects persona_dir and appends memory/semantic itself
    semantic_base = paths.dir / "memory" / "semantic"
    if not semantic_base.exists():
        return _json_response({"error": "用户不存在"}, 404)

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

    item = profile.to_dict()
    if group_id:
        item["group_id"] = group_id
    return _json_response({"user": item})


@handle_api_errors
async def api_persona_user_put(request: web.Request, data_dir: Path) -> web.Response:
    """Update a semantic user profile."""
    from sirius_pulse.memory.semantic.models import UserSemanticProfile
    from sirius_pulse.memory.semantic.store import SemanticProfileStore

    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少用户 ID"}, 400)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    group_id = str(body.get("group_id") or request.query.get("group_id", "")).strip()
    if not group_id:
        return _json_response({"error": "缺少 group_id"}, 400)

    paths = PersonaConfigPaths(data_dir)
    store = SemanticProfileStore(paths.dir)
    profile = store.load_user_profile(group_id, user_id) or UserSemanticProfile(
        user_id=user_id,
        name=str(body.get("name") or user_id),
    )

    if "name" in body:
        profile.name = str(body.get("name") or "")
    if "engagement_rate" in body:
        profile.engagement_rate = max(0.0, min(1.0, float(body.get("engagement_rate") or 0)))
    if "interaction_count" in body:
        profile.interaction_count = max(0, int(body.get("interaction_count") or 0))
    if "first_interaction_at" in body:
        profile.first_interaction_at = str(body.get("first_interaction_at") or "")
    if "last_interaction_at" in body:
        profile.last_interaction_at = str(body.get("last_interaction_at") or "")

    store.save_user_profile(group_id, user_id, profile)
    item = profile.to_dict()
    item["group_id"] = group_id
    return _json_response({"success": True, "user": item})


@handle_api_errors
async def api_persona_user_delete(request: web.Request, data_dir: Path) -> web.Response:
    """Delete a semantic user profile from one group or all groups."""
    user_id = str(request.match_info.get("user_id", "")).strip()
    if not user_id:
        return _json_response({"error": "缺少用户 ID"}, 400)

    paths = PersonaConfigPaths(data_dir)
    users_dir = paths.dir / "memory" / "semantic" / "users"
    if not users_dir.exists():
        return _json_response({"success": True, "deleted": 0})

    group_id = str(request.query.get("group_id", "")).strip()
    deleted = 0
    groups = [users_dir / _safe_memory_name(group_id)] if group_id else [
        path for path in users_dir.iterdir() if path.is_dir()
    ]
    safe_user_id = _safe_memory_name(user_id)
    for group_dir in groups:
        path = group_dir / f"{safe_user_id}.json"
        if path.exists():
            path.unlink()
            deleted += 1
    return _json_response({"success": True, "deleted": deleted})


@handle_api_errors
async def api_persona_glossary_get(request: web.Request, data_dir: Path) -> web.Response:
    """Return glossary terms for the current persona (paginated).

    Query params:
      - search: text search (optional)
      - limit: max terms per page (default 50)
      - offset: pagination offset (default 0)
    """
    from sirius_pulse.memory.glossary.manager import GlossaryManager

    paths = PersonaConfigPaths(data_dir)

    glossary_dir = paths.dir / "glossary"
    if not glossary_dir.exists():
        return _json_response({"terms": [], "stats": {}, "total": 0})

    search = request.query.get("search", "")
    group_filter = request.query.get("group", "").strip()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)

    manager = GlossaryManager(paths.dir, persona_name=data_dir.name)

    terms: list[dict[str, Any]] = []
    all_terms = manager._load()
    for term in all_terms.values():
        term_dict = term.to_dict()
        terms.append(term_dict)

    # 分组筛选
    if group_filter:
        terms = [t for t in terms if t.get("group", "") == group_filter]

    if search:
        search_lower = search.lower()
        terms = [
            t
            for t in terms
            if search_lower in t.get("term", "").lower()
            or search_lower in t.get("definition", "").lower()
        ]

    total = len(terms)
    terms.sort(key=lambda t: t.get("confidence", 0) * t.get("usage_count", 1), reverse=True)
    # 从末尾分页
    end = total - offset
    start = max(0, end - limit)
    terms = terms[start:end] if end > 0 else []

    stats = {"total": total}

    return _json_response({"terms": terms, "stats": stats, "total": total})


@handle_api_errors
async def api_persona_glossary_post(request: web.Request, data_dir: Path) -> web.Response:
    """Create a glossary term."""
    from sirius_pulse.memory.glossary.manager import GlossaryManager
    from sirius_pulse.memory.glossary.models import GlossaryTerm

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)
    term_text = str(body.get("term") or "").strip()
    if not term_text:
        return _json_response({"error": "术语不能为空"}, 400)

    manager = GlossaryManager(data_dir, persona_name=data_dir.name)
    existing = manager.get_term("", term_text)
    if existing is not None:
        return _json_response({"error": "术语已存在"}, 409)

    term = GlossaryTerm(
        term=term_text,
        definition=str(body.get("definition") or "").strip(),
        source=str(body.get("source") or "manual").strip() or "manual",
        confidence=float(body.get("confidence") if body.get("confidence") is not None else 0.8),
        usage_count=max(1, int(body.get("usage_count") or 1)),
        context_examples=(
            [str(v).strip() for v in body.get("context_examples", []) if str(v).strip()]
            if isinstance(body.get("context_examples"), list)
            else []
        ),
        related_terms=(
            [str(v).strip() for v in body.get("related_terms", []) if str(v).strip()]
            if isinstance(body.get("related_terms"), list)
            else []
        ),
        domain=str(body.get("domain") or "custom").strip() or "custom",
    )
    manager._load()[term.term.lower().strip()] = term
    manager._save()
    return _json_response({"success": True, "term": term.to_dict()}, 201)


@handle_api_errors
async def api_persona_glossary_put(request: web.Request, data_dir: Path) -> web.Response:
    """Update a glossary term."""
    from urllib.parse import unquote

    from sirius_pulse.memory.glossary.manager import GlossaryManager
    from sirius_pulse.memory.glossary.models import GlossaryTerm

    old_term = unquote(str(request.match_info.get("term", ""))).strip()
    if not old_term:
        return _json_response({"error": "缺少术语"}, 400)
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "Invalid JSON"}, 400)

    manager = GlossaryManager(data_dir, persona_name=data_dir.name)
    terms = manager._load()
    old_key = old_term.lower().strip()
    existing = terms.get(old_key)
    if existing is None:
        return _json_response({"error": "术语不存在"}, 404)

    data = existing.to_dict()
    for key in (
        "term",
        "definition",
        "source",
        "first_seen_at",
        "last_updated_at",
        "confidence",
        "usage_count",
        "context_examples",
        "related_terms",
        "domain",
    ):
        if key in body:
            data[key] = body[key]
    data["last_updated_at"] = str(body.get("last_updated_at") or _now_iso())
    updated = GlossaryTerm.from_dict(data)
    new_key = updated.term.lower().strip()
    if not new_key:
        return _json_response({"error": "术语不能为空"}, 400)
    if new_key != old_key and new_key in terms:
        return _json_response({"error": "目标术语已存在"}, 409)
    if new_key != old_key:
        terms.pop(old_key, None)
    terms[new_key] = updated
    manager._save()
    return _json_response({"success": True, "term": updated.to_dict()})


@handle_api_errors
async def api_persona_glossary_delete(request: web.Request, data_dir: Path) -> web.Response:
    """Delete a glossary term."""
    from urllib.parse import unquote

    from sirius_pulse.memory.glossary.manager import GlossaryManager

    term = unquote(str(request.match_info.get("term", ""))).strip()
    if not term:
        return _json_response({"error": "缺少术语"}, 400)

    manager = GlossaryManager(data_dir, persona_name=data_dir.name)
    terms = manager._load()
    removed = terms.pop(term.lower().strip(), None)
    if removed is None:
        return _json_response({"error": "术语不存在"}, 404)
    manager._save()
    return _json_response({"success": True})


@handle_api_errors
async def api_persona_memory_viz(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/memory-viz — 记忆可视化数据聚合接口。

    Query params:
        group_id     : 按群过滤（为空则全部）
        basic_limit  : 基础记忆条数上限（默认 500，最大 2000）
        diary_limit  : 日记条数上限（默认 200，最大 500）
    """
    group_filter = request.query.get("group_id", "").strip()
    limit_basic = min(int(request.query.get("basic_limit", "500")), 2000)
    limit_diary = min(int(request.query.get("diary_limit", "200")), 500)

    paths = PersonaConfigPaths(data_dir)

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

                            recent_entries.append(
                                {
                                    "group_id": gid,
                                    "speaker_name": data.get("speaker_name", ""),
                                    "role": role,
                                    "content": data.get("content", "")[:120],
                                    "timestamp": ts,
                                }
                            )
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
    # 群组 → 日记关键词集合（用于后续构建用户-话题二部图）
    group_keyword_map: dict[str, set[str]] = {}
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
                    diary_entries.append(
                        {
                            "entry_id": item.get("entry_id", ""),
                            "group_id": g_id,
                            "created_at": item.get("created_at", ""),
                            "summary": item.get("summary", ""),
                            "content": item.get("content", "")[:300],
                            "keywords": item.get("keywords", []),
                            "embedding": emb,
                        }
                    )
                    for kw in item.get("keywords", []):
                        keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
                        if kw not in group_keyword_map.setdefault(g_id, set()):
                            group_keyword_map[g_id].add(kw)
            except (OSError, json.JSONDecodeError):
                continue
    diary_entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    diary_entries = diary_entries[:limit_diary]
    top_keywords = sorted(keyword_freq.items(), key=lambda x: x[1], reverse=True)[:20]

    # ── 3. 用户-话题二部图 ──
    # 数据来源：用户语义画像 → 群组归属；日记 keywords → 话题节点
    semantic_base = paths.dir / "memory" / "semantic"
    user_nodes: list[dict[str, Any]] = []
    topic_nodes: list[dict[str, str]] = []
    user_topic_links: list[dict[str, Any]] = []
    # 群组 → 用户 ID 集合（用于后续将用户关联到群组话题）
    group_users: dict[str, set[str]] = {}
    if semantic_base.exists():
        users_dir = semantic_base / "users"
        if users_dir.exists():
            seen: set[str] = set()
            for g_dir in users_dir.iterdir():
                if not g_dir.is_dir():
                    continue
                if group_filter and g_dir.name != group_filter:
                    continue
                gid = g_dir.name
                group_users[gid] = set()
                for u_file in g_dir.glob("*.json"):
                    try:
                        u_data = json.loads(u_file.read_text(encoding="utf-8"))
                        uid = u_data.get("user_id", "")
                        if not uid or uid in seen:
                            continue
                        seen.add(uid)
                        group_users[gid].add(uid)
                        engagement = u_data.get("engagement_rate", 0)
                        count = u_data.get("interaction_count", 0)
                        user_nodes.append(
                            {
                                "user_id": uid,
                                "name": u_data.get("name", uid),
                                "engagement_rate": engagement,
                                "interaction_count": count,
                            }
                        )
                    except (OSError, json.JSONDecodeError, TypeError):
                        continue

    # 话题节点：取出现在 ≥2 个群组或频率 ≥3 的日记关键词
    topic_freq: dict[str, int] = {}
    topic_group_count: dict[str, int] = {}
    for g_id, kws in group_keyword_map.items():
        for kw in kws:
            topic_freq[kw] = topic_freq.get(kw, 0) + 1
            topic_group_count[kw] = topic_group_count.get(kw, 0) + 1
    # 过滤低价值关键词，保留跨群出现或高频的话题
    valid_topics = {
        kw for kw in topic_group_count if topic_group_count[kw] >= 2 or keyword_freq.get(kw, 0) >= 3
    }
    for t in sorted(valid_topics):
        topic_nodes.append({"id": t, "name": t})

    # 构建用户-话题边：用户 ∈ 群组 → 群组日记含关键词 → (user, topic) 边
    link_set: set[tuple[str, str]] = set()
    for gid, keywords in group_keyword_map.items():
        users = group_users.get(gid, set())
        for uid in users:
            for kw in keywords:
                if kw in valid_topics:
                    link_set.add((uid, kw))
    for uid, kw in sorted(link_set):
        user_topic_links.append({"source": f"u_{uid}", "target": f"t_{kw}"})

    return _json_response(
        {
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
        }
    )


@handle_api_errors
async def api_persona_conversation_history_get(
    request: web.Request, data_dir: Path
) -> web.Response:
    """GET /api/persona/conversations — 返回对话历史（分页，支持搜索筛选）。"""
    group_id = request.query.get("group_id", "").strip()
    limit = min(int(request.query.get("limit", "50")), 200)
    offset = max(int(request.query.get("offset", "0")), 0)
    search = request.query.get("search", "").strip().lower()
    speaker = request.query.get("speaker", "").strip().lower()
    start_time = request.query.get("start", "").strip()
    end_time = request.query.get("end", "").strip()

    paths = PersonaConfigPaths(data_dir)

    archive_dir = paths.dir / "archive"

    # 获取所有群组
    groups = []
    if archive_dir.exists():
        for f in archive_dir.glob("*.jsonl"):
            groups.append(f.stem)

    runtime_messages = _load_runtime_basic_memory_messages(paths, group_id)
    runtime_groups = {
        str(message.get("group_id") or "")
        for message in runtime_messages
        if str(message.get("group_id") or "").strip()
    }
    groups = sorted(set(groups) | runtime_groups)

    target_files = []
    if archive_dir.exists():
        if group_id:
            target_file = archive_dir / f"{group_id}.jsonl"
            if target_file.exists():
                target_files.append(target_file)
        else:
            target_files = sorted(archive_dir.glob("*.jsonl"))

    if not target_files and not runtime_messages:
        return _json_response({"messages": [], "groups": groups, "total": 0})

    has_filters = bool(search or speaker or start_time or end_time)

    if has_filters:
        # 有筛选条件时：全量读取并过滤（无法利用倒序优化）
        all_messages: list[dict[str, Any]] = []
        for fpath in target_files:
            g_id = fpath.stem
            try:
                with fpath.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            entry["group_id"] = g_id
                            if not entry.get("tags"):
                                entry["tags"] = []
                            all_messages.append(entry)
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

        all_messages = _merge_conversation_messages(all_messages, runtime_messages)

        # 应用筛选
        all_messages = [
            m
            for m in all_messages
            if _conversation_message_matches_filters(
                m,
                search=search,
                speaker=speaker,
                start_time=start_time,
                end_time=end_time,
            )
        ]

        all_messages.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        total = len(all_messages)
        messages = all_messages[offset : offset + limit]
    else:
        # 无筛选条件时：使用倒序读取优化
        # 统计总行数
        total = 0
        for fpath in target_files:
            try:
                with fpath.open("rb") as f:
                    total += sum(1 for _ in f)
            except OSError:
                continue

        # 倒序读取
        need = offset + limit
        raw_lines: list[tuple[str, str]] = []
        for fpath in target_files:
            g_id = fpath.stem
            try:
                lines = _read_tail_lines(fpath, need)
                for line in lines:
                    raw_lines.append((g_id, line))
            except OSError:
                continue

        messages_raw: list[dict[str, Any]] = []
        for g_id, line in raw_lines:
            try:
                entry = json.loads(line)
                entry["group_id"] = g_id
                if not entry.get("tags"):
                    entry["tags"] = []
                messages_raw.append(entry)
            except json.JSONDecodeError:
                continue

        merged_messages = _merge_conversation_messages(messages_raw, runtime_messages)
        runtime_extra = max(0, len(merged_messages) - len(messages_raw))
        total += runtime_extra

        merged_messages.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        messages = merged_messages[offset : offset + limit]

    return _json_response(
        {
            "messages": messages,
            "groups": sorted(groups),
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


@handle_api_errors
async def api_persona_conversation_history_delete(
    request: web.Request, data_dir: Path
) -> web.Response:
    """DELETE /api/persona/conversations — delete one archived/runtime message."""
    group_id = request.query.get("group_id", "").strip()
    key = _conversation_key_from_query(request)
    if not key:
        raise web.HTTPBadRequest(text="missing conversation message identifier")

    paths = PersonaConfigPaths(data_dir)
    archive_dir = paths.dir / "archive"

    deleted_archive = 0
    if archive_dir.exists():
        target_files: list[Path]
        if group_id:
            target_files = [archive_dir / f"{_safe_memory_name(group_id)}.jsonl"]
        else:
            target_files = sorted(archive_dir.glob("*.jsonl"))
        for path in target_files:
            deleted_archive += _rewrite_jsonl_without_conversation_key(path, path.stem, key)

    deleted_runtime = _delete_runtime_basic_memory_message(paths, key, group_id)
    deleted = deleted_archive + deleted_runtime
    return _json_response(
        {
            "success": True,
            "deleted": deleted,
            "deleted_archive": deleted_archive,
            "deleted_runtime": deleted_runtime,
        }
    )
