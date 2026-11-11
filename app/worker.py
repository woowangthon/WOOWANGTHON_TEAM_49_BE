"""Single-app durable queue consumers; no transaction spans agent execution."""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def document_worker(state):
    """Uploads trigger local reading only; an explicit API action starts AI."""
    while True:
        with state.db.connect() as con:
            row = con.execute('''SELECT d.id,w.user_id FROM documents d JOIN workplaces w ON w.id=d.workplace_id
                JOIN users u ON u.id=w.user_id WHERE d.processing_status='UPLOADED' AND d.deleted_at IS NULL
                AND w.deleted_at IS NULL AND u.deleted_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM agent_jobs j WHERE j.document_id=d.id AND j.status IN ('PENDING','RUNNING'))
                ORDER BY d.id LIMIT 1''').fetchone()
        if row:
            try:
                state.service.enqueue_read(row['user_id'],row['id'])
            except Exception:
                logger.exception('document_read_enqueue_failed document_id=%s',row['id'])
        await asyncio.sleep(0.1)


async def agent_worker(state):
    prefer_review = True
    while True:
        try:
            first, second = (state.reviews.execute_next, state.service.execute_next) if prefer_review else (
                state.service.execute_next, state.reviews.execute_next)
            did_work = await first()
            if not did_work:
                did_work = await second()
            prefer_review = not prefer_review
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('agent_worker_failed')
            did_work = False
        await asyncio.sleep(0 if did_work else 0.05)


recompute_worker = agent_worker
