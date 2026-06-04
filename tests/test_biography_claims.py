"""Biography projection from provenance claims."""

from __future__ import annotations

from sirius_pulse.memory.biography.view import BiographyView
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import EvolutionRecord, RecordStatus
from sirius_pulse.memory.provenance import (
    ClaimAttribution,
    ClaimStatus,
    ClaimType,
    MemoryClaim,
    ProvenanceStore,
)


def test_biography_when_profile_safe_claims_exist_then_uses_claim_projection(tmp_path):
    db_path = tmp_path / "persona.db"
    chain = EvolutionChain(db_path)
    provenance = ProvenanceStore(db_path)
    provenance.save_claim(MemoryClaim(
        subject_user_id="u1",
        subject_label="Alice",
        fact_type=ClaimType.IDENTITY,
        value="住深圳",
        status=ClaimStatus.ACTIVE,
        attribution=ClaimAttribution.SELF_STATED,
    ))
    provenance.save_claim(MemoryClaim(
        subject_user_id="u1",
        subject_label="Alice",
        fact_type=ClaimType.PREFERENCE,
        value="喜欢猫",
        status=ClaimStatus.ACTIVE,
        attribution=ClaimAttribution.THIRD_PARTY_CLAIM,
    ))

    bio = BiographyView(chain, provenance_store=provenance).get_biography("u1")

    assert bio.name == "Alice"
    assert bio.identity_anchors == ["住深圳"]
    assert "喜欢猫" not in bio.short_bio
    assert bio.source_claim_ids
    assert bio.source_record_ids == []


def test_biography_when_no_claims_then_falls_back_to_evolution_chain(tmp_path):
    db_path = tmp_path / "persona.db"
    chain = EvolutionChain(db_path)
    chain._persist_record(EvolutionRecord(
        subject="Alice",
        subject_user_id="u1",
        predicate="住在",
        obj="杭州",
        status=RecordStatus.ACTIVE,
    ))
    chain._store.commit()
    provenance = ProvenanceStore(db_path)

    bio = BiographyView(chain, provenance_store=provenance).get_biography("u1")

    assert bio.name == "Alice"
    assert bio.identity_anchors == ["住在杭州"]
    assert bio.source_record_ids
    assert bio.source_claim_ids == []
