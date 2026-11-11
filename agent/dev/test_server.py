"""Loopback-only development API and buildless HTML console.

Run: python -m agent.dev.test_server --port 4190
Not an operating backend, authentication service, or multi-process job store.
"""

import argparse
import asyncio
import json
import re
import socket
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from agent.config import ENGINE_VERSION, AgentSettings
from agent.dev.local_workflow import LOCAL_OUTPUT, ComparisonInputError, LocalStore
from agent.dev.pdf_preview import render_page
from agent.services.extraction_profiles import PROFILES

WEB = Path(__file__).parent / "web"


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR permits simultaneous listeners on Windows. An older process
    # must never share this console's port and serve stale code or job state.
    allow_reuse_address = (
        False if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else ThreadingHTTPServer.allow_reuse_address
    )

    def __init__(self, address, store, loop):
        if address[0] != "127.0.0.1":
            raise ValueError("This tool must bind to 127.0.0.1")
        self.store, self.loop = store, loop
        super().__init__(address, Handler)

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def call(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=15)
        except FutureTimeout:
            future.cancel()
            raise ValueError("로컬 연결 응답 시간이 초과되었습니다.") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalAgent/0.2"

    def log_message(self, format, *args):
        # Do not log uploaded bytes, queries, tokens, or PDF text.
        return

    def respond(self, status, body, mime="application/json; charset=utf-8", *, download=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'self'; frame-ancestors 'self'",
        )
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def guard(self, mutation=False):
        host = self.headers.get("Host", "")
        expected = f"127.0.0.1:{self.server.server_port}"
        if host != expected:
            raise PermissionError("허용되지 않은 호스트입니다.")
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + expected:
            raise PermissionError("다른 사이트의 요청은 허용되지 않습니다.")
        if mutation and self.headers.get("X-Local-Agent") != "1":
            raise PermissionError("로컬 화면의 요청 헤더가 필요합니다.")

    def body(self, *, pdf=False):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise ValueError("잘못된 요청 길이입니다.") from None
        limit = self.server.store.settings.max_file_bytes if pdf else 256 * 1024
        if not 0 <= length <= limit:
            raise ValueError("요청 크기 제한을 초과했습니다.")
        self.connection.settimeout(20)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("업로드가 완료되지 않았습니다.")
        if pdf:
            if self.headers.get("Content-Type", "").split(";")[0] != "application/pdf":
                raise ValueError("PDF 형식으로 업로드해 주세요.")
            return data
        return json.loads(data or b"{}")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def dispatch(self, method):
        try:
            self.guard(method != "GET")
            self.route(method, urlsplit(self.path).path)
        except BrokenPipeError, ConnectionResetError:
            pass
        except PermissionError as exc:
            self.respond(403, {"error": str(exc)})
        except KeyError:
            self.respond(404, {"error": "자료 또는 작업을 찾을 수 없습니다."})
        except ComparisonInputError as exc:
            self.respond(400, {"error": str(exc)})
        except ValueError, ValidationError, TypeError:
            self.respond(
                400,
                {
                    "error": "입력 형식을 확인해 주세요. 날짜·금액·시간 기준 또는 작업 순서가 올바르지 않습니다."
                },
            )
        except Exception:
            self.respond(500, {"error": "로컬 연결 처리에 실패했습니다."})

    def route(self, method, path):
        store, call = self.server.store, self.server.call
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/ocr": ("index.html", "text/html; charset=utf-8"),
            "/ai": ("index.html", "text/html; charset=utf-8"),
            "/compare": ("index.html", "text/html; charset=utf-8"),
            "/results": ("index.html", "text/html; charset=utf-8"),
            "/questions": ("index.html", "text/html; charset=utf-8"),
            "/inquiry": ("index.html", "text/html; charset=utf-8"),
            "/reports": ("index.html", "text/html; charset=utf-8"),
            "/glossary": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/results.js": ("results.js", "text/javascript; charset=utf-8"),
            "/compare.js": ("compare.js", "text/javascript; charset=utf-8"),
            "/pages.js": ("pages.js", "text/javascript; charset=utf-8"),
            "/preview.html": ("preview.html", "text/html; charset=utf-8"),
            "/preview.js": ("preview.js", "text/javascript; charset=utf-8"),
            "/preview.css": ("preview.css", "text/css; charset=utf-8"),
        }
        if method == "GET" and path in assets:
            name, mime = assets[path]
            return self.respond(200, (WEB / name).read_bytes(), mime)
        if method == "GET" and path == "/api/health":
            return self.respond(
                200,
                {
                    "status": "ok",
                    "provider_mode": store.settings.provider_mode,
                    "model": store.settings.extraction_model,
                    "release_ready": False,
                    "engine_version": ENGINE_VERSION,
                    "extraction_pipeline": "text_first_with_pdf_supplement",
                },
            )
        if method == "GET" and path == "/api/documents":
            return self.respond(200, {"documents": call(store.list_documents())})
        if method == "GET" and path == "/api/extraction-profiles":
            return self.respond(200, PROFILES)
        if method == "GET" and path == "/api/glossary":
            from agent.services.glossary import GLOSSARY_VERSION, glossary_entries

            query = parse_qs(urlsplit(self.path).query)
            return self.respond(
                200,
                {
                    "version": GLOSSARY_VERSION,
                    "entries": glossary_entries(
                        query.get("query", [""])[0], query.get("category", [None])[0]
                    ),
                },
            )
        if method == "GET" and path == "/api/comparison-sources":
            return self.respond(200, {"sources": call(store.list_comparison_sources())})
        match = re.fullmatch(r"/api/documents/([a-z0-9-]+)/pdf", path)
        if method == "GET" and match:
            return self.respond(200, store.document_data(match[1]), "application/pdf")
        match = re.fullmatch(r"/api/documents/([a-z0-9-]+)/pages/(\d+)\.png", path)
        if method == "GET" and match:
            doc_id, page = match[1], int(match[2])
            if not 1 <= page <= store.documents[doc_id]["pages"]:
                raise ValueError("Invalid page")
            return self.respond(
                200, render_page(store.document_data(doc_id), page, store.state / "previews"), "image/png"
            )
        if method == "POST" and path == "/api/uploads":
            query = parse_qs(urlsplit(self.path).query)
            document_type = query.get("document_type", ["auto"])[0]
            filename = unquote(self.headers.get("X-Document-Name", "")) or None
            return self.respond(201, call(store.upload(self.body(pdf=True), document_type, filename)))
        if method == "POST" and path == "/api/cases":
            body = self.body()
            return self.respond(
                201, call(store.create_case(body["document_id"], body.get("document_type", "auto")))
            )
        if path == "/api/comparisons":
            if method == "GET":
                return self.respond(200, {"comparisons": call(store.list_comparisons())})
            if method == "POST":
                return self.respond(201, call(store.create_comparison(**self.body())))
        match = re.fullmatch(
            r"/api/(cases|comparisons)/([a-z0-9-]+)(?:/(draft|confirm|jobs|document-type|materials|candidate-selection|inquiry|answers))?",
            path,
        )
        if match:
            kind, cid, action = match.groups()
            records = store.comparisons if kind == "comparisons" else store.cases
            if cid not in records:
                raise KeyError(cid)
            if method == "GET" and action is None:
                getter = store.get_comparison if kind == "comparisons" else store.get_case
                return self.respond(200, call(getter(cid)))
            if method == "PUT" and action == "draft":
                return self.respond(200, call(store.save_draft(cid, self.body())))
            if method in ("POST", "PUT") and action == "inquiry":
                return self.respond(200, call(store.inquiry(cid, self.body(), save=method == "PUT")))
            if method == "PUT" and action == "answers":
                return self.respond(200, call(store.answer_questions(cid, self.body())))
            if method == "PUT" and action == "document-type" and kind == "cases":
                return self.respond(200, call(store.change_document_type(cid, self.body()["document_type"])))
            if method == "PUT" and action == "materials":
                return self.respond(200, call(store.load_materials(cid, self.body())))
            if method == "POST" and action == "confirm":
                return self.respond(200, call(store.confirm(cid)))
            if method == "POST" and action == "candidate-selection" and kind == "cases":
                return self.respond(200, call(store.select_candidate(cid, self.body())))
            if method == "POST" and action == "jobs":
                body = self.body()
                if set(body) - {
                    "stage",
                    "include_original",
                    "field_ids",
                    "extraction_revision",
                    "selected_source_ids",
                    "result_id",
                }:
                    raise ValueError("지원하지 않는 작업 요청 항목입니다.")
                if type(body.get("include_original", False)) is not bool:
                    raise ValueError("Boolean options required")
                return self.respond(
                    202,
                    call(
                        store.create_job(
                            cid,
                            body["stage"],
                            include_original=body.get("include_original", False),
                            field_ids=body.get("field_ids"),
                            extraction_revision=body.get("extraction_revision"),
                            selected_source_ids=body.get("selected_source_ids"),
                            result_id=body.get("result_id"),
                        )
                    ),
                )
        match = re.fullmatch(r"/api/jobs/([a-z0-9-]+)(?:/(cancel|resume))?", path)
        if match:
            jid, action = match.groups()
            if method == "GET" and action is None:
                return self.respond(200, call(store.get_job(jid)))
            if method == "POST" and action in ("cancel", "resume"):
                return self.respond(
                    202, call(store.cancel_job(jid) if action == "cancel" else store.resume_job(jid))
                )
        match = re.fullmatch(r"/api/artifacts/([0-9a-f]{64})", path)
        if method == "GET" and match:
            data, mime = call(store.artifact(match[1]))
            return self.respond(
                200,
                data,
                mime,
                download="agent-report.pdf" if mime == "application/pdf" else "agent-report.zip",
            )
        self.respond(404, {"error": "지원하지 않는 경로입니다."})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4190)
    parser.add_argument("--env", type=Path, default=Path("agent/.env"))
    parser.add_argument("--corpus", type=Path, default=LOCAL_OUTPUT)
    args = parser.parse_args()
    settings = AgentSettings.from_env(args.env, provider_mode="openai")
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def initialize():
        return LocalStore(settings, corpus=args.corpus)

    store = asyncio.run_coroutine_threadsafe(initialize(), loop).result()
    server = LocalHTTPServer(("127.0.0.1", args.port), store, loop)
    print(f"Local agent console: http://127.0.0.1:{args.port} ({settings.provider_mode})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

        async def shutdown():
            for task in store.tasks.values():
                task.cancel()
            await asyncio.gather(*store.tasks.values(), return_exceptions=True)

        asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
