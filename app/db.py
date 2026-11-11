"""Small explicit SQLite repository. Each write transaction obtains the writer lock.

No database connection is held across an await/network call.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def load(value):
    return json.loads(value) if value else None


SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
 password_hash TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
 access_hash TEXT UNIQUE NOT NULL, refresh_hash TEXT UNIQUE NOT NULL,
 access_expires TEXT NOT NULL, refresh_expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS password_resets (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
 token_hash TEXT UNIQUE NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS deletion_receipts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
 token_hash TEXT UNIQUE NOT NULL, access_revoked_at TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workplaces (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 name TEXT NOT NULL, industry TEXT, region TEXT, employment_status TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS cases (
 id INTEGER PRIMARY KEY, workplace_id INTEGER NOT NULL REFERENCES workplaces(id),
 pay_type TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 scheduled_pay_date TEXT, status TEXT NOT NULL DEFAULT 'CREATED',
 current_step TEXT NOT NULL DEFAULT 'UPLOAD', input_version INTEGER NOT NULL DEFAULT 1,
 conditions TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, workplace_id INTEGER NOT NULL REFERENCES workplaces(id),
 document_type TEXT NOT NULL, file_name TEXT NOT NULL, storage_name TEXT NOT NULL,
 media_type TEXT NOT NULL, sha256 TEXT NOT NULL, page_count INTEGER,
 processing_status TEXT NOT NULL DEFAULT 'UPLOADED', progress INTEGER NOT NULL DEFAULT 10,
 error_code TEXT, ai_metadata TEXT, created_at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS document_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
 document_id INTEGER NOT NULL REFERENCES documents(id), purpose TEXT,
 applicable_from TEXT, applicable_to TEXT, review_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
 UNIQUE(case_id,document_id));
CREATE TABLE IF NOT EXISTS fields (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id),
 key TEXT NOT NULL, label TEXT NOT NULL, value_json TEXT NOT NULL, value_type TEXT NOT NULL,
 status TEXT NOT NULL, edited INTEGER NOT NULL DEFAULT 0, confirmed_at TEXT, source_json TEXT,
 UNIQUE(document_id,key));
CREATE TABLE IF NOT EXISTS field_overrides (
 case_id INTEGER NOT NULL REFERENCES cases(id), field_id INTEGER NOT NULL REFERENCES fields(id),
 value_json TEXT NOT NULL, status TEXT NOT NULL, confirmed_at TEXT, source_json TEXT,
 PRIMARY KEY(case_id,field_id));
CREATE TABLE IF NOT EXISTS document_replacements (
 id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id),
 original_document_id INTEGER NOT NULL REFERENCES documents(id),
 new_document_id INTEGER NOT NULL REFERENCES documents(id),
 status TEXT NOT NULL DEFAULT 'CANDIDATE', created_at TEXT NOT NULL, confirmed_at TEXT,
 UNIQUE(case_id,new_document_id));
CREATE TABLE IF NOT EXISTS field_history (
 id INTEGER PRIMARY KEY, field_id INTEGER NOT NULL REFERENCES fields(id), case_id INTEGER,
 user_id INTEGER NOT NULL, previous_json TEXT NOT NULL, new_json TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manual_records (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
 record_type TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payment_transactions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, workplace_id INTEGER NOT NULL REFERENCES workplaces(id),
 payment_method TEXT NOT NULL, transaction_date TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0),
 sender TEXT, memo TEXT, source TEXT NOT NULL, confirmation_status TEXT NOT NULL, provenance_json TEXT);
CREATE TABLE IF NOT EXISTS allocations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
 transaction_id INTEGER NOT NULL REFERENCES payment_transactions(id),
 allocated_amount INTEGER NOT NULL CHECK(allocated_amount>=0), UNIQUE(case_id,transaction_id));
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id), input_version INTEGER NOT NULL,
 checks_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
 ready_at REAL NOT NULL, completed_at TEXT, error_code TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id), kind TEXT NOT NULL,
 version INTEGER NOT NULL, input_version INTEGER NOT NULL, fingerprint TEXT NOT NULL,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(case_id,kind,version));
CREATE TABLE IF NOT EXISTS chats (
 id INTEGER PRIMARY KEY AUTOINCREMENT, workplace_id INTEGER NOT NULL REFERENCES workplaces(id),
 title TEXT NOT NULL, default_scope TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL REFERENCES chats(id), role TEXT NOT NULL,
 content TEXT NOT NULL, scope_json TEXT NOT NULL, status TEXT NOT NULL,
 run_id TEXT, metadata_json TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
 id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES cases(id), version INTEGER NOT NULL,
 input_version INTEGER NOT NULL, status TEXT NOT NULL, frozen_json TEXT NOT NULL,
 document_ids TEXT NOT NULL, created_at TEXT NOT NULL, error_code TEXT);
CREATE TABLE IF NOT EXISTS consultation_centers (
 id INTEGER PRIMARY KEY, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency_responses (
 user_id INTEGER NOT NULL REFERENCES users(id), path TEXT NOT NULL, key_hash TEXT NOT NULL,
 fingerprint TEXT NOT NULL, data_json TEXT NOT NULL, status_code INTEGER NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 PRIMARY KEY(user_id,path,key_hash));
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,ready_at);
CREATE INDEX IF NOT EXISTS links_document ON document_links(document_id);
'''


class Database:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self, write=False, snapshot=True):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                con.execute('BEGIN IMMEDIATE')
            elif snapshot:
                con.execute('BEGIN')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(snapshot=False) as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript(SCHEMA)
            # Additive migration for databases created by prototype v0.1.
            columns = {row['name'] for row in con.execute('PRAGMA table_info(field_overrides)')}
            if 'source_json' not in columns:
                con.execute('ALTER TABLE field_overrides ADD COLUMN source_json TEXT')
            payment_columns = {row['name'] for row in con.execute('PRAGMA table_info(payment_transactions)')}
            if 'provenance_json' not in payment_columns:
                con.execute('ALTER TABLE payment_transactions ADD COLUMN provenance_json TEXT')
        self._migrate_monotonic_ids()

    def _migrate_monotonic_ids(self):
        # Deleted numeric IDs must not be reused by an old citation, retry, or AI thread.
        tables = ('sessions', 'manual_records', 'payment_transactions', 'allocations', 'chats', 'messages', 'document_links')
        with self.connect(snapshot=False) as con:
            pending = []
            for table in tables:
                sql = con.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone()[0]
                if 'AUTOINCREMENT' not in sql.upper():
                    pending.append((table, sql))
            if not pending:
                con.execute('PRAGMA user_version=3')
                return
            # Startup-only migration. Foreign keys are checked before committing the rebuilt tables.
            con.execute('PRAGMA foreign_keys=OFF')
            con.execute('BEGIN IMMEDIATE')
            historical = {'manual_records': 0, 'payment_transactions': 0, 'allocations': 0}
            for row in con.execute('SELECT data_json FROM snapshots'):
                body = load(row[0])
                for source in body.get('_sources', []):
                    historical['manual_records'] = max(historical['manual_records'], source.get('recordId') or 0)
                for payment in body.get('deposits', []):
                    historical['payment_transactions'] = max(historical['payment_transactions'], payment.get('transactionId') or 0)
                    historical['allocations'] = max(historical['allocations'], payment.get('allocationId') or 0)
            for table, sql in pending:
                replacement = '_migrate_' + table
                definition = sql[sql.index('('):].replace('id INTEGER PRIMARY KEY', 'id INTEGER PRIMARY KEY AUTOINCREMENT', 1)
                con.execute(f'CREATE TABLE {replacement} {definition}')
                con.execute(f'INSERT INTO {replacement} SELECT * FROM {table}')
                con.execute(f'DROP TABLE {table}')
                con.execute(f'ALTER TABLE {replacement} RENAME TO {table}')
                high = historical.get(table, 0)
                if high:
                    if con.execute('SELECT 1 FROM sqlite_sequence WHERE name=?', (table,)).fetchone():
                        con.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?', (high, table))
                    else:
                        con.execute('INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)', (table, high))
            if con.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise RuntimeError('database migration failed foreign key validation')
            con.execute('PRAGMA user_version=3')
