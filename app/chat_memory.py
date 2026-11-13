"""Cross-chat memory for the free chat.

Every chat keeps one Korean summary per scope. A message is sent to the model
together with the summaries of the *other* chats in the same scope (a table of
contents); the model may then ask to open some of those chats before answering.

Access never widens: a CASE question only sees chats of that case, and every
summary or opened chat is rebuilt from messages that pass the same live source
access filter as the chat history itself. A summary built from a message that
is no longer visible is discarded and rebuilt.
"""
import asyncio
import logging

from app.db import dump, load, now

MAX_OTHER_CHATS = 20
MAX_OPENED_CHATS = 3
MAX_OPENED_MESSAGES = 30
MAX_MESSAGE_CHARS = 2000
MAX_SUMMARY_INPUT_MESSAGES = 60
MAX_SUMMARY_CHARS = 1500
logger = logging.getLogger(__name__)


def scope_key(scope):
    return f"case:{scope['caseId']}" if scope["scopeType"] == "CASE" else "workplace-all"


def key_scope(key):
    if key == "workplace-all":
        return {"scopeType": "WORKPLACE_ALL"}
    return {"scopeType": "CASE", "caseId": int(key.removeprefix("case:"))}


def visible_messages(con, workplace_id, chat_id, scope):
    """Completed messages of one chat in exactly this scope that the user may still see."""
    from app.routers.chats import message_dto

    key = scope_key(scope)
    rows = con.execute(
        "SELECT * FROM messages WHERE chat_id=? AND status='COMPLETED' ORDER BY id", (chat_id,)
    ).fetchall()
    # Compared by meaning, not by stored spelling ({"caseId": null} and a missing caseId are the same scope).
    rows = [row for row in rows if scope_key(load(row["scope_json"])) == key]
    return [m for m in (message_dto(con, row, workplace_id) for row in rows) if m is not None]


def _allowed_keys(con, workplace_id, scope):
    if scope["scopeType"] == "CASE":
        return {scope_key(scope)}
    cases = con.execute("SELECT id FROM cases WHERE workplace_id=? AND deleted_at IS NULL", (workplace_id,))
    return {"workplace-all", *(f"case:{row[0]}" for row in cases)}


def _valid(summary, visible):
    return set(load(summary["message_ids"])) <= {m["messageId"] for m in visible}


def own_summary(con, workplace_id, chat_id, scope):
    row = con.execute(
        "SELECT * FROM chat_summaries WHERE chat_id=? AND scope_key=?", (chat_id, scope_key(scope))
    ).fetchone()
    if row is None or not _valid(row, visible_messages(con, workplace_id, chat_id, scope)):
        return None
    return row["summary"]


def other_chats(con, workplace_id, chat_id, scope):
    """Summaries of other chats the current scope may use.

    Returns (entries for the model, source links of every message behind them,
    stale (chat_id, scope) pairs to rebuild).
    """
    allowed = _allowed_keys(con, workplace_id, scope)
    current = (chat_id, scope_key(scope))
    entries, links, stale = [], [], []
    rows = con.execute(
        """SELECT s.*,c.title FROM chat_summaries s JOIN chats c ON c.id=s.chat_id
        WHERE c.workplace_id=? ORDER BY s.updated_at DESC,s.chat_id DESC""",
        (workplace_id,),
    ).fetchall()
    for row in rows:
        if (row["chat_id"], row["scope_key"]) == current or row["scope_key"] not in allowed:
            continue
        other_scope = key_scope(row["scope_key"])
        visible = visible_messages(con, workplace_id, row["chat_id"], other_scope)
        if not _valid(row, visible):
            stale.append((row["chat_id"], other_scope))
            continue
        if len(entries) == MAX_OTHER_CHATS:
            break
        entries.append(
            {
                "chat_id": row["chat_id"],
                "title": row["title"],
                "scope": other_scope,
                "updated_at": row["updated_at"],
                "summary": row["summary"],
            }
        )
        # An answer that saw this chat is revoked together with the chat's own sources.
        links.extend(m["sourceLinks"] for m in visible)
    return entries, links, stale


def open_chats(con, workplace_id, chat_id, scope, requested_ids):
    """Recent visible messages of the requested chats; only chats offered by other_chats()."""
    entries, _, _ = other_chats(con, workplace_id, chat_id, scope)
    wanted = list(dict.fromkeys(i for i in requested_ids if type(i) is int))[:MAX_OPENED_CHATS]
    opened = []
    for entry in entries:
        if entry["chat_id"] not in wanted:
            continue
        visible = visible_messages(con, workplace_id, entry["chat_id"], entry["scope"])
        opened.append(
            {
                "chat_id": entry["chat_id"],
                "title": entry["title"],
                "scope": entry["scope"],
                "messages": [
                    {"role": m["role"], "content": m["content"][:MAX_MESSAGE_CHARS], "created_at": m["createdAt"]}
                    for m in visible[-MAX_OPENED_MESSAGES:]
                ],
            }
        )
    return opened


async def _summarize_once(state, workplace_id, chat_id, scope):
    key = scope_key(scope)
    with state.db.connect() as con:
        if con.execute("SELECT 1 FROM chats WHERE id=? AND workplace_id=?", (chat_id, workplace_id)).fetchone() is None:
            return
        visible = visible_messages(con, workplace_id, chat_id, scope)
        row = con.execute("SELECT * FROM chat_summaries WHERE chat_id=? AND scope_key=?", (chat_id, key)).fetchone()
    if not visible:
        if row is not None:
            with state.db.connect(write=True) as con:
                con.execute("DELETE FROM chat_summaries WHERE chat_id=? AND scope_key=?", (chat_id, key))
        return
    if row is not None and _valid(row, visible):
        previous, base_ids = row["summary"], load(row["message_ids"])
        fresh = [m for m in visible if m["messageId"] > row["last_message_id"]]
    else:
        # First summary, or a source behind the old one was revoked: rebuild from what is visible now.
        previous, base_ids, fresh = None, [], visible
    if not fresh:
        return
    fresh = fresh[-MAX_SUMMARY_INPUT_MESSAGES:]
    summary = await state.ai.summarize(
        {
            "previous_summary": previous,
            "messages": [{"role": m["role"], "content": m["content"][:MAX_MESSAGE_CHARS]} for m in fresh],
        }
    )
    if not summary:
        return
    ids = sorted({*base_ids, *(m["messageId"] for m in fresh)})
    with state.db.connect(write=True) as con:
        if con.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone() is None:
            return
        # Never store a summary of something revoked while the model was working.
        if not set(ids) <= {m["messageId"] for m in visible_messages(con, workplace_id, chat_id, scope)}:
            return
        con.execute(
            """INSERT OR REPLACE INTO chat_summaries(chat_id,scope_key,summary,message_ids,last_message_id,updated_at)
            VALUES(?,?,?,?,?,?)""",
            (chat_id, key, summary[:MAX_SUMMARY_CHARS], dump(ids), ids[-1], now()),
        )


async def update_summary(state, workplace_id, chat_id, scope):
    """Bring one chat summary up to date; concurrent requests for the same chat are coalesced."""
    key = (chat_id, scope_key(scope))
    if key in state.summary_running:
        state.summary_dirty.add(key)
        return
    state.summary_running.add(key)
    try:
        while True:
            state.summary_dirty.discard(key)
            try:
                await _summarize_once(state, workplace_id, chat_id, scope)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("chat_summary_failed chat=%s scope=%s", chat_id, key[1], exc_info=True)
            if key not in state.summary_dirty:
                return
    finally:
        state.summary_running.discard(key)


def schedule_summary(state, workplace_id, chat_id, scope):
    task = asyncio.create_task(update_summary(state, workplace_id, chat_id, scope))
    state.summary_tasks.add(task)
    task.add_done_callback(state.summary_tasks.discard)
    return task


async def catch_up_summaries(state):
    """Startup pass for chats whose summary is missing or behind (e.g. chats older than this feature).

    After one successful pass there is nothing left to do, because every answer updates its own chat.
    """
    if not state.ai.can_summarize:
        return
    with state.db.connect() as con:
        pending = con.execute(
            """SELECT m.chat_id,m.scope_json,c.workplace_id FROM messages m JOIN chats c ON c.id=m.chat_id
            JOIN workplaces w ON w.id=c.workplace_id LEFT JOIN chat_summaries s
              ON s.chat_id=m.chat_id AND s.scope_key=CASE WHEN json_extract(m.scope_json,'$.scopeType')='CASE'
                 THEN 'case:' || json_extract(m.scope_json,'$.caseId') ELSE 'workplace-all' END
            WHERE m.status='COMPLETED' AND w.deleted_at IS NULL
            GROUP BY m.chat_id,m.scope_json HAVING MAX(m.id)>COALESCE(MAX(s.last_message_id),0)
            ORDER BY m.chat_id"""
        ).fetchall()
    for row in pending:
        await update_summary(state, row["workplace_id"], row["chat_id"], load(row["scope_json"]))
        await asyncio.sleep(0)
    if pending:
        logger.info("chat_summary_catch_up chats=%d", len(pending))
