"""Independent blind review probes; failures are findings, not product edits."""
import ast
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import subprocess

import pytest
from api import agent_sessions, agent_session_summaries as summaries
from api import session_summary_index as index
from api import routes

ROOT = Path(__file__).resolve().parents[1]
BASE = 'cbd7e993'


def base_source(path):
    return subprocess.check_output(['git', 'show', f'{BASE}:{path}'], cwd=ROOT, text=True)


def base_function(path, name, namespace):
    tree = ast.parse(base_source(path))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = dict(namespace)
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), scope)
    return scope[name]


@pytest.fixture(autouse=True)
def clear_caches():
    summaries.clear()
    index.clear()
    yield
    summaries.clear()
    index.clear()


def test_response_bytes_match_base():
    old_item = base_function('api/routes.py', '_sidebar_session_response_item', vars(routes))
    row = {'session_id': 'synthetic-a', 'title': 'Synthetic title', 'message_count': 2, 'profile': 'default'}
    old = old_item(row, redact_enabled=False)
    new = routes._sidebar_session_response_item(row, redact_enabled=False)
    class Handler:
        headers = {}
        def __init__(self): self.wfile = io.BytesIO()
        def send_response(self, status): pass
        def send_header(self, key, value): pass
        def end_headers(self): pass
    from api.helpers import j
    before, after = Handler(), Handler()
    j(before, {'sessions': [old]}, pretty=False)
    j(after, {'sessions': [new]}, pretty=False)
    print('base_bytes=', before.wfile.getvalue())
    print('tip_bytes=', after.wfile.getvalue())
    # Supervisor decision: production's cold/set-cache order already differs.
    # Compare values, JSON types and key presence, never object key order.
    assert json.dumps(json.loads(before.wfile.getvalue()), sort_keys=True) == json.dumps(json.loads(after.wfile.getvalue()), sort_keys=True)


def test_valid_partial_index_schema_uses_legacy_fallback(tmp_path):
    path = tmp_path / 'state.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT,
                model TEXT, message_count INTEGER, started_at REAL, custom_flag INTEGER);
            CREATE INDEX idx_custom_source ON sessions(source) WHERE custom_flag = 1;
            INSERT INTO sessions VALUES('synthetic-a','cli','Synthetic title','m',2,1,1);
        ''')
    legacy_namespace = {'__name__': 'astra_base_agent_sessions'}
    exec(compile(base_source('api/agent_sessions.py'), 'base_agent_sessions.py', 'exec'), legacy_namespace)
    expected = legacy_namespace['read_importable_agent_session_rows'](path, limit=None)
    print('base_ids=', [row['id'] for row in expected])
    assert expected
    actual = agent_sessions.read_importable_agent_session_rows(path, limit=None)
    assert actual == expected


def test_deleted_sidecar_rows_are_reclaimed(tmp_path):
    index_path = tmp_path / '_index.json'
    index_path.write_text('[]')
    index.read_index(index_path)
    for i in range(25):
        sid = f'synthetic-{i}'
        path = tmp_path / f'{sid}.json'
        path.write_text('{}')
        row = {'session_id': sid, 'title': f'Synthetic sensitive title {i}'}
        index_path.write_text(json.dumps([row]))
        index.read_index(index_path)
        index.metadata_compact(path, 0, lambda: row)
        path.unlink()
        index_path.write_text('[]')
        assert index.read_index(index_path) == []
    with closing(sqlite3.connect(tmp_path / '_sidebar.db')) as conn:
        retained = conn.execute('SELECT COUNT(*) FROM rows').fetchone()[0]
        print('canonical_sessions=0 retained_derivative_rows=', retained)
    assert retained == 0


def section(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


@pytest.mark.parametrize('interacting', [False, True])
def test_304_reconciles_local_optimistic_rows(interacting):
    source = (ROOT / 'static/sessions.js').read_text()
    run = section(source, 'async function _runRenderSessionListRefresh(', 'let _sidebarSessionValidator')
    merge = section(source, 'function _mergeOptimisticFirstTurnSessions(', 'function _isSessionListUserInteracting(')
    loader = section(source, 'async function _loadSidebarSessionListPayload(', 'async function _drainRenderSessionListQueue(')
    classify = section(source, 'function _isOptimisticFirstTurnSessionRow(', 'function _shouldKeepLocalOnlyOptimisticSessionRow(')
    idle = section(source, 'function _isServerIdleSessionRow(', 'function _reconcileActiveSessionIdleStateFromList(')
    # Run actual conditional request, refresh, classification and merge functions;
    # only environment / transport / DOM and the larger apply wrapper are stubbed.
    script = '''
const assert = require('node:assert/strict');
const $=()=>({value:''}), _sessionListQueryString=()=>'', _restoreSidebarBootCache=()=>{};
let _pendingSessionListPayload=null, _sessionListHasLoadedOnce=true, _renderSessionListGen=1;
let _profileSwitchListEmbargo=false, _serverTimeDelta=0, _sessionListLoadError=null, _sessionListSkeletonActive=false;
const _allProjects=[];
const _isSessionListUserInteracting=()=>true,_schedulePendingSessionListApply=()=>{};
const _showAllProfiles=true;
let _allSessions=[{session_id:'synthetic-a',title:'Optimistic local title',message_count:1,is_streaming:true}];
const server=[{session_id:'synthetic-a',title:'Authoritative idle title',message_count:2,is_streaming:false}];
const _shouldKeepLocalOnlyOptimisticSessionRow=()=>false;
const _dropStaleOptimisticSessionRow=()=>{};
const _isSessionLocallyStreaming=()=>false;
const _sessionStreamingById=new Map();
const _sidebarSessionScopeKey=()=> 'synthetic-scope';
let _sidebarRequestSequence=0, _sidebarReceivedValidator=null, _sidebarSessionValidator=null;
let conditional=false;
async function api(path,opts){
  if(path.startsWith('/api/projects')) return {projects:[]};
  if(!conditional) return {data:{sessions:server},etag:'W/"same-idle-state"',unchanged:false};
  assert.equal(opts.headers['If-None-Match'],'W/"same-idle-state"');
  return {unchanged:true,etag:'W/"same-idle-state"'};
}
let applies=0;
const _applySessionListPayload=data=>{applies++;_allSessions=_mergeOptimisticFirstTurnSessions(data.sessions);};
const _showSessionListLoadError=e=>{throw e};
''' + classify + idle + merge + loader + run + '''
(async()=>{
  const initial=await _loadSidebarSessionListPayload('',{});
  _sidebarSessionValidator={...initial.sessData._sidebarValidator,sessData:initial.sessData};
  conditional=true;
  // Control: the existing adoption path heals this local-only divergence.
  assert.equal(_mergeOptimisticFirstTurnSessions(server)[0].is_streaming,false);
  await _runRenderSessionListRefresh({deferWhileInteracting:INTERACTING},1);
  console.log(JSON.stringify({applies,row:_allSessions[0]}));
  assert.equal(_allSessions[0].is_streaming,false,'304 must still reconcile local optimism');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script.replace('INTERACTING', json.dumps(interacting))], capture_output=True, text=True)
    print(result.stdout)
    assert result.returncode == 0, result.stderr
