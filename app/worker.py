"""Single-app durable queue consumers; no transaction spans agent execution."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def document_worker(state):
    """Legacy uploads read and extract; review workspaces keep explicit AI actions."""
    while True:
        with state.db.connect() as con:
            row = con.execute("""SELECT d.id,w.user_id FROM documents d JOIN workplaces w ON w.id=d.workplace_id
                JOIN users u ON u.id=w.user_id WHERE d.processing_status IN ('UPLOADED','READ_READY') AND d.deleted_at IS NULL
                AND w.deleted_at IS NULL AND u.deleted_at IS NULL
                AND (d.processing_status='UPLOADED' OR json_extract(d.ai_metadata,'$.legacyAutoExtract')=1)
                AND NOT EXISTS (SELECT 1 FROM agent_jobs j WHERE j.document_id=d.id AND j.status IN ('PENDING','RUNNING'))
                ORDER BY d.id LIMIT 1""").fetchone()
        if row:
            try:
                with state.db.connect() as con:
                    from app.db import load

                    doc = con.execute("SELECT * FROM documents WHERE id=?", (row["id"],)).fetchone()
                    metadata = load(doc["ai_metadata"]) or {}
                if doc["processing_status"] == "UPLOADED":
                    state.service.enqueue_read(row["user_id"], row["id"])
                elif metadata.get("legacyAutoExtract"):
                    if state.service.provider is not None or (
                        state.settings.openai_api_key and state.settings.openai_api_key.get_secret_value()
                    ):
                        state.service.enqueue_extraction(row["user_id"], row["id"])
                    else:
                        with state.db.connect(write=True) as con:
                            con.execute(
                                "UPDATE documents SET processing_status='REVIEW_REQUIRED',error_code='MODEL_NOT_CONFIGURED' WHERE id=? AND processing_status='READ_READY'",
                                (row["id"],),
                            )
            except Exception:
                logger.exception("document_read_enqueue_failed document_id=%s", row["id"])
        await asyncio.sleep(0.1)


async def agent_worker(state):
    prefer_review = True
    while True:
        try:
            from app.routers.reports import progress_reports

            progress_reports(state)
            first, second = (
                (state.reviews.execute_next, state.service.execute_next)
                if prefer_review
                else (state.service.execute_next, state.reviews.execute_next)
            )
            from app.compatibility import execute_next as execute_case

            did_work = await execute_case(state)
            if not did_work:
                did_work = await first()
            if not did_work:
                did_work = await second()
            prefer_review = not prefer_review
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent_worker_failed")
            did_work = False
        await asyncio.sleep(0 if did_work else 0.05)


recompute_worker = agent_worker
