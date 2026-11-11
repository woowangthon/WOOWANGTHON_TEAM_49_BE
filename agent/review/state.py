"""Pure review-state operations. No users, database, files, HTTP or local console."""

from copy import deepcopy
from datetime import date
import re

from agent.review.pipeline import field_seed, identifier, manual_value
from agent.review.registry import profiles
from agent.review.rows import link_rows
from agent.services.hashing import digest
from agent.services.identity import identity_key


def new_state(title, auto_process=False, auto_verify=False):
    return {
        "title": title, "basic": {"employer_name": "", "pay_type": "HOURLY",
            "period_start": "", "period_end": "", "payment_date": ""},
        "basic_confirmed": False, "auto_process": auto_process, "auto_verify": auto_verify,
        "options": {"subjects_confirmed": False, "earnings_complete": False,
            "deductions_complete": False, "payment_scope_confirmed": False,
            "worked_time_kind": None, "slip_time_kind": None,
            "contract_part_id": None, "payslip_part_id": None},
    }


def validate_basic(basic):
    if basic["pay_type"] not in ("HOURLY", "MONTHLY"):
        raise ValueError("시급 또는 월급을 선택해 주세요.")
    for key in ("period_start", "period_end", "payment_date"):
        if basic[key]:
            date.fromisoformat(basic[key])
    if basic["period_start"] and basic["period_end"] and basic["period_start"] > basic["period_end"]:
        raise ValueError("급여 산정기간의 시작일이 종료일보다 늦습니다.")


def validate_parts(parts, pages, manual=False):
    covered, result = [], []
    for source in parts:
        first, last, kind = source["start_page"], source["end_page"], source["document_type"]
        if type(first) is not int or type(last) is not int or not 1 <= first <= last <= pages:
            raise ValueError("문서의 페이지 범위를 확인해 주세요.")
        if kind not in profiles():
            raise ValueError("문서 유형을 확인해 주세요.")
        covered.extend(range(first, last + 1))
        result.append({"id": identifier("part"), "document_type": kind, "start_page": first,
            "end_page": last, "label": source.get("label") or profiles()[kind]["label"],
            "needs_review": False, "status": "review_ready" if manual else "classified",
            "fields": [field_seed(k, v) for k, v in profiles()[kind]["fields"].items()] if manual else []})
    if sorted(covered) != list(range(1, pages + 1)):
        raise ValueError("겹치거나 빠지는 페이지 없이 전체 문서를 구분해 주세요.")
    return result


def find_field(documents, field_id):
    for document in documents:
        if document.get("excluded"):
            continue
        for part in document.get("parts", []):
            for field in part.get("fields", []):
                if field["id"] == field_id:
                    return document, part, field
    raise ValueError("현재 검토의 항목을 찾을 수 없습니다.")


def conflict(field):
    values = {str(c["value"]) for c in field.get("candidates", []) if c.get("value") is not None}
    if "CONFLICT" not in field["issues"] and len(values) <= 1:
        return None
    return "conflict-" + digest([field["id"], sorted(values)])[:20]


def edit(field, part, *, value=None, unit=None, candidate_id=None, defer=False):
    candidate = None
    if candidate_id:
        candidate = next((c for c in field["candidates"] if c["id"] == candidate_id), None)
        if candidate is None:
            raise ValueError("현재 항목의 후보를 선택해 주세요.")
        value, unit = candidate["value"] or candidate["raw_value"] or "", candidate["unit"]
    unit = unit or field["unit"]
    allowed = {"rate", "text", "date", "time", "minute", "count", "category", "day_of_month",
        "KRW", "KRW/hour", "KRW/month", "KRW/year", "KRW/day"}
    if unit not in allowed:
        raise ValueError("단위를 확인해 주세요.")
    normalized = manual_value(value if value is not None else field["value"], unit,
        part["document_type"], field["field_id"])
    field.update(value="" if defer else normalized, unit=unit, touched=True,
        review_state="deferred" if defer else "unreviewed", revision=field["revision"] + 1,
        source_kind="document" if candidate else "user_statement")
    if candidate:
        field.update(evidence=deepcopy(candidate["evidence"]), raw_value=candidate["raw_value"])
    field.pop("confirmation_id", None)
    field.pop("conflict_acknowledgement", None)


def confirm(fields, *, source_seen, acknowledgement=None):
    if not source_seen:
        raise ValueError("원문과 입력값을 확인해 주세요.")
    for _, part, field in fields:
        if field["review_state"] == "deferred" or not field["value"]:
            raise ValueError("빈값이나 보류 항목은 확정할 수 없습니다.")
        manual_value(field["value"], field["unit"], part["document_type"], field["field_id"])
        token = conflict(field)
        if token and (len(fields) != 1 or not field["touched"] or acknowledgement != token):
            raise ValueError("충돌 항목은 기준값 선택과 원문 확인 후 개별 확정해 주세요.")
    for _, _, field in fields:
        field.update(review_state="confirmed", confirmation_id=identifier("confirmation"),
            revision=field["revision"] + 1)
        if conflict(field):
            field["conflict_acknowledgement"] = conflict(field)


def merge_parts(previous, incoming, *, only_field_ids=None):
    """Never lose edited/deleted/deferred values or pair repeated rows by index alone.

    only_field_ids limits a targeted recheck to those fields. A verification job
    carries a copy of every current field, so all other fields keep their exact
    previous state, candidates and confirmation.
    """
    result = deepcopy(incoming)
    for part in result:
        old = next((p for p in previous if all(p[k] == part[k] for k in
            ("document_type", "start_page", "end_page"))), None)
        if old is None:
            continue
        part["id"] = old["id"]
        for field in part["fields"]:
            repeated = re.match(r"(?:work|earning|deduction|transaction)_\d+_", field["field_id"])
            match = next((f for f in old["fields"] if f["field_id"] == field["field_id"] and
                (not repeated or (f.get("evidence") or {}).get("quote") ==
                 (field.get("evidence") or {}).get("quote"))), None)
            if match is None:
                continue
            field["id"] = match["id"]
            if only_field_ids is not None and match["id"] not in only_field_ids:
                field.clear()
                field.update(deepcopy(match))
                continue
            if match["touched"] or match["review_state"] in ("confirmed", "deferred"):
                # Append only candidates this job produced; a recheck copies the old ones.
                known = {c["id"] for c in match["candidates"]}
                candidates = [deepcopy(c) for c in field["candidates"] if c["id"] not in known]
                field.update(deepcopy(match))
                field["candidates"].extend(candidates)
                field["revision"] += 1
                field["review_state"] = "deferred" if match["review_state"] == "deferred" else "unreviewed"
                field.pop("confirmation_id", None)
                field.pop("conflict_acknowledgement", None)
                if any(c.get("value") is not None and c["value"] != field["value"] for c in candidates):
                    field["issues"] = list(dict.fromkeys([*field["issues"], "CONFLICT"]))
        present = {f["id"] for f in part["fields"]}
        for field in old["fields"]:
            if field["id"] not in present and (field["touched"] or field["review_state"] == "confirmed"):
                preserved = deepcopy(field)
                preserved.update(review_state="unreviewed", issues=[*field["issues"], "CONFLICT"])
                preserved.pop("confirmation_id", None)
                part["fields"].append(preserved)
    for old in previous:
        if not any(p["id"] == old["id"] for p in result) and any(
            f["touched"] or f["review_state"] == "confirmed" for f in old["fields"]):
            preserved = deepcopy(old)
            preserved.update(needs_review=True, status="needs_classification",
                reason="구간 변경 전 수정값입니다. 원문을 확인해 새 구간에 입력해 주세요.")
            for field in preserved["fields"]:
                field.update(review_state="unreviewed")
                field.pop("confirmation_id", None)
            result.append(preserved)
    return result


def selection(state, documents):
    selected, missing = [], []
    for document in documents:
        if document.get("excluded"):
            continue
        if document["status"] in ("PENDING", "RUNNING", "INTERRUPTED", "STALE", "FAILED"):
            missing.append("진행 중이거나 완료하지 못한 자료를 처리하거나 제외해 주세요.")
        for part in document.get("parts", []):
            if part.get("status") == "failed":
                missing.append("추출에 실패한 자료를 다시 처리하거나 직접 입력해 주세요.")
            if part.get("needs_review") or part["document_type"] not in profiles():
                missing.append("문서 유형과 페이지 구간을 확인해 주세요.")
                continue
            selected.append((document, part))
    for kind in ("contract", "payslip"):
        parts = [(d, p) for d, p in selected if p["document_type"] == kind]
        chosen = state["options"][kind + "_part_id"]
        if chosen:
            if not any(p["id"] == chosen for _, p in parts):
                missing.append("기준 자료를 다시 선택해 주세요.")
            selected = [(d, p) for d, p in selected if p["document_type"] != kind or p["id"] == chosen]
        elif len(parts) > 1:
            missing.append("기준 계약서 또는 급여명세서를 선택해 주세요.")
    if not selected:
        missing.append("분류된 자료가 필요합니다.")
    for document in documents:
        ranges = sorted((p["start_page"], p["end_page"]) for d, p in selected if d["id"] == document["id"])
        if any(right[0] <= left[1] for left, right in zip(ranges, ranges[1:])):
            missing.append("같은 원문의 겹치는 구간을 선택할 수 없습니다.")
    return selected, missing


def readiness(state, documents):
    selected, missing = selection(state, documents)
    basic = state["basic"]
    if not state["basic_confirmed"] or not all(basic[k] for k in ("employer_name", "period_start", "period_end")):
        missing.append("사업장과 급여기간의 기본 정보를 확정해 주세요.")
    if not state["options"]["subjects_confirmed"]:
        missing.append("같은 근로자·사업장의 자료인지 확인해 주세요.")
    usable = []
    identities = {key: set() for key in ("employee_name", "employee_id", "employer_name")}
    for _, part in selected:
        for field in part["fields"]:
            token = conflict(field)
            if token and (field["review_state"] != "confirmed" or field.get("conflict_acknowledgement") != token):
                missing.append("충돌 항목의 기준값을 선택하고 개별 확정해 주세요.")
            if field["review_state"] != "confirmed":
                continue
            if field["field_id"] in identities and field["value"]:
                identities[field["field_id"]].add(identity_key(field["value"], company=field["field_id"] == "employer_name"))
            if not field["extra"] and (field["unit"].startswith("KRW") or field["unit"] == "minute"):
                usable.append(field)
    identities["employer_name"].add(identity_key(basic["employer_name"], company=True))
    if any(len(values - {""}) > 1 for values in identities.values()):
        missing.append("근로자 또는 사업장이 다른 자료를 수정하거나 제외해 주세요.")
    if not usable:
        missing.append("계산·대조에 사용할 금액 또는 시간을 확정해 주세요.")
    return {"status": "needs_review" if missing else "ready", "missing": list(dict.fromkeys(missing))}


def decorate(document):
    for part in document.get("parts", []):
        part["rows"] = link_rows(part)
        for field in part.get("fields", []):
            field["conflict_id"] = conflict(field)
    return document
