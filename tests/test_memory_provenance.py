"""Tests for the evidence-first memory provenance ledger."""

from __future__ import annotations

import json
import sqlite3

from sirius_pulse.memory.provenance import (
    ClaimAttribution,
    ClaimStatus,
    ClaimType,
    Evidence,
    ExtractionRun,
    MemoryClaim,
    ProvenanceStore,
)
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.memory.user.unified_models import UnifiedUser


def test_evidence_when_same_source_snapshot_saved_then_deduplicates(tmp_path):
    store = ProvenanceStore(tmp_path / "persona.db")

    first = store.save_evidence(Evidence(
        group_id="g1",
        message_id="m1",
        speaker_user_id="u1",
        content_quote="我喜欢猫",
    ))
    second = store.save_evidence(Evidence(
        group_id="g1",
        message_id="m1",
        speaker_user_id="u1",
        content_quote="我喜欢猫",
    ))

    assert first.evidence_id == second.evidence_id
    assert store.stats()["total_evidence"] == 1


def test_claim_provenance_when_claim_has_evidence_and_run_then_returns_full_chain(tmp_path):
    store = ProvenanceStore(tmp_path / "persona.db")
    evidence = store.save_evidence(Evidence(
        group_id="g1",
        message_id="m1",
        speaker_user_id="u1",
        speaker_name="Alice",
        content_quote="我住深圳",
    ))
    run = store.save_run(ExtractionRun(
        task="user_fact_extract",
        model="test-model",
        input_evidence_ids=[evidence.evidence_id],
    ))
    claim = store.save_claim(MemoryClaim(
        subject_user_id="u1",
        subject_label="Alice",
        fact_type=ClaimType.IDENTITY,
        value="住深圳",
        status=ClaimStatus.ACTIVE,
        attribution=ClaimAttribution.SELF_STATED,
        evidence_ids=[evidence.evidence_id],
        extraction_run_id=run.run_id,
    ))

    provenance = store.get_claim_provenance(claim.claim_id)

    assert provenance is not None
    assert provenance["claim"]["value"] == "住深圳"
    assert provenance["evidence"][0]["content_quote"] == "我住深圳"
    assert provenance["extraction_run"]["model"] == "test-model"
    assert store.get_active_profile_claims("u1")[0].profile_safe is True


def test_list_claims_when_filters_are_supplied_then_returns_matching_total(tmp_path):
    store = ProvenanceStore(tmp_path / "persona.db")
    store.save_claim(MemoryClaim(
        subject_user_id="u1",
        subject_label="Alice",
        fact_type=ClaimType.IDENTITY,
        value="lives in Shenzhen",
        status=ClaimStatus.ACTIVE,
        attribution=ClaimAttribution.SELF_STATED,
        confidence=0.8,
    ))
    store.save_claim(MemoryClaim(
        subject_user_id="u1",
        subject_label="Alice",
        fact_type=ClaimType.PREFERENCE,
        value="likes tea",
        status=ClaimStatus.CANDIDATE,
        attribution=ClaimAttribution.THIRD_PARTY_CLAIM,
        confidence=0.4,
    ))

    claims, total = store.list_claims(user_id="u1", status=ClaimStatus.ACTIVE)

    assert total == 1
    assert [c.value for c in claims] == ["lives in Shenzhen"]


def test_read_only_store_when_provenance_tables_do_not_exist_then_returns_empty_results(tmp_path):
    db_path = tmp_path / "persona.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    store = ProvenanceStore(db_path, read_only=True)

    assert store.stats()["total_claims"] == 0
    assert store.list_claims() == ([], 0)
    assert store.get_claim("missing") is None
    assert store.list_subject_user_ids() == []


def test_alias_registration_when_llm_then_manual_then_claim_is_upgraded(tmp_path):
    db_path = tmp_path / "persona.db"
    store = ProvenanceStore(db_path)
    manager = UnifiedUserManager(db_path=db_path, provenance_store=store)
    manager.register_user(UnifiedUser(user_id="u1", name="Alice"), group_id="g1")

    manager.register_alias("alicey", "u1", "Alice", "g1", source="llm_discovery")
    claim = store.find_claim_by_source_record("alias:alicey:u1")

    assert claim is not None
    assert claim.status == ClaimStatus.CANDIDATE
    assert claim.attribution == ClaimAttribution.INFERRED

    manager.register_alias("alicey", "u1", "Alice", "g1", source="manual")
    upgraded = store.find_claim_by_source_record("alias:alicey:u1")

    assert upgraded is not None
    assert upgraded.claim_id == claim.claim_id
    assert upgraded.status == ClaimStatus.ACTIVE
    assert upgraded.attribution == ClaimAttribution.MANUAL
    assert upgraded.profile_safe is True


def test_migration_when_legacy_tables_exist_then_creates_typed_claims_idempotently(tmp_path):
    db_path = tmp_path / "persona.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE evolution_records (
            record_id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            subject_user_id TEXT DEFAULT '',
            predicate TEXT NOT NULL,
            obj TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            confidence REAL NOT NULL DEFAULT 0.5,
            initial_confidence REAL NOT NULL DEFAULT 0.5,
            supersedes TEXT DEFAULT '[]',
            superseded_by TEXT,
            source_type TEXT NOT NULL DEFAULT 'stated',
            source_situation_id TEXT DEFAULT '',
            source_group_id TEXT DEFAULT '',
            source_message_ids TEXT DEFAULT '[]',
            extracted_at TEXT NOT NULL DEFAULT '',
            extracted_by_model TEXT DEFAULT '',
            verifications TEXT DEFAULT '[]',
            corrections TEXT DEFAULT '[]'
        );
        CREATE TABLE aliases (
            alias TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_name TEXT NOT NULL DEFAULT '',
            weight REAL DEFAULT 1.0,
            groups TEXT DEFAULT '[]',
            mentioned_count INTEGER DEFAULT 1,
            confidence REAL DEFAULT 0.5,
            first_seen_at TEXT DEFAULT '',
            last_seen_at TEXT DEFAULT '',
            source TEXT DEFAULT 'napcat',
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT '',
            PRIMARY KEY (alias, user_id)
        );
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            identity_anchors TEXT DEFAULT '[]',
            relationships TEXT DEFAULT '[]',
            short_bio TEXT DEFAULT ''
        );
    """)
    conn.execute(
        """
        INSERT INTO evolution_records (
            record_id, subject, subject_user_id, predicate, obj, status,
            confidence, source_group_id, source_message_ids, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "er1", "Alice", "u1", "住在", "深圳", "active", 0.7,
            "g1", json.dumps(["m1"]), "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO aliases (
            alias, user_id, user_name, groups, confidence, source, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("小爱", "u1", "Alice", json.dumps(["g1"]), 0.8, "manual", "active"),
    )
    conn.execute(
        """
        INSERT INTO users (user_id, name, identity_anchors, relationships, short_bio)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "u1",
            "Alice",
            json.dumps(["后端工程师"], ensure_ascii=False),
            json.dumps([{"relation": "朋友", "target_name": "Bob"}], ensure_ascii=False),
            "Alice 是后端工程师",
        ),
    )
    conn.commit()

    store = ProvenanceStore(conn=conn)
    first = store.migrate_from_legacy_tables()
    second = store.migrate_from_legacy_tables()

    assert first == {
        "evolution_records": 1,
        "aliases": 1,
        "user_profile_fields": 3,
    }
    assert second == {
        "evolution_records": 0,
        "aliases": 0,
        "user_profile_fields": 0,
    }
    active_claims = store.get_claims_for_user("u1", status=ClaimStatus.ACTIVE)
    values = {c.value for c in active_claims}
    assert {"住在深圳", "小爱", "后端工程师", "朋友"}.issubset(values)
    assert store.get_claim_provenance(
        store.find_claim_by_source_record("er1").claim_id
    )["evidence"][0]["message_id"] == "m1"
