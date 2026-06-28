from __future__ import annotations

import uuid
from typing import Any

from sirius_pulse.memory.profile.models import (
    ProfileItem,
    ProfileUpdate,
    UserPersonaProfile,
    clamp_confidence,
    now_iso,
)
from sirius_pulse.memory.profile.prompt import ProfilePromptRenderer
from sirius_pulse.memory.profile.store import UserPersonaProfileStore


class UserPersonaProfileManager:
    """High-level service for model-maintained people profiles."""

    def __init__(
        self,
        store: UserPersonaProfileStore,
        *,
        persona_name: str,
        user_manager: Any | None = None,
        semantic_memory: Any | None = None,
    ) -> None:
        self._store = store
        self._persona_name = persona_name
        self._user_manager = user_manager
        self._semantic_memory = semantic_memory
        self._renderer = ProfilePromptRenderer()

    def get_profile(self, group_id: str, user_id: str, *, create: bool = True) -> UserPersonaProfile | None:
        group_key = group_id or "default"
        profile = self._store.get_profile(self._persona_name, group_key, user_id)
        if profile is None and create:
            profile = UserPersonaProfile(user_id=user_id, group_id=group_key)
        if profile is not None:
            self._hydrate_runtime_fields(profile)
        return profile

    def list_group_profiles(self, group_id: str) -> list[UserPersonaProfile]:
        profiles = self._store.list_group_profiles(self._persona_name, group_id or "default")
        for profile in profiles:
            self._hydrate_runtime_fields(profile)
        return profiles

    def update_profile(
        self,
        *,
        group_id: str,
        user_id: str,
        updates: list[dict[str, Any]] | list[ProfileUpdate],
        display_name: str = "",
        short_impression: str = "",
        reason: str = "",
        created_by: str = "model",
    ) -> dict[str, Any]:
        if not user_id:
            return {"success": False, "error": "user_id 不能为空"}
        group_key = group_id or "default"
        profile = self.get_profile(group_key, user_id, create=True)
        if profile is None:
            return {"success": False, "error": "无法创建人物画像"}

        normalized_updates = [
            update if isinstance(update, ProfileUpdate) else ProfileUpdate.from_dict(update)
            for update in updates
        ]
        normalized_updates = [update for update in normalized_updates if update.key]
        if not normalized_updates and not display_name and not short_impression:
            return {"success": False, "error": "没有可写入的画像更新"}

        changed: list[dict[str, Any]] = []
        now = now_iso()
        if display_name:
            profile.display_name = display_name[:120]
            changed.append({"field": "display_name", "value": profile.display_name})
        if short_impression:
            profile.short_impression = short_impression[:800]
            changed.append({"field": "short_impression", "value": profile.short_impression})

        for update in normalized_updates:
            section = profile.section(update.section)
            existing = section.find(update.key)
            if update.operation == "upsert":
                if not update.value:
                    continue
                if existing is None:
                    item = ProfileItem(
                        key=update.key,
                        value=update.value,
                        confidence=update.confidence,
                        evidence=update.evidence,
                        evidence_message_ids=list(update.evidence_message_ids),
                        first_seen_at=now,
                        last_seen_at=now,
                        status="active" if update.confidence >= 0.6 else "uncertain",
                    )
                    section.items.append(item)
                else:
                    existing.value = update.value
                    existing.confidence = max(existing.confidence, update.confidence)
                    existing.evidence = update.evidence or existing.evidence
                    existing.evidence_message_ids = _merge_ids(
                        existing.evidence_message_ids, update.evidence_message_ids
                    )
                    existing.last_seen_at = now
                    existing.update_count += 1
                    existing.status = "active" if existing.confidence >= 0.6 else "uncertain"
                changed.append(update_to_dict(update))
                continue

            if existing is None:
                continue
            if update.operation in {"reject", "delete"}:
                existing.status = "rejected"
            elif update.operation == "stale":
                existing.status = "stale"
            existing.evidence = update.evidence or existing.evidence
            existing.evidence_message_ids = _merge_ids(
                existing.evidence_message_ids, update.evidence_message_ids
            )
            existing.last_seen_at = now
            changed.append(update_to_dict(update))

        if not changed:
            return {"success": False, "error": "画像更新未产生变化"}

        profile.version += 1
        self._hydrate_runtime_fields(profile)
        self._store.save_profile(self._persona_name, profile)
        evidence_ids: list[str] = []
        for update in normalized_updates:
            evidence_ids = _merge_ids(evidence_ids, update.evidence_message_ids)
        self._store.append_event(
            event_id=str(uuid.uuid4()),
            persona_name=self._persona_name,
            group_id=group_key,
            user_id=user_id,
            event_type="profile_patch",
            patch={"updates": changed, "reason": reason[:500]},
            evidence_message_ids=evidence_ids,
            created_by=created_by[:80],
        )
        return {
            "success": True,
            "user_id": user_id,
            "group_id": group_key,
            "changed": changed,
            "profile_card": self.render_profile_card(group_key, user_id),
        }

    def mark_item(
        self,
        *,
        group_id: str,
        user_id: str,
        section: str,
        key: str,
        status: str,
        reason: str = "",
        created_by: str = "model",
    ) -> dict[str, Any]:
        operation = "stale" if status == "stale" else "reject"
        return self.update_profile(
            group_id=group_id,
            user_id=user_id,
            updates=[{"section": section, "key": key, "operation": operation, "evidence": reason}],
            reason=reason,
            created_by=created_by,
        )

    def render_profile_card(self, group_id: str, user_id: str) -> str:
        profile = self.get_profile(group_id, user_id, create=False)
        if profile is None:
            return ""
        return self._renderer.render_card(profile)

    def render_profiles_section(self, profiles: list[UserPersonaProfile]) -> str | None:
        return self._renderer.render_section(profiles)

    def list_events(self, group_id: str, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._store.list_events(self._persona_name, group_id or "default", user_id, limit=limit)

    def _hydrate_runtime_fields(self, profile: UserPersonaProfile) -> None:
        if self._user_manager is not None:
            user = self._user_manager.get_user(profile.user_id, profile.group_id)
            if user is None:
                user = self._user_manager.get_global_user(profile.user_id)
            if user is not None and not profile.display_name:
                profile.display_name = getattr(user, "name", "") or profile.user_id
        if self._semantic_memory is not None:
            semantic = self._semantic_memory.get_user_profile(profile.group_id, profile.user_id)
            profile.familiarity_score = semantic.compute_familiarity()
            profile.affinity_score = clamp_confidence(getattr(semantic, "engagement_rate", 0.0), 0.0)


    def register_alias(
        self,
        *,
        alias: str,
        user_id: str,
        user_name: str = "",
        group_id: str = "",
        confidence: float = 0.8,
        evidence: str = "",
        created_by: str = "model",
    ) -> dict[str, Any]:
        alias_key = _normalize_alias(alias)
        if not alias_key:
            return {"success": False, "error": "alias 不能为空"}
        result = self.update_profile(
            group_id=group_id or "default",
            user_id=user_id,
            display_name=user_name,
            updates=[
                {
                    "section": "aliases",
                    "key": alias_key,
                    "value": alias_key,
                    "confidence": confidence,
                    "evidence": evidence,
                    "operation": "upsert",
                }
            ],
            reason=f"登记人物别称：{alias_key}",
            created_by=created_by,
        )
        if not result.get("success"):
            return result
        return {
            "success": True,
            "alias": alias_key,
            "user_id": user_id,
            "user_name": user_name,
            "confidence": clamp_confidence(confidence),
            "evidence": evidence,
        }

    def resolve_alias(
        self,
        alias: str,
        *,
        group_id: str = "",
        recent_speakers: list[str] | None = None,
        at_user_id: str | None = None,
    ) -> tuple[str | None, float, list[str]]:
        alias_key = _normalize_alias(alias)
        if not alias_key:
            return None, 0.0, []
        matches: list[tuple[UserPersonaProfile, ProfileItem]] = []
        for profile in self.list_group_profiles(group_id or "default"):
            item = profile.section("aliases").find(alias_key)
            if item is not None and item.status == "active":
                matches.append((profile, item))
        if not matches:
            return None, 0.0, []
        if len(matches) == 1:
            profile, item = matches[0]
            return profile.user_id, item.confidence, []
        recent = set(recent_speakers or [])
        scored: list[tuple[UserPersonaProfile, ProfileItem, float]] = []
        for profile, item in matches:
            score = item.confidence
            if at_user_id and profile.user_id == at_user_id:
                score += 0.3
            if profile.user_id in recent:
                score += 0.2
            scored.append((profile, item, score))
        scored.sort(key=lambda row: row[2], reverse=True)
        candidates = [profile.user_id for profile, _item, _score in scored]
        best_profile, _best_item, best_score = scored[0]
        second_score = scored[1][2] if len(scored) > 1 else 0.0
        if at_user_id or best_score >= second_score * 1.5:
            return best_profile.user_id, best_score, candidates
        return None, best_score, candidates

    def list_alias_entries(self, group_id: str = "") -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for profile in self.list_group_profiles(group_id or "default"):
            for item in profile.section("aliases").active_items():
                result[item.key] = {
                    "alias": item.key,
                    "user_id": profile.user_id,
                    "user_name": profile.display_name,
                    "group_id": profile.group_id,
                    "confidence": item.confidence,
                    "source": item.source,
                    "updated_at": item.last_seen_at,
                }
        return result

    def get_aliases_for_group(self, group_id: str = "") -> dict[str, str]:
        aliases: dict[str, str] = {}
        for alias, entry in self.list_alias_entries(group_id).items():
            aliases[alias] = str(entry.get("user_name") or entry.get("user_id") or "")
        return aliases

    def delete_alias(self, alias: str, user_id: str = "", group_id: str = "") -> bool:
        alias_key = _normalize_alias(alias)
        if not alias_key:
            return False
        profiles = self.list_group_profiles(group_id or "default")
        changed = False
        for profile in profiles:
            if user_id and profile.user_id != user_id:
                continue
            item = profile.section("aliases").find(alias_key)
            if item is None or item.status != "active":
                continue
            result = self.mark_item(
                group_id=profile.group_id,
                user_id=profile.user_id,
                section="aliases",
                key=alias_key,
                status="rejected",
                reason="删除人物别称",
                created_by="model",
            )
            changed = changed or bool(result.get("success"))
        return changed
def update_to_dict(update: ProfileUpdate) -> dict[str, Any]:
    return {
        "section": update.section,
        "key": update.key,
        "value": update.value,
        "confidence": update.confidence,
        "evidence": update.evidence,
        "evidence_message_ids": list(update.evidence_message_ids),
        "operation": update.operation,
    }


def _merge_ids(existing: list[str], new_values: list[str]) -> list[str]:
    merged = list(existing)
    for value in new_values:
        if value and value not in merged:
            merged.append(value)
    return merged[-20:]



def _normalize_alias(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())[:80]
