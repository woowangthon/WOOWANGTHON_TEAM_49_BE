import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.ai import AIAdapter
from app.config import Settings
from app.centers import seed_centers
from app.db import Database, load
from app.errors import ApiError, ok
from app.schemas import Envelope
from app.outputs import API, ErrorData
from app.routers import analysis, auth, chats, documents, payments, workspaces, integration, reviews, reports
from app.worker import document_worker, agent_worker
from app.agent_service import AgentService
from agent.schemas.public.errors import AgentError


def create_app(settings=None, ai_client=None, *, provider=None, recognizer=None, review_model=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        for directory in ('uploads', 'reports'):
            (settings.data_dir / directory).mkdir(exist_ok=True)
        app.state.settings = settings
        app.state.db = Database(settings.db_path)
        app.state.db.initialize()
        app.state.agent_settings = settings.agent_settings()
        app.state.service = AgentService(app.state.db, settings, app.state.agent_settings,
            provider=provider, recognizer=recognizer)
        app.state.service.store.initialize()
        from app.agent_review import initialize_review
        initialize_review(app.state.db)
        app.state.service.store.recover()
        from app.review_service import ReviewService
        app.state.reviews = ReviewService(app.state.service, review_model)
        app.state.reviews.store.initialize()
        with app.state.db.connect(write=True) as con:
            seed_centers(con)
            con.execute("UPDATE jobs SET status='PENDING' WHERE status='RUNNING'")
            con.execute("UPDATE messages SET status='FAILED' WHERE status='STREAMING'")
        app.state.ai = AIAdapter(settings, ai_client)
        app.state.chat_runs = {}
        tasks = ([asyncio.create_task(document_worker(app.state))] +
            [asyncio.create_task(agent_worker(app.state)) for _ in range(settings.worker_concurrency)]) if settings.workers_enabled else []
        app.state.worker_tasks = tasks
        try:
            yield
        finally:
            for control in list(app.state.chat_runs.values()):
                control.cancelled.set()
                if control.task:
                    control.task.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.ai.close()

    app = FastAPI(title='월급잇다 Integrated Backend', version='1.0.0', lifespan=lifespan,
        description='Python 직접 호출 agent, 비교 묶음별 확인 입력·불변 결과. 별도 Next.js용 API.')
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],
                       allow_headers=['Authorization', 'Content-Type', 'Accept', 'Idempotency-Key'],
                       expose_headers=['Content-Disposition', 'Idempotency-Replayed'])

    @app.middleware('http')
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'code': exc.status, 'message': exc.message,
            'data': {'errorCode': exc.code, 'details': exc.details}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={'code': 422, 'message': '입력값을 확인해 주세요.',
            'data': {'errorCode': 'VALIDATION_ERROR', 'errors': [
                {'field': '.'.join(str(p) for p in e['loc']), 'reason': e['msg']} for e in exc.errors()]}})

    @app.exception_handler(AgentError)
    async def agent_error(request, exc):
        status = 404 if exc.code == 'ACCESS_REVOKED' else 409
        return JSONResponse(status_code=status, content={'code': status, 'message': '자료 접근 또는 실행 조건을 확인해 주세요.',
            'data': {'errorCode': exc.code}})

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={'code': exc.status_code,
            'message': str(exc.detail), 'data': {'errorCode': 'RESOURCE_NOT_FOUND' if exc.status_code == 404 else 'HTTP_ERROR'}})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger(__name__).exception('request_failed path=%s', request.url.path)
        return JSONResponse(status_code=500, content={'code': 500, 'message': '서버 처리에 실패했습니다.',
                                                      'data': {'errorCode': 'INTERNAL_SERVER_ERROR'}})

    @app.get('/health', tags=['System'], response_model=Envelope)
    def health():
        return ok({'status': 'ok', 'aiMode': 'direct', 'version': '1.0.0'})

    @app.get('/api/v1/consultation-centers', tags=['Consultation'], response_model=Envelope)
    def centers(request: Request, region: str | None = None, category: str | None = None,
                page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
        from app.routers.workspaces import page_data
        with request.app.state.db.connect() as con:
            records = [load(row[0]) for row in con.execute('SELECT data_json FROM consultation_centers ORDER BY id')]
        records = [r for r in records if not category or category in r['categories']]
        regional = [r for r in records if not r['isNationwide'] and (not region or region in r['region'])]
        nationwide = [r for r in records if r['isNationwide']]
        return ok({**page_data(regional + nationwide, page, size),
                   'nationwideFallbackUsed': bool(region and not regional and nationwide),
                   'dataStatus': 'NATIONWIDE_ONLY' if not regional else 'AVAILABLE'})

    for router in (auth.router, workspaces.router, documents.router, analysis.router, payments.router, chats.router, integration.router, reviews.router, reports.router):
        app.include_router(router, prefix='/api/v1', responses={
            code: {'model': API[ErrorData], 'description': description} for code, description in {
                401: '인증 필요/토큰 만료', 404: '없는 자원/권한 없음/삭제',
                409: '버전/배분/처리 상태 충돌', 422: '입력 검증 오류 (표준 래퍼)',
                502: 'AI 통신/응답 오류', 504: 'AI 타임아웃'}.items()})
    return app


app = create_app()
