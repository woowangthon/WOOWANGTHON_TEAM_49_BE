"""Cross-chat memory: summaries as a table of contents, opened on the model's request, never beyond scope."""
import sqlite3
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.chat_memory import key_scope, scope_key
from app.config import Settings
from app.db import load, now
from app.main import create_app


class MemoryModel:
    """Recording stub. `open_ids` makes the first turn ask for past chats."""

    def __init__(self):
        self.calls, self.summary_calls, self.open_ids = [], [], None

    async def chat(self, payload):
        self.calls.append(deepcopy(payload))
        if self.open_ids is not None and "past_chats" not in payload and payload.get("other_chats"):
            return {"action": "READ_PAST_CHATS", "past_chat_ids": self.open_ids}
        return {"content": "답변: " + payload["content"], "sources": []}

    async def summarize(self, payload):
        self.summary_calls.append(deepcopy(payload))
        asked = " / ".join(m["content"] for m in payload["messages"] if m["role"] == "USER")
        return ((payload["previous_summary"] + " / ") if payload["previous_summary"] else "요약: ") + asked


class ChatOnlyModel:
    async def chat(self, payload):
        return {"content": "답변", "sources": []}


def make_app(tmp_path, model, idle=0.05):
    return create_app(Settings(_env_file=None, data_dir=tmp_path / "data", workers_enabled=False,
                               chat_summary_idle_seconds=idle), ai_client=model)


def login(client, email="memory@example.test"):
    client.post("/api/v1/auth/signup", json={"email": email, "password": "secure-test-password", "name": "기억"})
    token = client.post("/api/v1/auth/login", json={"email": email, "password": "secure-test-password"})
    return {"Authorization": "Bearer " + token.json()["data"]["accessToken"]}


def new_case(h, wid, start="2026-10-01", end="2026-10-31"):
    cid = h.client.post("/api/v1/cases", json={"workplaceId": wid, "payType": "HOURLY",
        "periodStart": start, "periodEnd": end}).json()["data"]["caseId"]
    with h.app.state.db.connect(write=True) as con:
        did = con.execute("""INSERT INTO documents(workplace_id,document_type,file_name,storage_name,
            media_type,sha256,page_count,created_at) VALUES(?,'PAYSLIP','synthetic.pdf','unused.pdf',
            'application/pdf',?,1,?)""", (wid, f"hash-{start}", now())).lastrowid
        lid = con.execute("INSERT INTO document_links(case_id,document_id) VALUES(?,?)", (cid, did)).lastrowid
    return cid, lid


def new_chat(h, cid, title="자료 질문", wid=None):
    return h.client.post(f"/api/v1/workplaces/{wid or h.wid}/chats", json={
        "title": title, "defaultScope": {"scopeType": "CASE", "caseId": cid}}).json()["data"]["chatId"]


def send(h, chat_id, content, scope=None, wid=None):
    body = {"content": content, **({"scope": scope} if scope else {})}
    response = h.client.post(f"/api/v1/workplaces/{wid or h.wid}/chats/{chat_id}/messages", json=body)
    assert response.status_code == 200 and '"status":"COMPLETED"' in response.text, response.text
    return response


def summary(h, chat_id, key=None):
    with sqlite3.connect(h.app.state.settings.db_path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM chat_summaries WHERE chat_id=?", (chat_id,)).fetchall()
    rows = [r for r in rows if key is None or r["scope_key"] == key]
    return rows[0] if rows else None


def wait_summary(h, chat_id, containing, key=None, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = summary(h, chat_id, key)
        if row is not None and containing in row["summary"]:
            return row
        time.sleep(0.02)
    raise AssertionError(f"summary of chat {chat_id} never contained {containing!r}: {summary(h, chat_id, key)}")


def last_message(h, chat_id):
    return h.client.get(f"/api/v1/workplaces/{h.wid}/chats/{chat_id}").json()["data"]["messages"][-1]


@pytest.fixture
def h(tmp_path):
    model = MemoryModel()
    app = make_app(tmp_path, model)
    with TestClient(app) as client:
        client.headers.update(login(client))
        wid = client.post("/api/v1/workplaces", json={"name": "테스트 사업장"}).json()["data"]["workplaceId"]
        harness = SimpleNamespace(client=client, app=app, model=model, wid=wid, tmp_path=tmp_path)
        harness.cid, harness.lid = new_case(harness, wid)
        yield harness


def test_scope_keys_round_trip():
    assert scope_key({"scopeType": "CASE", "caseId": 7}) == "case:7"
    assert scope_key({"scopeType": "WORKPLACE_ALL", "caseId": None}) == scope_key({"scopeType": "WORKPLACE_ALL"})
    assert key_scope("case:7") == {"scopeType": "CASE", "caseId": 7}
    assert key_scope("workplace-all") == {"scopeType": "WORKPLACE_ALL"}


def test_each_answer_updates_a_korean_summary_incrementally(h):
    chat = new_chat(h, h.cid)
    send(h, chat, "주휴수당 조건이 뭐야?")
    first = wait_summary(h, chat, "주휴수당")
    assert first["scope_key"] == f"case:{h.cid}" and len(load(first["message_ids"])) == 2
    send(h, chat, "지난달에 퇴사했어")
    second = wait_summary(h, chat, "퇴사")
    assert second["summary"] == "요약: 주휴수당 조건이 뭐야? / 지난달에 퇴사했어"
    assert len(load(second["message_ids"])) == 4
    # The second pass only receives the new exchange plus the previous summary.
    assert [m["content"] for m in h.model.summary_calls[-1]["messages"]] == ["지난달에 퇴사했어", "답변: 지난달에 퇴사했어"]
    assert h.model.summary_calls[-1]["previous_summary"] == first["summary"]


def test_new_chat_in_the_same_case_is_offered_other_chats_without_opening_them(h):
    first = new_chat(h, h.cid, "주휴수당 문의")
    send(h, first, "앞으로 답변은 짧게 해 줘")
    wait_summary(h, first, "짧게")
    second = new_chat(h, h.cid, "새 대화")
    send(h, second, "10월 실수령액은?")
    payload = h.model.calls[-1]
    assert payload["history"] == [] and "past_chats" not in payload
    assert [(c["chat_id"], c["title"], c["summary"]) for c in payload["other_chats"]] == [
        (first, "주휴수당 문의", "요약: 앞으로 답변은 짧게 해 줘")]
    assert last_message(h, second)["memory"] == {"offeredChatIds": [first], "openedChatIds": []}
    # The first chat itself never lists itself.
    send(h, first, "고마워")
    assert "other_chats" not in h.model.calls[-1] or all(
        c["chat_id"] != first for c in h.model.calls[-1]["other_chats"])


def test_model_can_open_a_past_chat_before_answering(h):
    first = new_chat(h, h.cid, "주휴수당 문의")
    send(h, first, "주 15시간 조건을 확인했어")
    wait_summary(h, first, "15시간")
    second = new_chat(h, h.cid)
    h.model.open_ids = [first]
    before = len(h.model.calls)
    send(h, second, "저번에 말한 조건이 뭐였지?")
    asked, answered = h.model.calls[before:]
    assert "past_chats" not in asked
    assert [(c["chat_id"], [m["content"] for m in c["messages"]]) for c in answered["past_chats"]] == [
        (first, ["주 15시간 조건을 확인했어", "답변: 주 15시간 조건을 확인했어"])]
    assert last_message(h, second)["memory"] == {"offeredChatIds": [first], "openedChatIds": [first]}
    assert last_message(h, second)["content"] == "답변: 저번에 말한 조건이 뭐였지?"


def test_case_scope_never_sees_other_cases_but_workplace_scope_does(h):
    other_case, _ = new_case(h, h.wid, "2026-09-01", "2026-09-30")
    this_chat, other_chat = new_chat(h, h.cid, "10월"), new_chat(h, other_case, "9월")
    send(h, this_chat, "10월 질문")
    send(h, other_chat, "9월 질문")
    wait_summary(h, this_chat, "10월")
    wait_summary(h, other_chat, "9월")
    fresh = new_chat(h, h.cid, "새 대화")
    # Even when the model asks for the other case's chat, it is neither offered nor opened.
    h.model.open_ids = [other_chat]
    before = len(h.model.calls)
    send(h, fresh, "지난 대화 알려줘")
    asked, answered = h.model.calls[before:]
    assert [c["chat_id"] for c in asked["other_chats"]] == [this_chat]
    assert answered["past_chats"] == []
    assert last_message(h, fresh)["memory"] == {"offeredChatIds": [this_chat], "openedChatIds": []}
    h.model.open_ids = None
    send(h, fresh, "전체에서 찾아줘", scope={"scopeType": "WORKPLACE_ALL"})
    wide = h.model.calls[-1]["other_chats"]
    assert {(c["chat_id"], c["scope"]["scopeType"]) for c in wide} >= {(this_chat, "CASE"), (other_chat, "CASE")}
    # The same chat's CASE-scope part is offered to its own workplace-wide question.
    assert (fresh, "CASE") in {(c["chat_id"], c["scope"]["scopeType"]) for c in wide}


def test_other_workplaces_and_users_are_never_offered(h):
    chat = new_chat(h, h.cid)
    send(h, chat, "첫 근무지 질문")
    wait_summary(h, chat, "첫 근무지")
    second_wid = h.client.post("/api/v1/workplaces", json={"name": "다른 사업장"}).json()["data"]["workplaceId"]
    second_case, _ = new_case(h, second_wid, "2026-08-01", "2026-08-31")
    elsewhere = new_chat(h, second_case, wid=second_wid)
    send(h, elsewhere, "다른 근무지 질문", scope={"scopeType": "WORKPLACE_ALL"}, wid=second_wid)
    assert "other_chats" not in h.model.calls[-1]


def test_unlinking_a_source_revokes_the_summary_and_the_offer(h):
    first = new_chat(h, h.cid)
    send(h, first, "명세서 내용 정리해 줘")
    wait_summary(h, first, "명세서")
    assert h.client.delete(f"/api/v1/cases/{h.cid}/document-links/{h.lid}").status_code == 204
    second = new_chat(h, h.cid)
    send(h, second, "새 질문")
    assert "other_chats" not in h.model.calls[-1]
    deadline = time.monotonic() + 5
    while summary(h, first) is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert summary(h, first) is None


def test_deleting_a_chat_removes_it_from_recall(h):
    first, second = new_chat(h, h.cid), new_chat(h, h.cid)
    send(h, first, "지울 대화")
    wait_summary(h, first, "지울")
    assert h.client.delete(f"/api/v1/workplaces/{h.wid}/chats/{first}").status_code == 204
    assert summary(h, first) is None
    send(h, second, "새 질문")
    assert "other_chats" not in h.model.calls[-1]


def test_long_chat_carries_its_own_summary_beyond_the_history_window(h):
    chat = new_chat(h, h.cid)
    for index in range(11):
        send(h, chat, f"질문 {index}")
    wait_summary(h, chat, "질문 10")
    send(h, chat, "마지막 질문")
    payload = h.model.calls[-1]
    assert len(payload["history"]) == 20 and payload["history"][0]["content"] != "질문 0"
    assert payload["conversation_summary"].startswith("요약: 질문 0")
    assert "conversation_summary" not in h.model.calls[5]


def test_chats_older_than_the_feature_are_summarized_once_at_startup(tmp_path):
    with TestClient(make_app(tmp_path, ChatOnlyModel())) as client:
        client.headers.update(login(client))
        wid = client.post("/api/v1/workplaces", json={"name": "테스트 사업장"}).json()["data"]["workplaceId"]
        old = SimpleNamespace(client=client, app=client.app, wid=wid)
        cid, _ = new_case(old, wid)
        chat = new_chat(old, cid)
        send(old, chat, "예전 대화 내용")
        time.sleep(0.1)
        assert summary(old, chat) is None
    model = MemoryModel()
    with TestClient(make_app(tmp_path, model)) as client:
        started = SimpleNamespace(client=client, app=client.app, wid=wid)
        assert wait_summary(started, chat, "예전 대화")["summary"] == "요약: 예전 대화 내용"
    assert len(model.summary_calls) == 1
    again = MemoryModel()
    with TestClient(make_app(tmp_path, again)) as client:
        time.sleep(0.2)
    assert again.summary_calls == []


def lazy_harness(tmp_path, idle):
    model = MemoryModel()
    app = make_app(tmp_path, model, idle)
    client = TestClient(app)
    client.__enter__()
    client.headers.update(login(client))
    wid = client.post("/api/v1/workplaces", json={"name": "테스트 사업장"}).json()["data"]["workplaceId"]
    harness = SimpleNamespace(client=client, app=app, model=model, wid=wid)
    harness.cid, harness.lid = new_case(harness, wid)
    return harness


def test_a_single_chat_costs_no_summary_calls_until_it_goes_quiet(tmp_path):
    h = lazy_harness(tmp_path, idle=3600)
    try:
        chat = new_chat(h, h.cid)
        for index in range(3):
            send(h, chat, f"질문 {index}")
        time.sleep(0.2)
        assert h.model.summary_calls == [] and summary(h, chat) is None
    finally:
        h.client.__exit__(None, None, None)


def test_quick_answers_are_summarized_once_after_the_quiet_period(tmp_path):
    h = lazy_harness(tmp_path, idle=0.4)
    try:
        chat = new_chat(h, h.cid)
        for index in range(3):
            send(h, chat, f"질문 {index}")
        assert h.model.summary_calls == []
        row = wait_summary(h, chat, "질문 2")
        assert len(h.model.summary_calls) == 1 and len(load(row["message_ids"])) == 6
    finally:
        h.client.__exit__(None, None, None)


def test_another_chat_asking_brings_a_late_summary_up_to_date_first(tmp_path):
    h = lazy_harness(tmp_path, idle=3600)
    try:
        first = new_chat(h, h.cid, "첫 대화")
        send(h, first, "11월 5일에 퇴사했어")
        send(h, first, "답변은 짧게 해 줘")
        assert summary(h, first) is None
        second = new_chat(h, h.cid)
        send(h, second, "내 상황 알아?")
        assert [c["summary"] for c in h.model.calls[-1]["other_chats"]] == ["요약: 11월 5일에 퇴사했어 / 답변은 짧게 해 줘"]
        assert len(h.model.summary_calls) == 1
        # Nothing changed in the first chat, so the next question does not summarize again.
        send(h, second, "고마워")
        assert len(h.model.summary_calls) == 1
        assert last_message(h, second)["memory"] == {"offeredChatIds": [first], "openedChatIds": []}
    finally:
        h.client.__exit__(None, None, None)
