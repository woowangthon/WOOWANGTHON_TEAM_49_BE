import copy
import io
import json
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader

from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.schemas.public.inputs import InputSnapshot
from agent.schemas.public.results import AnalysisResult
from agent.services.hashing import content_hash, digest, result_hash, snapshot_hash
from agent.tests.support.comparison_documents import NoModelCalls, analyzed
from agent.tests.support.monthly_documents import DIRECTORY, seed_monthly_documents


@pytest.mark.skipif(not DIRECTORY.is_dir(), reason="Authored local PDF corpus is optional")
async def test_monthly_documents_calculation_answers_inquiry_reports_and_history(tmp_path):
    originals = {path.name: content_hash(path.read_bytes()) for path in DIRECTORY.glob("*.pdf")}
    store = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    cid, cases, gold = await seed_monthly_documents(store)
    document_id = store.cases[cases["attendance"]]["document_id"]
    await store.create_case(document_id, "auto")
    listed = next(doc for doc in await store.list_documents() if doc["id"] == document_id)
    assert listed["case_id"] == cases["attendance"]
    current, comparisons = await analyzed(store, cid)
    first_id = current["analysis"]["result_id"]
    assert current["analysis"]["wage_section"]["gross_total"] == str(gold["earnings"]["gross"])
    assert [comparisons[key]["status"] for key in ("R-03", "R-04", "R-05", "R-06", "R-07")] == ["match"] * 5
    assert comparisons["R-06"]["rhs"] == str(gold["net"])
    assert current["explanation"]["model_config_version"] == "deterministic-v1"
    await store.inquiry(cid, {"result_id": first_id, "question_ids": []})
    edited = "직접 편집한 문의입니다. 적용한 계산 기준을 알려 주세요."
    await store.inquiry(cid, {"result_id": first_id, "text": edited}, save=True)
    current = await store.inquiry(cid, {"result_id": first_id, "question_ids": []})
    assert current["inquiry"]["text"] == edited
    ids = [s["source_id"] for s in current["report_sources"]]
    assert len(ids) == 4
    artifacts = []
    for selected in ([], ids[:1], ids):
        job = await store.create_job(cid, "report", selected_source_ids=selected, result_id=first_id)
        await store.tasks[job["id"]]
        assert store.jobs[job["id"]]["status"] == "completed", store.jobs[job["id"]]
        assert not store.jobs[job["id"]]["usage"]
        current = await store.get_comparison(cid)
        report = current["report"]
        assert report["included_source_ids"] == selected
        pdf_ref, zip_ref = report["artifacts"]
        pdf, _ = await store.artifact(pdf_ref["artifact_id"])
        text = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        assert "3,272,727" in text and "2,992,727" in text
        assert "04_월급제_표준근로계약서_스캔.pdf" in text
        archive, _ = await store.artifact(zip_ref["artifact_id"])
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            assert len([name for name in z.namelist() if name.startswith("originals/")]) == len(selected)
            assert json.loads(z.read("result.json"))["result_id"] == first_id
        artifacts.append(pdf_ref["artifact_id"])
    draft = copy.deepcopy(current["draft"])
    draft["wage_conditions"]["five_or_more_workers"] = dict(state="unknown", value=None)
    await store.save_draft(cid, draft)
    with pytest.raises(ValueError, match="입력이 변경"):
        await store.answer_questions(cid, {"result_id": first_id, "answers": [], "confirm": True})
    await store.confirm(cid)
    current, comparisons = await analyzed(store, cid)
    assert comparisons["R-06"]["status"] == "match"
    assert current["analysis"]["wage_section"]["items"][0]["status"] == "calculated"
    assert current["analysis"]["wage_section"]["gross_total"] is None
    question = next(
        q
        for q in current["analysis"]["questions"]
        if "wage_conditions.five_or_more_workers" in q["target_field_ids"]
    )
    await store.answer_questions(
        cid,
        {
            "result_id": current["analysis"]["result_id"],
            "confirm": True,
            "answers": [
                dict(
                    question_id=question["question_id"],
                    field_id="wage_conditions.five_or_more_workers",
                    state="known",
                    value=True,
                )
            ],
        },
    )
    current, _ = await analyzed(store, cid)
    assert current["analysis"]["wage_section"]["gross_total"] == str(gold["earnings"]["gross"])
    assert current["inquiries"][first_id]["text"] == edited
    assert len(current["analysis_history"]) == 3
    for aid in artifacts:
        assert (await store.artifact(aid))[0].startswith(b"%PDF")
    old_job = await store.create_job(cid, "report", selected_source_ids=[], result_id=first_id)
    await store.tasks[old_job["id"]]
    assert store.jobs[old_job["id"]]["status"] == "completed"
    historical = next(
        h for h in store.comparisons[cid]["analysis_history"] if h["analysis"]["result_id"] == first_id
    )
    old_pdf, _ = await store.artifact(historical["report"]["artifacts"][0]["artifact_id"])
    assert "이전 입력 결과" in PdfReader(io.BytesIO(old_pdf)).pages[0].extract_text()
    assert store.comparisons[cid]["analysis"]["result_id"] != first_id
    restored = LocalStore(AgentSettings(), state=tmp_path, provider=NoModelCalls())
    assert (await restored.get_comparison(cid))["inquiries"][first_id]["text"] == edited
    assert originals == {path.name: content_hash(path.read_bytes()) for path in DIRECTORY.glob("*.pdf")}


def test_legacy_stored_hashes_survive_additive_schema_fields():
    root = Path(__file__).resolve().parents[2] / "output/document-workspace/comparisons"
    checked = 0
    for path in root.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("analysis"):
            result = AnalysisResult.model_validate(data["analysis"])
            assert result_hash(result) == result.result_hash
            snapshot = InputSnapshot.model_validate(data["analysis_snapshot"])
            assert snapshot_hash(snapshot) == snapshot.snapshot_hash
            checked += 1
    # Portable independent legacy payload check is kept even without local user data.
    from agent.tests.fixtures_support import analysis_fixture

    task, _, _ = analysis_fixture()
    snapshot = task.payload.snapshot
    old = snapshot.model_dump(exclude={"snapshot_hash", "snapshot_id", "wage_conditions"})
    assert snapshot_hash(snapshot) == digest(old)
