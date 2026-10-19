"""Exact read-only summary reconciliation, not max-id/hash attribution."""
from contextlib import closing, contextmanager
import os
import sqlite3

import pytest
from api import agent_sessions as agent
from api import agent_session_summaries as summaries


@pytest.fixture(autouse=True)
def reset_summaries():
    summaries.clear()
    yield
    summaries.clear()


@pytest.fixture
def db(tmp_path):
    path = tmp_path/'state.db'
    with sqlite3.connect(path) as c:
        c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,model TEXT,message_count INTEGER,started_at REAL,ended_at REAL,end_reason TEXT,parent_session_id TEXT,model_config TEXT,project_id TEXT,archived INTEGER);
            CREATE INDEX idx_sessions_source ON sessions(source);
            CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
            CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content,timestamp REAL,active INTEGER,compacted INTEGER);
            CREATE INDEX idx_messages_session ON messages(session_id,timestamp);
            INSERT INTO sessions VALUES('a','cli','A','model',999,1,5,'compression',NULL,NULL,NULL,0);
            INSERT INTO sessions VALUES('b','cli','B','model',2,6,NULL,NULL,'a',NULL,'p',0);
            INSERT INTO sessions VALUES('c','subagent','Subagent Session','model',2,6,NULL,NULL,'b',NULL,NULL,0);
            CREATE TABLE locks(id INTEGER);
            INSERT INTO messages VALUES(1,'a','user','first',1,1,0),(2,'a','assistant','reply',5,0,1),(3,'b','user','second',6,1,0),(4,'b','assistant','reply',7,1,0),(5,'c','user','goal',8,1,0);
        ''')
    return path


@contextmanager
def legacy(path):
    with closing(agent.open_state_db_readonly(path)) as c:
        yield c


def compare(path, monkeypatch):
    outputs = []
    real = summaries.summary_connection
    for connection in (real, legacy):
        with monkeypatch.context() as m:
            m.setattr(summaries, 'summary_connection', connection)
            outputs.append([
                agent.read_importable_agent_session_rows(path, limit=limit, **scope)
                for limit in (None, 1, 2, 200)
                for scope in ({}, {'include_sources': ('cli',), 'exclude_sources': None},
                              {'project_assignment': 'assigned'}, {'project_assignment': 'unassigned'})
            ] + [agent.read_session_lineage_metadata(path, {'a','b','c'})])
    assert outputs[0] == outputs[1]
    assert summaries.counters().get('legacy_fallbacks', 0) == 0


@pytest.mark.parametrize('sql', [
    "UPDATE messages SET role='assistant' WHERE id=1",
    "UPDATE messages SET role='USER' WHERE id=1",
    "UPDATE messages SET timestamp=-20 WHERE id=2",
    "UPDATE messages SET timestamp=NULL WHERE id=2",
    "UPDATE messages SET timestamp='z' WHERE id=2",
    "DELETE FROM messages WHERE id=2",
    "DELETE FROM messages WHERE session_id='a'",
    "UPDATE messages SET session_id='b',id=20 WHERE id=1",
    "UPDATE messages SET content='changed goal only' WHERE id=5",
    "UPDATE sessions SET title='renamed',source='tui',project_id='q',archived=1 WHERE id='b'",
    "DELETE FROM sessions WHERE id='b'",
    "DELETE FROM messages WHERE id=2; INSERT INTO messages VALUES(20,'a','USER','balanced',5,0,1); UPDATE messages SET role='assistant' WHERE id=1",
    "INSERT OR REPLACE INTO messages VALUES(1,'b','assistant','replace',40,1,0)",
    "DELETE FROM sessions WHERE id='b'; INSERT INTO sessions(id,source,title,model,message_count,started_at) VALUES('b','cli','recreated','model',0,10)",
])
def test_changed_generation_exact(db, monkeypatch, sql):
    compare(db, monkeypatch)
    before = summaries.counters()['full_fact_rereads']
    with sqlite3.connect(db) as writer:
        writer.executescript(sql)
    compare(db, monkeypatch)
    assert summaries.counters()['full_fact_rereads'] == before + 1
    compare(db, monkeypatch)
    assert summaries.counters()['full_fact_rereads'] == before + 1


def test_only_changed_derivative_rows_and_no_agent_writes(db):
    with summaries.summary_connection(db) as c:
        assert c.sidebar_summary
        statements = []
        c.sidebar_source.set_trace_callback(statements.append)
        local = []
        c.set_trace_callback(local.append)
    with sqlite3.connect(db) as writer:
        writer.execute("UPDATE sessions SET title='new' WHERE id='b'")
    with summaries.summary_connection(db) as c:
        assert not c.sidebar_source.in_transaction
        assert c.execute("SELECT title FROM sessions WHERE id='b'").fetchone()[0] == 'new'
    assert summaries.counters()['last_changed_rows'] == 1
    inserts = [sql for sql in local if sql.startswith('INSERT INTO sessions')]
    assert len(inserts) == 1 and "'b'" in inserts[0]
    assert statements
    assert all(sql.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'PRAGMA SCHEMA_VERSION', 'BEGIN', 'ROLLBACK')) for sql in statements)
    with summaries.summary_connection(db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.sidebar_source.execute("UPDATE sessions SET title='forbidden'")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute('SELECT COUNT(*) FROM messages')  # Never fake an empty history.


def test_source_commit_during_build_is_not_restamped(db):
    with summaries.summary_connection(db) as c:
        source = c.sidebar_source
        fired = []
        def mutate(sql):
            if sql.startswith('SELECT rowid,') and not fired:
                fired.append(True)
                with sqlite3.connect(db) as writer:
                    writer.execute("UPDATE sessions SET title='during read' WHERE id='b'")
        source.set_trace_callback(mutate)
    with sqlite3.connect(db) as writer:
        writer.execute("UPDATE sessions SET title='before read' WHERE id='b'")
    with summaries.summary_connection(db) as c:
        assert c.execute("SELECT title FROM sessions WHERE id='b'").fetchone()[0] == 'before read'
    with summaries.summary_connection(db) as c:
        assert c.execute("SELECT title FROM sessions WHERE id='b'").fetchone()[0] == 'during read'
        assert not c.sidebar_source.in_transaction
    assert len(fired) == 1
    assert summaries.counters()['full_fact_rereads'] == 3


def test_rollback_unrelated_commit_checkpoint_and_replace(db, tmp_path, monkeypatch):
    compare(db, monkeypatch)
    before = summaries.counters()['full_fact_rereads']
    writer = sqlite3.connect(db)
    writer.execute("UPDATE sessions SET title='rolled back' WHERE id='b'")
    writer.rollback()
    compare(db, monkeypatch)
    assert summaries.counters()['full_fact_rereads'] == before
    writer.execute('INSERT INTO locks VALUES(1)')
    writer.commit()
    compare(db, monkeypatch)
    assert summaries.counters()['last_changed_rows'] == 0
    writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    writer.close()
    compare(db, monkeypatch)
    replacement = tmp_path/'replacement.db'
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(replacement)) as target:
        source.backup(target)
        target.execute("UPDATE sessions SET title='replacement' WHERE id='b'")
        target.commit()
    # A safe restore replaces the database generation, not a new main file
    # combined with the old generation's WAL/SHM. No writer is active here.
    for suffix in ('-wal', '-shm'):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    os.replace(replacement, db)
    compare(db, monkeypatch)
    assert summaries.counters()['bootstraps'] >= 2


@pytest.mark.parametrize('indexed', [True, False])
def test_equal_times_order_and_new_optional_columns(db, monkeypatch, indexed):
    with sqlite3.connect(db) as writer:
        writer.execute('UPDATE messages SET timestamp=10')
        writer.execute('UPDATE sessions SET started_at=10')
        if not indexed:
            writer.execute('DROP INDEX idx_messages_session')
    compare(db, monkeypatch)
    with sqlite3.connect(db) as writer:
        writer.execute('ALTER TABLE sessions ADD COLUMN platform TEXT')
        writer.execute("UPDATE sessions SET platform='telegram' WHERE id='b'")
    compare(db, monkeypatch)


@pytest.mark.parametrize('schema', [
    'CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,model TEXT,message_count,started_at); CREATE TABLE messages(session_id TEXT,role TEXT)',
    'CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,model TEXT,message_count,started_at); CREATE TABLE messages(id TEXT PRIMARY KEY,session_id TEXT,role TEXT,timestamp)',
    'CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,model TEXT,message_count,started_at) WITHOUT ROWID',
])
def test_unsupported_legacy_exact_fallback(tmp_path, schema):
    path = tmp_path/'legacy.db'
    with sqlite3.connect(path) as writer:
        writer.executescript(schema)
        writer.execute("INSERT INTO sessions VALUES('a','cli','legacy','m',3,1)")
    with summaries.summary_connection(path) as c:
        assert not getattr(c, 'sidebar_summary', False)
        assert c.execute('SELECT title FROM sessions').fetchone()[0] == 'legacy'
    assert summaries.counters()['legacy_fallbacks'] == 1


@pytest.mark.parametrize('content', [
    '', '   ', '[Attached files: synthetic.txt]', 'Synthetic usable goal',
    chr(0) + 'Synthetic goal', b'legacy bytes', 12345,
    '\\x1eHERMES_MULTIMODAL\\x1e[{"type":"text","text":"Synthetic goal"}]',
    'Synthetic ' + 'x' * 5000,
])
def test_delegated_titles_content_only_exact_original_sql(db, monkeypatch, content):
    from api import models
    def read(connection):
        with monkeypatch.context() as m:
            m.setattr(summaries, 'summary_connection', connection)
            return models._read_state_db_sidebar_overrides(db, {'a','b','c'}, {'c'})
    read(summaries.summary_connection)
    with sqlite3.connect(db) as writer:
        writer.execute('DELETE FROM messages WHERE session_id=\'c\'')
        writer.executemany('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                           [('c','user','',20+i) for i in range(9)])
        writer.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)', ('c','user',content,40))
    assert read(summaries.summary_connection) == read(legacy)
    with sqlite3.connect(db) as writer:
        writer.execute("UPDATE messages SET content='Synthetic content-only rewrite' WHERE session_id='c' AND timestamp=40")
    result = read(summaries.summary_connection)
    assert result == read(legacy)
    assert result['c']['_state_db_display_title'] == 'Synthetic content-only rewrite'


def test_same_ids_separate_homes_and_reader_eviction(db, tmp_path, monkeypatch):
    from shutil import copyfile
    first = agent.read_importable_agent_session_rows(db, limit=None)
    other = tmp_path/'other.db'
    with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(other)) as target:
        source.backup(target)
        target.execute("UPDATE sessions SET title='other home' WHERE id='a'")
        target.commit()
    second = agent.read_importable_agent_session_rows(other, limit=None)
    assert first != second
    assert agent.read_importable_agent_session_rows(db, limit=None) == first
    monkeypatch.setattr(summaries, '_MAX_READERS', 1)
    summaries.source_revision(other)
    summaries.source_revision(db)
    assert len(summaries._READERS) == 1
    assert agent.read_importable_agent_session_rows(db, limit=None) == first
    assert summaries.counters()['bootstraps'] >= 3


def test_component_reuse_tracks_parent_changes_not_just_changed_row(db):
    agent.read_importable_agent_session_rows(db, limit=None)
    before = summaries.counters()['physical_rows_rederived']
    agent.read_importable_agent_session_rows(db, limit=None)
    assert summaries.counters()['physical_rows_rederived'] == before
    with sqlite3.connect(db) as writer:
        writer.execute("UPDATE sessions SET title='changed parent' WHERE id='b'")
    rows = agent.read_importable_agent_session_rows(db, limit=None)
    assert next(row for row in rows if row['id']=='c')['parent_title'] == 'changed parent'
    assert summaries.counters()['physical_rows_rederived'] == before + 3
    assert summaries.counters().get('legacy_raw_projections', 0) == 0


def test_storage_class_changes_are_not_python_numeric_equality(tmp_path):
    path = tmp_path/'types.db'
    with sqlite3.connect(path) as writer:
        writer.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title,model,message_count,started_at);
            INSERT INTO sessions VALUES('a','cli','title',1,2,1);''')
    with summaries.summary_connection(path) as conn:
        assert type(conn.execute('SELECT model FROM sessions').fetchone()[0]) is int
    with sqlite3.connect(path) as writer:
        writer.execute('UPDATE sessions SET model=1.0')
    with summaries.summary_connection(path) as conn:
        assert type(conn.execute('SELECT model FROM sessions').fetchone()[0]) is float
    assert summaries.counters()['last_changed_rows'] == 1
