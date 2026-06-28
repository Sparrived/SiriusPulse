"""管家端数据同步 HTTP API。

供助手端通过 HTTP 调用，实现运行时数据的远程读写。
管家端是数据的 single source of truth，助手端通过此 API 读写所有持久化数据。

端点：
    GET  /api/data/snapshot   — 加载完整运行时状态快照（助手启动时调用）
    POST /api/data/snapshot   — 保存完整运行时状态快照（助手关闭时调用）
    POST /api/data/messages   — 追加对话消息到归档（实时推送）
    POST /api/data/users      — 更新用户画像（实时推送）
    POST /api/data/glossary   — 更新术语（实时推送）
    POST /api/data/batch      — 批量写入（定期 flush：token、cognition、semantic 等）
"""

from __future__ import annotations

import functools
import json
import logging
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

LOG = logging.getLogger("sirius.data_sync")


def _json_response(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, indent=2)
    )


def handle_api_errors(func: Callable) -> Callable:
    """API 错误处理装饰器。"""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> web.Response:
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            LOG.warning("%s 失败: %s", func.__name__, exc)
            return _json_response({"error": str(exc)}, 500)

    return wrapper


# ======================================================================
# GET /api/data/snapshot — 加载完整运行时状态
# ======================================================================


@handle_api_errors
async def api_data_snapshot_get(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """加载完整运行时状态快照。

    返回助手端启动时需要的所有数据：persona 配置、运行时状态、
    用户数据、术语表等。助手端可用这些数据初始化本地缓存。
    """
    snapshot: dict[str, Any] = {}

    # 1. Persona 配置
    persona_path = data_dir / "persona.json"
    if persona_path.exists():
        snapshot["persona"] = json.loads(persona_path.read_text(encoding="utf-8"))

    # 2. Orchestration 配置（仅 task_params 相关字段，不含 model routing）
    orch_path = data_dir / "orchestration.json"
    if orch_path.exists():
        orch_data = json.loads(orch_path.read_text(encoding="utf-8"))
        snapshot["orchestration"] = orch_data
        # 提取 task_params 子集用于同步
        snapshot["task_params"] = {
            k: orch_data[k]
            for k in (
                "task_temperatures",
                "task_max_tokens",
                "task_timeout",
                "task_fallback_model",
                "_updated_at",
            )
            if k in orch_data
        }

    # 3. Experience 配置
    exp_path = data_dir / "experience.json"
    if exp_path.exists():
        snapshot["experience"] = json.loads(exp_path.read_text(encoding="utf-8"))

    # 4. EngineStateStore 运行时状态
    state_dir = data_dir / "engine_state"
    state_files = {
        "basic_memory": "basic_memory.json",
        "assistant_emotion": "assistant_emotion.json",
        "group_timestamps": "group_timestamps.json",
        "diary_state": "diary_state.json",
        "user_manager": "user_manager.json",
    }
    for key, filename in state_files.items():
        path = state_dir / filename
        if path.exists():
            try:
                snapshot[key] = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                snapshot[key] = None

    # 各群工作记忆快照
    groups_dir = state_dir / "groups"
    working_memories: dict[str, Any] = {}
    if groups_dir.exists():
        for p in groups_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                gid = data.get("group_id")
                if gid:
                    working_memories[gid] = data.get("entries", [])
            except (json.JSONDecodeError, OSError):
                continue
    snapshot["working_memories"] = working_memories

    # 5. 术语表
    glossary_path = data_dir / "glossary" / "terms.json"
    if glossary_path.exists():
        try:
            snapshot["glossary"] = json.loads(glossary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            snapshot["glossary"] = {}

    # 6. 用户数据（从 SQLite 读取）
    snapshot["users"] = _load_users_from_db(data_dir)

    # 7. 归档消息（各群最近的消息）
    snapshot["archives"] = _load_recent_archives(data_dir, max_per_group=100)

    return _json_response({"snapshot": snapshot})


def _load_users_from_db(data_dir: Path) -> list[dict[str, Any]]:
    """从 persona.db 加载所有用户数据。"""
    import sqlite3

    db_path = data_dir / "persona.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM users").fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception:
        LOG.debug("从 persona.db 加载用户数据失败", exc_info=True)
        return []


def _load_recent_archives(data_dir: Path, max_per_group: int = 100) -> dict[str, list[dict]]:
    """加载各群最近的归档消息。"""
    archive_dir = data_dir / "archive"
    if not archive_dir.exists():
        return {}
    result: dict[str, list[dict]] = {}
    for path in archive_dir.glob("*.jsonl"):
        group_id = path.stem
        entries: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            # 只保留最近 N 条
            result[group_id] = entries[-max_per_group:]
        except OSError:
            continue
    return result


# ======================================================================
# POST /api/data/snapshot — 保存完整运行时状态
# ======================================================================


@handle_api_errors
async def api_data_snapshot_post(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """保存完整运行时状态快照。

    助手端关闭时调用，将引擎运行时状态推送到管家端持久化。
    """
    body = await request.json()
    state = body.get("state", {})

    state_dir = data_dir / "engine_state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # 保存各子状态
    _write_json(state_dir / "basic_memory.json", state.get("basic_memory"))
    _write_json(state_dir / "assistant_emotion.json", state.get("assistant_emotion"))
    _write_json(state_dir / "group_timestamps.json", state.get("group_timestamps"))
    _write_json(state_dir / "diary_state.json", state.get("diary_state"))
    _write_json(state_dir / "user_manager.json", state.get("user_manager"))

    # 保存各群工作记忆
    working_memories = state.get("working_memories", {})
    groups_dir = state_dir / "groups"
    groups_dir.mkdir(parents=True, exist_ok=True)
    for group_id, entries in working_memories.items():
        _write_json(groups_dir / f"{_safe_name(group_id)}.json", {
            "group_id": group_id,
            "entries": entries,
        })

    # 保存 persona
    persona = state.get("persona")
    if persona:
        _write_json(data_dir / "persona.json", persona)

    # 保存术语表
    glossary = state.get("glossary")
    if glossary is not None:
        glossary_dir = data_dir / "glossary"
        glossary_dir.mkdir(parents=True, exist_ok=True)
        _write_json(glossary_dir / "terms.json", glossary)

    # 保存归档消息
    archives = state.get("archives", {})
    if archives:
        archive_dir = data_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for group_id, entries in archives.items():
            path = archive_dir / f"{_safe_name(group_id)}.jsonl"
            lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
            path.write_text(lines, encoding="utf-8")

    LOG.info("运行时状态快照已保存: %s", data_dir.name)
    return _json_response({"success": True})


# ======================================================================
# POST /api/data/messages — 追加对话消息
# ======================================================================


@handle_api_errors
async def api_data_messages_post(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """追加对话消息到归档文件。

    助手端每处理一条消息后实时调用，确保对话历史不丢失。
    """
    body = await request.json()
    messages = body.get("messages", [])

    if not messages:
        return _json_response({"success": True, "count": 0})

    archive_dir = data_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 按 group_id 分组追加
    by_group: dict[str, list[dict]] = {}
    for msg in messages:
        gid = msg.get("group_id", "default")
        by_group.setdefault(gid, []).append(msg)

    count = 0
    for group_id, entries in by_group.items():
        path = archive_dir / f"{_safe_name(group_id)}.jsonl"
        lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
        # 追加模式
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(existing + lines, encoding="utf-8")
        count += len(entries)

    LOG.debug("已追加 %d 条消息到归档", count)
    return _json_response({"success": True, "count": count})


# ======================================================================
# POST /api/data/users — 更新用户画像
# ======================================================================


@handle_api_errors
async def api_data_users_post(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """更新用户画像数据。

    助手端在用户数据变更时实时调用。
    """
    body = await request.json()
    users = body.get("users", [])

    if not users:
        return _json_response({"success": True, "count": 0})

    import sqlite3

    db_path = data_dir / "persona.db"
    if not db_path.exists():
        return _json_response({"error": "persona.db 不存在"}, 404)

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    count = 0

    try:
        for user in users:
            user_id = user.get("user_id")
            if not user_id:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO users
                   (user_id, name, persona, identities, traits,
                    group_memberships, metadata, identity_anchors, relationships,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    user.get("name", ""),
                    user.get("persona", ""),
                    user.get("identities", "{}"),
                    user.get("traits", "[]"),
                    user.get("group_memberships", "{}"),
                    user.get("metadata", "{}"),
                    user.get("identity_anchors", "[]"),
                    user.get("relationships", "[]"),
                    user.get("created_at", ""),
                    user.get("updated_at", ""),
                ),
            )
            count += 1
        conn.commit()
    finally:
        conn.close()

    LOG.debug("已更新 %d 个用户画像", count)
    return _json_response({"success": True, "count": count})


# ======================================================================
# POST /api/data/glossary — 更新术语
# ======================================================================


@handle_api_errors
async def api_data_glossary_post(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """更新术语表数据。

    助手端在 learn_term 技能触发时调用。
    """
    body = await request.json()
    terms = body.get("terms")

    if terms is None:
        return _json_response({"success": True})

    glossary_dir = data_dir / "glossary"
    glossary_dir.mkdir(parents=True, exist_ok=True)

    # 合并策略：读取现有术语，合并新术语（覆盖同名）
    terms_path = glossary_dir / "terms.json"
    existing: dict[str, Any] = {}
    if terms_path.exists():
        try:
            existing = json.loads(terms_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    if isinstance(terms, dict):
        existing.update(terms)
    elif isinstance(terms, list):
        for term in terms:
            key = term.get("term", "")
            if key:
                existing[key] = term

    _write_json(terms_path, existing)
    LOG.debug("术语表已更新，共 %d 条", len(existing))
    return _json_response({"success": True, "count": len(existing)})


# ======================================================================
# POST /api/data/batch — 批量写入
# ======================================================================


@handle_api_errors
async def api_data_batch_post(
    request: web.Request,
    data_dir: Path,
) -> web.Response:
    """批量写入多种运行时数据。

    助手端定期（30s）调用，一次性推送累积的非关键数据。
    body 格式：
    {
        "operations": [
            {"type": "token_usage", "data": {...}},
            {"type": "cognition_event", "data": {...}},
            {"type": "semantic_profile", "data": {...}},
            {"type": "working_memory", "data": {"group_id": "...", "entries": [...]}},
            {"type": "timestamps", "data": {...}},
            {"type": "assistant_emotion", "data": {...}},
            ...
        ]
    }
    """
    body = await request.json()
    operations = body.get("operations", [])

    if not operations:
        return _json_response({"success": True, "count": 0})

    results: dict[str, int] = {}

    for op in operations:
        op_type = op.get("type", "")
        data = op.get("data", {})

        try:
            if op_type == "token_usage":
                _write_token_usage(data_dir, data)
            elif op_type == "cognition_event":
                _write_cognition_event(data_dir, data)
            elif op_type == "working_memory":
                _write_working_memory(data_dir, data)
            elif op_type == "timestamps":
                _write_timestamps(data_dir, data)
            elif op_type == "assistant_emotion":
                _write_json(data_dir / "engine_state" / "assistant_emotion.json", data)
            elif op_type == "diary_state":
                _write_json(data_dir / "engine_state" / "diary_state.json", data)
            elif op_type == "user_manager":
                _write_json(data_dir / "engine_state" / "user_manager.json", data)
            elif op_type == "basic_memory":
                _write_json(data_dir / "engine_state" / "basic_memory.json", data)
            elif op_type == "semantic_profile":
                _write_semantic_profile(data_dir, data)
            elif op_type.startswith("config_"):
                _write_config_update(data_dir, op_type, data)
            else:
                LOG.warning("未知的批量操作类型: %s", op_type)
                continue
            results[op_type] = results.get(op_type, 0) + 1
        except Exception as exc:
            LOG.warning("批量操作失败 [%s]: %s", op_type, exc)

    total = sum(results.values())
    LOG.debug("批量写入完成: %d 条操作, 分布: %s", total, results)
    return _json_response({"success": True, "count": total, "results": results})


# ======================================================================
# 内部辅助函数
# ======================================================================


def _write_json(path: Path, data: Any) -> None:
    """原子写入 JSON 文件。"""
    if data is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _safe_name(name: str) -> str:
    """将字符串转换为安全的文件名。"""
    import re

    base = re.sub(r"[^a-zA-Z0-9_\-一-鿿]+", "_", name.strip())
    base = re.sub(r"_+", "_", base).strip("_")
    return base or "default"


def _write_token_usage(data_dir: Path, data: dict) -> None:
    """写入 token 使用记录到 SQLite。"""
    import sqlite3

    db_path = data_dir / "persona.db"
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        # 确保表存在
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT DEFAULT '',
                persona TEXT DEFAULT '',
                model TEXT DEFAULT '',
                task_name TEXT DEFAULT '',
                provider TEXT DEFAULT '',
                group_id TEXT DEFAULT '',
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                input_chars INTEGER DEFAULT 0,
                output_chars INTEGER DEFAULT 0,
                estimation_method TEXT DEFAULT '',
                retries_used INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                error_info TEXT DEFAULT '',
                breakdown TEXT DEFAULT '{}',
                conversation_depth INTEGER DEFAULT 0,
                created_at TEXT DEFAULT ''
            )
        """)
        conn.execute(
            """INSERT INTO token_usage
               (session_id, persona, model, task_name, provider, group_id,
                prompt_tokens, completion_tokens, total_tokens,
                input_chars, output_chars, estimation_method, retries_used,
                duration_ms, error_info, breakdown, conversation_depth, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("session_id", ""),
                data.get("persona", ""),
                data.get("model", ""),
                data.get("task_name", ""),
                data.get("provider", ""),
                data.get("group_id", ""),
                data.get("prompt_tokens", 0),
                data.get("completion_tokens", 0),
                data.get("total_tokens", 0),
                data.get("input_chars", 0),
                data.get("output_chars", 0),
                data.get("estimation_method", ""),
                data.get("retries_used", 0),
                data.get("duration_ms", 0),
                data.get("error_info", ""),
                json.dumps(data.get("breakdown", {}), ensure_ascii=False),
                data.get("conversation_depth", 0),
                data.get("created_at", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_cognition_event(data_dir: Path, data: dict) -> None:
    """写入认知事件到 SQLite。"""
    import sqlite3

    db_path = data_dir / "persona.db"
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cognition_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT DEFAULT '',
                user_id TEXT DEFAULT '',
                valence REAL DEFAULT 0,
                arousal REAL DEFAULT 0,
                basic_emotion TEXT DEFAULT '',
                intensity REAL DEFAULT 0,
                social_intent TEXT DEFAULT '',
                urgency REAL DEFAULT 0,
                relevance REAL DEFAULT 0,
                confidence REAL DEFAULT 0,
                directed_score REAL DEFAULT 0,
                sarcasm REAL DEFAULT 0,
                entitlement REAL DEFAULT 0,
                turn_gap_readiness REAL DEFAULT 0,
                directed_signals TEXT DEFAULT '{}',
                created_at TEXT DEFAULT ''
            )
        """)
        conn.execute(
            """INSERT INTO cognition_events
               (group_id, user_id, valence, arousal, basic_emotion, intensity,
                social_intent, urgency, relevance, confidence, directed_score,
                sarcasm, entitlement, turn_gap_readiness, directed_signals, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get("group_id", ""),
                data.get("user_id", ""),
                data.get("valence", 0),
                data.get("arousal", 0),
                data.get("basic_emotion", ""),
                data.get("intensity", 0),
                data.get("social_intent", ""),
                data.get("urgency", 0),
                data.get("relevance", 0),
                data.get("confidence", 0),
                data.get("directed_score", 0),
                data.get("sarcasm", 0),
                data.get("entitlement", 0),
                data.get("turn_gap_readiness", 0),
                json.dumps(data.get("directed_signals", {}), ensure_ascii=False),
                data.get("created_at", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_working_memory(data_dir: Path, data: dict) -> None:
    """写入工作记忆快照。"""
    group_id = data.get("group_id", "")
    entries = data.get("entries", [])
    if not group_id:
        return
    state_dir = data_dir / "engine_state" / "groups"
    state_dir.mkdir(parents=True, exist_ok=True)
    _write_json(state_dir / f"{_safe_name(group_id)}.json", {
        "group_id": group_id,
        "entries": entries,
    })


def _write_timestamps(data_dir: Path, data: dict) -> None:
    """更新时间戳（合并而非覆盖）。"""
    path = data_dir / "engine_state" / "group_timestamps.json"
    existing: dict[str, str] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(data)
    _write_json(path, existing)


def _write_semantic_profile(data_dir: Path, data: dict) -> None:
    """写入语义画像到 SQLite。"""
    import sqlite3

    db_path = data_dir / "persona.db"
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS semantic_profiles (
                group_id TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                engagement_rate REAL DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_interaction_at TEXT DEFAULT '',
                last_interaction_at TEXT DEFAULT '',
                PRIMARY KEY (group_id, user_id)
            )
        """)
        conn.execute(
            """INSERT OR REPLACE INTO semantic_profiles
               (group_id, user_id, engagement_rate, interaction_count,
                first_interaction_at, last_interaction_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                data.get("group_id", ""),
                data.get("user_id", ""),
                data.get("engagement_rate", 0),
                data.get("interaction_count", 0),
                data.get("first_interaction_at", ""),
                data.get("last_interaction_at", ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _write_config_update(data_dir: Path, op_type: str, data: dict) -> None:
    """处理助手端推送的配置更新（experience 或 task_params）。

    只接受比管家端更新的配置（基于 _updated_at 时间戳）。
    """
    from datetime import datetime

    def _parse_ts(ts_str: str) -> datetime:
        if not ts_str:
            return datetime.min
        try:
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return datetime.min

    if op_type == "config_experience":
        exp_path = data_dir / "experience.json"
        local_exp: dict = {}
        if exp_path.exists():
            try:
                local_exp = json.loads(exp_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if _parse_ts(data.get("_updated_at", "")) > _parse_ts(local_exp.get("_updated_at", "")):
            _write_json(exp_path, data)
            LOG.info("Experience 配置已从助手端更新")

    elif op_type == "config_task_params":
        orch_path = data_dir / "engine_state" / "orchestration.json"
        local_orch: dict = {}
        if orch_path.exists():
            try:
                local_orch = json.loads(orch_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if _parse_ts(data.get("_updated_at", "")) > _parse_ts(local_orch.get("_updated_at", "")):
            for key in (
                "task_temperatures",
                "task_max_tokens",
                "task_timeout",
                "task_fallback_model",
                "_updated_at",
            ):
                if key in data:
                    local_orch[key] = data[key]
            _write_json(orch_path, local_orch)
            LOG.info("TaskParams 已从助手端更新")
