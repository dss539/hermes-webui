"""Round-two review regressions. Synthetic state only; run with isolated HOME."""
from collections import OrderedDict
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest
from api import agent_sessions as agent, agent_session_summaries as summaries
from api import models, session_summary_index as index, sidebar_revision as revision
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clear():
    summaries.clear(); index.clear()
    yield
    summaries.clear(); index.clear()


def node(script):
    p = subprocess.run(['node','-e',script],capture_output=True,text=True)
    assert p.returncode == 0, p.stderr


def test_nested_server_rows_do_not_alias_local_adoption():
    source=(ROOT/'static/sessions.js').read_text()
    functions='\n'.join(extract_function(source,n) for n in ('_mergeOptimisticFirstTurnSessions','_isOptimisticFirstTurnSessionRow'))
    node('''const assert=require('node:assert/strict');let _allSessions=[];
const _isSessionLocallyStreaming=()=>false,_sessionStreamingById=new Map();
'''+functions+'''
const rows=[{session_id:'a',message_count:3,attention:{count:2,nested:['truth']},lineage:[{title:'truth'}]}];
_allSessions=_mergeOptimisticFirstTurnSessions(rows);
_allSessions[0].attention.nested[0]='local';_allSessions[0].lineage[0].title='local';
assert.equal(rows[0].attention.nested[0],'truth');assert.equal(rows[0].lineage[0].title,'truth');
''')


@pytest.mark.parametrize('bail', ['false','rename','menu','skeleton'])
def test_sync_paint_bail_cannot_certify_dom(bail):
    fn=extract_function((ROOT/'static/sessions.js').read_text(),'_flushPendingSidebarPaint')
    node('''const assert=require('node:assert/strict');
let _allSessions=[],_renamingSid=null,_sessionActionMenu=null,_sessionListSkeletonActive=false;
let _lastSessionListRenderSig='old',recorded=0;
let _sidebarPendingPaint={signature:'new',sessions:_allSessions,sessData:{}};
const _recordSidebarPaint=()=>recorded++;
const renderSessionListFromCache=()=>{BAIL;};
'''.replace('BAIL',{'false':'return false','rename':"_renamingSid='a'",'menu':'_sessionActionMenu={}','skeleton':'_sessionListSkeletonActive=true'}[bail])+fn+'''
_flushPendingSidebarPaint();assert.equal(recorded,0);assert.equal(_lastSessionListRenderSig,'old');assert.ok(_sidebarPendingPaint);
''')


def test_adoption_releases_redundant_received_validator():
    fn=extract_function((ROOT/'static/sessions.js').read_text(),'_recordSidebarProjection')
    node('''const assert=require('node:assert/strict');
let _sidebarReceivedValidator=null,_sidebarSessionValidator=null,_sidebarShowingLastKnown=true;
'''+fn+'''
const data={sessions:[]};Object.defineProperty(data,'_sidebarValidator',{value:{scope:'s',etag:'e'}});
_sidebarReceivedValidator={scope:'s',etag:'e',sessData:data};_recordSidebarProjection(data);
assert.equal(_sidebarReceivedValidator,null);assert.equal(_sidebarSessionValidator.sessData,data);
const newer={sessions:[]};_sidebarReceivedValidator={scope:'s',etag:'newer',sessData:newer};
_recordSidebarProjection(data);assert.equal(_sidebarReceivedValidator.sessData,newer);
''')


def test_dead_client_validator_and_unchanged_flag_are_removed():
    source=(ROOT/'static/sessions.js').read_text()
    assert '_sidebarPaintedValidator' not in source
    refresh=extract_function(source,'_runRenderSessionListRefresh')
    loader=extract_function(source,'_loadSidebarSessionListPayload')
    assert 'projData, unchanged' not in refresh
    assert 'unchanged:' not in loader
    assert 'includes JSON of ALL merged rows' in source


@pytest.mark.parametrize('count',[355,3000])
def test_actual_index_writer_projects_only_changed_ids_and_preserves_order(tmp_path,monkeypatch,count):
    path=tmp_path/'_index.json'
    rows=[{'session_id':str(i),'title':str(i),'updated_at':i,'message_count':i} for i in range(count)]
    path.write_text(json.dumps(rows));index.read_index(path)
    monkeypatch.setattr(models,'SESSION_DIR',tmp_path)
    monkeypatch.setattr(models,'SESSION_INDEX_FILE',path)
    monkeypatch.setattr(models,'_persisted_session_ids_snapshot',lambda:set(str(i) for i in range(count)))
    monkeypatch.setattr(models,'SESSIONS',{})
    seen=[];real=index._image
    monkeypatch.setattr(index,'_image',lambda row:seen.append(row['session_id']) or real(row))
    row={'session_id':'0','title':'changed','updated_at':count+10,'message_count':1.0}
    class Changed:
        session_id='0'
        def compact(self):return row
    models._write_session_index(updates=[Changed()])
    assert seen == ['0'], 'per-save maintenance must not project every unchanged row'
    expected=json.loads(path.read_text());actual=index.read_index(path)
    assert json.dumps(actual,sort_keys=True)==json.dumps(expected,sort_keys=True)
    assert actual[0]['session_id']=='0' and type(actual[0]['message_count']) is float
    index.clear()
    assert index.read_index(path)==expected


def test_delta_external_canonical_rewrite_forces_full_walk(tmp_path):
    path=tmp_path/'_index.json';rows=[{'session_id':'a','title':'old'},{'session_id':'b','title':'old'}]
    path.write_text(json.dumps(rows));index.read_index(path)
    # External writer changes b without participating in derivative publication.
    rows[1]['title']='external';path.write_text(json.dumps(rows));previous=revision.path_revision(path)
    rows[0]['title']='ours';path.write_text(json.dumps(rows));st=path.stat()
    before=index.counters().get('index_rows_examined',0)
    index.publish_index(path,rows,(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size),
                        updates={'a':rows[0]},order=['a','b'],previous_source=previous)
    assert index.counters()['index_rows_examined']-before==2
    assert index.read_index(path)==rows


def test_delta_after_other_connection_generation_and_deletions(tmp_path):
    path=tmp_path/'_index.json';rows=[{'session_id':'a','title':'a'},{'session_id':'b','title':'b'}]
    path.write_text(json.dumps(rows));index.read_index(path)
    code="import sys;from api.session_summary_index import read_index;read_index(sys.argv[1])"
    rows=[{'session_id':'c','title':'external'}];path.write_text(json.dumps(rows))
    subprocess.run([os.sys.executable,'-c',code,str(path)],cwd=ROOT,check=True)
    previous=revision.path_revision(path);rows=[{'session_id':'d','title':'new'}];path.write_text(json.dumps(rows));st=path.stat()
    index.publish_index(path,rows,(st.st_dev,st.st_ino,st.st_mtime_ns,st.st_size),
                        updates={'d':rows[0]},order=['d'],previous_source=previous)
    assert index.read_index(path)==rows
    index.clear();assert index.read_index(path)==rows
    with closing(sqlite3.connect(tmp_path/'_sidebar.db')) as c:
        assert c.execute('SELECT session_id FROM rows').fetchall()==[('d',)]


def test_metadata_fill_does_not_scan_other_stems(tmp_path):
    path=tmp_path/'new.json';path.write_text('x')
    store=index._store(tmp_path)
    class NoScan(OrderedDict):
        def __iter__(self):raise AssertionError('eviction scans every cached key')
    store.metadata=NoScan((str(i),None) for i in range(2000))
    assert index.metadata_compact(path,1,lambda:{'title':'new'})=={'title':'new'}
    assert index.counters().get('metadata_fallbacks',0)==0
    assert len(store.metadata)==2000


def test_directory_guard_cannot_replace_exact_sidecar_stamp(tmp_path):
    path=tmp_path/'a.json';path.write_text('old');before=revision.sidecar_revision(tmp_path)
    stat=path.stat();directory=tmp_path.stat();path.write_text('new');os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    after_directory=tmp_path.stat()
    assert directory.st_mtime_ns==after_directory.st_mtime_ns
    assert directory.st_ctime_ns==after_directory.st_ctime_ns
    assert path.stat().st_mtime_ns==stat.st_mtime_ns
    assert revision.sidecar_revision(tmp_path)!=before


def canonical_db(tmp_path):
    fixture=json.loads((ROOT/'tests/fixtures/sidebar_agent_schema.json').read_text())
    path=tmp_path/'canonical.db'
    with sqlite3.connect(path) as c:
        for kind,name,sql in fixture:
            if kind=='table':c.execute(sql)
        for kind,name,sql in fixture:
            if kind=='index' and sql:c.execute(sql)
        for i in range(120):
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count) VALUES(?,?,?,?,?,?)',
                      (f'r{i:03}',('cli','cron','kanban','subagent')[i%4],f'Title {i}','model',1000-i%4,1))
            c.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',(f'r{i:03}','user','synthetic',900-i%5))
    return path


@pytest.mark.parametrize('mutation',['canonical','extra','missing','wrong_columns','partial','unique','analyzed'])
def test_tied_bounded_reads_use_exact_legacy_sql_across_index_layouts(tmp_path,mutation):
    path=canonical_db(tmp_path)
    with sqlite3.connect(path) as c:
        if mutation=='extra':c.execute('CREATE INDEX surprise ON sessions(title,source)')
        if mutation=='missing':c.execute('DROP INDEX idx_sessions_source_id')
        if mutation=='wrong_columns':c.executescript('DROP INDEX idx_sessions_source_id;CREATE INDEX idx_sessions_source_id ON sessions(id,source)')
        if mutation=='partial':c.executescript("DROP INDEX idx_sessions_source_id;CREATE INDEX idx_sessions_source_id ON sessions(source,id) WHERE source='cli'")
        if mutation=='unique':c.executescript('DROP INDEX idx_sessions_source_id;CREATE UNIQUE INDEX idx_sessions_source_id ON sessions(source,id)')
        if mutation=='analyzed':c.execute('ANALYZE')
    oracle={"__name__":"production_cbd7e993"}
    exec(compile(subprocess.check_output(['git','show','cbd7e993:api/agent_sessions.py'],cwd=ROOT,text=True),'production_reader','exec'),oracle)
    comparisons=0
    for limit in (1,2,5,20,50,200):
        for scope in ({},{'include_sources':('cron',),'exclude_sources':None},{'include_sources':('cli',),'exclude_sources':None},
                      {'project_assignment':'assigned'},{'project_assignment':'unassigned'}):
            expected=oracle['read_importable_agent_session_rows'](path,limit=limit,**scope)
            actual=agent.read_importable_agent_session_rows(path,limit=limit,**scope)
            assert json.dumps(actual,sort_keys=True)==json.dumps(expected,sort_keys=True),(mutation,limit,scope)
            comparisons+=1
    counters=summaries.counters()
    assert counters['legacy_fallbacks']==comparisons
    assert counters.get('source_read_failures',0)==0
    assert counters['full_fact_rereads']==1
    assert counters['tie_fallbacks']==comparisons
    print('bounded_guard',mutation,'comparisons',comparisons,'fallbacks',counters.get('legacy_fallbacks',0))
