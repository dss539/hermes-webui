"""Tie-free ordered reads, detached JSON adoption and bounded cache regressions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from api import agent_sessions as agent, agent_session_summaries as summaries
from api import session_summary_index as index, sidebar_revision as revision
from tests.js_source_extract import extract_function

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture(autouse=True)
def clear():
    summaries.clear(); index.clear()
    yield
    summaries.clear(); index.clear()


def build(path, *, keys=(10,20,30), schema='canonical', messages=True):
    fixture=json.loads((ROOT/'tests/fixtures/sidebar_agent_schema.json').read_text())
    with sqlite3.connect(path) as c:
        for kind,name,sql in fixture:
            if kind!='table' or (not messages and name=='messages'): continue
            if None in keys:
                sql=sql.replace('started_at REAL NOT NULL','started_at REAL').replace('timestamp REAL NOT NULL','timestamp REAL')
            if name=='sessions':
                if schema=='desc': sql=sql.replace('id TEXT PRIMARY KEY,','id TEXT PRIMARY KEY DESC,')
                if schema=='nocase': sql=sql.replace('id TEXT PRIMARY KEY,','id TEXT PRIMARY KEY COLLATE NOCASE,')
                if schema=='unique': sql=sql.replace('source TEXT NOT NULL,','source TEXT NOT NULL UNIQUE,')
            c.execute(sql)
        for kind,name,sql in fixture:
            if kind=='index' and sql and (messages or ' ON messages' not in sql): c.execute(sql)
        for i,k in enumerate(keys):
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count) VALUES(?,?,?,?,?,?)',
                (str(i),'cli' if schema!='unique' else 'source'+str(i),'row '+str(i),'model',k,1))
            if messages: c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,'user','x',?)",(str(i),k))
    return path


def production():
    ns={'__name__':'production_cbd7e993'}
    exec(compile(subprocess.check_output(['git','show','cbd7e993:api/agent_sessions.py'],cwd=ROOT,text=True),'production.py','exec'),ns)
    return ns


def same(path, **kwargs):
    expected=production()['read_importable_agent_session_rows'](path,**kwargs)
    actual=agent.read_importable_agent_session_rows(path,**kwargs)
    assert json.dumps(actual,sort_keys=True)==json.dumps(expected,sort_keys=True)


@pytest.mark.parametrize('messages',[False,True])
@pytest.mark.parametrize('keys',[(10,20,30),(10,10,30),(None,None,30),(1,1.0,30)])
def test_tie_admission_for_every_limit_and_scope(tmp_path,keys,messages):
    path=build(tmp_path/'state.db',keys=keys,messages=messages)
    tied=len(set(keys))!=len(keys)
    for limit in (None,1,2,100):
        for scope in ({},{'include_sources':('cli',),'exclude_sources':None},{'include_sources':('cron',),'exclude_sources':None}):
            before=summaries.counters().get('legacy_fallbacks',0)
            same(path,limit=limit,**scope)
            assert summaries.counters().get('legacy_fallbacks',0)-before==int(tied)
    assert summaries.counters()['full_fact_rereads']==1


@pytest.mark.parametrize('schema',['desc','nocase','unique'])
@pytest.mark.parametrize('limit',[None,1])
def test_unsupported_implicit_keys_fall_back_even_without_ties(tmp_path,schema,limit):
    path=build(tmp_path/'state.db',schema=schema)
    same(path,limit=limit)
    assert summaries.counters()['legacy_fallbacks']==1
    assert not summaries._READERS


def test_data_guard_not_layout_or_statistics_guard(tmp_path):
    path=build(tmp_path/'state.db')
    with sqlite3.connect(path) as c:
        c.execute('CREATE INDEX extra ON sessions(title COLLATE NOCASE DESC)')
        c.execute('ALTER TABLE sessions ADD COLUMN harmless INTEGER DEFAULT 0')
        c.execute('ANALYZE')
    for limit in (1,None): same(path,limit=limit)
    assert summaries.counters().get('legacy_fallbacks',0)==0
    assert summaries.counters()['summary_policy_passes']==2


def test_tie_changes_invalidate_admission_and_keep_one_source(tmp_path):
    path=build(tmp_path/'state.db')
    with summaries.summary_connection(path) as conn: source=conn.sidebar_source
    same(path,limit=1)
    with sqlite3.connect(path) as c: c.execute("UPDATE messages SET timestamp=10 WHERE session_id='1'")
    same(path,limit=None)
    assert summaries.counters()['tie_fallbacks']==1
    with summaries.summary_connection(path) as conn:
        assert conn.sidebar_source is source
        assert not source.in_transaction
        with pytest.raises(sqlite3.OperationalError):source.execute("DELETE FROM sessions")
        source.rollback()
    with sqlite3.connect(path) as c: c.execute("UPDATE messages SET timestamp=20 WHERE session_id='1'")
    same(path,limit=1)
    assert summaries.counters()['tie_fallbacks']==1
    assert summaries.counters()['full_fact_rereads']==3


def test_unchanged_schema_read_precedes_order_certification(tmp_path):
    path=build(tmp_path/'state.db',keys=(10,10,20))
    with summaries.summary_connection(path) as conn:
        source=conn.sidebar_source
        trace=[];source.set_trace_callback(trace.append)
    same(path,limit=1)
    assert next(i for i,s in enumerate(trace) if s=='PRAGMA schema_version') < next(i for i,s in enumerate(trace) if s.startswith('PRAGMA table_info'))
    assert summaries.counters()['unchanged']==1
    with sqlite3.connect(path) as c:c.execute('ALTER TABLE sessions ADD COLUMN harmless INTEGER')
    same(path,limit=1)
    assert summaries.counters()['bootstraps']==2


def test_unchanged_order_skips_order_write_but_changed_rows_invalidate(tmp_path):
    path=tmp_path/'_index.json';rows=[{'session_id':'a','title':'old'},{'session_id':'b','title':'b'}]
    path.write_text(json.dumps(rows));index.read_index(path)
    store=index._store(tmp_path);other=index._Store(tmp_path)
    trace=[];store.conn.set_trace_callback(trace.append)
    try:
        old_generation=store.generation
        for changed in (False,True):
            previous=revision.path_revision(path)
            if changed:rows[0]['title']='new'
            path.write_text(json.dumps(rows));st=path.stat()
            trace.clear()
            index.publish_index(path,rows,(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size),
                updates={'a':rows[0]},order=['a','b'],previous_source=previous)
            updates=[s for s in trace if s.startswith('UPDATE meta SET')]
            assert len(updates)==1 and 'index_order=' not in updates[0]
            assert store.generation==old_generation+int(changed)
            other.conn.execute('BEGIN')
            try:other.sync()
            finally:other.conn.rollback()
            assert other.entries['a'][0]['title']==rows[0]['title']
        assert index.read_index(path)==rows
    finally:other.conn.close()


def test_metadata_retains_both_variants_and_replaces_counted_generation(tmp_path):
    path=tmp_path/'a.json';path.write_text('x');loads=[]
    def load(count):
        loads.append(count);return {'count':count,'nested':['truth']}
    for _ in range(3):
        for count in (None,4):
            row=index.metadata_compact(path,count,lambda:load(count))
            row['nested'][0]='local'
    assert loads==[None,4]
    assert index.metadata_compact(path,None,lambda:load(None))['nested']==['truth']
    index.metadata_compact(path,5,lambda:load(5))
    assert len(index._store(tmp_path).metadata['a'][1])==2
    path.write_text('changed')
    for count in (None,5):index.metadata_compact(path,count,lambda:load(count))
    assert loads==[None,4,5,None,5]


def node(script):
    p=subprocess.run(['node','-e',script],capture_output=True,text=True)
    assert p.returncode==0,p.stderr


def test_json_copy_accepts_functions_and_detaches_nested_values():
    src=(ROOT/'static/sessions.js').read_text()
    funcs='\n'.join(extract_function(src,n) for n in ('_mergeOptimisticFirstTurnSessions','_isOptimisticFirstTurnSessionRow'))
    node("const assert=require('node:assert/strict');let _allSessions=[];const _isSessionLocallyStreaming=()=>false;"+funcs+'''
const rows=[{session_id:'a',message_count:1,attention:{nested:['truth']},fn(){}}];
const copy=_mergeOptimisticFirstTurnSessions(rows);copy[0].attention.nested[0]='local';
assert.equal(rows[0].attention.nested[0],'truth');assert.equal(copy[0].fn,undefined);
''')


def test_response_serialization_reused_across_304_and_local_enrichment():
    src=(ROOT/'static/sessions.js').read_text()
    loader='async '+extract_function(src,'_loadSidebarSessionListPayload')
    merge=extract_function(src,'_mergeOptimisticFirstTurnSessions')
    apply=extract_function(src,'_applySessionListPayload')
    # Exercise the real response loader and the real apply boundary through its
    # first adoption assignment; the rest of apply is covered by the full gates.
    boundary=apply[apply.index('  // Reuse response-boundary serialization'):apply.index('  // Tag the cache')]
    node('''const assert=require('node:assert/strict');let _allSessions=[],_sidebarReferenceSessions=[];
let _sidebarReceivedValidator=null,_sidebarSessionValidator=null,_sessionListHasLoadedOnce=false,_sidebarRequestSequence=0;
const _allProjects=[],_showAllProfiles=false,_optimisticallyRemovedSessionIds=new Set();
const _sidebarSessionScopeKey=()=> 'scope',_isOptimisticFirstTurnSessionRow=()=>false,_reconcileActiveSessionIdleStateFromList=()=>{};
let n=0;const raw={sessions:[{session_id:'a',message_count:1,nested:{value:'truth'}}],sidebar_reference_sessions:[{session_id:'r',nested:{value:'ref'}}]};
const api=async path=>path.startsWith('/api/projects')?{projects:[]}:(n++===0?{data:raw,etag:'e'}:{unchanged:true});
'''+loader+'\n'+merge+'\nfunction adopt(sessData){'+boundary+'''}
(async()=>{
 const first=await _loadSidebarSessionListPayload('',{});
 const serialized=first.sessData._sidebarRowsJSON;assert.ok(serialized);
 assert.equal(Object.keys(first.sessData).includes('_sidebarRowsJSON'),false);
 adopt(first.sessData);_allSessions[0].nested.value='local';_sidebarReferenceSessions[0].nested.value='local';
 raw.sessions[0].fn=()=>{};raw.sidebar_reference_sessions[0].fn=()=>{};
 const stringify=JSON.stringify;JSON.stringify=()=>{throw Error('must reuse boundary serialization')};
 const second=await _loadSidebarSessionListPayload('',{});adopt(second.sessData);JSON.stringify=stringify;
 assert.equal(_allSessions[0].nested.value,'truth');assert.equal(_sidebarReferenceSessions[0].nested.value,'ref');
 assert.equal(second.sessData._sidebarRowsJSON,serialized);assert.equal(_allSessions[0].fn,undefined);
})().catch(e=>{console.error(e);process.exitCode=1});
''')


@pytest.mark.parametrize('outcome',['complete','declined','superseded','skeleton'])
def test_chunk_completion_does_not_repaint_just_because_menu_opened(outcome):
    flush=extract_function((ROOT/'static/sessions.js').read_text(),'_flushPendingSidebarPaint')
    node('''const assert=require('node:assert/strict');let _allSessions=[],_renamingSid=null,_sessionActionMenu=null,_sessionListSkeletonActive=false;
let _lastSessionListRenderSig='old',recorded=0,resolve;const _recordSidebarPaint=()=>recorded++;
let _sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
const renderSessionListFromCache=()=>new Promise(r=>resolve=r);
'''+flush+'''
(async()=>{const paint=_flushPendingSidebarPaint();_sessionActionMenu={};
OUTCOME
await paint;assert.equal(recorded,EXPECTED);assert.equal(_lastSessionListRenderSig,EXPECTED?'new':'old');
_sessionActionMenu=null;if(EXPECTED) await _flushPendingSidebarPaint();else assert.ok(_sidebarPendingPaint);assert.equal(recorded,EXPECTED);
})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('OUTCOME',{'complete':'resolve(true);','declined':'resolve(false);','superseded':'_allSessions=[];resolve(true);','skeleton':'_sessionListSkeletonActive=true;resolve(true);'}[outcome]).replace('EXPECTED','1' if outcome=='complete' else '0'))
