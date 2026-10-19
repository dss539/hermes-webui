"""Independent round-three correctness probes. Use run-tests.sh only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from api import agent_sessions as agent, agent_session_summaries as summaries
from api import session_summary_index as index, sidebar_revision as revision

ROOT=Path.cwd()

@pytest.fixture(autouse=True)
def clear():
    summaries.clear();index.clear()
    yield
    summaries.clear();index.clear()

@pytest.fixture
def production():
    namespace={'__name__':'review3_production'}
    exec(compile(subprocess.check_output(['git','show','cbd7e993:api/agent_sessions.py'],text=True),'production_agent_sessions.py','exec'),namespace)
    return namespace

# Reduced from random seed 0: nine candidates cross the limit=1 x 8 window.
ROWS=[('s066',100),('s015',300),('s025',100),('s040',100),('s043',300),
      ('s004',200),('s032',100),('s064',200),('s027',300)]

def minimal(path, *, reverse=False, pk_desc=False):
    fixture=json.loads((ROOT/'tests/fixtures/sidebar_agent_schema.json').read_text())
    with sqlite3.connect(path) as c:
        for kind,name,sql in fixture:
            if kind=='table':
                if pk_desc and name=='sessions':sql=sql.replace('id TEXT PRIMARY KEY,','id TEXT PRIMARY KEY DESC,')
                c.execute(sql)
        ddls=[sql for kind,name,sql in fixture if kind=='index' and sql]
        for sql in reversed(ddls) if reverse else ddls:c.execute(sql)
        for sid,started in ROWS:
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count) VALUES(?,?,?,?,?,?)',(sid,'cli',sid,'model',started,1))
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,'user','synthetic',600)",(sid,))
    return path

@pytest.mark.parametrize('reverse',[False,True])
def test_nine_canonical_sessions_bounded_order(tmp_path,production,reverse):
    path=minimal(tmp_path/'state.db',reverse=reverse)
    kwargs=dict(limit=1,include_sources=('cli',),exclude_sources=None)
    expected=production['read_importable_agent_session_rows'](path,**kwargs)
    actual=agent.read_importable_agent_session_rows(path,**kwargs)
    print('canonical reverse_creation=',reverse,'production=',[r['id'] for r in expected],
          'current=',[r['id'] for r in actual],'counters=',summaries.counters())
    assert json.dumps(actual,sort_keys=True)==json.dumps(expected,sort_keys=True)

@pytest.mark.parametrize('limit',[1,None])
def test_implicit_primary_key_direction_is_not_certified(tmp_path,production,limit):
    path=minimal(tmp_path/'desc.db',pk_desc=True)
    with sqlite3.connect(path) as c:
        print('implicit_index_xinfo=',c.execute('PRAGMA index_xinfo(sqlite_autoindex_sessions_1)').fetchall())
    expected=production['read_importable_agent_session_rows'](path,limit=limit)
    actual=agent.read_importable_agent_session_rows(path,limit=limit)
    print('pk_desc limit=',limit,'production=',[r['id'] for r in expected], 'current=',[r['id'] for r in actual])
    assert json.dumps(actual,sort_keys=True)==json.dumps(expected,sort_keys=True)


def test_fallback_executes_identical_production_candidate_sql(tmp_path,production,monkeypatch):
    path=minimal(tmp_path/'fallback.db')
    # A real extra index invokes the fallback, without replacing its SQL.
    with sqlite3.connect(path) as c:c.execute('CREATE INDEX extra ON sessions(title,source)')
    expected_sql=[];actual_sql=[]
    real=production['open_state_db_readonly']
    def opened(*args,**kwargs):
        conn=real(*args,**kwargs);conn.set_trace_callback(expected_sql.append);return conn
    production['open_state_db_readonly']=opened
    kwargs=dict(limit=1,include_sources=('cli',),exclude_sources=None)
    expected=production['read_importable_agent_session_rows'](path,**kwargs)
    with summaries.summary_connection(path,ordered=True) as conn:
        assert not getattr(conn,'sidebar_summary',False)
        conn.set_trace_callback(actual_sql.append)
    actual=agent.read_importable_agent_session_rows(path,**kwargs)
    candidate=lambda sql:[s for s in sql if s.lstrip().startswith('WITH candidates AS')]
    assert candidate(expected_sql) and candidate(expected_sql)==candidate(actual_sql)
    assert json.dumps(actual,sort_keys=True)==json.dumps(expected,sort_keys=True)
    print('exact_legacy_candidate_SQL=PASS')


def test_actual_format_two_derivative_rebuild(tmp_path):
    path=tmp_path/'_index.json';rows=[{'session_id':'a','title':'truth'}];path.write_text(json.dumps(rows))
    with closing(sqlite3.connect(tmp_path/'_sidebar.db')) as c:
        c.executescript('CREATE TABLE meta(singleton INTEGER PRIMARY KEY,format_version INTEGER,index_signature TEXT);INSERT INTO meta VALUES(1,2,NULL);CREATE TABLE rows(session_id TEXT PRIMARY KEY,index_row_json TEXT NOT NULL,index_position INTEGER NOT NULL);')
    assert index.read_index(path)==rows
    assert index.counters()['derivative_rebuilds']==1
    index.clear();assert index.read_index(path)==rows
    assert index.counters()['index_disk_hits']==1


def test_commit_then_memory_install_failure_recovers(tmp_path):
    path=tmp_path/'_index.json';old=[{'session_id':'a','title':'old'}];path.write_text(json.dumps(old));index.read_index(path)
    store=index._store(tmp_path)
    class FailOnce(dict):
        def __setitem__(self,key,value):
            if not getattr(self,'failed',False):
                self.failed=True
                raise RuntimeError('synthetic post-commit memory installation failure')
            super().__setitem__(key,value)
    store.entries=FailOnce(store.entries)
    previous=revision.path_revision(path)
    new=[{'session_id':'a','title':'new'}];path.write_text(json.dumps(new));st=path.stat()
    index.publish_index(path,new,(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size),updates={'a':new[0]},order=['a'],previous_source=previous)
    assert index.counters()['publish_failures']==1
    assert store.generation==-1
    assert index.read_index(path)==new
    index.clear();assert index.read_index(path)==new


def test_generation_and_images_share_read_transaction(tmp_path):
    path=tmp_path/'_index.json';old=[{'session_id':'a','title':'old'}];new=[{'session_id':'b','title':'new'}]
    path.write_text(json.dumps(old));index.read_index(path)
    store=index._store(tmp_path);other=index._Store(tmp_path)
    real=store.conn
    class Interleaved:
        fired=False
        def execute(self,sql,*args):
            cursor=real.execute(sql,*args)
            if sql.startswith('SELECT format_version') and not self.fired:
                self.fired=True
                path.write_text(json.dumps(new))
                other.publish(path,new,revision.path_revision(path))
            return cursor
        def __getattr__(self,name):return getattr(real,name)
    store.conn=Interleaved()
    try:
        assert index.read_index(path)==new
        assert index.read_index(path)==new
    finally:
        other.conn.close()
        store.conn=real


def test_delta_deletes_reorders_and_preserves_nested_types(tmp_path):
    path=tmp_path/'_index.json'
    old=[{'session_id':'a','title':'a','gateway_routing':{'n':1}}, {'session_id':'b','title':'b'},{'session_id':'c','title':'c'}]
    path.write_text(json.dumps(old));index.read_index(path);previous=revision.path_revision(path)
    changed={'session_id':'c','title':'changed','gateway_routing':{'n':1.0,'zero':-0.0,'flag':True}}
    new=[changed,old[0]];path.write_text(json.dumps(new));st=path.stat()
    before=index.counters()['index_rows_examined']
    index.publish_index(path,new,(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size),updates={'c':changed},order=['c','a'],previous_source=previous)
    assert index.counters()['index_rows_examined']-before==1
    assert json.dumps(index.read_index(path),sort_keys=True)==json.dumps(new,sort_keys=True)
    index.clear();assert json.dumps(index.read_index(path),sort_keys=True)==json.dumps(new,sort_keys=True)
    with closing(sqlite3.connect(tmp_path/'_sidebar.db')) as c:
        assert c.execute('SELECT session_id FROM rows ORDER BY session_id').fetchall()==[('a',),('c',)]
