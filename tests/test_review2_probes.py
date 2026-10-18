"""Independent round-two probes. Run only via the isolated project runner."""
import ast
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from api import models, session_summary_index as index, sidebar_revision as revision
from tests.js_source_extract import extract_function

ROOT = Path.cwd()

@pytest.fixture(autouse=True)
def reset():
    index.clear()
    yield
    index.clear()


def _other_process_read(path):
    code = "import json,sys; from api.session_summary_index import read_index; print(json.dumps(read_index(sys.argv[1])))"
    result = subprocess.run([sys.executable, '-c', code, str(path)], cwd=ROOT,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_external_derivative_commit_does_not_bless_old_memory(tmp_path):
    path = tmp_path / '_index.json'
    old = [{'session_id':'a','title':'old'}]
    new = [{'session_id':'b','title':'new'}]
    path.write_text(json.dumps(old))
    assert index.read_index(path) == old
    path.write_text(json.dumps(new))
    assert _other_process_read(path) == new
    actual = index.read_index(path)
    print('canonical=', new, 'existing_process=', actual, 'counters=', index.counters())
    assert actual == new


def test_incremental_publish_reclaims_other_process_rows(tmp_path):
    path = tmp_path / '_index.json'
    old = [{'session_id':'a','title':'old'}]
    other = [{'session_id':'b','title':'new'}]
    path.write_text(json.dumps(old))
    assert index.read_index(path) == old
    path.write_text(json.dumps(other))
    assert _other_process_read(path) == other
    # Original process publishes a new canonical generation using its old map.
    path.write_text('[]')
    st = path.stat()
    index.publish_index(path, [], (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size))
    assert index.read_index(path) == []
    index.clear()
    actual = index.read_index(path)
    print('canonical=[] restarted_process=', actual)
    assert actual == []


def test_old_format_rebuild_and_restart(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id':'a','title':'fresh'}]
    path.write_text(json.dumps(rows))
    with closing(sqlite3.connect(tmp_path / '_sidebar.db')) as c:
        c.executescript('''CREATE TABLE meta(singleton INTEGER PRIMARY KEY,format_version INTEGER,revision INTEGER,index_signature TEXT,epoch TEXT,floor_revision INTEGER,ready INTEGER,namespace_signature TEXT);
        INSERT INTO meta VALUES(1,1,0,NULL,'x',0,0,NULL);
        CREATE TABLE rows(session_id TEXT PRIMARY KEY,updated_seq INTEGER,deleted INTEGER,file_signature TEXT,index_row_json TEXT,index_position INTEGER,sidecar_row_json TEXT,saved_compact_json TEXT,policy_inputs_json TEXT);''')
    assert index.read_index(path) == rows
    assert index.counters()['derivative_rebuilds'] == 1
    index.clear()
    assert index.read_index(path) == rows
    assert index.counters()['index_disk_hits'] == 1
    with closing(sqlite3.connect(tmp_path / '_sidebar.db')) as c:
        assert c.execute('SELECT format_version FROM meta').fetchone() == (3,)
        assert {r[1] for r in c.execute('PRAGMA table_info(rows)')} == {'session_id','index_row_json'}


def test_sidecar_only_symlink_target_and_equal_length_edit(tmp_path):
    directory = tmp_path / 'sessions'
    directory.mkdir()
    target = tmp_path / 'target'
    target.write_text('before')
    (directory / 'a.json').symlink_to(target)
    before = revision.webui_revision(directory, tmp_path/'projects.json')
    target.write_text('after!')
    assert revision.webui_revision(directory, tmp_path/'projects.json') != before


def test_bulk_copy_mutation_isolation_and_graph_aliases():
    row = {'title':'x', 'nested':{'values':[1, {'x':2}]}}
    other = {'title':'y','shared':row['nested']}
    rows = [row, other, row]
    row['rows'] = rows
    one, two = models._copy_cli_sessions(rows), models._copy_cli_sessions(rows)
    assert one[0] is one[2] and one[0]['rows'] is one
    assert one[0]['nested'] is one[1]['shared']
    one[1]['shared']['values'][1]['x'] = 99
    assert two[0]['nested']['values'][1]['x'] == 2
    assert row['nested']['values'][1]['x'] == 2


def test_cancel_rename_does_not_corrupt_validated_server_rows():
    source = (ROOT/'static/sessions.js').read_text()
    names = ['_loadSidebarSessionListPayload','_recordSidebarProjection',
             '_mergeOptimisticFirstTurnSessions','_isOptimisticFirstTurnSessionRow',
             '_buildSessionRenameStarter']
    functions = '\n'.join(('async ' if name == '_loadSidebarSessionListPayload' else '') + extract_function(source, name) for name in names)
    script = r'''
const assert=require('node:assert/strict');
let _allSessions=[{session_id:'a',title:'Old title',message_count:2,is_streaming:false}];
let _allProjects=[],_sidebarReceivedValidator=null,_sidebarSessionValidator=null;
let _sidebarRequestSequence=0,_sessionListHasLoadedOnce=true,_sidebarShowingLastKnown=false;
const _showAllProfiles=true,_sidebarSessionScopeKey=()=> 'scope';
const S={session:null};
const _isSessionLocallyStreaming=()=>false,_sessionStreamingById=new Map();
const renderSessionListFromCache=()=>{},_resumePendingSidebarPaint=()=>{};
let _renamingSid=null,_loadingSessionId=null,input;
const _isReadOnlySession=()=>false,closeSessionActionMenu=()=>{},_sessionDisplayTitle=s=>s.title;
const _markNonCredentialInput=()=>{},setTimeout=()=>0;
const document={createElement:()=>({addEventListener(){},isConnected:true,replaceWith(){}})};
const display={replaceWith:el=>input=el};
let requests=0;
const api=async(path,opts)=>{
 if(path.startsWith('/api/projects'))return {projects:[]};
 if(requests++===0)return {data:{sessions:[{session_id:'a',title:'New server title',message_count:2,is_streaming:false}]},etag:'W/"new"'};
 assert.equal(opts.headers['If-None-Match'],'W/"new"');
 return {unchanged:true,etag:'W/"new"'};
};
''' + functions + r'''
(async()=>{
 // Begin editing the old row. A poll adopts a new server title while renaming.
 _buildSessionRenameStarter(_allSessions[0],display,()=>{})();
 const first=await _loadSidebarSessionListPayload('',{});
 _allSessions=_mergeOptimisticFirstTurnSessions(first.sessData.sessions);
 _recordSidebarProjection(first.sessData);
 assert.equal(_allSessions[0].title,'New server title');
 // Escape restores the captured old title into the current local cache.
 input.onkeydown({key:'Escape',preventDefault(){},stopPropagation(){}});
 assert.equal(_renamingSid,null);
 const second=await _loadSidebarSessionListPayload('',{});
 _allSessions=_mergeOptimisticFirstTurnSessions(second.sessData.sessions);
 console.log(JSON.stringify({validated:first.sessData.sessions[0].title,after304:_allSessions[0].title}));
 assert.equal(_allSessions[0].title,'New server title','304 must reconcile original server facts after rename cancel');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node','-e',script], capture_output=True, text=True)
    print(result.stdout)
    assert result.returncode == 0, result.stderr
