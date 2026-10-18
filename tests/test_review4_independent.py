"""Independent synthetic fixtures; run ONLY with the project run-tests.sh."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import subprocess

import pytest
from api import agent_sessions as current, agent_session_summaries as summaries

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=json.loads((ROOT/'tests/fixtures/sidebar_agent_schema.json').read_text())

@pytest.fixture(autouse=True)
def reset():
    summaries.clear()
    yield
    summaries.clear()

@pytest.fixture
def production():
    namespace={'__name__':'review4_production'}
    exec(compile(subprocess.check_output(['git','show','cbd7e993:api/agent_sessions.py'],cwd=ROOT,text=True),'production.py','exec'),namespace)
    return namespace['read_importable_agent_session_rows']

def encode(value):return json.dumps(value,sort_keys=True)
def ids(rows):return [r['id'] for r in rows]

def schema(conn, reverse=False):
    for kind,name,sql in FIXTURE:
        if kind=='table':conn.execute(sql)
    ddls=[sql for kind,name,sql in FIXTURE if kind=='index' and sql]
    for sql in reversed(ddls) if reverse else ddls:conn.execute(sql)

def stats_fixture(path):
    with closing(sqlite3.connect(path)) as c:
        schema(c)
        # A genuine ANALYZE creates sqlite_stat1 on an empty DB. No manual
        # sqlite_stat1 writes, PRAGMA schema_version writes, or monkeypatches.
        c.execute('ANALYZE')
        for i in range(60):
            sid=f'p{i:02d}'
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count) VALUES(?,?,?,?,?,?)',
                (sid,('cli','cron','kanban','subagent')[i%4],f'T{i}','m',1000.0,1))
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,'user','x',900.0)",(sid,))
        c.commit()
    return path

@pytest.mark.parametrize('limit',[1,2,5])
def test_warm_fallback_after_external_analyze(tmp_path,production,limit,monkeypatch):
    path=stats_fixture(tmp_path/'state.db')
    assert encode(current.read_importable_agent_session_rows(path,limit=limit))==encode(production(path,limit=limit))
    source=summaries._READERS[str(path)].source
    before_data=source.execute('PRAGMA data_version').fetchone()[0]
    before_schema=source.execute('PRAGMA schema_version').fetchone()[0]
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('ANALYZE');writer.commit()
    after_data=source.execute('PRAGMA data_version').fetchone()[0]
    after_schema=source.execute('PRAGMA schema_version').fetchone()[0]
    assert after_data!=before_data and after_schema==before_schema
    before_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    expected_trace=[];actual_trace=[]
    old_open=production.__globals__['open_state_db_readonly']
    def traced(*args,**kwargs):
        conn=old_open(*args,**kwargs);conn.set_trace_callback(expected_trace.append);return conn
    production.__globals__['open_state_db_readonly']=traced
    expected=production(path,limit=limit)
    # Trace the per-read fallback handle too, not only the retained fact reader.
    real_connect=sqlite3.connect
    def traced_actual(*args,**kwargs):
        conn=real_connect(*args,**kwargs)
        conn.set_trace_callback(actual_trace.append)
        return conn
    monkeypatch.setattr(sqlite3,'connect',traced_actual)
    source.set_trace_callback(actual_trace.append)
    actual=current.read_importable_agent_session_rows(path,limit=limit)
    source.set_trace_callback(None)
    queries=lambda trace:[sql for sql in trace if sql.lstrip().startswith('WITH candidates AS')]
    assert queries(expected_trace) and queries(expected_trace)==queries(actual_trace)
    assert summaries.counters()['full_fact_rereads']==2
    assert summaries.counters()['tie_fallbacks']==2
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before_hash
    summaries.clear()
    reopened=current.read_importable_agent_session_rows(path,limit=limit)
    assert encode(reopened)==encode(expected), 'fresh connection is the causal control'
    print('external ANALYZE',{'limit':limit,'schema_version':(before_schema,after_schema),
          'data_version':(before_data,after_data),'production':ids(expected),'warm':ids(actual),
          'fresh':ids(reopened),'identical_candidate_sql':True,'source_unchanged_by_reads':True})
    assert encode(actual)==encode(expected)

@pytest.mark.parametrize('analyzed',[False,True])
@pytest.mark.parametrize('reverse',[False,True])
def test_unique_keys_lineage_refill_and_scopes(tmp_path,production,analyzed,reverse):
    path=tmp_path/'unique.db'
    rng=random.Random(941)
    order=list(range(96));rng.shuffle(order)
    with closing(sqlite3.connect(path)) as c:
        schema(c,reverse=reverse)
        c.execute('ALTER TABLE sessions ADD COLUMN project_id TEXT')
        for i in order:
            sid=f's{i:03d}'
            # The 64 newest raw rows form one continuation: limit=2 must
            # exhaust its 16-row candidate window and exercise the 64-row refill.
            chained=i>=32
            parent=f's{i-1:03d}' if i>32 else None
            source='cli' if chained else ('cli','cron','subagent','kanban')[i%4]
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count,parent_session_id,end_reason,ended_at,project_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (sid,source,sid,'m',100+i,1,parent,'compression' if chained and i<95 else None,
                 100+i+.5 if chained and i<95 else None, 'p1' if i in (35,7) else ('p2' if i==18 else None)))
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,'user','x',?)",(sid,1000+i))
        if analyzed:c.execute('ANALYZE')
        c.commit()
    scopes=[{}, {'include_sources':('cli',),'exclude_sources':None},
        {'include_sources':('subagent',),'exclude_sources':None},
        {'exclude_sources':('cron','webhook','kanban')},
        {'project_assignment':'assigned'}, {'project_assignment':'unassigned'},
        {'project_assignment':'assigned','project_ids':['p1']},
        {'project_assignment':'assigned','project_ids':['p2']}]
    comparisons=0;refills=0
    with summaries.summary_connection(path) as conn:
        trace=[];conn.set_trace_callback(trace.append)
    for limit in (1,2,5,20,100,None):
        for scope in scopes:
            expected=production(path,limit=limit,**scope)
            trace.clear()
            actual=current.read_importable_agent_session_rows(path,limit=limit,**scope)
            refills+=int(sum(sql.lstrip().startswith('WITH candidates AS') for sql in trace)>1)
            assert encode(actual)==encode(expected),(limit,scope,ids(actual),ids(expected))
            comparisons+=1
    assert refills>0
    assert summaries.counters().get('legacy_fallbacks',0)==0
    assert summaries.counters()['full_fact_rereads']==1
    print('unique lineage/scopes',{'analyzed':analyzed,'reverse':reverse,'comparisons':comparisons,'refills':refills,'fallbacks':0})


def test_sqlite_grouping_excludes_null_sources_not_numeric_ties(tmp_path,production):
    path=tmp_path/'nullable.db'
    with closing(sqlite3.connect(path)) as c:
        c.execute('CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,model TEXT,message_count INTEGER,started_at)')
        c.executemany('INSERT INTO sessions VALUES(?,?,?,?,?,?)',[
            ('null-a',None,'null a','m',1,7),('null-b',None,'null b','m',1,7),
            ('a','cli','a','m',1,1),('b','cli','b','m',1,'1'),('c','cli','c','m',1,None)])
        c.commit()
    for limit in (1,None):
        assert encode(current.read_importable_agent_session_rows(path,limit=limit))==encode(production(path,limit=limit))
    assert summaries.counters().get('tie_fallbacks',0)==0
    with closing(sqlite3.connect(path)) as c:
        c.execute("UPDATE sessions SET started_at=1.0 WHERE id='b'");c.commit()
    for limit in (1,None):
        assert encode(current.read_importable_agent_session_rows(path,limit=limit))==encode(production(path,limit=limit))
    assert summaries.counters()['tie_fallbacks']==2
