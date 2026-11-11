"""Case report compatibility API; durable stages use the signed agent pipeline."""

from app.schemas import Envelope

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import Field

from agent.schemas.public.errors import AgentError
from app.compatibility import enqueue_current
from app.db import dump, load, now
from app.engine import results, snapshot_accessible
from app.errors import ApiError, ok
from app.idempotency import idempotency_header, replay, remember
from app.routers.workspaces import page_data
from app.schemas import Input
from app.security import case, current_user

router = APIRouter(tags=["Reports"])


class ReportCreate(Input):
    includeOriginalDocuments: bool = True
    documentIds: list[int] = Field(default_factory=list, max_length=100)
    useLatestAvailableResults: bool = True
    acknowledgeOutdatedResults: bool = False


def owned_report(con, cid, rid, uid):
    case(con, cid, uid)
    row = con.execute("SELECT * FROM reports WHERE id=? AND case_id=?", (rid, cid)).fetchone()
    if row is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    frozen = load(row["frozen_json"])
    # Recheck the original grants even if the job is pending or the case changed.
    for sid in frozen["snapshotIds"]:
        saved = con.execute("SELECT * FROM snapshots WHERE id=? AND case_id=?", (sid, cid)).fetchone()
        if saved is None or not snapshot_accessible(con, saved):
            raise ApiError(404, "RESOURCE_NOT_FOUND")
    return row


def report_dto(con, row):
    frozen = load(row["frozen_json"])
    current = results(con, row["case_id"])
    same = all(
        current["versions"][k]["snapshotId"] == v["snapshotId"]
        and current["versions"][k]["freshness"] == "CURRENT"
        for k, v in frozen["referencedSnapshots"].items()
    )
    return {
        "reportId": row["id"],
        "caseId": row["case_id"],
        "version": row["version"],
        "status": row["status"],
        "basedOnInputVersion": row["input_version"],
        "referencedSnapshots": frozen["referencedSnapshots"],
        "selectedDocumentIds": load(row["document_ids"]),
        "createdAt": row["created_at"],
        "isCurrent": same,
        "errorCode": row["error_code"],
        "pdfUrl": f"/api/v1/cases/{row['case_id']}/reports/{row['id']}/pdf"
        if row["status"] == "READY"
        else None,
        "originalsUrl": f"/api/v1/cases/{row['case_id']}/reports/{row['id']}/originals"
        if row["status"] == "READY"
        else None,
    }


@router.post(
    "/cases/{case_id}/reports",
    status_code=202,
    dependencies=[Depends(idempotency_header)],
    response_model=Envelope,
)
def create_report(case_id: int, body: ReportCreate, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect(write=True) as con:
        c = case(con, case_id, user["id"])
        cached = replay(con, request, user["id"], body)
        if cached is not None:
            return cached
        source = results(con, case_id)
        if not body.useLatestAvailableResults or source["freshness"]["overall"] != "CURRENT":
            raise ApiError(409, "REPORT_SOURCE_STALE")
        docs = body.documentIds if body.includeOriginalDocuments else []
        linked = {
            r[0]
            for r in con.execute(
                "SELECT l.document_id FROM document_links l JOIN documents d ON l.document_id=d.id WHERE l.case_id=? AND d.deleted_at IS NULL",
                (case_id,),
            )
        }
        if len(docs) != len(set(docs)) or any(type(d) is not int or d <= 0 or d not in linked for d in docs):
            raise ApiError(422, "REPORT_SOURCE_OUTSIDE_RESULT")
        jid = enqueue_current(con, request.app.state.service, case_id, metadata={"legacyReport": True})
        version = con.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM reports WHERE case_id=?", (case_id,)
        ).fetchone()[0]
        frozen = {
            "analysisJobId": jid,
            "snapshotIds": [r["snapshotId"] for r in source["versions"].values()],
            "referencedSnapshots": source["versions"],
        }
        rid = con.execute(
            "INSERT INTO reports(case_id,version,input_version,status,frozen_json,document_ids,created_at) VALUES(?,?,?,?,?,?,?)",
            (case_id, version, c["input_version"], "PROCESSING", dump(frozen), dump(docs), now()),
        ).lastrowid
        row = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        return remember(con, request, user["id"], body, report_dto(con, row), 202)


@router.get("/cases/{case_id}/reports", response_model=Envelope)
def list_reports(
    case_id: int,
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user=Depends(current_user),
):
    with request.app.state.db.connect() as con:
        case(con, case_id, user["id"])
        items = []
        for r in con.execute("SELECT * FROM reports WHERE case_id=? ORDER BY version DESC", (case_id,)):
            try:
                owned_report(con, case_id, r["id"], user["id"])
            except ApiError:
                continue
            items.append(report_dto(con, r))
        return ok(page_data(items, page, size))


@router.get("/cases/{case_id}/reports/{report_id}", response_model=Envelope)
def get_report(case_id: int, report_id: int, request: Request, user=Depends(current_user)):
    with request.app.state.db.connect() as con:
        return ok(report_dto(con, owned_report(con, case_id, report_id, user["id"])))


def download(cid, rid, request, user, mime, name):
    with request.app.state.db.connect() as con:
        row = owned_report(con, cid, rid, user["id"])
        if row["status"] != "READY":
            raise ApiError(409, "REPORT_NOT_READY")
        artifacts = load(row["frozen_json"])["artifacts"]
        aid = next((a["artifact_id"] for a in artifacts if a["mime_type"] == mime), None)
    if aid is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND")
    path, artifact = request.app.state.service.store.artifact(user["id"], aid)
    return FileResponse(path, media_type=artifact["mime_type"], filename=f"report-{rid}.{name}")


@router.get("/cases/{case_id}/reports/{report_id}/pdf", response_class=FileResponse)
def pdf(case_id: int, report_id: int, request: Request, user=Depends(current_user)):
    return download(case_id, report_id, request, user, "application/pdf", "pdf")


@router.get("/cases/{case_id}/reports/{report_id}/originals", response_class=FileResponse)
def originals(case_id: int, report_id: int, request: Request, user=Depends(current_user)):
    return download(case_id, report_id, request, user, "application/zip", "zip")


def progress_reports(state):
    """One resumable step; enqueue with stable keys outside the write transaction."""
    with state.db.connect() as con:
        rows = con.execute(
            "SELECT r.*,w.user_id FROM reports r JOIN cases c ON r.case_id=c.id JOIN workplaces w ON c.workplace_id=w.id WHERE r.status='PROCESSING' AND c.deleted_at IS NULL AND w.deleted_at IS NULL ORDER BY r.id"
        ).fetchall()
    changed = False
    for row in rows:
        frozen = load(row["frozen_json"])
        try:
            with state.db.connect() as con:
                owned_report(con, row["case_id"], row["id"], row["user_id"])
                jid = frozen.get("reportJobId") or frozen.get("explanationJobId") or frozen["analysisJobId"]
                job = con.execute("SELECT * FROM agent_jobs WHERE id=?", (jid,)).fetchone()
            if job["status"] in ("PENDING", "RUNNING"):
                continue
            if job["status"] != "COMPLETED":
                raise ApiError(409, job["error_code"] or "REPORT_GENERATION_FAILED")
            if "reportJobId" in frozen:
                frozen["artifacts"] = load(job["output_json"])["artifacts"]
                status = "READY"
            elif "explanationJobId" in frozen:
                frozen["reportJobId"] = state.service.enqueue_report(
                    row["user_id"],
                    frozen["resultId"],
                    frozen["explanationJobId"],
                    selected_document_ids=load(row["document_ids"]),
                    idempotency_key=f"case-report:{row['id']}:build",
                )
                status = "PROCESSING"
            else:
                frozen["resultId"] = job["result_id"]
                frozen["explanationJobId"] = state.service.enqueue_explanation(
                    row["user_id"], job["result_id"], idempotency_key=f"case-report:{row['id']}:explain"
                )
                status = "PROCESSING"
            with state.db.connect(write=True) as con:
                con.execute(
                    "UPDATE reports SET frozen_json=?,status=? WHERE id=? AND status='PROCESSING'",
                    (dump(frozen), status, row["id"]),
                )
            changed = True
        except (ApiError, AgentError) as exc:
            with state.db.connect(write=True) as con:
                con.execute(
                    "UPDATE reports SET status='FAILED',error_code=? WHERE id=? AND status='PROCESSING'",
                    (exc.code, row["id"]),
                )
            changed = True
    return changed
