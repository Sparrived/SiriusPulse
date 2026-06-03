"""SQLite store for the evidence-first memory ledger."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sirius_pulse.memory.provenance.models import (
    ClaimAttribution,
    ClaimStatus,
    ClaimType,
    Evidence,
    ExtractionRun,
    MemoryClaim,
)
from sirius_pulse.utils.sqlite_base import BaseSqliteStore

logger = logging.getLogger(__name__)

__all__ = ["ProvenanceStore"]


class ProvenanceStore(BaseSqliteStore):
    """Evidence, extraction run, and claim storage.

    The store is designed to live in ``persona.db`` and share the engine's
    existing SQLite connection.
    """

    def _create_tables(self) -> None:
        self.executescript("""
            CREATE TABLE IF NOT EXISTS memory_evidence (
                evidence_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL DEFAULT 'message',
                group_id TEXT DEFAULT '',
                message_id TEXT DEFAULT '',
                platform_message_id TEXT DEFAULT '',
                speaker_user_id TEXT DEFAULT '',
                speaker_name TEXT DEFAULT '',
                content_quote TEXT DEFAULT '',
                content_digest TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                observed_at TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}'
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_evidence_digest
                ON memory_evidence(source_type, group_id, message_id, content_digest);
            CREATE INDEX IF NOT EXISTS idx_memory_evidence_group
                ON memory_evidence(group_id);
            CREATE INDEX IF NOT EXISTS idx_memory_evidence_message
                ON memory_evidence(message_id);

            CREATE TABLE IF NOT EXISTS memory_extraction_runs (
                run_id TEXT PRIMARY KEY,
                task TEXT NOT NULL DEFAULT '',
                model TEXT DEFAULT '',
                prompt_version TEXT DEFAULT '',
                input_evidence_ids TEXT DEFAULT '[]',
                created_at TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS memory_claims (
                claim_id TEXT PRIMARY KEY,
                subject_user_id TEXT DEFAULT '',
                subject_label TEXT DEFAULT '',
                fact_type TEXT NOT NULL DEFAULT 'other',
                value TEXT NOT NULL DEFAULT '',
                predicate TEXT DEFAULT '',
                object_value TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'candidate',
                attribution TEXT NOT NULL DEFAULT 'inferred',
                confidence REAL NOT NULL DEFAULT 0.5,
                evidence_ids TEXT DEFAULT '[]',
                extraction_run_id TEXT DEFAULT '',
                source TEXT DEFAULT '',
                source_record_id TEXT DEFAULT '',
                source_situation_id TEXT DEFAULT '',
                source_group_id TEXT DEFAULT '',
                observed_at TEXT DEFAULT '',
                expires_at TEXT DEFAULT '',
                supersedes TEXT DEFAULT '[]',
                superseded_by TEXT,
                corrections TEXT DEFAULT '[]',
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_memory_claims_subject
                ON memory_claims(subject_user_id);
            CREATE INDEX IF NOT EXISTS idx_memory_claims_status
                ON memory_claims(status);
            CREATE INDEX IF NOT EXISTS idx_memory_claims_type
                ON memory_claims(fact_type);
            CREATE INDEX IF NOT EXISTS idx_memory_claims_source_record
                ON memory_claims(source_record_id);
            CREATE INDEX IF NOT EXISTS idx_memory_claims_situation
                ON memory_claims(source_situation_id);
        """)
        self._ensure_columns("memory_claims", {
            "source_record_id": "TEXT DEFAULT ''",
            "source_situation_id": "TEXT DEFAULT ''",
            "source_group_id": "TEXT DEFAULT ''",
            "metadata": "TEXT DEFAULT '{}'",
        })

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    def save_evidence(self, evidence: Evidence) -> Evidence:
        """Insert evidence and return the stored row.

        Evidence is immutable; if an equivalent source snapshot already exists
        we return the existing evidence id instead of duplicating it.
        """
        existing = self.fetchone(
            """
            SELECT * FROM memory_evidence
            WHERE source_type = ? AND group_id = ? AND message_id = ?
              AND content_digest = ?
            """,
            (
                evidence.source_type,
                evidence.group_id,
                evidence.message_id,
                evidence.content_digest,
            ),
        )
        if existing:
            return self._row_to_evidence(existing)

        data = evidence.to_dict()
        self.execute(
            """
            INSERT INTO memory_evidence (
                evidence_id, source_type, group_id, message_id,
                platform_message_id, speaker_user_id, speaker_name,
                content_quote, content_digest, created_at, observed_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["evidence_id"],
                data["source_type"],
                data["group_id"],
                data["message_id"],
                data["platform_message_id"],
                data["speaker_user_id"],
                data["speaker_name"],
                data["content_quote"],
                data["content_digest"],
                data["created_at"],
                data["observed_at"],
                json.dumps(data["metadata"], ensure_ascii=False),
            ),
        )
        self.commit()
        return evidence

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        row = self.fetchone(
            "SELECT * FROM memory_evidence WHERE evidence_id = ?",
            (evidence_id,),
        )
        return self._row_to_evidence(row) if row else None

    def get_evidence_many(self, evidence_ids: list[str]) -> list[Evidence]:
        if not evidence_ids:
            return []
        result: list[Evidence] = []
        for evidence_id in evidence_ids:
            item = self.get_evidence(evidence_id)
            if item:
                result.append(item)
        return result

    # ------------------------------------------------------------------
    # Extraction runs
    # ------------------------------------------------------------------

    def save_run(self, run: ExtractionRun) -> ExtractionRun:
        data = run.to_dict()
        self.execute(
            """
            INSERT OR REPLACE INTO memory_extraction_runs (
                run_id, task, model, prompt_version, input_evidence_ids,
                created_at, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["run_id"],
                data["task"],
                data["model"],
                data["prompt_version"],
                json.dumps(data["input_evidence_ids"], ensure_ascii=False),
                data["created_at"],
                json.dumps(data["metadata"], ensure_ascii=False),
            ),
        )
        self.commit()
        return run

    def get_run(self, run_id: str) -> ExtractionRun | None:
        row = self.fetchone(
            "SELECT * FROM memory_extraction_runs WHERE run_id = ?",
            (run_id,),
        )
        return self._row_to_run(row) if row else None

    # ------------------------------------------------------------------
    # Claims
    # ------------------------------------------------------------------

    def save_claim(self, claim: MemoryClaim) -> MemoryClaim:
        data = claim.to_dict()
        self.execute(
            """
            INSERT OR REPLACE INTO memory_claims (
                claim_id, subject_user_id, subject_label, fact_type, value,
                predicate, object_value, status, attribution, confidence,
                evidence_ids, extraction_run_id, source, source_record_id,
                source_situation_id, source_group_id, observed_at, expires_at,
                supersedes, superseded_by, corrections, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["claim_id"],
                data["subject_user_id"],
                data["subject_label"],
                data["fact_type"],
                data["value"],
                data["predicate"],
                data["object_value"],
                data["status"],
                data["attribution"],
                data["confidence"],
                json.dumps(data["evidence_ids"], ensure_ascii=False),
                data["extraction_run_id"],
                data["source"],
                data["source_record_id"],
                data["source_situation_id"],
                data["source_group_id"],
                data["observed_at"],
                data["expires_at"],
                json.dumps(data["supersedes"], ensure_ascii=False),
                data["superseded_by"],
                json.dumps(data["corrections"], ensure_ascii=False),
                json.dumps(data["metadata"], ensure_ascii=False),
            ),
        )
        self.commit()
        return claim

    def save_claims(self, claims: list[MemoryClaim]) -> None:
        for claim in claims:
            self.save_claim(claim)

    def get_claim(self, claim_id: str) -> MemoryClaim | None:
        row = self.fetchone(
            "SELECT * FROM memory_claims WHERE claim_id = ?",
            (claim_id,),
        )
        return self._row_to_claim(row) if row else None

    def get_claims_for_user(
        self,
        user_id: str,
        *,
        status: str | None = None,
        profile_safe_only: bool = False,
        limit: int = 200,
    ) -> list[MemoryClaim]:
        conditions = ["subject_user_id = ?"]
        params: list[Any] = [user_id]
        if status:
            conditions.append("status = ?")
            params.append(status)
        where = " AND ".join(conditions)
        rows = self.fetchall(
            f"""
            SELECT * FROM memory_claims
            WHERE {where}
            ORDER BY observed_at DESC
            LIMIT ?
            """,
            [*params, limit],
        )
        claims = [self._row_to_claim(r) for r in rows]
        if profile_safe_only:
            claims = [c for c in claims if c.profile_safe]
        return claims

    def get_active_profile_claims(self, user_id: str) -> list[MemoryClaim]:
        return self.get_claims_for_user(
            user_id,
            status=ClaimStatus.ACTIVE,
            profile_safe_only=True,
        )

    def list_subject_user_ids(self, *, status: str | None = None) -> list[str]:
        if status:
            rows = self.fetchall(
                """
                SELECT DISTINCT subject_user_id FROM memory_claims
                WHERE subject_user_id != '' AND status = ?
                """,
                (status,),
            )
        else:
            rows = self.fetchall(
                """
                SELECT DISTINCT subject_user_id FROM memory_claims
                WHERE subject_user_id != ''
                """
            )
        return [r["subject_user_id"] for r in rows]

    def get_claim_provenance(self, claim_id: str) -> dict[str, Any] | None:
        claim = self.get_claim(claim_id)
        if not claim:
            return None
        return {
            "claim": claim.to_dict(),
            "evidence": [e.to_dict() for e in self.get_evidence_many(claim.evidence_ids)],
            "extraction_run": (
                self.get_run(claim.extraction_run_id).to_dict()
                if claim.extraction_run_id and self.get_run(claim.extraction_run_id)
                else None
            ),
        }

    def find_claim_by_source_record(self, source_record_id: str) -> MemoryClaim | None:
        row = self.fetchone(
            "SELECT * FROM memory_claims WHERE source_record_id = ? LIMIT 1",
            (source_record_id,),
        )
        return self._row_to_claim(row) if row else None

    def stats(self) -> dict[str, Any]:
        total_claims = self.fetchone("SELECT COUNT(*) AS n FROM memory_claims")["n"]
        total_evidence = self.fetchone("SELECT COUNT(*) AS n FROM memory_evidence")["n"]
        by_status = {
            r["status"]: r["n"]
            for r in self.fetchall(
                "SELECT status, COUNT(*) AS n FROM memory_claims GROUP BY status"
            )
        }
        by_type = {
            r["fact_type"]: r["n"]
            for r in self.fetchall(
                "SELECT fact_type, COUNT(*) AS n FROM memory_claims GROUP BY fact_type"
            )
        }
        return {
            "total_claims": total_claims,
            "total_evidence": total_evidence,
            "by_status": by_status,
            "by_type": by_type,
        }

    # ------------------------------------------------------------------
    # Migration helpers
    # ------------------------------------------------------------------

    def migrate_from_legacy_tables(self) -> dict[str, int]:
        """Populate provenance tables from existing SQLite memory tables.

        This migration is deterministic and idempotent. It does not delete or
        rewrite legacy rows.
        """
        migrated = {
            "evolution_records": self._migrate_evolution_records(),
            "aliases": self._migrate_aliases(),
            "user_profile_fields": self._migrate_user_profile_fields(),
        }
        logger.info("Provenance migration complete: %s", migrated)
        return migrated

    def _migrate_evolution_records(self) -> int:
        if not self._table_exists("evolution_records"):
            return 0
        rows = self.fetchall("SELECT * FROM evolution_records")
        if not rows:
            return 0

        run = self.save_run(ExtractionRun(
            task="migration:evolution_records",
            model="migration:direct",
            prompt_version="provenance-v1",
        ))
        migrated = 0
        for row in rows:
            source_record_id = row.get("record_id", "")
            if source_record_id and self.find_claim_by_source_record(source_record_id):
                continue

            fact_type = self._fact_type_from_predicate(row.get("predicate", ""))
            status = self._status_from_legacy(row.get("status", ""))
            attribution = (
                ClaimAttribution.MIGRATION
                if status == ClaimStatus.ACTIVE
                else ClaimAttribution.INFERRED
            )
            message_ids = self._json_list(row.get("source_message_ids"))
            evidence_ids: list[str] = []
            for message_id in message_ids:
                evidence = self.save_evidence(Evidence(
                    source_type="legacy_evolution_message",
                    group_id=row.get("source_group_id", ""),
                    message_id=message_id,
                    speaker_user_id=row.get("subject_user_id", ""),
                    speaker_name=row.get("subject", ""),
                    content_quote="",
                    observed_at=row.get("extracted_at", ""),
                    metadata={"source_record_id": source_record_id},
                ))
                evidence_ids.append(evidence.evidence_id)

            claim = MemoryClaim(
                subject_user_id=row.get("subject_user_id", "") or row.get("subject", ""),
                subject_label=row.get("subject", ""),
                fact_type=fact_type,
                value=self._render_value(row.get("predicate", ""), row.get("obj", "")),
                predicate=row.get("predicate", ""),
                object_value=row.get("obj", ""),
                status=status,
                attribution=attribution,
                confidence=float(row.get("confidence", 0.5)),
                evidence_ids=evidence_ids,
                extraction_run_id=run.run_id,
                source="legacy_evolution",
                source_record_id=source_record_id,
                source_situation_id=row.get("source_situation_id", ""),
                source_group_id=row.get("source_group_id", ""),
                observed_at=row.get("extracted_at", ""),
                supersedes=self._json_list(row.get("supersedes")),
                superseded_by=row.get("superseded_by"),
                corrections=self._json_list(row.get("corrections")),
            )
            self.save_claim(claim)
            migrated += 1
        return migrated

    def _migrate_aliases(self) -> int:
        if not self._table_exists("aliases"):
            return 0
        rows = self.fetchall("SELECT * FROM aliases")
        if not rows:
            return 0
        run = self.save_run(ExtractionRun(
            task="migration:aliases",
            model="migration:direct",
            prompt_version="provenance-v1",
        ))
        migrated = 0
        for row in rows:
            source_key = f"alias:{row.get('alias', '')}:{row.get('user_id', '')}"
            if self.find_claim_by_source_record(source_key):
                continue
            source = row.get("source", "") or "legacy_alias"
            status = self._status_from_legacy(row.get("status", "active"))
            attribution = (
                ClaimAttribution.MANUAL
                if source == "manual"
                else ClaimAttribution.MIGRATION
            )
            claim = MemoryClaim(
                subject_user_id=row.get("user_id", ""),
                subject_label=row.get("user_name", "") or row.get("user_id", ""),
                fact_type=ClaimType.ALIAS,
                value=row.get("alias", ""),
                predicate="别称",
                object_value=row.get("alias", ""),
                status=status,
                attribution=attribution,
                confidence=float(row.get("confidence", 0.5)),
                extraction_run_id=run.run_id,
                source="legacy_alias",
                source_record_id=source_key,
                source_group_id=",".join(self._json_list(row.get("groups"))),
                observed_at=row.get("last_seen_at", "") or row.get("created_at", ""),
                metadata={
                    "groups": self._json_list(row.get("groups")),
                    "mentioned_count": row.get("mentioned_count", 0),
                    "legacy_source": source,
                },
            )
            self.save_claim(claim)
            migrated += 1
        return migrated

    def _migrate_user_profile_fields(self) -> int:
        if not self._table_exists("users"):
            return 0
        rows = self.fetchall("SELECT * FROM users")
        if not rows:
            return 0
        run = self.save_run(ExtractionRun(
            task="migration:user_profile_fields",
            model="migration:direct",
            prompt_version="provenance-v1",
        ))
        migrated = 0
        for row in rows:
            user_id = row.get("user_id", "")
            name = row.get("name", "") or user_id
            for index, anchor in enumerate(self._json_list(row.get("identity_anchors"))):
                migrated += self._save_migrated_profile_claim(
                    run, user_id, name, ClaimType.IDENTITY, str(anchor),
                    f"user_identity:{user_id}:{index}",
                )
            for index, rel in enumerate(self._json_list(row.get("relationships"))):
                if isinstance(rel, dict):
                    value = rel.get("fact_hint") or rel.get("relation") or rel.get("target_name") or ""
                    predicate = rel.get("relation", "")
                    obj = rel.get("target_name") or rel.get("target") or ""
                else:
                    value = str(rel)
                    predicate = "关系"
                    obj = str(rel)
                migrated += self._save_migrated_profile_claim(
                    run, user_id, name, ClaimType.RELATIONSHIP, value,
                    f"user_relationship:{user_id}:{index}",
                    predicate=predicate,
                    obj=obj,
                )
            short_bio = str(row.get("short_bio", "") or "").strip()
            if short_bio:
                migrated += self._save_migrated_profile_claim(
                    run, user_id, name, ClaimType.OTHER, short_bio,
                    f"user_short_bio:{user_id}",
                    predicate="摘要",
                    obj=short_bio,
                    status=ClaimStatus.CANDIDATE,
                )
        return migrated

    def _save_migrated_profile_claim(
        self,
        run: ExtractionRun,
        user_id: str,
        name: str,
        fact_type: str,
        value: str,
        source_record_id: str,
        *,
        predicate: str = "",
        obj: str = "",
        status: str = ClaimStatus.ACTIVE,
    ) -> int:
        if not value or self.find_claim_by_source_record(source_record_id):
            return 0
        claim = MemoryClaim(
            subject_user_id=user_id,
            subject_label=name,
            fact_type=fact_type,
            value=value,
            predicate=predicate,
            object_value=obj,
            status=status,
            attribution=ClaimAttribution.MIGRATION,
            confidence=0.55 if status == ClaimStatus.ACTIVE else 0.35,
            extraction_run_id=run.run_id,
            source="legacy_user_profile",
            source_record_id=source_record_id,
        )
        self.save_claim(claim)
        return 1

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    def _row_to_evidence(self, row: dict[str, Any]) -> Evidence:
        return Evidence.from_dict({
            **row,
            "metadata": self._json_dict(row.get("metadata")),
        })

    def _row_to_run(self, row: dict[str, Any]) -> ExtractionRun:
        return ExtractionRun.from_dict({
            **row,
            "input_evidence_ids": self._json_list(row.get("input_evidence_ids")),
            "metadata": self._json_dict(row.get("metadata")),
        })

    def _row_to_claim(self, row: dict[str, Any]) -> MemoryClaim:
        return MemoryClaim.from_dict({
            **row,
            "evidence_ids": self._json_list(row.get("evidence_ids")),
            "supersedes": self._json_list(row.get("supersedes")),
            "corrections": self._json_list(row.get("corrections")),
            "metadata": self._json_dict(row.get("metadata")),
        })

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _table_exists(self, table_name: str) -> bool:
        row = self.fetchone(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        )
        return row is not None

    @staticmethod
    def _json_list(raw: Any) -> list[Any]:
        if raw is None or raw == "":
            return []
        if isinstance(raw, list):
            return raw
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        return data if isinstance(data, list) else []

    @staticmethod
    def _json_dict(raw: Any) -> dict[str, Any]:
        if raw is None or raw == "":
            return {}
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _status_from_legacy(status: str) -> str:
        value = (status or "").strip().lower()
        if value == "active":
            return ClaimStatus.ACTIVE
        if value == "superseded":
            return ClaimStatus.SUPERSEDED
        if value == "rejected":
            return ClaimStatus.REJECTED
        if value == "shadow":
            return ClaimStatus.SHADOW
        return ClaimStatus.CANDIDATE

    @staticmethod
    def _render_value(predicate: str, obj: str) -> str:
        predicate = (predicate or "").strip()
        obj = (obj or "").strip()
        if predicate and obj:
            return f"{predicate}{obj}"
        return predicate or obj

    @staticmethod
    def _fact_type_from_predicate(predicate: str) -> str:
        pred = predicate or ""
        if pred == "别名":
            return ClaimType.ALIAS
        if any(p in pred for p in ("住", "来自", "工作", "就读", "职业", "专业", "学校", "公司", "是")):
            return ClaimType.IDENTITY
        if any(p in pred for p in ("喜欢", "爱吃", "爱好", "兴趣", "讨厌", "擅长")):
            return ClaimType.PREFERENCE
        if any(p in pred for p in ("习惯", "常用")):
            return ClaimType.HABIT
        if any(p in pred for p in ("朋友", "同事", "同学", "室友", "关系", "认识")):
            return ClaimType.RELATIONSHIP
        if any(p in pred for p in ("最近", "正在", "计划", "准备")):
            return ClaimType.LONG_STATE
        return ClaimType.OTHER
