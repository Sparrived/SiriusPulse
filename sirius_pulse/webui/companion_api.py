"""伙伴视角接口 — 新版 WebUI「月白的小屋」所需的聚合数据。

- ``GET /api/persona/glance``：她此刻的心情与最近一段时间的动静（听到几句、回了几句、
  在哪些群、新记住了什么、主动做了什么），首页一次请求即可画出来。
- ``GET/POST/DELETE /api/personas/{name}/avatar``：每个人格自己的立绘。
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.webui.server_utils import _json_response, handle_api_errors

LOG = logging.getLogger("sirius.webui.companion")

AVATAR_FILENAME = "avatar.png"
AVATAR_MAX_BYTES = 8 * 1024 * 1024
AVATAR_MAX_SIDE = 2048
GLANCE_WINDOW_HOURS = 24
GLANCE_TAIL_LINES = 4000
GLANCE_RECENT_LIMIT = 8


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _mood(persona_dir: Path) -> dict[str, Any] | None:
    raw = _read_json(persona_dir / "engine_state" / "assistant_emotion.json")
    if not isinstance(raw, dict):
        return None
    state = EmotionState(
        valence=float(raw.get("valence", 0.2)),
        arousal=float(raw.get("arousal", 0.3)),
    )
    emotion = state.basic_emotion
    return {
        "valence": state.valence,
        "arousal": state.arousal,
        "baseline_valence": float(raw.get("baseline_valence", 0.2)),
        "baseline_arousal": float(raw.get("baseline_arousal", 0.3)),
        "emotion": emotion.name if emotion else "NEUTRAL",
        "emotion_cn": emotion.name_cn if emotion else "中性",
        "updated_at": str(raw.get("last_updated_at") or ""),
    }


def _recent_conversation(persona_dir: Path, since: datetime) -> dict[str, Any]:
    from sirius_pulse.webui.memory_api import _iter_tail_lines

    heard = 0
    replied = 0
    groups: dict[str, int] = {}
    last_at: datetime | None = None
    recent: list[dict[str, Any]] = []
    archive_dir = persona_dir / "archive"
    for path in sorted(archive_dir.glob("*.jsonl")) if archive_dir.is_dir() else []:
        # 文件从新到旧读：窗口内的条目全数计入；即使最近很安静，也给每个群留几条
        # 「最后说过的话」，首页才不至于空白。
        file_recent = 0
        try:
            for line in _iter_tail_lines(path, GLANCE_TAIL_LINES):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(entry.get("timestamp"))
                if ts is None:
                    continue
                if last_at is None or ts > last_at:
                    last_at = ts
                if file_recent < GLANCE_RECENT_LIMIT and entry.get("content"):
                    file_recent += 1
                    recent.append(
                        {
                            "group_id": str(entry.get("group_id") or path.stem),
                            "speaker_name": str(entry.get("speaker_name") or ""),
                            "role": str(entry.get("role") or "human"),
                            "content": str(entry.get("content") or "")[:120],
                            "timestamp": ts.isoformat(),
                        }
                    )
                if ts < since:
                    if file_recent >= GLANCE_RECENT_LIMIT:
                        break
                    continue
                role = entry.get("role")
                if role == "human":
                    heard += 1
                elif role == "assistant":
                    replied += 1
                else:
                    continue
                gid = str(entry.get("group_id") or path.stem)
                groups[gid] = groups.get(gid, 0) + 1
        except OSError:
            continue
    recent.sort(key=lambda item: item["timestamp"], reverse=True)
    return {
        "heard": heard,
        "replied": replied,
        "groups": [
            {"group_id": gid, "messages": count}
            for gid, count in sorted(groups.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "last_activity_at": last_at.isoformat() if last_at else "",
        "recent": recent[:GLANCE_RECENT_LIMIT],
    }


def _memory_counts(persona_dir: Path, since: datetime) -> dict[str, int]:
    from sirius_pulse.webui.memory_api import _load_memory_units_file, _memory_units_dir

    total = 0
    new = 0
    units_dir = _memory_units_dir(persona_dir)
    for path in units_dir.glob("*.json") if units_dir.is_dir() else []:
        _, units = _load_memory_units_file(path)
        total += len(units)
        new += sum(1 for unit in units if (_parse_ts(unit.get("created_at")) or since) > since)
    return {"total": total, "new": new}


def _recent_episodes(persona_dir: Path, since: datetime) -> list[dict[str, Any]]:
    from sirius_pulse.webui.autonomy_api import _load_episodes

    episodes = []
    for item in _load_episodes(persona_dir):
        ts = _parse_ts(item.get("ended_at") or item.get("started_at"))
        if ts is None or ts < since:
            continue
        episodes.append(
            {
                "kind": str(item.get("kind") or ""),
                "outcome": str(item.get("outcome") or ""),
                "seed": str(item.get("seed") or "")[:160],
                "at": ts.isoformat(),
            }
        )
    episodes.sort(key=lambda item: item["at"], reverse=True)
    return episodes[:GLANCE_RECENT_LIMIT]


@handle_api_errors
async def api_persona_glance_get(request: web.Request, data_dir: Path) -> web.Response:
    """GET /api/persona/glance — 首页一瞥：心情 + 最近 24 小时的动静。"""
    since = datetime.now(timezone.utc) - timedelta(hours=GLANCE_WINDOW_HOURS)
    return _json_response(
        {
            "window_hours": GLANCE_WINDOW_HOURS,
            "mood": _mood(data_dir),
            "conversation": _recent_conversation(data_dir, since),
            "memory": _memory_counts(data_dir, since),
            "episodes": _recent_episodes(data_dir, since),
        }
    )


def _avatar_persona_dir(request: web.Request, data_dir: Path) -> Path | None:
    name = str(request.match_info.get("name", "")).strip()
    if not name:
        return None
    personas_dir = (data_dir / "personas").resolve()
    target = (personas_dir / name).resolve()
    if target.parent != personas_dir or not (target / "persona.json").is_file():
        return None
    return target


def _default_avatar() -> Path | None:
    webui_dir = Path(__file__).parent
    for candidate in (webui_dir / "app" / "yuebai.webp", webui_dir / "static" / "yuebai.png"):
        if candidate.is_file():
            return candidate
    return None


@handle_api_errors
async def api_avatar_get(request: web.Request, data_dir: Path) -> web.StreamResponse:
    """GET /api/personas/{name}/avatar — 没有自定义立绘时返回内置的月白。

    ``X-Avatar-Custom`` 告诉前端这是不是她自己的图（镜子里据此显示「恢复默认」）。
    """
    persona_dir = _avatar_persona_dir(request, data_dir)
    if persona_dir is None:
        return _json_response({"error": "人格不存在"}, 404)
    path = persona_dir / AVATAR_FILENAME
    custom = path.is_file()
    if not custom:
        fallback = _default_avatar()
        if fallback is None:
            return _json_response({"error": "尚未设置立绘"}, 404)
        path = fallback
    response = web.FileResponse(path)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Avatar-Custom"] = "1" if custom else "0"
    return response


@handle_api_errors
async def api_avatar_post(request: web.Request, data_dir: Path) -> web.Response:
    """POST /api/personas/{name}/avatar — 请求体是图片本身（image/*）。

    统一用 Pillow 解码再存成 PNG：既校验确实是图片，也剥掉 EXIF 等附带数据。
    """
    from PIL import Image, UnidentifiedImageError

    persona_dir = _avatar_persona_dir(request, data_dir)
    if persona_dir is None:
        return _json_response({"error": "人格不存在"}, 404)
    if request.content_length and request.content_length > AVATAR_MAX_BYTES:
        return _json_response({"error": "图片不能超过 8 MB"}, 413)
    body = await request.content.read(AVATAR_MAX_BYTES + 1)
    if len(body) > AVATAR_MAX_BYTES:
        return _json_response({"error": "图片不能超过 8 MB"}, 413)
    try:
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            converted = image.convert("RGBA")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return _json_response({"error": "无法识别的图片"}, 400)
    converted.thumbnail((AVATAR_MAX_SIDE, AVATAR_MAX_SIDE))
    tmp = persona_dir / f".{AVATAR_FILENAME}.tmp"
    converted.save(tmp, format="PNG", optimize=True)
    tmp.replace(persona_dir / AVATAR_FILENAME)
    return _json_response({"success": True, "width": converted.width, "height": converted.height})


@handle_api_errors
async def api_avatar_delete(request: web.Request, data_dir: Path) -> web.Response:
    """DELETE /api/personas/{name}/avatar — 恢复默认立绘。"""
    persona_dir = _avatar_persona_dir(request, data_dir)
    if persona_dir is None:
        return _json_response({"error": "人格不存在"}, 404)
    (persona_dir / AVATAR_FILENAME).unlink(missing_ok=True)
    return _json_response({"success": True})
