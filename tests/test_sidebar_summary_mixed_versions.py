"""Review regressions: schema affinity/PK fallback and external writer fences."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3

import pytest
from api import agent_sessions as agent
from api import agent_session_summaries as summaries
from api import models
from api import session_summary_index as index


@pytest.fixture(autouse=True)
def reset():
    summaries.clear()
    index.clear()
    yield
    summaries.clear()
    index.clear()


@contextmanager
def legacy(path):
    with closing(agent.open_state_db_readonly(path)) as connection:
        yield connection


@pytest.mark.parametrize('shape', ['typeless-timestamp','composite-session-pk','composite-message-pk','descending-integer-pk'])
def test_unknown_sql_shapes_use_exact_legacy_reader(tmp_path, monkeypatch, shape):
    path=tmp_path/'state.db'
    session_pk = ', PRIMARY KEY(id,source)' if shape=='composite-session-pk' else ''
    id_type = 'TEXT' if session_pk else 'TEXT PRIMARY KEY'
    with sqlite3.connect(path) as writer:
        writer.execute(f'CREATE TABLE sessions(id {id_type},source TEXT,title TEXT,model TEXT,message_count INTEGER,started_at REAL{session_pk})')
        writer.execute("INSERT INTO sessions VALUES('a','cli','First','model',2,1)")
        if session_pk:
            writer.execute("INSERT INTO sessions VALUES('a','tui','Second','model',2,1)")
        else:
            if shape=='composite-message-pk':
                ddl='id INTEGER,session_id TEXT,role TEXT,timestamp REAL,PRIMARY KEY(id,session_id)'
            elif shape=='descending-integer-pk':
                ddl='id INTEGER PRIMARY KEY DESC,session_id TEXT,role TEXT,timestamp REAL'
            else:
                ddl='id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,timestamp'
            writer.execute('CREATE TABLE messages('+ddl+')')
            writer.execute('CREATE INDEX idx_messages_session ON messages(session_id,timestamp)')
            writer.execute("INSERT INTO messages VALUES(1,'a','user',1)")
            writer.execute("INSERT INTO messages VALUES(2,'a','assistant',1.0)")
            if shape in ('composite-message-pk','descending-integer-pk'):
                writer.execute("INSERT INTO messages VALUES(NULL,'a','user',2)")
    expected = None
    with monkeypatch.context() as patch:
        patch.setattr(summaries,'summary_connection',legacy)
        expected=agent.read_importable_agent_session_rows(path,limit=None)
    actual=agent.read_importable_agent_session_rows(path,limit=None)
    assert json.dumps(actual,sort_keys=True) == json.dumps(expected,sort_keys=True)
    assert summaries.counters()['legacy_fallbacks'] == 1
    with summaries.summary_connection(path) as conn:
        assert not getattr(conn,'sidebar_summary',False)


@pytest.mark.parametrize('replace_inode',[False,True])
def test_external_index_writer_between_replace_and_summary_commit(tmp_path, monkeypatch, replace_inode):
    directory=tmp_path/'sessions'
    directory.mkdir()
    path=directory/'_index.json'
    session=models.Session(session_id='synthetic-race',title='Our older title',messages=[{'role':'user','content':'Synthetic'}])
    path.write_text(json.dumps([session.compact()]))
    monkeypatch.setattr(models,'SESSION_DIR',directory)
    monkeypatch.setattr(models,'SESSION_INDEX_FILE',path)
    monkeypatch.setattr(models,'SESSIONS',{session.session_id:session})
    real_replace=models._safe_replace
    def external_wins(source,target):
        real_replace(source,target)
        if Path(target)==path:
            rows=json.loads(path.read_bytes())
            rows[0]['title']='External newer title'
            if replace_inode:
                other=directory/'external.tmp'
                other.write_text(json.dumps(rows))
                other.replace(path)
            else:
                path.write_text(json.dumps(rows))
    monkeypatch.setattr(models,'_safe_replace',external_wins)
    models._write_session_index(updates=[session])
    assert index.read_index(path)[0]['title']=='External newer title'


def test_summary_images_never_persist_transcripts_pending_text_or_drafts(tmp_path):
    path=tmp_path/'_index.json'
    row={'session_id':'a','title':'Synthetic','message_count':99,'pending_user_message':'PRIVATE PENDING',
         'messages':[{'content':'PRIVATE TRANSCRIPT'}],'composer_draft':{'text':'PRIVATE DRAFT'},
         'system_prompt':'PRIVATE PROMPT','gateway_routing_history':[{'text':'PRIVATE HISTORY'}]}
    path.write_text(json.dumps([row]))
    result=index.read_index(path)
    assert result[0]['pending_user_message'] is True
    assert result[0]['message_count']==99
    with sqlite3.connect(tmp_path/'_sidebar.db') as conn:
        stored=conn.execute('SELECT index_row_json FROM rows').fetchone()[0]
        assert 'PRIVATE' not in stored
        assert 'messages' not in json.loads(stored)
    assert json.loads(path.read_text())==[row]
