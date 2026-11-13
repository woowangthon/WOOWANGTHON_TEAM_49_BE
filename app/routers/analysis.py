from app.outputs import API, RecomputeDTO, ResultsDTO
from app.schemas import Envelope
from fastapi import APIRouter, Depends, Query, Request, Response

from app.db import dump, load, now
from app.engine import ALL_CHECKS, latest, results, schedule, snapshot_accessible, snapshot_dto
from app.errors import ApiError, ok
from app.i18n import localize_conditions, localize_results, user_language
from app.idempotency import idempotency_header, replay, remember
from app.routers.workspaces import page_data
from app.schemas import ConditionsPut, Execute, Inquiry, ManualCreate, ManualPatch
from app.security import case, check_version, current_user

router = APIRouter(tags=["Analysis & Results"])
CONDITIONS = [
    {
        "conditionId": "workplace_size",
        "question": "상시 근로자 수를 알고 있나요?",
        "type": "SELECT",
        "options": ["LESS_THAN_5", "AT_LEAST_5", "UNKNOWN"],
        "requiredFor": ["OVERTIME_PAY"],
    },
    {
        "conditionId": "regular_minutes",
        "question": "이번 기간의 확인된 기본급 산정시간은 몇 분인가요?",
        "type": "NUMBER",
        "unit": "MINUTE",
        "options": None,
        "requiredFor": ["WAGE_CALCULATION"],
    },
    {
        "conditionId": "base_scope_confirmed",
        "question": "기본급의 산정기간·임금 구성·시간 기준이 이 사례와 일치하나요?",
        "type": "BOOLEAN",
        "options": None,
        "requiredFor": ["WAGE_CALCULATION"],
    },
    {
        "conditionId": "comparison_basis_confirmed",
        "question": "계약과 명세서의 기간·시급 기준이 같은지 확인했나요?",
        "type": "BOOLEAN",
        "options": None,
        "requiredFor": ["CONTRACT_ATTENDANCE_COMPARISON"],
    },
    {
        "conditionId": "hours_basis_confirmed",
        "question": "근태와 명세서의 산정기간 및 실근로시간 기준이 같나요? 유급시간과 구분해 주세요.",
        "type": "BOOLEAN",
        "options": None,
        "requiredFor": ["CONTRACT_ATTENDANCE_COMPARISON"],
    },
    {
        "conditionId": "base_pay_basis_confirmed",
        "question": "기본급과 명세서 기본급의 기간·포함 항목이 같은지 확인했나요?",
        "type": "BOOLEAN",
        "options": None,
        "requiredFor": ["CONTRACT_ATTENDANCE_COMPARISON"],
    },
]


def condition_dto(con, case_id):
    row = con.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
    answers = load(row["conditions"])
    return {
        "conditions": [
            {
                **c,
                "value": answers.get(c["conditionId"]),
                "status": "REQUIRED"
                if c["conditionId"] not in answers
                else ("UNKNOWN" if answers[c["conditionId"]] == "UNKNOWN" else "ANSWERED"),
                "source": "USER_INPUT" if c["conditionId"] in answers else None,
            }
            for c in CONDITIONS
        ],
        "inputVersion": row["input_version"],
    }


def readiness_data(con, case_id):
    from app.compatibility import collect, _single

    data = collect(con, case_id)
    contract, cc, _ = _single(data, "CONTRACT")
    payslip, pc, _ = _single(data, "PAYSLIP")
    attendance, ac, _ = _single(data, "ATTENDANCE")
    conditions = data["conditions"]
    basic = (
        not cc
        and conditions.get("base_scope_confirmed") is True
        and (
            contract.get("monthlyBasePay") is not None
            if data["case"]["pay_type"] == "MONTHLY"
            else contract.get("hourlyWage") is not None and type(conditions.get("regular_minutes")) is int
        )
    )
    statement = [
        not pc and payslip.get("grossPay") is not None and payslip.get("paymentItems") is not None,
        not pc and payslip.get("totalDeductions") is not None and payslip.get("deductionItems") is not None,
        not pc and all(payslip.get(k) is not None for k in ("grossPay", "totalDeductions", "netPay")),
    ]
    comparison = not (pc or cc or ac) and (
        (
            conditions.get("comparison_basis_confirmed") is True
            and contract.get("hourlyWage") is not None
            and payslip.get("hourlyWage") is not None
        )
        or (
            conditions.get("hours_basis_confirmed") is True
            and attendance.get("workedMinutes") is not None
            and payslip.get("workedMinutes") is not None
        )
    )
    payment = not pc and payslip.get("netPay") is not None and bool(data["payments"])
    states = [
        (
            basic,
            "PARTIAL" if basic else "NONE",
            ["verified_allowance_rules"] if basic else ["confirmed_contract_base", "base_scope_confirmed"],
        ),
        (comparison, "PARTIAL" if comparison else "NONE", [] if comparison else ["confirmed_matching_basis"]),
        (
            any(statement),
            "FULL" if all(statement) else ("PARTIAL" if any(statement) else "NONE"),
            [
                key
                for key, ready in zip(
                    ("paymentItems", "deductionItems", "confirmed_payslip_totals"), statement
                )
                if not ready
            ],
        ),
        (
            payment,
            "FULL" if payment else "NONE",
            [] if payment else ["confirmed_payslip_net_pay", "linked_payment"],
        ),
    ]
    group = con.execute("SELECT * FROM comparison_groups WHERE id=?", (f"default-{case_id}",)).fetchone()
    modern = bool(group and group["confirmed_json"] != "{}" and not group["needs_review"])
    return {
        "caseId": case_id,
        "inputVersion": data["case"]["input_version"],
        "checks": [
            {
                "checkType": check,
                "available": available or modern,
                "mode": mode if available else ("PARTIAL" if modern else "NONE"),
                "missingInputs": missing,
                "reasonCode": None
                if mode == "FULL"
                else "PARTIAL_INPUTS"
                if available or modern
                else "MISSING_REQUIRED_INPUT",
            }
            for check, (available, mode, missing) in zip(ALL_CHECKS, states)
        ],
    }


@router.get("/cases/{case_id}/conditions", response_model=Envelope)
def get_conditions(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(localize_conditions(condition_dto(con, case_id), user_language(user)))


@router.put("/cases/{case_id}/conditions", response_model=Envelope)
def put_conditions(case_id: int, body: ConditionsPut, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user["id"])
        check_version(c, body.expectedInputVersion)
        answers = load(c["conditions"])
        seen = set()
        for answer in body.answers:
            key, value = answer.conditionId, answer.value
            if key in seen or key not in {v["conditionId"] for v in CONDITIONS}:
                raise ApiError(422, "INVALID_CONDITION_ID")
            seen.add(key)
            if value != "UNKNOWN":
                valid = (
                    (key == "regular_minutes" and type(value) is int and 0 <= value <= 1000000)
                    or (
                        key
                        in (
                            "base_scope_confirmed",
                            "comparison_basis_confirmed",
                            "hours_basis_confirmed",
                            "base_pay_basis_confirmed",
                        )
                        and type(value) is bool
                    )
                    or (key == "workplace_size" and value in ("LESS_THAN_5", "AT_LEAST_5"))
                )
                if not valid:
                    raise ApiError(422, "INVALID_CONDITION_VALUE", details={"conditionId": key})
            answers[key] = value
        con.execute("UPDATE cases SET conditions=? WHERE id=?", (dump(answers), case_id))
        mutation = schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)
        return ok(
            {
                "remainingQuestions": sum(
                    c["conditionId"] not in answers or answers[c["conditionId"]] == "UNKNOWN"
                    for c in CONDITIONS
                ),
                "calculationAvailable": readiness_data(con, case_id)["checks"][0]["available"],
                **mutation,
            }
        )


@router.get("/cases/{case_id}/readiness", response_model=Envelope)
def readiness(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(readiness_data(con, case_id))


def manual_dto(row):
    return {
        "recordId": row["id"],
        "caseId": row["case_id"],
        "recordType": row["record_type"],
        "data": load(row["data_json"]),
        "source": "USER_INPUT",
        "createdAt": row["created_at"],
    }


def validate_manual_period(c, body):
    if body.recordType == "ATTENDANCE" and not c["period_start"] <= body.data["workDate"] <= c["period_end"]:
        raise ApiError(422, "INVALID_PERIOD", "근무일이 사례 산정기간 밖입니다.")


@router.post("/cases/{case_id}/manual-records", status_code=201, response_model=Envelope)
def create_manual(case_id: int, body: ManualCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user["id"])
        check_version(c, body.expectedInputVersion)
        validate_manual_period(c, body)
        if body.recordType == "DEPOSIT":
            from app.routers.payments import insert_transaction, insert_allocation

            tid = insert_transaction(con, c["workplace_id"], body.data)
            allocation = insert_allocation(con, case_id, tid, body.data["amount"])
            return ok(
                {
                    "recordId": None,
                    "transactionId": tid,
                    "allocationId": allocation["allocationId"],
                    "canonicalResource": "PAYMENT_TRANSACTION",
                    **schedule(
                        con,
                        case_id,
                        ["DEPOSIT_COMPARISON"],
                        request.app.state.settings.recompute_debounce_seconds,
                    ),
                },
                201,
            )
        rid = con.execute(
            "INSERT INTO manual_records(case_id,record_type,data_json,created_at) VALUES(?,?,?,?)",
            (case_id, body.recordType, dump(body.data), now()),
        ).lastrowid
        return ok(
            {
                "recordId": rid,
                **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds),
            },
            201,
        )


@router.get("/cases/{case_id}/manual-records", response_model=Envelope)
def list_manual(
    case_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user=Depends(current_user),
):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(
            page_data(
                [
                    manual_dto(row)
                    for row in con.execute(
                        "SELECT * FROM manual_records WHERE case_id=? ORDER BY id", (case_id,)
                    )
                ],
                page,
                size,
            )
        )


def manual_record(con, case_id, record_id):
    row = con.execute(
        "SELECT * FROM manual_records WHERE id=? AND case_id=?", (record_id, case_id)
    ).fetchone()
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    return row


@router.get("/cases/{case_id}/manual-records/{record_id}", response_model=Envelope)
def get_manual(case_id: int, record_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(manual_dto(manual_record(con, case_id, record_id)))


@router.patch("/cases/{case_id}/manual-records/{record_id}", response_model=Envelope)
def patch_manual(
    case_id: int, record_id: int, body: ManualPatch, request: Request, user=Depends(current_user)
):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user["id"])
        check_version(c, body.expectedInputVersion)
        original = manual_record(con, case_id, record_id)
        if body.recordType is not None and original["record_type"] != body.recordType:
            raise ApiError(422, "RECORD_TYPE_IMMUTABLE")
        try:
            body = ManualCreate(
                recordType=original["record_type"],
                data=body.data,
                expectedInputVersion=body.expectedInputVersion,
            )
        except ValueError as exc:
            raise ApiError(422, "INVALID_MANUAL_RECORD") from exc
        validate_manual_period(c, body)
        con.execute("UPDATE manual_records SET data_json=? WHERE id=?", (dump(body.data), record_id))
        return ok(
            {
                "recordId": record_id,
                **schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds),
            }
        )


@router.delete("/cases/{case_id}/manual-records/{record_id}", status_code=204)
def delete_manual(case_id: int, record_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        case(con, case_id, user["id"])
        manual_record(con, case_id, record_id)
        con.execute("DELETE FROM manual_records WHERE id=?", (record_id,))
        schedule(con, case_id, delay=request.app.state.settings.recompute_debounce_seconds)
    return Response(status_code=204)


@router.get("/cases/{case_id}/results", response_model=API[ResultsDTO])
def get_results(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(localize_results(results(con, case_id), user_language(user)))


@router.get("/cases/{case_id}/recompute-status", response_model=API[RecomputeDTO])
def recompute_status(case_id: int, request: Request, user=Depends(current_user)):
    from app.engine import job_dto

    with request.app.state.db.connect() as con:
        c = case(con, case_id, user["id"])
        rows = con.execute(
            "SELECT * FROM jobs WHERE case_id=? ORDER BY created_at DESC", (case_id,)
        ).fetchall()
        active = next((j for j in rows if j["status"] in ("PENDING", "RUNNING")), None)
        return ok(
            {
                "caseId": case_id,
                "currentInputVersion": c["input_version"],
                "activeJob": job_dto(active) if active else None,
                "latestJob": job_dto(rows[0]) if rows else None,
                "results": results(con, case_id)["versions"],
            }
        )


@router.get("/cases/{case_id}/recompute-jobs/{job_id}", response_model=Envelope)
def get_job(case_id: int, job_id: str, request: Request, user=Depends(current_user)):
    from app.engine import job_dto

    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        row = con.execute("SELECT * FROM jobs WHERE id=? AND case_id=?", (job_id, case_id)).fetchone()
        if row is None:
            raise ApiError(404, "RESOURCE_NOT_FOUND")
        return ok(job_dto(row))


def queue_checks(case_id, body, request, user, checks):
    from app.engine import job_dto

    if not body.confirmed:
        raise ApiError(422, "CONFIRMATION_REQUIRED")
    with request.app.state.db.connect(write=True) as con:
        current = case(con, case_id, user["id"])
        cached = replay(con, request, user["id"], body)
        if cached is not None:
            return cached
        check_version(current, body.expectedInputVersion)
        if body.checks and not request.url.path.endswith("/recompute") and set(body.checks) != set(checks):
            raise ApiError(422, "INVALID_CHECK_TYPE")
        mutation = schedule(con, case_id, body.checks or checks, delay=0, increment=False)
        row = con.execute("SELECT * FROM jobs WHERE id=?", (mutation["recompute"]["jobId"],)).fetchone()
        return remember(con, request, user["id"], body, {**job_dto(row), **mutation}, 202)


@router.post(
    "/cases/{case_id}/recompute",
    status_code=202,
    dependencies=[Depends(idempotency_header)],
    response_model=Envelope,
)
def recompute(case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    return queue_checks(case_id, body, request, user, ALL_CHECKS)


@router.post(
    "/cases/{case_id}/calculations",
    status_code=202,
    dependencies=[Depends(idempotency_header)],
    response_model=Envelope,
)
def calculations(case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    return queue_checks(case_id, body, request, user, ["WAGE_CALCULATION"])


@router.post(
    "/cases/{case_id}/comparisons",
    status_code=202,
    dependencies=[Depends(idempotency_header)],
    response_model=Envelope,
)
def comparisons(case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)):
    return queue_checks(case_id, body, request, user, ["CONTRACT_ATTENDANCE_COMPARISON"])


@router.post(
    "/cases/{case_id}/payslip-validations",
    status_code=202,
    dependencies=[Depends(idempotency_header)],
    response_model=Envelope,
)
def payslip_validations(
    case_id: int, request: Request, body: Execute = Execute(), user=Depends(current_user)
):
    return queue_checks(case_id, body, request, user, ["PAYSLIP_VALIDATION"])


def snapshot_list(con, case_id, kind, page, size):
    _, ref = latest(con, case_id, kind)
    rows = con.execute(
        "SELECT * FROM snapshots WHERE case_id=? AND kind=? ORDER BY version DESC", (case_id, kind)
    ).fetchall()
    return page_data(
        [
            snapshot_dto(row, ref["freshness"] if row["id"] == ref["snapshotId"] else "STALE")
            for row in rows
            if snapshot_accessible(con, row)
        ],
        page,
        size,
    )


@router.get("/cases/{case_id}/calculations", response_model=Envelope)
def list_calculations(
    case_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user=Depends(current_user),
):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(snapshot_list(con, case_id, "calculation", page, size))


@router.get("/cases/{case_id}/calculations/{calculation_id}", response_model=Envelope)
def get_calculation(case_id: int, calculation_id: str, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        row = con.execute(
            "SELECT * FROM snapshots WHERE id=? AND case_id=? AND kind='calculation'",
            (calculation_id, case_id),
        ).fetchone()
        if row is None or not snapshot_accessible(con, row):
            raise ApiError(404, "RESOURCE_NOT_FOUND")
        current, ref = latest(con, case_id, "calculation")
        return ok(snapshot_dto(row, ref["freshness"] if current and row["id"] == current["id"] else "STALE"))


@router.get("/cases/{case_id}/comparisons", response_model=Envelope)
def list_comparisons(
    case_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user=Depends(current_user),
):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(snapshot_list(con, case_id, "comparison", page, size))


@router.get("/cases/{case_id}/payslip-validations", response_model=Envelope)
def list_validations(
    case_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user=Depends(current_user),
):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        return ok(snapshot_list(con, case_id, "payslipValidation", page, size))


@router.get("/cases/{case_id}/questions", response_model=Envelope)
def questions(case_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        row, ref = latest(con, case_id)
        if row is None:
            return ok({"questions": []})
        body = load(row["data_json"])
        result = load(
            con.execute(
                "SELECT data_json FROM agent_results WHERE id=?", (body["_agentResultId"],)
            ).fetchone()[0]
        )
        questions = [
            {
                "questionId": i + row["id"] * 1000,
                "agentQuestionId": q["question_id"],
                "type": q["reason_code"],
                "question": q["text"],
                "reason": q.get("reason", ""),
                "relatedItemId": None,
            }
            for i, q in enumerate(result["questions"])
        ]
    # No database transaction is held across the translation call.
    language = user_language(user)
    texts = request.app.state.localizer.translate(
        [text for q in questions for text in (q["question"], q["reason"])], language
    )
    for index, q in enumerate(questions):
        q["question"], q["reason"] = texts[2 * index], texts[2 * index + 1]
    return ok({"questions": questions})


@router.post("/cases/{case_id}/company-inquiry", response_model=Envelope)
def inquiry(case_id: int, body: Inquiry, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        data = results(con, case_id)
        if data["freshness"]["overall"] != "CURRENT":
            raise ApiError(409, "REPORT_SOURCE_STALE")
        versions = {key: ref["version"] for key, ref in data["versions"].items()}
        if body.basedOnResultVersions and any(
            key not in versions or versions[key] != value for key, value in body.basedOnResultVersions.items()
        ):
            raise ApiError(409, "INPUT_VERSION_CONFLICT")
        allowed = {i["id"] for i in data["items"]}
        if set(body.resultItemIds) - allowed:
            raise ApiError(422, "INVALID_RESULT_ITEM")
        mapping = {
            "HOURLY_WAGE": ("comparison", "R-01"),
            "WORKED_MINUTES": ("comparison", "R-02"),
            "BASE_PAY": ("comparison", "R-07"),
            "paymentTotal": ("payslipValidation", "R-03"),
            "deductionTotal": ("payslipValidation", "R-04"),
            "netPay": ("payslipValidation", "R-05"),
            "NET_VS_PAYMENT": ("depositComparison", "R-06"),
        }
        selected = body.resultItemIds or [i["id"] for i in data["items"]]
        grouped = {}
        for item in selected:
            kind, rule = mapping[item]
            row, _ = latest(con, case_id, kind)
            rid = load(row["data_json"])["_agentResultId"]
            grouped.setdefault(rid, []).append(rule)
    generated = [
        request.app.state.service.inquiry(user["id"], rid, rule_ids=rules) for rid, rules in grouped.items()
    ]
    text = {**generated[0], "text": "\n\n".join(g["text"] for g in generated)}
    language = user_language(user)
    if language != "ko":
        # The draft is written in the user's language; lines that cannot be translated safely stay Korean.
        lines = text["text"].split("\n")
        text["text"] = "\n".join(request.app.state.localizer.translate(lines, language))
    return ok(
        {
            **text,
            "basedOnResultVersions": versions,
            "requiresUserReview": True,
            "selectedResultItemIds": selected,
        }
    )
