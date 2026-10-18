"""Behavioral regressions that execute on ce5350ee (no new-module import)."""
import json
from pathlib import Path
import sqlite3

from api import agent_sessions as agent
from api import models


def test_unchanged_source_does_not_reproject_agent_rows(tmp_path, monkeypatch):
    path = tmp_path/'state.db'
    with sqlite3.connect(path) as c:
        c.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,model TEXT,message_count INTEGER,started_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL);
            CREATE INDEX idx_messages_session ON messages(session_id,timestamp);
            INSERT INTO sessions VALUES('a','cli','Synthetic','m',2,1);
            INSERT INTO messages VALUES(1,'a','user','Synthetic',1),(2,'a','assistant','Reply',2);''')
    original = agent.read_importable_agent_session_rows(path, limit=None)
    calls = []
    project = agent._project_agent_session_rows
    def count(rows):
        calls.append(len(rows))
        return project(rows)
    monkeypatch.setattr(agent, '_project_agent_session_rows', count)
    assert agent.read_importable_agent_session_rows(path, limit=None) == original
    assert calls == [], 'an unchanged refresh repeated the full Agent projection'


def test_sidebar_unchanged_compatibility_index_not_reparsed(tmp_path, monkeypatch):
    directory = tmp_path/'sessions'
    directory.mkdir()
    path = directory/'_index.json'
    row = {'session_id':'synthetic-summary','title':'Synthetic','message_count':2,
           'user_message_count':1,'last_message_at':2,'created_at':1,'updated_at':2,
           'source_tag':'webui','pinned':False,'archived':False,'profile':'default'}
    (directory/'synthetic-summary.json').write_text(json.dumps({**row,'messages':[]}))
    path.write_text(json.dumps([row]))
    monkeypatch.setattr(models, 'SESSION_DIR', directory)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', path)
    monkeypatch.setattr(models, 'SESSIONS', {})
    monkeypatch.setattr(models, '_active_state_db_path', lambda: tmp_path/'absent.db')
    expected = models.all_sessions(include_lineage_metadata=False, sidebar_metadata_only=True)
    reads = []
    read_bytes = Path.read_bytes
    def counted(self):
        if self == path:
            reads.append(True)
        return read_bytes(self)
    monkeypatch.setattr(Path, 'read_bytes', counted)
    assert models.all_sessions(include_lineage_metadata=False, sidebar_metadata_only=True) == expected
    assert reads == [], 'an unchanged sidebar reparsed the whole compatibility index'
