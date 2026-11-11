from agent.config import AgentSettings
from agent.dev.local_workflow import LocalStore
from agent.tests.fixtures_support import pdf_bytes


async def test_empty_checkout_starts_without_private_corpus_and_preserves_uploads(tmp_path):
    corpus = tmp_path / "corpus"
    store = LocalStore(AgentSettings(), corpus=corpus)
    assert store.documents == {}
    assert not (corpus / "manifest.json").exists()
    case = await store.upload(pdf_bytes(["SYNTHETIC fresh-checkout upload"]), "attendance", "demo.pdf")
    restored = LocalStore(AgentSettings(), corpus=corpus)
    assert (await restored.get_case(case["id"]))["document_type"] == "attendance"
    assert restored.documents[case["document_id"]]["title"] == "demo.pdf"
    assert not (corpus / "manifest.json").exists()
