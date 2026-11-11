import io
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

from agent.config import AgentSettings
from agent.services import local_ocr
from agent.tests import fixtures_support as fixtures
from agent.tests.fake_ports import FakePorts
from agent.tests.fixture_provider import FixtureProvider
from agent.tests.fixtures_support import analysis_fixture


def pytest_collection_modifyitems(items):
    root = Path(__file__).resolve().parents[2]
    corpus_available = (root / "output/contract-validation/pdfs/filled-hourly.pdf").is_file() and (
        root / "계약_법률_문서_서식_데이터"
    ).is_dir()
    if not corpus_available:
        skip = pytest.mark.skip(reason="The original validation corpus is not distributed with the source.")
        for item in items:
            if item.get_closest_marker("private_corpus"):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def synthetic_ocr(monkeypatch, request):
    """Keep pipeline tests deterministic while preserving the production OCR step.

    Synthetic PDFs retain their known source lines in this fixture only. Tests of
    the OCR engine itself opt out; explicit per-test OCR mocks also take priority.
    """
    if request.node.get_closest_marker("real_ocr"):
        return

    sources = {}
    original_pdf_bytes = fixtures.pdf_bytes

    def synthetic_pdf(lines, **options):
        lines = list(lines)
        data = original_pdf_bytes(lines, **options)
        sources[data] = "\n".join(lines)
        return data

    # Test modules import the helper before fixtures run. Patch those aliases as
    # well so image-only synthetic PDFs have known text without invoking an engine.
    for name, module in list(sys.modules.items()):
        if name == "agent.tests.fixtures_support" or name.startswith(("agent.tests.", "test_")):
            if getattr(module, "pdf_bytes", None) is original_pdf_bytes:
                monkeypatch.setattr(module, "pdf_bytes", synthetic_pdf)

    def recognize(data, settings, page_numbers=None):
        if data in sources:
            texts = [sources[data]]
        else:
            texts = [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]
        indices = page_numbers if page_numbers is not None else range(len(texts))
        return [
            {
                "text": texts[index],
                "lines": [{"text": line, "confidence": 99} for line in texts[index].splitlines()],
            }
            for index in indices
        ]

    monkeypatch.setattr(local_ocr, "recognize_pdf", recognize)


@pytest.fixture(autouse=True)
def isolated_agent_services(monkeypatch, request):
    """Inject test doubles here; the application has no fake-provider fallback."""
    import agent
    from agent.dev.local_workflow import LocalStore

    original_run = agent.run_task

    async def run_with_test_provider(task, *, provider=None, **options):
        ports = options.get("ports")
        if isinstance(ports, FakePorts):
            ports.ocr_settings = options["settings"]
        return await original_run(task, provider=provider or FixtureProvider(), **options)

    for name, module in list(sys.modules.items()):
        if name.startswith(("agent.tests.", "test_")) and getattr(module, "run_task", None) is original_run:
            monkeypatch.setattr(module, "run_task", run_with_test_provider)

    original_init = LocalStore.__init__

    def initialize_test_store(self, settings, **options):
        if options.get("provider") is None:
            options["provider"] = FixtureProvider()
        if options.get("state") is None:
            options["state"] = request.getfixturevalue("tmp_path") / "local-state"
        return original_init(self, settings, **options)

    monkeypatch.setattr(LocalStore, "__init__", initialize_test_store)
    if request.node.path.name in {"test_separated_workflow.py", "test_separated_agent.py"}:
        return
    original_job = LocalStore.create_job

    async def create_job_with_test_ocr(self, case_id, stage, **options):
        if stage == "extract" and case_id in self.cases:
            try:
                self._validate_ocr(self.cases[case_id])
            except ValueError:
                job = await original_job(self, case_id, "ocr")
                await self.tasks[job["id"]]
        return await original_job(self, case_id, stage, **options)

    monkeypatch.setattr(LocalStore, "create_job", create_job_with_test_ocr)


@pytest.fixture
def setup_agent():
    task, ref, data = analysis_fixture()
    ports = FakePorts()
    ports.bind(task.context)
    ports.add_source(ref, data)
    return task, ports, AgentSettings(retry_delay_seconds=0)
