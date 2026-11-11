"""Contract tests use real agent graphs/rules; no model or OCR quality claims."""

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import jsonschema
from pydantic import ValidationError

from agent import run_task
from agent.schemas.public.tasks import AgentTask, JobContext
from agent.services.hashing import result_hash, snapshot_hash
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import analysis_fixture, context, settings
from app.agent_mapping import (
    CONTENT_FIELDS, _preserve_source_conflicts, analysis_payload, build_snapshot,
    confirm_result, result_response,
)


def mapped_fixture():
    task, source, data = analysis_fixture()
    original = task.payload.snapshot
    confirmed = {key: value for key, value in original.model_dump(mode="json").items() if key in CONTENT_FIELDS}
    mapped = build_snapshot(confirmed, snapshot_id="mapped-snapshot", input_version=4,
        owner_id=original.owner_id, workplace_id=original.workplace_id, case_id=original.case_id,
        period=original.period, authorized_sources=[source])
    return mapped, source, data


def test_lossless_semantics_and_canonical_hash():
    original_task, _, _ = analysis_fixture()
    mapped, _, _ = mapped_fixture()
    for key in CONTENT_FIELDS:
        assert getattr(mapped, key) == getattr(original_task.payload.snapshot, key)
    assert mapped.input_version == 4
    assert mapped.snapshot_hash == snapshot_hash(mapped)
    assert mapped.rule_bundle_version != "pending-v1"
    assert mapped.support_manifest_version != "pending-v1"


def test_money_numbers_and_invented_profile_rejected():
    mapped, source, _ = mapped_fixture()
    confirmed = {key: value for key, value in mapped.model_dump(mode="json").items() if key in CONTENT_FIELDS}
    kwargs = dict(snapshot_id="x", input_version=1, owner_id=mapped.owner_id,
        workplace_id=mapped.workplace_id, case_id=mapped.case_id, period=mapped.period,
        authorized_sources=[source])
    without_profile = deepcopy(confirmed)
    without_profile.pop("profile")
    with pytest.raises(ValueError, match="explicit"):
        build_snapshot(without_profile, **kwargs)
    confirmed["confirmed_fields"][0]["value"] = 10000
    with pytest.raises(ValidationError, match="decimal strings"):
        build_snapshot(confirmed, **kwargs)


def test_document_case_scope_is_explicitly_versioned():
    base = context("extract_document").model_dump()
    modern = JobContext.model_validate(base | {"schema_version": "1.1", "case_id": None})
    assert modern.case_id is None
    with pytest.raises(ValidationError, match="Only schema 1.1"):
        JobContext.model_validate(base | {"case_id": None})
    with pytest.raises(ValidationError, match="Only schema 1.1"):
        JobContext.model_validate(base | {"schema_version": "1.1", "case_id": None, "task_kind": "analyze_case"})
    public = modern.model_dump(mode="json")
    jsonschema.validate(public, JobContext.model_json_schema())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(public | {"schema_version": "1.0"}, JobContext.model_json_schema())


async def test_real_analysis_preserves_partial_input_and_result_sign(tmp_path):
    mapped, source, data = mapped_fixture()
    fields = mapped.model_dump(mode="json")["confirmed_fields"]
    for field in fields:
        if field["field_id"] == "contract_rate":
            field.update(value=None, state="unknown", evidence_refs=[])
        if field["field_id"] == "net":
            field["value"] = "1800100"
    mapped = type(mapped).model_validate(mapped.model_dump() | {"confirmed_fields": fields})
    mapped.snapshot_hash = snapshot_hash(mapped)
    task = AgentTask(context=context("analyze_case"), payload=analysis_payload(mapped))
    ports = FakePorts(directory=tmp_path / "storage")
    ports.bind(task.context)
    ports.add_source(source, data)
    outcome = await run_task(task, ports=ports, settings=settings())
    assert outcome.status == "completed", outcome.error
    result = confirm_result(outcome.payload, mapped, result_id="r1", result_version=2,
        freshness="current", created_at=datetime(2026, 11, 9, tzinfo=UTC))
    assert result.result_hash == result_hash(result) == outcome.payload.result_hash
    comparisons = {item.rule_id: item for item in result.comparisons}
    assert comparisons["R-01"].status == "insufficient"
    assert comparisons["R-06"].lhs == Decimal("1800100")
    assert comparisons["R-06"].rhs == Decimal("1800000")
    assert comparisons["R-06"].delta == Decimal("100")
    assert result.wage_section.scope_complete is False
    payload = result_response(result)
    assert payload["comparisons"][5]["delta"] == "100"
    assert payload["presentation"]["comparisons"]["R-01"]["lhs_label"] == "명세서 단가"
    assert payload["presentation"]["delta_formula"] == "lhs - rhs"
    corrupted = outcome.payload.model_copy(update={"result_hash": "0" * 64})
    with pytest.raises(ValueError, match="hash"):
        confirm_result(corrupted, mapped, result_id="r2", result_version=3,
            freshness="current", created_at=datetime.now(UTC))


def test_generic_confirmation_does_not_erase_source_period_or_unit_conflict():
    confirmed = {"confirmed_fields": [
        {"field_id": "contract_rate", "value": "10000", "evidence_refs": []},
        {"field_id": "gross", "value": "100000", "evidence_refs": []},
    ], "wage_conditions": {"contract_unchanged": {"state": "known", "value": True}}}
    _preserve_source_conflicts(confirmed, {"issues": [
        {"code": "PERIOD_MISMATCH", "source_case_ids": ["4"]},
        {"code": "UNIT_MISMATCH", "source_case_ids": ["4", "5"]},
    ]}, [{"case_id": "4", "document_type": "contract"}])
    assert confirmed["condition_states"] == [{"field_id": "contract_rate", "state": "conflict",
        "confirmed_field_ref": None, "conflicting_source_refs": ["doc:4"]}]
    assert confirmed["wage_conditions"]["contract_unchanged"]["state"] == "conflict"
    assert confirmed["confirmed_fields"][1]["value"] == "100000"
