"""Glossary manager: term definitions learned from conversations.

Terms are scoped to a persona so that different personas maintain
independent vocabularies.  All terms for a persona are stored in a
single file under <work_path>/glossary/terms.json.

Legacy per-group files under <work_path>/glossary/ are migrated on
first access.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sirius_pulse.memory.glossary.models import GlossaryTerm
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

MAX_GLOSSARY_TERMS = 200
MAX_CONTEXT_EXAMPLES = 5
GLOSSARY_PROMPT_MAX_TERMS = 20


class GlossaryManager:
    """Manages glossary terms with per-persona persistence.

    Terms are scoped to a persona so that different personas maintain
    independent vocabularies.  The *group_id* parameter in the public
    API is retained for backward compatibility but is ignored internally.
    """

    def __init__(
        self,
        work_path: Path | WorkspaceLayout,
        persona_name: str = "default",
    ) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._base_dir = layout.work_path / "glossary"
        self._legacy_dir = self._base_dir  # v1 flat storage
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._persona_name = persona_name
        self._terms: dict[str, GlossaryTerm] | None = None
        self._migrated: bool = False
        self._path = self._base_dir / "terms.json"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_migrated(self) -> None:
        """One-shot migration from legacy flat storage to persona-scoped."""
        if self._migrated:
            return
        self._migrated = True

        # If the persona file already exists, assume migration done.
        if self._path.exists():
            return

        # Migrate legacy group files into the persona file.
        migrated_count = 0
        all_terms: dict[str, GlossaryTerm] = {}
        for legacy_path in self._legacy_dir.glob("*.json"):
            try:
                import json

                data = json.loads(legacy_path.read_text(encoding="utf-8"))
                terms = {
                    k: GlossaryTerm.from_dict(v)
                    for k, v in data.items()
                    if isinstance(v, dict)
                }
                for key, term in terms.items():
                    existing = all_terms.get(key)
                    if existing is not None:
                        existing.usage_count += term.usage_count
                        if term.confidence > existing.confidence:
                            existing.definition = term.definition
                            existing.confidence = term.confidence
                            existing.source = term.source
                        seen = set(existing.context_examples)
                        for ex in term.context_examples:
                            if ex not in seen and len(existing.context_examples) < MAX_CONTEXT_EXAMPLES:
                                existing.context_examples.append(ex)
                                seen.add(ex)
                        related_set = set(existing.related_terms)
                        for rt in term.related_terms:
                            if rt not in related_set:
                                existing.related_terms.append(rt)
                                related_set.add(rt)
                    else:
                        all_terms[key] = term
                if terms:
                    migrated_count += len(terms)
                    # Rename legacy file to .migrated backup
                    backup = legacy_path.with_suffix(".json.migrated")
                    legacy_path.rename(backup)
            except Exception as exc:
                logger.warning("Glossary migration failed for %s: %s", legacy_path, exc)

        if all_terms:
            self._terms = all_terms
            self._save()

        if migrated_count:
            logger.info(
                "Glossary migrated %d terms to persona '%s'", migrated_count, self._persona_name
            )

    def _load(self) -> dict[str, GlossaryTerm]:
        self._ensure_migrated()
        if self._terms is not None:
            return self._terms
        if not self._path.exists():
            self._terms = {}
            return self._terms
        try:
            import json

            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._terms = {
                k: GlossaryTerm.from_dict(v)
                for k, v in data.items()
                if isinstance(v, dict)
            }
        except (OSError, json.JSONDecodeError):
            self._terms = {}
        return self._terms

    def _save(self) -> None:
        import json

        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        terms = self._load()
        tmp.write_text(
            json.dumps({k: v.to_dict() for k, v in terms.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    @staticmethod
    def _safe_name(name: str) -> str:
        import re

        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"

    # ------------------------------------------------------------------
    # Public API (group_id retained for backward compat)
    # ------------------------------------------------------------------

    def add_or_update(self, group_id: str, term: GlossaryTerm) -> None:
        """Add or merge a glossary term."""
        del group_id  # ignored: persona-level storage
        key = term.term.lower().strip()
        if not key:
            return

        terms = self._load()
        existing = terms.get(key)
        if existing is not None:
            existing.usage_count += 1
            existing.last_updated_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()
            if term.confidence > existing.confidence:
                existing.definition = term.definition
                existing.confidence = term.confidence
                existing.source = term.source
            seen = set(existing.context_examples)
            for ex in term.context_examples:
                if ex not in seen and len(existing.context_examples) < MAX_CONTEXT_EXAMPLES:
                    existing.context_examples.append(ex)
                    seen.add(ex)
            related_set = set(existing.related_terms)
            for rt in term.related_terms:
                if rt not in related_set:
                    existing.related_terms.append(rt)
                    related_set.add(rt)
            if term.domain != "custom":
                existing.domain = term.domain
        else:
            terms[key] = term

        if len(terms) > MAX_GLOSSARY_TERMS:
            self._evict_least_used()

        self._save()

    def get_term(self, group_id: str, term: str) -> GlossaryTerm | None:
        del group_id  # ignored
        return self._load().get(term.lower().strip())

    def search(
        self, group_id: str, text: str, max_terms: int = GLOSSARY_PROMPT_MAX_TERMS
    ) -> list[GlossaryTerm]:
        """Find glossary terms mentioned in or relevant to the given text."""
        del group_id  # ignored
        text_lower = text.lower()
        matched: list[tuple[float, GlossaryTerm]] = []
        for term in self._load().values():
            if term.term.lower() in text_lower:
                score = term.confidence * (1.0 + 0.1 * min(term.usage_count, 10))
                matched.append((score, term))
        matched.sort(key=lambda x: x[0], reverse=True)
        return [t for _, t in matched[:max_terms]]

    def build_prompt_section(
        self, group_id: str, text: str = "", max_terms: int = GLOSSARY_PROMPT_MAX_TERMS
    ) -> str:
        """Build a compact glossary section for the system prompt."""
        del group_id  # ignored
        if text:
            terms = self.search("", text, max_terms=max_terms)
        else:
            all_terms = sorted(
                self._load().values(),
                key=lambda t: t.confidence * t.usage_count,
                reverse=True,
            )
            terms = all_terms[:max_terms]
        if not terms:
            return ""
        lines: list[str] = []
        for term in terms:
            conf_tag = "?" if term.confidence < 0.6 else ("~" if term.confidence < 0.8 else "")
            defn = term.definition[:100] if term.definition else "待明确"
            lines.append(f"{term.term}{conf_tag}: {defn}")
        return "\n".join(lines)

    def _evict_least_used(self) -> None:
        terms = self._load()
        if len(terms) <= MAX_GLOSSARY_TERMS:
            return
        scored = sorted(
            terms.items(), key=lambda kv: kv[1].confidence * kv[1].usage_count, reverse=True
        )
        self._terms = dict(scored[:MAX_GLOSSARY_TERMS])
