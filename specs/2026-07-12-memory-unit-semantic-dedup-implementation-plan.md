# Memory Unit Semantic Deduplication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure semantically equivalent memory units converge to one canonical unit during live extraction and through a previewable WebUI cleanup job.

**Architecture:** A small deduplicator owns deterministic merging and LLM verdict validation; the existing indexer supplies boundary-scoped semantic candidates. `MemoryUnitManager` uses the same rules for live writes and delegates historical scan/apply to a maintenance helper. WebUI submits file-backed jobs to the running persona worker, which performs model calls and atomically refreshes persistence plus the in-memory index.

**Tech Stack:** Python 3.12, asyncio, dataclasses, aiohttp, existing embedding client and `Brain.raw_call`, JSON files, vanilla JavaScript, pytest, Node assert tests.

**Design reference:** `specs/2026-07-12-memory-unit-semantic-dedup-design.md`

**Plan location note:** The default `docs/superpowers/plans/` location is inside this repository's separately versioned `docs` submodule. This plan stays in the main repository under `specs/` beside the approved design so it can be committed without changing the submodule pointer.

---

## File Map

**Create**

- `sirius_pulse/memory/units/deduplicator.py`: normalization, boundary matching, verdict parsing, LLM adjudication, deterministic merge/conflict helpers.
- `sirius_pulse/memory/units/maintenance.py`: stable fingerprints, historical dry-run scan, backup, staged apply, rollback.
- `tests/test_memory_unit_deduplication.py`: pure rules, candidate and model verdict behavior.
- `tests/test_memory_dedupe_maintenance.py`: historical scan, stale detection, backup, rollback.
- `tests/test_memory_dedupe_jobs.py`: worker file-job lifecycle.
- `tests/js/memory-dedupe-ui.test.mjs`: WebUI button, modal states, apply and cleanup lifecycle.

**Modify**

- `sirius_pulse/memory/units/indexer.py`: boundary-scoped cosine candidates and group replacement.
- `sirius_pulse/memory/units/manager.py`: mutation lock, live reconcile, maintenance delegation and runtime refresh.
- `sirius_pulse/memory/units/store.py`: enumerate groups and atomically save prepared group sets.
- `sirius_pulse/memory/units/__init__.py`: export the dedupe types used by tests and integrations.
- `sirius_pulse/core/bg_tasks.py`: poll and execute scan/apply/reconcile requests.
- `sirius_pulse/webui/memory_api.py`: task endpoints, maintenance guard and CRUD reconcile signals.
- `sirius_pulse/webui/routes.py`: declare four dedupe routes.
- `sirius_pulse/webui/server.py`: import and delegate four dedupe handlers.
- `sirius_pulse/webui/static/pages/memory-viz.js`: cleanup button, report modal, polling and apply flow.
- `tests/test_memory_units.py`: live generation regression coverage.
- `tests/test_webui_memory_crud.py`: memory-unit CRUD consistency and dedupe API coverage.
- `tests/test_webui_routes.py`: route/handler registration coverage.
- `docs/guide/memory-system.md`: user-facing cleanup workflow in the docs submodule.

---

### Task 1: Deterministic Dedupe Rules

**Files:**

- Create: `sirius_pulse/memory/units/deduplicator.py`
- Create: `tests/test_memory_unit_deduplication.py`
- Modify: `sirius_pulse/memory/units/__init__.py`

- [ ] **Step 1: Write failing normalization, merge and conflict tests**

Create `tests/test_memory_unit_deduplication.py` with these first tests:

```python
from sirius_pulse.memory.units import MemoryUnit
from sirius_pulse.memory.units.deduplicator import (
    DedupVerdict,
    link_conflict,
    merge_memory_units,
    normalize_summary,
    same_boundary,
)


def _unit(unit_id: str, summary: str, **changes) -> MemoryUnit:
    values = {
        "unit_id": unit_id,
        "group_id": "group-a",
        "created_at": "2026-07-12T00:00:00+00:00",
        "unit_type": "preference",
        "scope": "user",
        "scope_id": "alice",
        "summary": summary,
        "participants": ["alice"],
        "topics": ["reply-style"],
        "keywords": ["concise"],
        "salience": 0.6,
        "confidence": 0.7,
        "lifespan": "medium",
        "source_ids": ["src-1"],
        "embedding": [1.0, 0.0],
    }
    values.update(changes)
    return MemoryUnit(**values)


def test_normalized_equal_summary_is_duplicate_only_inside_same_boundary():
    old = _unit("mem-old", "Alice prefers concise replies。")
    same = _unit("mem-new", "  alice prefers concise replies!  ")
    other_group = _unit("mem-other", same.summary, group_id="group-b")

    assert normalize_summary(old.summary) == normalize_summary(same.summary)
    assert same_boundary(old, same) is True
    assert same_boundary(old, other_group) is False


def test_merge_keeps_canonical_identity_and_all_sources():
    old = _unit("mem-old", "Alice prefers concise replies.")
    new = _unit(
        "mem-new",
        "Alice prefers concise replies with examples.",
        created_at="2026-07-12T01:00:00+00:00",
        participants=["alice", "sirius"],
        topics=["reply-style", "examples"],
        keywords=["examples"],
        salience=0.9,
        confidence=0.8,
        lifespan="long",
        source_ids=["src-2"],
    )
    merged = merge_memory_units(
        old,
        new,
        DedupVerdict("MERGE", "mem-old", "Alice prefers concise replies with examples.", "补充"),
        now_iso="2026-07-12T02:00:00+00:00",
    )

    assert merged.unit_id == "mem-old"
    assert merged.created_at == old.created_at
    assert merged.source_ids == ["src-1", "src-2"]
    assert merged.participants == ["alice", "sirius"]
    assert merged.topics == ["reply-style", "examples"]
    assert merged.keywords == ["concise", "examples"]
    assert merged.salience == 0.9
    assert merged.confidence == 0.8
    assert merged.lifespan == "long"
    assert merged.embedding is None
    assert merged.metadata["merged_unit_ids"] == ["mem-new"]
    assert merged.metadata["revision_count"] == 1


def test_conflict_keeps_both_units_and_links_them():
    old = _unit("mem-old", "Alice prefers concise replies.")
    new = _unit("mem-new", "Alice now prefers detailed explanations.")

    linked_old, linked_new = link_conflict(old, new, "偏好发生变化")

    assert linked_old.metadata["conflicts_with"] == ["mem-new"]
    assert linked_new.metadata["conflicts_with"] == ["mem-old"]
    assert linked_old.metadata["conflict_reason"] == "偏好发生变化"
```

- [ ] **Step 2: Run the tests and verify import failure**

Run:

```powershell
python -m pytest tests/test_memory_unit_deduplication.py -q
```

Expected: FAIL during collection because `deduplicator.py` and its exports do not exist.

- [ ] **Step 3: Implement the pure rules**

Create `sirius_pulse/memory/units/deduplicator.py` with this pure rule layer. Later tasks add the LLM
methods to the same file:

```python
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sirius_pulse.memory.units.models import MemoryUnit

_END_PUNCTUATION = "。！？.!?"
_LIFESPAN_RANK = {"short": 0, "medium": 1, "long": 2}


@dataclass(slots=True, frozen=True)
class DedupVerdict:
    decision: str
    target_unit_id: str = ""
    merged_summary: str = ""
    reason: str = ""


def _clone(unit: MemoryUnit) -> MemoryUnit:
    return MemoryUnit.from_dict(unit.to_dict())


def _union(left: list[str], right: list[str], limit: int | None = None) -> list[str]:
    result: list[str] = []
    for value in [*left, *right]:
        if value and value not in result:
            result.append(value)
        if limit is not None and len(result) >= limit:
            break
    return result


def normalize_summary(summary: str) -> str:
    normalized = unicodedata.normalize("NFKC", summary).casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.rstrip(_END_PUNCTUATION).rstrip()


def same_boundary(left: MemoryUnit, right: MemoryUnit) -> bool:
    return (
        left.group_id,
        left.scope,
        left.scope_id,
        left.unit_type,
    ) == (
        right.group_id,
        right.scope,
        right.scope_id,
        right.unit_type,
    )


def _index_text_fields(unit: MemoryUnit) -> tuple[object, ...]:
    return (unit.summary, unit.participants, unit.topics, unit.keywords)


def merge_memory_units(
    canonical: MemoryUnit,
    incoming: MemoryUnit,
    verdict: DedupVerdict,
    *,
    now_iso: str,
) -> MemoryUnit:
    if verdict.decision not in {"DUPLICATE", "MERGE"}:
        raise ValueError("merge requires DUPLICATE or MERGE")
    if not same_boundary(canonical, incoming):
        raise ValueError("cannot merge memory units across boundaries")
    merged = _clone(canonical)
    before = _index_text_fields(merged)
    if verdict.decision == "MERGE":
        summary = verdict.merged_summary.strip()
        if not summary or len(summary) > 180:
            raise ValueError("invalid merged summary")
        merged.summary = summary
    merged.created_at = min(canonical.created_at, incoming.created_at)
    merged.source_ids = _union(canonical.source_ids, incoming.source_ids)
    merged.participants = _union(canonical.participants, incoming.participants, 8)
    merged.topics = _union(canonical.topics, incoming.topics, 8)
    merged.keywords = _union(canonical.keywords, incoming.keywords, 12)
    merged.salience = max(canonical.salience, incoming.salience)
    merged.confidence = max(canonical.confidence, incoming.confidence)
    merged.lifespan = max(
        (canonical.lifespan, incoming.lifespan),
        key=lambda value: _LIFESPAN_RANK.get(value, 1),
    )
    merged.should_prompt = canonical.should_prompt or incoming.should_prompt
    metadata = dict(canonical.metadata)
    metadata["revision_count"] = int(metadata.get("revision_count", 0)) + 1
    metadata["merged_unit_ids"] = _union(
        list(metadata.get("merged_unit_ids") or []), [incoming.unit_id]
    )
    metadata["last_merged_at"] = now_iso
    metadata["decision"] = verdict.decision.lower()
    merged.metadata = metadata
    if _index_text_fields(merged) != before:
        merged.embedding = None
    return merged


def link_conflict(
    canonical: MemoryUnit,
    incoming: MemoryUnit,
    reason: str,
) -> tuple[MemoryUnit, MemoryUnit]:
    left, right = _clone(canonical), _clone(incoming)
    left.metadata = dict(left.metadata)
    right.metadata = dict(right.metadata)
    left.metadata["conflicts_with"] = _union(
        list(left.metadata.get("conflicts_with") or []), [right.unit_id]
    )
    right.metadata["conflicts_with"] = _union(
        list(right.metadata.get("conflicts_with") or []), [left.unit_id]
    )
    left.metadata["conflict_reason"] = reason
    right.metadata["conflict_reason"] = reason
    return left, right


def apply_verdict(
    units: list[MemoryUnit],
    incoming: MemoryUnit,
    verdict: DedupVerdict,
    *,
    now_iso: str,
) -> tuple[list[MemoryUnit], MemoryUnit]:
    working = [_clone(unit) for unit in units]
    if verdict.decision == "NEW":
        accepted = _clone(incoming)
        working.append(accepted)
        return working, accepted
    target_index = next(
        (index for index, unit in enumerate(working) if unit.unit_id == verdict.target_unit_id),
        -1,
    )
    if target_index < 0:
        accepted = _clone(incoming)
        working.append(accepted)
        return working, accepted
    if verdict.decision == "CONFLICT":
        linked_target, accepted = link_conflict(
            working[target_index], incoming, verdict.reason
        )
        working[target_index] = linked_target
        working.append(accepted)
        return working, accepted
    accepted = merge_memory_units(
        working[target_index], incoming, verdict, now_iso=now_iso
    )
    working[target_index] = accepted
    return working, accepted
```

Export `DedupVerdict` from `sirius_pulse/memory/units/__init__.py`.

- [ ] **Step 4: Run the focused tests**

Run:

```powershell
python -m pytest tests/test_memory_unit_deduplication.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit the pure rule layer**

```powershell
git add sirius_pulse/memory/units/deduplicator.py sirius_pulse/memory/units/__init__.py tests/test_memory_unit_deduplication.py
git commit -m "feat(memory): 增加记忆单元去重规则"
```

---

### Task 2: Boundary-Scoped Semantic Candidates

**Files:**

- Modify: `sirius_pulse/memory/units/indexer.py`
- Modify: `tests/test_memory_unit_deduplication.py`

- [ ] **Step 1: Add failing semantic-candidate tests**

Append a fake embedding client and a test that adds a same-boundary match, a cross-group match and a
different-type match. Assert only the same-boundary unit is returned:

```python
class _Embedding:
    available = True

    def encode_single(self, text: str) -> list[float]:
        return [1.0, 0.0] if "concise" in text.lower() else [0.0, 1.0]


def test_indexer_returns_only_same_boundary_semantic_candidates():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer(_Embedding())
    match = _unit("mem-match", "Alice prefers concise replies.")
    indexer.add(match)
    indexer.add(_unit("mem-group", match.summary, group_id="group-b"))
    indexer.add(_unit("mem-type", match.summary, unit_type="note"))

    incoming = _unit("mem-new", "Alice likes concise answers.", embedding=None)
    candidates = indexer.semantic_candidates(incoming, top_k=5, min_similarity=0.80)

    assert [(unit.unit_id, score) for unit, score in candidates] == [("mem-match", 1.0)]


def test_indexer_replace_group_removes_stale_units():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer()
    indexer.add(_unit("old", "old"))
    replacement = _unit("new", "new")
    indexer.replace_group("group-a", [replacement])
    assert indexer.list_all() == [replacement]
```

- [ ] **Step 2: Run the new tests and verify missing methods**

```powershell
python -m pytest tests/test_memory_unit_deduplication.py -q
```

Expected: FAIL because `semantic_candidates` and `replace_group` are missing.

- [ ] **Step 3: Add minimal indexer methods**

Add an internal `_ensure_embedding(unit)` helper reused by `add()` and `semantic_candidates()`.
`semantic_candidates()` must filter with `same_boundary`, exclude the incoming `unit_id`, use raw
cosine only, apply the threshold, sort descending and slice `top_k`. Add:

```python
def replace_group(self, group_id: str, units: list[MemoryUnit]) -> None:
    self.clear_group(group_id)
    for unit in units:
        self.add(unit)
```

- [ ] **Step 4: Verify the indexer tests**

```powershell
python -m pytest tests/test_memory_unit_deduplication.py tests/test_memory_units.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit semantic candidate support**

```powershell
git add sirius_pulse/memory/units/indexer.py tests/test_memory_unit_deduplication.py
git commit -m "feat(memory): 支持记忆单元语义候选召回"
```

---

### Task 3: Strict LLM Adjudication

**Files:**

- Modify: `sirius_pulse/memory/units/deduplicator.py`
- Modify: `tests/test_memory_unit_deduplication.py`

- [ ] **Step 1: Write failing verdict tests**

Add `json`, `pytest`, and `MemoryUnitDeduplicator` imports to the test module. Use a fake brain that
records `RawRequest` and returns configurable JSON. Cover a valid merge, an
unknown target ID, an overlong merge summary and a raised exception. The latter three must produce
`DedupVerdict("NEW")`:

```python
class _Brain:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    async def raw_call(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return json.dumps(self.response)


@pytest.mark.asyncio
async def test_adjudicator_accepts_only_valid_candidate_target():
    brain = _Brain({
        "decision": "MERGE",
        "target_unit_id": "mem-old",
        "merged_summary": "Alice prefers concise replies with examples.",
        "reason": "兼容补充",
    })
    adjudicator = MemoryUnitDeduplicator()
    verdict = await adjudicator.adjudicate(
        _unit("mem-new", "Alice likes short answers with examples."),
        [_unit("mem-old", "Alice prefers concise replies.")],
        brain=brain,
        model_name="memory-model",
    )
    assert verdict.decision == "MERGE"
    assert brain.requests[0].purpose == "memory_unit_deduplicate"
    assert brain.requests[0].response_format == {"type": "json_object"}
```

- [ ] **Step 2: Run and verify the missing adjudicator**

```powershell
python -m pytest tests/test_memory_unit_deduplication.py -q
```

Expected: FAIL because `MemoryUnitDeduplicator` is not defined.

- [ ] **Step 3: Implement the adjudicator**

Add `json`, `logging`, `TYPE_CHECKING`, and `Any` imports, a module logger, the approved
decision definitions as `_DEDUP_SYSTEM_PROMPT`, and this class:

```python
if TYPE_CHECKING:
    from sirius_pulse.memory.units.indexer import MemoryUnitIndexer
```

Keep the indexer import type-only because `indexer.py` imports `same_boundary()` from this module.

```python
class MemoryUnitDeduplicator:
    async def adjudicate(
        self,
        incoming: MemoryUnit,
        candidates: list[MemoryUnit],
        *,
        brain: Any,
        model_name: str,
    ) -> DedupVerdict:
        from sirius_pulse.core.brain import RawRequest

        payload = {
            "incoming": {k: v for k, v in incoming.to_dict().items() if k != "embedding"},
            "candidates": [
                {k: v for k, v in unit.to_dict().items() if k != "embedding"}
                for unit in candidates
            ],
        }
        request = RawRequest(
            model=model_name,
            system_prompt=_DEDUP_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
            ],
            temperature=0.0,
            max_tokens=512,
            purpose="memory_unit_deduplicate",
            response_format={"type": "json_object"},
        )
        try:
            parsed = json.loads((await brain.raw_call(request)).strip())
            decision = str(parsed.get("decision") or "").upper()
            target_id = str(parsed.get("target_unit_id") or "")
            summary = str(parsed.get("merged_summary") or "").strip()
            reason = str(parsed.get("reason") or "").strip()[:200]
            candidate_ids = {unit.unit_id for unit in candidates}
            if decision not in {"NEW", "DUPLICATE", "MERGE", "CONFLICT"}:
                raise ValueError("invalid decision")
            if decision != "NEW" and target_id not in candidate_ids:
                raise ValueError("invalid target")
            if decision == "MERGE" and (not summary or len(summary) > 180):
                raise ValueError("invalid merged summary")
            return DedupVerdict(decision, target_id, summary, reason)
        except Exception as exc:
            logger.warning("Memory unit dedupe adjudication failed: %s", exc)
            return DedupVerdict("NEW")

    async def decide(
        self,
        incoming: MemoryUnit,
        existing: list[MemoryUnit],
        indexer: "MemoryUnitIndexer",
        *,
        brain: Any,
        model_name: str,
    ) -> DedupVerdict:
        normalized = normalize_summary(incoming.summary)
        exact = next(
            (
                unit
                for unit in existing
                if same_boundary(unit, incoming)
                and normalize_summary(unit.summary) == normalized
            ),
            None,
        )
        if exact is not None:
            return DedupVerdict("DUPLICATE", exact.unit_id, reason="normalized exact match")
        candidates = [
            unit
            for unit, _score in indexer.semantic_candidates(
                incoming, top_k=5, min_similarity=0.80
            )
        ]
        if not candidates:
            return DedupVerdict("NEW")
        return await self.adjudicate(
            incoming, candidates, brain=brain, model_name=model_name
        )
```

The `_DEDUP_SYSTEM_PROMPT` must contain the exact NEW/DUPLICATE/MERGE/CONFLICT meanings and the five
forbidden-merge cases from design sections 7.1 through 7.5, followed by the existing system-prompt
confidentiality instruction required by `CLAUDE.md`.

- [ ] **Step 4: Run adjudicator and existing generator tests**

```powershell
python -m pytest tests/test_memory_unit_deduplication.py tests/test_memory_units.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit model adjudication**

```powershell
git add sirius_pulse/memory/units/deduplicator.py tests/test_memory_unit_deduplication.py
git commit -m "feat(memory): 增加重复记忆模型裁决"
```

---

### Task 4: Live Reconciliation in MemoryUnitManager

**Files:**

- Modify: `sirius_pulse/memory/units/manager.py`
- Modify: `tests/test_memory_units.py`

- [ ] **Step 1: Add failing business tests for duplicate, merge and conflict generation**

Extend the fake brain so its first response serves extraction and the second serves adjudication. Add
three async tests. The duplicate test must assert one persisted ID and both source IDs; the conflict
test must assert two persisted units with reciprocal metadata; a cross-group test must assert two
independent files.

Use this observable duplicate assertion:

```python
units = manager.get_units_for_group("group_a")
assert len(units) == 1
assert units[0].unit_id == "mem-existing"
assert units[0].source_ids == ["src-existing", new_entry.entry_id]
assert manager.is_source_checkpointed("group_a", new_entry.entry_id) is True
```

- [ ] **Step 2: Run the manager tests and verify duplicate persistence**

```powershell
python -m pytest tests/test_memory_units.py -q
```

Expected: FAIL because a second UUID-backed unit is persisted.

- [ ] **Step 3: Add the manager mutation lock and reconcile flow**

In `__init__`, add one `asyncio.Lock`, one `MemoryUnitDeduplicator`, and retain the embedding client for
temporary historical indexes. Add:

```python
async def reconcile_units(
    self,
    group_id: str,
    units: list[MemoryUnit],
    *,
    brain: Any,
    model_name: str,
) -> list[MemoryUnit]:
    if not units:
        return []
    async with self._mutation_lock:
        self.ensure_group_loaded(group_id)
        existing = self._store.load(group_id)
        accepted: dict[str, MemoryUnit] = {}
        for incoming in units:
            verdict = await self._deduplicator.decide(
                incoming,
                existing,
                self._indexer,
                brain=brain,
                model_name=model_name,
            )
            existing, result = apply_verdict(
                existing,
                incoming,
                verdict,
                now_iso=datetime.now(timezone.utc).isoformat(),
            )
            accepted[result.unit_id] = result
            self._indexer.replace_group(group_id, existing)
        self._store.save(group_id, existing)
        self._replace_loaded_group(group_id, existing)
        return list(accepted.values())

def _replace_loaded_group(self, group_id: str, units: list[MemoryUnit]) -> None:
    self._indexer.replace_group(group_id, units)
    self._checkpointed_sources[group_id] = {
        source_id for unit in units for source_id in unit.source_ids
    }


async def reconcile_persisted_units(
    self,
    group_ids: list[str],
    unit_ids: list[str],
    *,
    brain: Any,
    model_name: str,
) -> None:
    selected_ids = set(unit_ids)
    async with self._mutation_lock:
        for group_id in sorted(set(group_ids)):
            loaded = self._store.load(group_id)
            incoming = [unit for unit in loaded if unit.unit_id in selected_ids]
            working = [unit for unit in loaded if unit.unit_id not in selected_ids]
            self._indexer.replace_group(group_id, working)
            for unit in sorted(incoming, key=lambda item: (item.created_at, item.unit_id)):
                verdict = await self._deduplicator.decide(
                    unit,
                    working,
                    self._indexer,
                    brain=brain,
                    model_name=model_name,
                )
                working, _accepted = apply_verdict(
                    working,
                    unit,
                    verdict,
                    now_iso=datetime.now(timezone.utc).isoformat(),
                )
                self._indexer.replace_group(group_id, working)
            self._store.save(group_id, working)
            self._replace_loaded_group(group_id, working)
```

Within the lock, process incoming units sequentially against the live index. For `NEW`, append and add;
for `DUPLICATE`/`MERGE`, replace the target with `merge_memory_units`; for `CONFLICT`, replace the
linked target and append the linked incoming unit. Save once and call `_replace_loaded_group` once.

Change `generate_from_candidates()` to call
`canonical_results = await self.reconcile_units(group_id, result.units, brain=brain, model_name=model_name)`
and return `MemoryUnitGenerationResult(units=canonical_results)` so the checkpoint caller sees merged
source IDs.
Keep `add_units()` as a non-semantic import/bootstrap path for existing callers.

- [ ] **Step 4: Run memory unit and checkpoint-context regressions**

```powershell
python -m pytest tests/test_memory_units.py tests/test_prompt_factory.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit live reconciliation**

```powershell
git add sirius_pulse/memory/units/manager.py tests/test_memory_units.py
git commit -m "feat(memory): 实时合并重复记忆单元"
```

---

### Task 5: Historical Scan, Fingerprint, Backup and Apply

**Files:**

- Create: `sirius_pulse/memory/units/maintenance.py`
- Create: `tests/test_memory_dedupe_maintenance.py`
- Modify: `sirius_pulse/memory/units/store.py`
- Modify: `sirius_pulse/memory/units/manager.py`

- [ ] **Step 1: Write failing dry-run, apply and stale-report tests**

Create fixtures with three equivalent units in one group and one unit in another. Tests must assert:

```python
report = await manager.scan_duplicates(brain=brain, model_name="memory-model")
assert len(manager.get_units_for_group("group-a")) == 3
assert report["summary"]["exact_duplicate"] == 2
assert len(report["groups"]["group-a"]["final_units"]) == 1

result = await manager.apply_duplicate_report(report)
assert result["status"] == "completed"
assert len(manager.get_units_for_group("group-a")) == 1
assert list((tmp_path / "backups" / "memory_units").glob("*/group-a.json"))
```

For stale detection, save a fourth unit after scan and assert apply returns `{"status": "stale"}` with
no changed group files. For rollback, monkeypatch the staged replace helper to fail on the second file
and assert every original file is restored.

- [ ] **Step 2: Run and verify missing maintenance APIs**

```powershell
python -m pytest tests/test_memory_dedupe_maintenance.py -q
```

Expected: FAIL because scan/apply methods do not exist.

- [ ] **Step 3: Implement maintenance and storage primitives**

Add to `MemoryUnitFileStore`:

```python
@property
def base_dir(self) -> Path:
    return self._base_dir


def list_group_ids(self) -> list[str]:
    result: set[str] = set()
    for path in self._base_dir.glob("*.json"):
        units = self.load(path.stem)
        if units:
            result.add(units[0].group_id)
    return sorted(result)


def save_many_atomically(self, groups: dict[str, list[MemoryUnit]]) -> None:
    stage_dir = self._base_dir.parent / f".memory_units_stage_{uuid.uuid4().hex}"
    stage_dir.mkdir(parents=True)
    try:
        staged: dict[str, Path] = {}
        for group_id, units in groups.items():
            path = stage_dir / f"{self._safe_name(group_id)}.json"
            atomic_write_json(
                path,
                {"group_id": group_id, "units": [unit.to_dict() for unit in units]},
            )
            staged[group_id] = path
        self._base_dir.mkdir(parents=True, exist_ok=True)
        for group_id, path in staged.items():
            path.replace(self._path(group_id))
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
```

`save_many_atomically` writes every payload to a sibling temporary directory before replacing any
official group file. Cleanup of its own temporary directory belongs in `finally`.

Create `MemoryUnitDedupeMaintenance` with the imports and implementation shape below:

```python
import hashlib
import json
import shutil
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.units.deduplicator import (
    MemoryUnitDeduplicator,
    apply_verdict,
)
from sirius_pulse.memory.units.indexer import MemoryUnitIndexer
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.memory.units.store import MemoryUnitFileStore

if TYPE_CHECKING:
    from sirius_pulse.memory.units.manager import MemoryUnitManager


class MemoryUnitDedupeMaintenance:
    def __init__(
        self,
        manager: "MemoryUnitManager",
        store: MemoryUnitFileStore,
        embedding_client: EmbeddingClient | None,
        deduplicator: MemoryUnitDeduplicator,
    ) -> None:
        self._manager = manager
        self._store = store
        self._embedding_client = embedding_client
        self._deduplicator = deduplicator

    @staticmethod
    def fingerprint(units: list[MemoryUnit]) -> str:
        payload = json.dumps(
            [unit.to_dict() for unit in units],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def scan(
        self,
        *,
        brain: Any,
        model_name: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        group_ids = self._store.list_group_ids()
        total = sum(len(self._store.load(group_id)) for group_id in group_ids)
        processed = 0
        summary = {
            "new": 0,
            "exact_duplicate": 0,
            "duplicate": 0,
            "merge": 0,
            "conflict": 0,
        }
        groups: dict[str, Any] = {}
        for group_id in group_ids:
            originals = self._store.load(group_id)
            working: list[MemoryUnit] = []
            indexer = MemoryUnitIndexer(self._embedding_client)
            operations: list[dict[str, Any]] = []
            for incoming in sorted(originals, key=lambda unit: (unit.created_at, unit.unit_id)):
                verdict = await self._deduplicator.decide(
                    incoming,
                    working,
                    indexer,
                    brain=brain,
                    model_name=model_name,
                )
                target = next(
                    (unit for unit in working if unit.unit_id == verdict.target_unit_id),
                    None,
                )
                working, accepted = apply_verdict(
                    working,
                    incoming,
                    verdict,
                    now_iso=datetime.now(timezone.utc).isoformat(),
                )
                indexer.replace_group(group_id, working)
                summary_key = (
                    "exact_duplicate"
                    if verdict.decision == "DUPLICATE"
                    and verdict.reason == "normalized exact match"
                    else verdict.decision.lower()
                )
                summary[summary_key] += 1
                operations.append(
                    {
                        "incoming_unit_id": incoming.unit_id,
                        "incoming_summary": incoming.summary,
                        "target_unit_id": verdict.target_unit_id,
                        "target_summary": target.summary if target is not None else "",
                        "result_unit_id": accepted.unit_id,
                        "result_summary": accepted.summary,
                        "decision": verdict.decision,
                        "reason": verdict.reason,
                    }
                )
                processed += 1
                if progress is not None:
                    progress(processed, total)
            groups[group_id] = {
                "fingerprint": self.fingerprint(originals),
                "operations": operations,
                "final_units": [unit.to_dict() for unit in working],
            }
        return {"summary": summary, "groups": groups, "total": total}

    async def apply(self, report: dict[str, Any]) -> dict[str, Any]:
        report_groups = dict(report.get("groups") or {})
        current = {
            group_id: self._store.load(group_id) for group_id in report_groups
        }
        if any(
            self.fingerprint(current[group_id]) != group_report.get("fingerprint")
            for group_id, group_report in report_groups.items()
        ):
            return {"status": "stale"}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self._store.base_dir.parent / "backups" / "memory_units" / stamp
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        if self._store.base_dir.exists():
            shutil.copytree(self._store.base_dir, backup_dir)
        else:
            backup_dir.mkdir()
        prepared = {
            group_id: [
                MemoryUnit.from_dict(item)
                for item in group_report.get("final_units", [])
            ]
            for group_id, group_report in report_groups.items()
        }
        try:
            self._store.save_many_atomically(prepared)
            for group_id, units in prepared.items():
                self._manager._replace_loaded_group(group_id, units)
        except Exception as exc:
            self._store.save_many_atomically(current)
            for group_id, units in current.items():
                self._manager._replace_loaded_group(group_id, units)
            return {"status": "failed", "error": str(exc), "backup": str(backup_dir)}
        return {"status": "completed", "backup": str(backup_dir)}
```

Scan each boundary bucket by `(created_at, unit_id)`, maintaining a temporary index and canonical list;
store operations plus final unit dictionaries in the report but do not write the store. Fingerprints
are SHA-256 over stable compact JSON of complete unit dictionaries. Apply validates every fingerprint,
backs up the full `memory_units` directory, writes all prepared groups, refreshes manager state, and
restores both files and the old index on any exception.

After constructing the deduplicator in `MemoryUnitManager.__init__`, construct maintenance with the
manager, store, embedding client and deduplicator. Add these exact delegates; apply executes under the
same mutation lock as live reconciliation:

```python
async def scan_duplicates(
    self,
    *,
    brain: Any,
    model_name: str,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    return await self._maintenance.scan(
        brain=brain,
        model_name=model_name,
        progress=progress,
    )


async def apply_duplicate_report(self, report: dict[str, Any]) -> dict[str, Any]:
    async with self._mutation_lock:
        return await self._maintenance.apply(report)
```

- [ ] **Step 4: Run maintenance and live memory tests**

```powershell
python -m pytest tests/test_memory_dedupe_maintenance.py tests/test_memory_units.py tests/test_memory_unit_deduplication.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit historical maintenance**

```powershell
git add sirius_pulse/memory/units/maintenance.py sirius_pulse/memory/units/store.py sirius_pulse/memory/units/manager.py tests/test_memory_dedupe_maintenance.py
git commit -m "feat(memory): 支持历史记忆扫描与回滚"
```

---

### Task 6: Persona Worker Job Execution

**Files:**

- Modify: `sirius_pulse/core/bg_tasks.py`
- Create: `tests/test_memory_dedupe_jobs.py`

- [ ] **Step 1: Write failing one-shot job lifecycle tests**

Test a factored `_process_memory_dedupe_request_once()` instead of the infinite polling loop. A fake
engine supplies `work_path`, manager, model router and brain. Cover scan success, apply stale, reconcile
coalescing and raised scan failure. Assert atomic status values and report location:

```python
await tasks._process_memory_dedupe_request_once()
status = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
assert status["job_id"] == "job-1"
assert status["status"] == "ready"
assert Path(status["report_path"]).name == "job-1.json"
```

- [ ] **Step 2: Run and verify the missing job processor**

```powershell
python -m pytest tests/test_memory_dedupe_jobs.py -q
```

Expected: FAIL because `_process_memory_dedupe_request_once` is missing.

- [ ] **Step 3: Add the worker loop and one-shot processor**

Add a fourth task in `BackgroundTasks.start()`:

```python
asyncio.create_task(self._memory_dedupe_job_worker(), name="memory_dedupe")
```

Implement the loop and one-shot processor below, importing `json`, `Path` and `atomic_write_json`:

```python
async def _memory_dedupe_job_worker(self) -> None:
    while self._engine._bg_running:
        try:
            await self._process_memory_dedupe_request_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Memory dedupe job worker failed: %s", exc)
        await asyncio.sleep(1)


async def _process_memory_dedupe_request_once(self) -> None:
    engine = self._engine
    job_dir = Path(engine.work_path) / "engine_state" / "memory_dedupe"
    job_dir.mkdir(parents=True, exist_ok=True)
    request_path = job_dir / "request.json"
    if request_path.exists():
        claimed = job_dir / "request.processing.json"
        try:
            request_path.replace(claimed)
        except FileNotFoundError:
            claimed = None
        if claimed is not None:
            try:
                request = json.loads(claimed.read_text(encoding="utf-8"))
                job_id = str(request.get("job_id") or "")
                action = str(request.get("action") or "")
                status_path = job_dir / "status.json"
                report_path = (
                    Path(engine.work_path) / "logs" / "memory-dedupe" / f"{job_id}.json"
                )
                cfg = engine.model_router.resolve("memory_extract")
                if action == "scan":
                    atomic_write_json(
                        status_path,
                        {"job_id": job_id, "status": "scanning", "progress": 0},
                    )

                    def update_progress(done: int, total: int) -> None:
                        atomic_write_json(
                            status_path,
                            {
                                "job_id": job_id,
                                "status": "scanning",
                                "progress": int(done * 100 / total) if total else 100,
                            },
                        )

                    report = await engine.memory_unit_manager.scan_duplicates(
                        brain=engine.brain,
                        model_name=cfg.model_name,
                        progress=update_progress,
                    )
                    report_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write_json(report_path, report)
                    atomic_write_json(
                        status_path,
                        {
                            "job_id": job_id,
                            "status": "ready",
                            "progress": 100,
                            "report_path": str(report_path),
                        },
                    )
                elif action == "apply":
                    atomic_write_json(
                        status_path,
                        {"job_id": job_id, "status": "applying", "progress": 0},
                    )
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    result = await engine.memory_unit_manager.apply_duplicate_report(report)
                    atomic_write_json(
                        status_path,
                        {
                            "job_id": job_id,
                            "status": result["status"],
                            "progress": 100,
                            "report_path": str(report_path),
                            **{key: value for key, value in result.items() if key != "status"},
                        },
                    )
                else:
                    raise ValueError(f"unknown memory dedupe action: {action}")
            except Exception as exc:
                atomic_write_json(
                    job_dir / "status.json",
                    {
                        "job_id": locals().get("job_id", ""),
                        "status": "failed",
                        "error": str(exc),
                    },
                )
            finally:
                claimed.unlink(missing_ok=True)

    reconcile_path = job_dir / "reconcile.json"
    if not reconcile_path.exists():
        return
    claimed_reconcile = job_dir / "reconcile.processing.json"
    try:
        reconcile_path.replace(claimed_reconcile)
    except FileNotFoundError:
        return
    try:
        payload = json.loads(claimed_reconcile.read_text(encoding="utf-8"))
        cfg = engine.model_router.resolve("memory_extract")
        await engine.memory_unit_manager.reconcile_persisted_units(
            [str(value) for value in payload.get("group_ids", [])],
            [str(value) for value in payload.get("unit_ids", [])],
            brain=engine.brain,
            model_name=cfg.model_name,
        )
    finally:
        claimed_reconcile.unlink(missing_ok=True)
```

Never let reconcile write the historical job status. Add a regression where a reconcile request exists
beside a ready historical report and assert `status.json` remains `ready`.

- [ ] **Step 4: Run job and background task regressions**

```powershell
python -m pytest tests/test_memory_dedupe_jobs.py tests/test_memory_units.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit worker execution**

```powershell
git add sirius_pulse/core/bg_tasks.py tests/test_memory_dedupe_jobs.py
git commit -m "feat(memory): 执行记忆去重后台任务"
```

---

### Task 7: WebUI Job API and CRUD Coordination

**Files:**

- Modify: `sirius_pulse/webui/memory_api.py`
- Modify: `sirius_pulse/webui/routes.py`
- Modify: `sirius_pulse/webui/server.py`
- Modify: `tests/test_webui_memory_crud.py`
- Modify: `tests/test_webui_routes.py`

- [ ] **Step 1: Write failing API lifecycle tests**

Add tests for worker-offline scan `409`, running-worker scan `202`, duplicate scan `409`, status/report
reads, apply request validation, apply-time CRUD `409`, exact duplicate POST collapse, and reconcile file
coalescing after POST/PUT/DELETE. Use a current process PID in `worker_status.json` for the running case.

The scan assertion must be:

```python
response = await api_persona_memory_dedupe_scan(_request(), tmp_path)
assert response.status == 202
payload = _payload(response)
request_data = json.loads(
    (tmp_path / "engine_state" / "memory_dedupe" / "request.json").read_text("utf-8")
)
assert request_data == {"action": "scan", "job_id": payload["job_id"]}
```

- [ ] **Step 2: Run API and route tests to verify missing handlers**

```powershell
python -m pytest tests/test_webui_memory_crud.py tests/test_webui_routes.py -q
```

Expected: FAIL during import or route assertions because the four handlers are absent.

- [ ] **Step 3: Implement API helpers and handlers**

Import `_is_persona_running` from `persona_manager_api`, then add these helpers and handlers to
`memory_api.py`:

```python
_ACTIVE_DEDUPE_STATES = {"queued", "scanning", "ready", "applying"}


def _memory_dedupe_dir(data_dir: Path) -> Path:
    return data_dir / "engine_state" / "memory_dedupe"


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dedupe_status(data_dir: Path) -> dict[str, Any]:
    return _read_json_dict(_memory_dedupe_dir(data_dir) / "status.json")


def _memory_units_applying(data_dir: Path) -> bool:
    return _dedupe_status(data_dir).get("status") == "applying"


def _queue_memory_reconcile(
    data_dir: Path,
    *,
    group_ids: list[str],
    unit_ids: list[str],
) -> None:
    path = _memory_dedupe_dir(data_dir) / "reconcile.json"
    current = _read_json_dict(path)
    payload = {
        "group_ids": sorted(set(current.get("group_ids") or []) | set(group_ids)),
        "unit_ids": sorted(set(current.get("unit_ids") or []) | set(unit_ids)),
    }
    _atomic_write_json(path, payload)


@handle_api_errors
async def api_persona_memory_dedupe_scan(
    request: web.Request, data_dir: Path
) -> web.Response:
    if not _is_persona_running(data_dir):
        return _json_response({"error": "请先启动当前人格"}, 409)
    status = _dedupe_status(data_dir)
    if status.get("status") in _ACTIVE_DEDUPE_STATES:
        return _json_response({"error": "已有记忆清理任务", "status": status}, 409)
    job_id = f"dedupe_{uuid4().hex}"
    job_dir = _memory_dedupe_dir(data_dir)
    _atomic_write_json(job_dir / "request.json", {"action": "scan", "job_id": job_id})
    _atomic_write_json(
        job_dir / "status.json",
        {"job_id": job_id, "status": "queued", "progress": 0},
    )
    return _json_response({"job_id": job_id, "status": "queued"}, 202)


@handle_api_errors
async def api_persona_memory_dedupe_status(
    request: web.Request, data_dir: Path
) -> web.Response:
    status = _dedupe_status(data_dir) or {"status": "idle"}
    return _json_response({**status, "worker_running": _is_persona_running(data_dir)})


@handle_api_errors
async def api_persona_memory_dedupe_apply(
    request: web.Request, data_dir: Path
) -> web.Response:
    if not _is_persona_running(data_dir):
        return _json_response({"error": "请先启动当前人格"}, 409)
    body = await request.json()
    job_id = str(body.get("job_id") or "") if isinstance(body, dict) else ""
    status = _dedupe_status(data_dir)
    if status.get("status") != "ready" or status.get("job_id") != job_id:
        return _json_response({"error": "扫描报告不可应用"}, 409)
    _atomic_write_json(
        _memory_dedupe_dir(data_dir) / "request.json",
        {"action": "apply", "job_id": job_id},
    )
    _atomic_write_json(
        _memory_dedupe_dir(data_dir) / "status.json",
        {"job_id": job_id, "status": "queued", "phase": "apply"},
    )
    return _json_response({"job_id": job_id, "status": "queued"}, 202)


@handle_api_errors
async def api_persona_memory_dedupe_report(
    request: web.Request, data_dir: Path
) -> web.Response:
    status = _dedupe_status(data_dir)
    job_id = str(status.get("job_id") or "")
    if not job_id:
        return _json_response({"error": "暂无扫描报告"}, 404)
    report_path = data_dir / "logs" / "memory-dedupe" / f"{job_id}.json"
    report = _read_json_dict(report_path)
    if not report:
        return _json_response({"error": "扫描报告不存在"}, 404)
    return _json_response(report)
```

Call `_memory_units_applying()` at the start of POST/PUT/DELETE. Add `_queue_memory_reconcile()` after
each successful POST/PUT/DELETE with both old and new groups for a group-moving PUT.

For offline POST/PUT, use `normalize_summary` plus `same_boundary` to collapse exact duplicates before
saving. Do not attempt an LLM call from WebUI.

Register the four routes exactly as approved, import handlers in `server.py`, and add them to
`DELEGATED_HANDLERS`.

- [ ] **Step 4: Run API, route and CRUD tests**

```powershell
python -m pytest tests/test_webui_memory_crud.py tests/test_webui_routes.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit WebUI backend support**

```powershell
git add sirius_pulse/webui/memory_api.py sirius_pulse/webui/routes.py sirius_pulse/webui/server.py tests/test_webui_memory_crud.py tests/test_webui_routes.py
git commit -m "feat(webui): 提供记忆去重任务接口"
```

---

### Task 8: WebUI Cleanup Button and Report Modal

**Files:**

- Modify: `sirius_pulse/webui/static/pages/memory-viz.js`
- Create: `tests/js/memory-dedupe-ui.test.mjs`

- [ ] **Step 1: Write a failing source-contract test**

Create `tests/js/memory-dedupe-ui.test.mjs`:

```javascript
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync('sirius_pulse/webui/static/pages/memory-viz.js', 'utf8');
assert.match(source, /id="memoryDedupeBtn"/);
assert.match(source, /清理重复/);
assert.match(source, /\/persona\/memory-units\/dedupe\/scan/);
assert.match(source, /\/persona\/memory-units\/dedupe\/status/);
assert.match(source, /\/persona\/memory-units\/dedupe\/apply/);
assert.match(source, /queued|scanning/);
assert.match(source, /ready/);
assert.match(source, /applying/);
assert.match(source, /completed/);
assert.match(source, /stale/);
assert.match(source, /failed/);
assert.match(source, /clearInterval\(dedupePollTimer\)/);
```

- [ ] **Step 2: Run and verify the missing UI**

```powershell
node tests/js/memory-dedupe-ui.test.mjs
```

Expected: assertion failure for `memoryDedupeBtn`.

- [ ] **Step 3: Implement the toolbar button and modal state machine**

Add this button after Refresh:

```html
<button class="btn" id="memoryDedupeBtn" style="display:none">清理重复</button>
```

Bind it with:

```javascript
$('memoryDedupeBtn')?.addEventListener('click', openDedupeModal);
```

At the end of `updateCreateButton()`, show it only for `state.tab === 'units'`. On every units-tab load,
fetch `/persona/memory-units/dedupe/status`, set `disabled = !status.worker_running`, and set the title
to `请先启动当前人格` when disabled. Reuse existing `.modal-overlay`, `.modal`, `.modal-header`,
`.modal-body`, `.modal-footer`, `.btn`, `.btn-danger` styles.

Implement the modal with no second abstraction. Add these module-scope variables and functions, using
the existing `escapeHtml()` helper for all report text:

```javascript
let dedupePollTimer = null;
let dedupeStatus = { status: 'idle' };
let dedupeReport = null;

function closeDedupeModal() {
  if (dedupePollTimer) clearInterval(dedupePollTimer);
  dedupePollTimer = null;
  document.getElementById('memoryDedupeModal')?.remove();
}

async function openDedupeModal() {
  closeDedupeModal();
  const overlay = document.createElement('div');
  overlay.id = 'memoryDedupeModal';
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:920px;max-height:88vh;overflow:auto">
      <div class="modal-header">
        <span style="font-size:16px;font-weight:600">重复记忆扫描</span>
        <button class="btn btn-sm" id="memoryDedupeClose">✕</button>
      </div>
      <div class="modal-body" id="memoryDedupeBody"></div>
      <div class="modal-footer" id="memoryDedupeFooter"></div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#memoryDedupeClose').addEventListener('click', closeDedupeModal);
  dedupeStatus = await get('/persona/memory-units/dedupe/status');
  dedupeReport = ['ready', 'completed', 'stale', 'failed'].includes(dedupeStatus.status)
    ? await get('/persona/memory-units/dedupe/report').catch(() => null)
    : null;
  renderDedupeStatus(dedupeStatus, dedupeReport);
  if (['queued', 'scanning', 'applying'].includes(dedupeStatus.status)) startDedupePolling();
}

async function startDedupeScan() {
  const response = await post('/persona/memory-units/dedupe/scan', {});
  dedupeStatus = response;
  dedupeReport = null;
  renderDedupeStatus(dedupeStatus);
  startDedupePolling();
}

function startDedupePolling() {
  if (dedupePollTimer) clearInterval(dedupePollTimer);
  dedupePollTimer = setInterval(pollDedupeStatus, 1000);
}

async function pollDedupeStatus() {
  if (!scopedPage.isActive() || !document.getElementById('memoryDedupeModal')) {
    closeDedupeModal();
    return;
  }
  dedupeStatus = await get('/persona/memory-units/dedupe/status');
  if (['ready', 'completed', 'stale', 'failed'].includes(dedupeStatus.status)) {
    if (dedupePollTimer) clearInterval(dedupePollTimer);
    dedupePollTimer = null;
    dedupeReport = await get('/persona/memory-units/dedupe/report').catch(() => null);
  }
  renderDedupeStatus(dedupeStatus, dedupeReport);
  if (dedupeStatus.status === 'completed') await loadActiveTab({ force: true });
}

function renderDedupeStatus(status, report = null) {
  const body = document.getElementById('memoryDedupeBody');
  const footer = document.getElementById('memoryDedupeFooter');
  if (!body || !footer) return;
  const labels = {
    idle: '尚未扫描', queued: '等待执行', scanning: '正在扫描', ready: '扫描完成',
    applying: '正在应用', completed: '清理完成', stale: '记忆数据已变化，请重新扫描',
    failed: '任务失败',
  };
  const summary = report?.summary || {};
  const rows = Object.entries(report?.groups || {}).flatMap(([groupId, group]) =>
    (group.operations || [])
      .filter(item => item.decision !== 'NEW')
      .map(item => `<tr><td>${escapeHtml(groupId)}</td><td>${escapeHtml(item.target_summary || '')}</td><td>${escapeHtml(item.incoming_summary || '')}</td><td>${escapeHtml(item.decision)}</td><td>${escapeHtml(item.reason || '')}</td></tr>`)
  ).join('');
  body.innerHTML = `
    <div style="margin-bottom:14px">${escapeHtml(labels[status.status] || status.status)}</div>
    <div class="stat-grid">
      ${statCard('扫描单元', report?.total || 0)}
      ${statCard('完全重复', summary.exact_duplicate || 0)}
      ${statCard('建议合并', (summary.duplicate || 0) + (summary.merge || 0))}
      ${statCard('事实冲突', summary.conflict || 0)}
      ${statCard('保持不变', summary.new || 0)}
    </div>
    ${rows ? `<table class="data-table"><thead><tr><th>群组</th><th>保留单元</th><th>候选单元</th><th>判断</th><th>原因</th></tr></thead><tbody>${rows}</tbody></table>` : ''}`;
  const applyCount = (summary.exact_duplicate || 0) + (summary.duplicate || 0) + (summary.merge || 0);
  footer.innerHTML = `
    <button class="btn" id="memoryDedupeCancel">关闭</button>
    ${report ? '<button class="btn" id="memoryDedupeDownload">导出报告</button>' : ''}
    ${status.status === 'idle' || ['completed', 'stale', 'failed'].includes(status.status) ? '<button class="btn btn-primary" id="memoryDedupeScan">扫描重复</button>' : ''}
    ${status.status === 'ready' ? `<button class="btn btn-danger" id="memoryDedupeApply">应用 ${applyCount} 项清理</button>` : ''}`;
  document.getElementById('memoryDedupeCancel')?.addEventListener('click', closeDedupeModal);
  document.getElementById('memoryDedupeScan')?.addEventListener('click', startDedupeScan);
  document.getElementById('memoryDedupeDownload')?.addEventListener('click', downloadDedupeReport);
  document.getElementById('memoryDedupeApply')?.addEventListener('click', () => applyDedupeReport(status.job_id));
}

async function applyDedupeReport(jobId) {
  if (!confirmDanger('确定应用本次重复记忆清理吗？系统会先创建完整备份。')) return;
  await post('/persona/memory-units/dedupe/apply', { job_id: jobId });
  dedupeStatus = { ...dedupeStatus, status: 'applying' };
  renderDedupeStatus(dedupeStatus, dedupeReport);
  startDedupePolling();
}

async function downloadDedupeReport() {
  const report = await get('/persona/memory-units/dedupe/report');
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${dedupeStatus.job_id || 'memory-dedupe'}.json`;
  link.click();
  URL.revokeObjectURL(url);
}
```

The modal must render all seven job states, five summary counts, group rows with canonical/candidate/
decision/reason, and the exact stale message “记忆数据已变化，请重新扫描”。 Apply uses
`confirmDanger`, remains disabled unless status is `ready`, and refreshes the units tab after
`completed`. Download creates a JSON Blob from the authenticated `get()` response.

Store the polling handle in module scope, clear it in `closeDedupeModal()` and `dispose()`, and guard all
async updates with `scopedPage.isActive()`.

Add this first line to `dispose()` before disposing the conversation analysis:

```javascript
closeDedupeModal();
```

- [ ] **Step 4: Run JS tests and inspect the live page locally**

```powershell
node tests/js/memory-dedupe-ui.test.mjs
node tests/js/memory-navigation-current-module.test.mjs
```

Expected: both commands exit 0.

Start the local WebUI, open `#memory-viz`, select MemoryUnit, and verify desktop plus mobile widths:
button visibility, modal containment, non-overlapping text, progress updates, stale state, failed state,
confirmation and completed refresh. Do not invoke apply against non-fixture persona data.

- [ ] **Step 5: Commit the WebUI workflow**

```powershell
git add sirius_pulse/webui/static/pages/memory-viz.js tests/js/memory-dedupe-ui.test.mjs
git commit -m "feat(webui): 增加重复记忆清理界面"
```

---

### Task 9: Documentation and Full Verification

**Files:**

- Modify: `docs/guide/memory-system.md`
- Modify: parent repository submodule pointer for `docs`

- [ ] **Step 1: Document the operator workflow in the docs submodule**

Add a “重复记忆清理” section describing:

```markdown
## 重复记忆清理

在“记忆管理 → 记忆单元”中点击“清理重复”可启动只读扫描。扫描会在同一群、同一作用域和
同一单元类型内召回语义候选，再由记忆模型判断重复、补充、冲突或独立事实。

扫描报告不会修改数据。确认“应用清理”后，系统会先校验扫描快照并备份
`memory_units/`；快照过期时必须重新扫描。冲突事实不会被删除。
```

Preserve the docs submodule's existing ahead commit and unrelated changes. Commit only this file inside
the submodule, then stage only the updated `docs` pointer in the parent repository.

- [ ] **Step 2: Run all targeted feature tests**

```powershell
python -m pytest tests/test_memory_unit_deduplication.py tests/test_memory_units.py tests/test_memory_dedupe_maintenance.py tests/test_memory_dedupe_jobs.py tests/test_webui_memory_crud.py tests/test_webui_routes.py -q
node tests/js/memory-dedupe-ui.test.mjs
node tests/js/memory-navigation-current-module.test.mjs
```

Expected: every command exits 0 with no failures.

- [ ] **Step 3: Run repository verification**

```powershell
python -m pytest
python scripts/ci_check.py
git diff --check
```

Expected: pytest reports zero failures, CI check exits 0, and `git diff --check` prints no errors.

- [ ] **Step 4: Review the final diff against the design acceptance criteria**

Use `git diff --stat`, `git diff`, and `git status --short`. Explicitly confirm all ten acceptance
criteria in the design: canonical uniqueness, group isolation, factual merge, source preservation,
conflict links, failure safety, dry-run purity, stale rejection, backup, and immediate runtime refresh.
Confirm the user's pre-existing unrelated modifications remain outside feature commits.

- [ ] **Step 5: Commit docs and the parent submodule pointer**

Inside `docs`:

```powershell
git add guide/memory-system.md
git commit -m "docs(memory): 说明重复记忆清理流程"
```

In the parent repository:

```powershell
git add docs
git commit -m "docs(memory): 同步重复记忆清理文档"
```

Do not push or deploy unless the user separately requests it.
