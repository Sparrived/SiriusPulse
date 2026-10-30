"""受控人物管理入口；普通画像工具不能借此修改部署额度或角色权限。"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from aiohttp import web

from sirius_pulse.core.persona_db import PersonaDatabase
from sirius_pulse.core.persona_store import PersonaStore
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.people import PersonAccess, PersonError
from sirius_pulse.visual import VisualService
from sirius_pulse.webui.server_utils import _json_response

LOG = logging.getLogger("sirius.webui")


@contextmanager
def _services(server: Any) -> Iterator[tuple[Any, Any]]:
    directory = Path(server.persona_dir).resolve()
    if not (directory / "persona.json").is_file():
        raise ValueError("没有选择有效人格")
    workers = getattr(server, "persona_manager", None)
    worker = workers.get(directory.name) if isinstance(workers, dict) else workers
    runtime = getattr(worker, "_runtime", None)
    engine = getattr(runtime, "engine", None)
    if engine is not None and Path(engine.work_path).resolve() == directory:
        yield engine.person_service, engine.visual_service
        return
    persona = PersonaStore.load(directory)
    if persona is None:
        raise ValueError("人格配置不可读取")
    with PersonaDatabase(directory / "persona.db") as database:
        users = UnifiedUserManager(
            directory,
            persona_name=persona.name,
            persona_aliases=persona.aliases,
            conn=database.conn,
        )
        yield users.people, VisualService(directory, people=users.people, conn=database.conn)


async def people_request(request: web.Request, server: Any) -> web.Response:
    """所有管理读写都要求应用管理员，不把 viewer 或群管理员当人物控制者。"""
    if request.get("auth_role") != "admin":
        return _json_response({"error": "需要应用管理员权限", "code": "permission_denied"}, 403)
    try:
        body = await request.json() if request.method == "POST" else {}
        if not isinstance(body, dict):
            raise ValueError("请求必须为 JSON 对象")
        with _services(server) as (people, visual):
            access = PersonAccess(
                actor_id=f"web:{request.get('auth_user', 'admin')}",
                group_id=str(body.get("group_id") or request.query.get("group_id") or ""),
                is_admin=True,
            )
            action = str(request.match_info.get("operation") or "")
            if request.method == "GET":
                person_id = request.match_info["person_id"]
                if person_id == "state":
                    return _json_response(await visual.management_state(access))
                if person_id == "migration":
                    return _json_response(people.migration_dry_run())
                person = people.get_person(person_id, access)
                return _json_response(
                    {
                        "person": person,
                        "visual": await visual.get_summary(person_id, access, detail="detail"),
                    }
                )
            if action == "visual":
                payload = body.get("payload") or {}
                if not isinstance(payload, dict) or set(payload) & {
                    "access",
                    "queue",
                    "target",
                    "evidence_ref",
                    "source_ref",
                    "action",
                }:
                    raise ValueError("payload 包含无效或保留字段")
                result = await visual.manage(
                    str(body.get("action") or ""),
                    access=access,
                    target=body.get("target"),
                    evidence_ref=str(body.get("evidence_ref") or ""),
                    source_ref=str(body.get("source_ref") or ""),
                    **payload,
                )
            elif action == "policy":
                fields = {
                    key: body[key]
                    for key in (
                        "controller",
                        "total_quota",
                        "auto_enabled",
                        "retention_days",
                        "per_person_daily",
                        "per_persona_daily",
                    )
                    if key in body
                }
                result = await visual.set_deployment_policy(access=access, **fields)
            elif action == "migration-preview":
                result = people.migration_dry_run()
            elif action == "migration-apply":
                result = people.migration_apply()
            elif action == "migration-rollback":
                result = people.migration_rollback(
                    body.get("report") or body.get("migration_id") or body.get("user_ids") or ""
                )
            elif action == "deletion-preview":
                result = await visual.prepare_deletion(
                    str(body.get("person_id") or ""),
                    access,
                    scope=str(body.get("scope") or "person"),
                )
            elif action == "deletion-confirm":
                result = await visual.execute_deletion(str(body.get("confirm_token") or ""), access)
            else:
                return _json_response({"error": "未知人物管理操作"}, 404)
            if not isinstance(result, dict):
                result = {"success": True, "result": result}
            status = 200 if result.get("success", True) else 400
            if result.get("code") in {"permission_denied", "consent_required"}:
                status = 403
            elif result.get("code") in {"revision_conflict", "confirm_invalid"}:
                status = 409
            return _json_response(result, status)
    except PersonError as exc:
        code = exc.code
        status = 403 if code == "permission_denied" else 409 if code == "revision_conflict" else 400
        return _json_response({"error": str(exc), "code": code}, status)
    except (ValueError, TypeError) as exc:
        return _json_response({"error": str(exc), "code": "invalid_request"}, 400)
    except Exception:
        LOG.exception("人物管理请求失败")
        return _json_response({"error": "人物管理服务暂不可用", "code": "service_unavailable"}, 503)
