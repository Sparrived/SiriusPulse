"""对外说话：往聊天里投递消息、分享与表情包，以及她能说给谁听。

实现 :class:`sirius_pulse.tools.engine_context.DeliveryAccess`。
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.core.interaction_markers import (
    collect_allowed_poke_user_ids,
    parse_interaction_markers,
)

logger = logging.getLogger(__name__)


class DeliveryMixin:
    """对外说话：往聊天里投递消息、分享与表情包，以及她能说给谁听。"""

    _engine: Any

    def queue_pending_message(self, group_id: str, text: str, adapter_type: str = "") -> None:
        self._engine._pending_reminders.setdefault(group_id, []).append(
            {"text": text, "adapter_type": adapter_type}
        )

    async def dispatch_proactive_message(
        self,
        *,
        group_id: str,
        text: str,
        adapter_type: str = "",
        event_id: str = "",
        image_path: str = "",
        reply_references: list[dict[str, Any]] | None = None,
        sticker_names: list[str] | None = None,
        poke_user_ids: list[str] | None = None,
        record_delivery: bool = False,
    ) -> bool:
        handler = getattr(self._engine, "dispatch_proactive_message", None)
        # 工具参数里的文本同样可能带交互标记，先统一剥离再投递/记账。
        markers = parse_interaction_markers(
            text,
            available_stickers=getattr(self._engine, "_sticker_names", []) or [],
            allowed_poke_ids=collect_allowed_poke_user_ids(self._engine, group_id),
        )
        text = markers.text
        sticker_names = [*(sticker_names or []), *markers.sticker_names]
        poke_user_ids = [*(poke_user_ids or []), *markers.poke_user_ids]
        if callable(handler):
            result = await handler(
                group_id=group_id,
                text=text,
                adapter_type=adapter_type,
                event_id=event_id,
                image_path=image_path,
                reply_references=reply_references,
                sticker_names=sticker_names,
                poke_user_ids=poke_user_ids,
            )
            if result is not True:
                return False
            if record_delivery and text:
                self._record_delivered_text(group_id, text, event_id)
            return True

        # 旧宿主只有“交给事件总线”而没有平台回执，不能把入队冒充送达。
        logger.warning("宿主不支持带平台回执的主动投递，未发送: %s", event_id)
        return False

    def _record_delivered_text(self, group_id: str, text: str, event_id: str) -> None:
        """ACK 后尽力记账；记忆失败绝不能把已发送的副作用改判为可重试。"""
        record = getattr(self._engine, "_record_assistant_message", None)
        if not callable(record):
            return
        try:
            memory = getattr(self._engine, "basic_memory", None)
            entries = memory.get_all(group_id) if memory is not None else []
            if event_id and any(
                tag.get("delivery_event_id") == event_id
                for entry in entries
                for tag in (getattr(entry, "tags", None) or [])
                if isinstance(tag, dict)
            ):
                return
            record(
                group_id=group_id,
                target_user_id=group_id.removeprefix("private_")
                if group_id.startswith("private_")
                else "",
                content=text,
                tags=[{"delivery_event_id": event_id}] if event_id else [],
            )
            last_reply_at = getattr(self._engine, "_last_reply_at", None)
            if isinstance(last_reply_at, dict):
                from datetime import datetime, timezone

                last_reply_at[group_id] = datetime.now(timezone.utc).timestamp()
        except Exception:
            logger.warning("平台已确认送达，但通知历史记录失败: %s", event_id, exc_info=True)

    def list_audiences(self) -> list[Any]:
        """列出她可以"说给谁听"的候选会话，供她自己挑选受众。

        只返回确实可达的目标：群聊来自活跃群，私聊来自已配置的主人 QQ。
        不可达的目标不会出现，避免意图指向一个发不出去的会话而永远悬着。
        """
        from sirius_pulse.core.intent import Audience

        audiences: list[Audience] = []
        stamps = getattr(self._engine, "_group_last_message_at", {}) or {}
        for group_id in sorted(stamps, key=lambda gid: str(stamps.get(gid, "")), reverse=True):
            chat_id = str(group_id)
            if not chat_id or chat_id.startswith("private_"):
                continue
            audiences.append(
                Audience(
                    chat_id=chat_id,
                    label=self._group_label(chat_id),
                    kind="group",
                    last_active_at=str(stamps.get(group_id, "") or ""),
                )
            )
        master = self._master_private_chat_id()
        if master:
            audiences.append(Audience(chat_id=master, label="主人（私聊）", kind="private"))
        return audiences

    async def deliver_share(
        self,
        *,
        audience: str,
        text: str,
        event_id: str = "",
        adapter_type: str = "",
    ) -> bool:
        """把一条自主分享投递到指定会话，复用既有主动消息管线。

        返回 False 表示不可达或投递失败，调用方应保留意图以便稍后重试。
        """
        chat_id = str(audience or "").strip()
        body = str(text or "").strip()
        if not chat_id or not body:
            return False
        if not self._audience_reachable(chat_id):
            return False
        return await self.dispatch_proactive_message(
            group_id=chat_id,
            text=body,
            adapter_type=adapter_type,
            event_id=event_id,
        )

    def _audience_reachable(self, chat_id: str) -> bool:
        if chat_id.startswith("private_"):
            return chat_id == self._master_private_chat_id()
        stamps = getattr(self._engine, "_group_last_message_at", {}) or {}
        return chat_id in stamps

    def _master_private_chat_id(self) -> str:
        """主人私聊会话 ID；未配置 root QQ 时为空（即无此候选受众）。"""
        from sirius_pulse.tools.builtin.interaction_with_master import _master_qq_from_adapter

        adapter = getattr(self._engine, "_adapter", None)
        root = _master_qq_from_adapter(adapter)
        return f"private_{root}" if root else ""

    def _group_label(self, group_id: str) -> str:
        """群名，取不到时退回群号——她要能认出自己想去说话的地方。"""
        manager = getattr(self._engine, "semantic_memory", None)
        getter = getattr(manager, "get_group_profile", None)
        if callable(getter):
            try:
                profile = getter(group_id)
                name = str(getattr(profile, "group_name", "") or "").strip()
                if name:
                    return name
            except Exception:
                logger.debug("读取群名失败: %s", group_id)
        return f"群 {group_id}"

    def get_current_adapter_type(self) -> str:
        return self._engine._current_adapter_type

    def is_qq_bot_group_admin(self, group_id: str) -> bool:
        checker = getattr(self._engine, "is_qq_bot_group_admin", None)
        return bool(checker(group_id)) if callable(checker) else False

    def activate_private_group(self, group_id: str) -> None:
        self._engine._active_private_groups.add(group_id)

    async def send_sticker_by_names(
        self,
        group_id: str,
        names: list[str],
    ) -> dict[str, Any]:
        """Send one sticker chosen from the provided candidate names."""
        return await self._engine._send_stickers_by_names(group_id, names)

    def list_sticker_names(self) -> list[str]:
        """Return sticker names available to the current persona."""
        return list(getattr(self._engine, "_sticker_names", []) or [])

    # ── 用户查找 API（委托给 UserLookupService）──────────────
