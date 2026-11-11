"""Opt-in, resumable real-model corpus evaluation (two concurrent requests)."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from agent import run_task
from agent.config import AgentSettings
from agent.schemas.public.tasks import AgentOutcome, AgentTask, ExtractionInput
from agent.services.hashing import content_hash, digest
from agent.tests.fake_ports import FakePorts
from agent.tests.fixtures_support import context, source
from agent.tests.support.contract_corpus import DEFAULT_OUTPUT, FIELDS, read_json, write_json


def score_document(gold, outcome):
    completed = outcome.status == "completed"
    fields = {f.field_id: f for f in outcome.payload.fields} if completed else {}
    evidence = {e.evidence_id: e for e in outcome.payload.evidence_catalog} if completed else {}
    rows = []
    for key, answer in gold["fields"].items():
        found = fields.get(key)
        value = found.normalized_value if found else None
        present = value is not None
        exact = present and value == answer["value"]
        # Category/date/clock types are defined by their field ID, not physical units.
        # Physical currency and duration units must still be explicitly correct.
        physical = answer["unit"] == "minute" or str(answer["unit"]).startswith("KRW")
        unit = exact and (not physical or found.unit == answer["unit"])
        refs = [evidence[e] for e in (found.evidence_refs if found else []) if e in evidence]
        valid_page = bool(refs) and all(1 <= e.page <= outcome.payload.page_count for e in refs)
        correct_page = valid_page and (
            answer.get("page") is None or any(e.page == answer["page"] for e in refs)
        )
        text_verified = bool(refs) and all(e.verification_method == "text_match" for e in refs)
        rows.append(
            {
                "field_id": key,
                "label": FIELDS[key],
                "gold_state": answer["state"],
                "expected": answer["value"],
                "expected_unit": answer["unit"],
                "actual": value,
                "actual_unit": found.unit if found else None,
                "exact": exact if answer["state"] == "present" else None,
                "unit_period_match": unit if answer["state"] == "present" else None,
                "blank_hallucination": present if completed and answer["state"] == "absent" else None,
                "evidence_page_valid": valid_page if present else None,
                "expected_page_match": correct_page if answer["state"] == "present" else None,
                "text_quote_verified": text_verified if present else None,
                "vision_requires_confirmation": bool(refs) and not text_verified,
                "gold_quote": answer["source_quote"],
                "reason": answer.get("reason"),
            }
        )
    return {
        "status": outcome.status,
        "error": outcome.error.model_dump() if outcome.error else None,
        "fields": rows,
        "usage": outcome.usage,
    }


def summarize(output=DEFAULT_OUTPUT, *, save=True):
    manifest = read_json(output / "manifest.json")
    records = []
    for doc in manifest["documents"]:
        path = output / "results" / (doc["id"] + ".json")
        if not path.exists():
            continue
        outcome = AgentOutcome.model_validate(read_json(path))
        record = score_document(read_json(output / "gold" / (doc["id"] + ".json")), outcome)
        record.update(document_id=doc["id"], title=doc["title"], kind=doc["kind"])
        records.append(record)
    rows = [r for doc in records for r in doc["fields"]]
    positives = [r for r in rows if r["gold_state"] == "present"]
    blanks = [r for r in rows if r["blank_hallucination"] is not None]
    predicted = [r for r in rows if r["evidence_page_valid"] is not None]
    usage = {}
    calls = 0
    uncertain_jobs = 0
    for path in (output / "usage").rglob("*.json"):
        item = read_json(path)
        calls += item.get("model_calls", 0)
        uncertain_jobs += bool(item.get("usage_may_be_incomplete"))
        for key, value in item.get("usage", {}).items():
            usage[key] = usage.get(key, 0) + value
    summary = {
        "model": "gpt-6-luna",
        "count": len(manifest["documents"]),
        "evaluated": len(records),
        "completed": sum(r["status"] == "completed" for r in records),
        "failed": sum(r["status"] != "completed" for r in records),
        "positive_values": {"correct": sum(r["exact"] for r in positives), "total": len(positives)},
        "unit_period": {"correct": sum(r["unit_period_match"] for r in positives), "total": len(positives)},
        "blanks": {"hallucinated": sum(r["blank_hallucination"] for r in blanks), "total": len(blanks)},
        "unscored": sum(r["gold_state"] == "unscored" for r in rows),
        "evidence": {
            "valid_pages": sum(r["evidence_page_valid"] for r in predicted),
            "predicted_values": len(predicted),
            "text_verified": sum(r["text_quote_verified"] for r in predicted),
            "vision_requires_confirmation": sum(r["vision_requires_confirmation"] for r in predicted),
        },
        "model_calls": calls,
        "usage_incomplete_jobs": uncertain_jobs,
        "usage": usage,
        "records": records,
        "limitations": [
            "Small local corpus; many blank templates; not a release accuracy estimate.",
            "Image quotes are not independently OCR verified; require human confirmation.",
            "Unscored contextual facts excluded; failed calls do not count as correct blanks.",
        ],
    }
    if save:
        write_json(output / "evaluation.json", summary)
    return summary


async def evaluate(output, settings, selected=None):
    manifest = read_json(output / "manifest.json")
    semaphore, unavailable = asyncio.Semaphore(2), asyncio.Event()

    async def one(doc):
        if selected and doc["id"] not in selected:
            return
        async with semaphore:
            result_path = output / "results" / (doc["id"] + ".json")
            if result_path.exists():
                print(doc["id"], "saved result restored", flush=True)
                return
            if unavailable.is_set():
                return
            gold_path = output / "gold" / (doc["id"] + ".json")
            gold = read_json(gold_path)
            assert gold["reviewed_before_calls"] and set(gold["fields"]) == set(FIELDS)
            data = (output / doc["pdf"]).read_bytes()
            assert content_hash(data) == doc["sha256"]
            history = list((output / "usage/history").glob(doc["id"] + "-*.json"))
            previous_calls = sum(read_json(p).get("model_calls", 0) for p in history)
            if previous_calls >= 3:
                raise ValueError("Cumulative extraction call budget exhausted for " + doc["id"])
            document_settings = settings.model_copy(
                update={"max_stage_calls": min(settings.max_stage_calls, 3 - previous_calls)}
            )
            revision = "-" + doc["sha256"][:12] if history else ""
            job_id = "corpus-v1-" + doc["id"] + revision
            write_json(
                output / "usage" / (doc["id"] + ".inputs.json"),
                {
                    "gold_hash": content_hash(gold_path.read_bytes()),
                    "pdf_hash": doc["sha256"],
                    "started_at": datetime.now(UTC).isoformat(),
                    "job_id": job_id,
                    "previous_calls": previous_calls,
                },
            )
            ref = source(data, source_id="corpus-" + doc["id"], status="pending")
            ports = FakePorts(directory=output / "private" / (doc["id"] + revision))
            ports.add_source(ref, data)
            task = AgentTask(
                context=context("extract_document", job_id=job_id),
                payload=ExtractionInput(ocr_result_id="test-ocr-1", source=ref, document_type="contract"),
            )
            ports.bind(task.context)
            outcome = await run_task(task, ports=ports, settings=document_settings)
            write_json(result_path, outcome.model_dump(mode="json"))
            cp = ports.checkpoints.get(task.context.job_id)
            write_json(
                output / "usage" / (doc["id"] + ".json"),
                {
                    "usage": outcome.usage,
                    "model_calls": sum(
                        v for k, v in cp.call_budget.items() if not k.endswith(":format_failures")
                    )
                    if cp
                    else 0,
                    "call_budget": cp.call_budget if cp else {},
                    "outcome_hash": digest(outcome),
                },
            )
            summarize(output)
            print(doc["id"], outcome.status, outcome.error.code if outcome.error else "", flush=True)
            if outcome.error and outcome.error.code == "MODEL_UNAVAILABLE":
                unavailable.set()

    await asyncio.gather(*(one(d) for d in manifest["documents"]))
    report = summarize(output)
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--env", type=Path, default=Path("agent/.env"))
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--summarize", action="store_true")
    args = parser.parse_args()
    if args.summarize:
        summarize(args.output)
    else:
        asyncio.run(
            evaluate(args.output, AgentSettings.from_env(args.env, provider_mode="openai"), args.only)
        )


if __name__ == "__main__":
    main()
