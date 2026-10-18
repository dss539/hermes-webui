"""Round-one reviewer regressions. Synthetic state only; no Agent writes."""
from contextlib import closing, contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import threading

import pytest
from api import agent_sessions, agent_session_summaries as summaries
from api import models, route_session_list_cache as cache, routes
from api import session_summary_index as index, sidebar_revision as revision
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clean():
    index.clear()
    summaries.clear()
    yield
    index.clear()
    summaries.clear()


def test_surrogate_title_http_200_304_and_exact_serializer_bytes():
    from api.sidebar_response import send_session_list, session_list_etag
    from api.helpers import _json_response_body
    payload = {'sessions': [{'session_id': 'a', 'title': 'bad \udc80 title'}], 'server_time': 1}
    scope = ('default', True)
    stable = {k: v for k, v in payload.items() if k != 'server_time'}
    expected = 'W/"' + hashlib.sha256(_json_response_body((scope, stable), pretty=False)).hexdigest() + '"'
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                send_session_list(self, payload, scope)
            except Exception:
                self.send_error(500)
        def log_message(self, *args): pass
    server = HTTPServer(('127.0.0.1', 8949), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = http.client.HTTPConnection('127.0.0.1', 8949, timeout=5)
        c.request('GET', '/api/sessions')
        response = c.getresponse()
        body = response.read()
        assert response.status == 200
        assert body == _json_response_body(payload, pretty=False)
        assert json.loads(body)['sessions'][0]['title'] == 'bad \udc80 title'
        assert response.getheader('ETag') == expected == session_list_etag(payload, scope)
        c.request('GET', '/api/sessions', headers={'If-None-Match': expected})
        response = c.getresponse()
        assert response.status == 304 and response.read() == b''
        c.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _db(path, index_sql=''):
    with closing(sqlite3.connect(path)) as c:
        c.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, model TEXT,
                message_count INTEGER, started_at REAL, hidden INTEGER);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
            CREATE INDEX idx_messages_session ON messages(session_id,timestamp);
            CREATE INDEX idx_sessions_source ON sessions(source);
            CREATE INDEX idx_sessions_source_id ON sessions(source,id);
            CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
        ''' + index_sql)
        for sid in ('zzz', 'aaa'):
            c.execute("INSERT INTO sessions VALUES(?,'cron',?,'m',1,1000,0)", (sid, sid))
            c.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,'user','x',900)", (sid,))
        c.commit()


@pytest.mark.parametrize('sql', [
    'CREATE INDEX idx_hidden ON sessions(title) WHERE hidden=0;',
    'CREATE INDEX idx_custom_source ON sessions(source) WHERE hidden=0;',
])
def test_both_partial_index_probes_reuse_one_readonly_source(tmp_path, monkeypatch, sql):
    path = tmp_path/'state.db'
    _db(path, sql)
    original = summaries.sqlite3.connect
    opens = []
    def connect(name, *args, **kwargs):
        opens.append(str(name))
        return original(name, *args, **kwargs)
    monkeypatch.setattr(summaries.sqlite3, 'connect', connect)
    rows = agent_sessions.read_importable_agent_session_rows(path, limit=None, exclude_sources=None)
    assert {r['id'] for r in rows} == {'zzz','aaa'}
    assert len([name for name in opens if name != ':memory:']) == 1
    assert 'mode=ro' in opens[0]
    assert summaries.counters().get('legacy_fallbacks') == 1
    assert not summaries.counters().get('source_read_failures')


@pytest.mark.parametrize('summary', [True, False])
def test_bounded_tie_matches_real_cbd7e993_fixture_order(tmp_path, monkeypatch, summary):
    path = tmp_path/'state.db'
    _db(path)
    if not summary:
        @contextmanager
        def legacy(db):
            with closing(agent_sessions.open_state_db_readonly(db)) as conn:
                yield conn
        monkeypatch.setattr(summaries, 'summary_connection', legacy)
    # Oracle recorded by executing DeepSeek probe_tie2 on real cbd7e993.
    for limit in (1, 2, 3, 8, 50):
        rows = agent_sessions.read_importable_agent_session_rows(path, limit=limit, include_sources=('cron',), exclude_sources=None)
        assert [r['id'] for r in rows] == ['zzz', 'aaa'][:limit]


def test_publish_encodes_only_changed_rows_and_preserves_json_types(tmp_path, monkeypatch):
    path = tmp_path/'_index.json'
    rows = [{'session_id': str(i), 'message_count': i} for i in range(100)]
    def publish():
        path.write_text(json.dumps(rows))
        st = path.stat()
        index.publish_index(path, rows, (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size))
    publish()
    dumps = index.json.dumps
    encoded = []
    def count(value, *args, **kwargs):
        if isinstance(value, dict) and 'session_id' in value:
            encoded.append(value['session_id'])
        return dumps(value, *args, **kwargs)
    monkeypatch.setattr(index.json, 'dumps', count)
    rows[5]['message_count'] = 5.0
    publish()
    assert encoded == ['5']
    assert type(index.read_index(path)[5]['message_count']) is float
    publish()
    assert encoded == ['5']


@pytest.mark.parametrize('broken', ['corrupt', 'version'])
def test_derivative_self_heals_once(tmp_path, broken):
    path = tmp_path/'_index.json'
    rows = [{'session_id': 'a', 'message_count': 1}]
    path.write_text(json.dumps(rows))
    if broken == 'corrupt':
        (tmp_path/'_sidebar.db').write_bytes(b'not sqlite')
    else:
        index.read_index(path)
        index.clear()
        with closing(sqlite3.connect(tmp_path/'_sidebar.db')) as c:
            c.execute('UPDATE meta SET format_version=-1')
            c.commit()
    for _ in range(3):
        assert index.read_index(path) == rows
    assert index.counters().get('derivative_rebuilds') == 1
    assert index.counters().get('full_index_parses') == 1
    assert index.counters().get('index_hits') == 2


def test_revision_uses_direntry_and_still_detects_sidecar_only_edits(tmp_path, monkeypatch):
    sidecar = tmp_path/'a.json'
    sidecar.write_text('before')
    original = revision.path_revision
    calls = []
    def count(path):
        calls.append(str(path))
        return original(path)
    monkeypatch.setattr(revision, 'path_revision', count)
    before = revision.webui_revision(tmp_path, tmp_path/'projects.json')
    sidecar.write_text('after!')
    after = revision.webui_revision(tmp_path, tmp_path/'projects.json')
    assert before != after
    assert str(sidecar) not in calls


def test_one_sqlite_observation_per_models_key(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(revision, 'sqlite_revision', lambda p: calls.append(p) or len(calls))
    models._sqlite_file_stat_cache_key(tmp_path/'state.db')
    assert len(calls) == 1


@pytest.mark.parametrize('all_profiles', [False, True])
def test_one_sqlite_observation_per_route_stamp(tmp_path, monkeypatch, all_profiles):
    calls = []
    monkeypatch.setattr(revision, 'sqlite_revision', lambda p: calls.append(p) or len(calls))
    monkeypatch.setattr(cache, '_session_list_cache_streaming_freeze_marker', lambda: None)
    monkeypatch.setattr(cache, '_session_list_cache_state_db_path', lambda: tmp_path/'state.db')
    monkeypatch.setattr(cache, '_session_list_cache_session_dir', lambda: tmp_path)
    monkeypatch.setattr(models, '_all_profiles_cli_contexts', lambda: ([(tmp_path, tmp_path/'state.db', 'default')], True))
    cache._session_list_cache_source_stamp(routes._session_list_cache_key('default', all_profiles, True, False, False))
    assert len(calls) == 1


def test_named_scope_preserves_route_suffix_and_gates():
    key = routes._session_list_cache_key('default', True, True, False, False,
                                        exclude_hidden=True, sidebar_source='webui', source_filter='cli')
    scope = cache.session_list_cache_scope(key)
    assert scope.all_profiles and scope.show_cli_sessions and scope.exclude_hidden
    assert scope.sidebar_source == 'webui' and scope.source_filter == 'cli'
    assert cache.session_list_cache_scope(('test',)) is None


def node(script):
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('blocker', ['menu', 'rename'])
def test_adopted_payload_paints_when_interaction_releases(blocker):
    source = (ROOT/'static/sessions.js').read_text()
    names = ['_applySessionListPayload', '_recordSidebarProjection', 'closeSessionActionMenu',
             '_flushPendingSidebarPaint', '_resumePendingSidebarPaint', '_recordSidebarPaint']
    functions = '\n'.join(
        source[source.index('function closeSessionActionMenu('):source.index('function _sessionActionMenuShouldIgnoreScrollTarget(')]
        if name == 'closeSessionActionMenu' else extract_function(source, name)
        for name in names if 'function '+name+'(' in source)
    env = r'''
const assert=require('node:assert/strict');
let _otherProfileCount=0,_archivedWebuiCount=0,_archivedCliCount=0,_serverWebuiSessionCount=null,_serverCliSessionCount=null;
let _allSessions=[],_sidebarReferenceSessions=[],_allProjects=[],_allSessionsScope=null;
let _sessionListLoadError=null,_sessionListHasLoadedOnce=false,_sessionListSkeletonActive=false;
let _sessionListFirstRenderAnimated=true,_sessionListRefreshAnimationPending=false;
let _renamingSid=null,_sessionActionMenu=null,_sessionActionAnchor=null,_sessionActionSessionId=null,_sessionActionPreviousFocus=null;
let _sidebarSessionValidator=null,_sidebarReceivedValidator=null,_sidebarPaintedValidator=null,_sidebarPendingPaint=null;
let _sidebarShowingLastKnown=false,_lastSessionListRenderSig='old',painted=[],idle=0,merged=0,polls=0,stopped=0;
const _optimisticallyRemovedSessionIds=new Set(),S={activeProfile:'default'};
const list={dataset:{freshness:'old'},removeAttribute(){}};
const $=id=>id==='sessionList'?list:null;
const _reconcileActiveSessionIdleStateFromList=()=>idle++;
const _mergeOptimisticFirstTurnSessions=rows=>{merged++;return rows.map(r=>({...r}));};
const _showAllProfiles=true,_requestedSessionSidebarSource=()=> 'webui',_sessionListExcludeHiddenEnabled=()=>true;
const _syncSessionAttentionSoundState=()=>{},_pruneLineageReportCacheToVisibleSessions=()=>{};
const _markPollingCompletionUnreadTransitions=()=>polls++,_isSessionEffectivelyStreaming=()=>false;
const startStreamingPoll=()=>{},stopStreamingPoll=()=>stopped++;
const ensureSessionTimeRefreshPoll=()=>{},ensureActiveSessionExternalRefreshPoll=()=>{},ensureSessionEventsSSE=()=>{};
const _sessionListRenderSignature=()=>JSON.stringify(_allSessions);
const _saveSidebarBootCache=()=>{},_focusSessionActionMenuRestoreTarget=()=>false;
const renderSessionListFromCache=()=>{if(_renamingSid||_sessionActionMenu)return;painted=_allSessions;return Promise.resolve(true);};
'''
    node(env + functions + '''
(async()=>{
 const data={sessions:[{session_id:'new',is_streaming:false}]};
 Object.defineProperty(data,'_sidebarValidator',{value:{scope:'s',etag:'new'}});
 if(BLOCK==='menu')_sessionActionMenu={remove(){}};else _renamingSid='old';
 await _applySessionListPayload(data,{projects:[]},{});
 assert.equal(_allSessions[0].session_id,'new');assert.equal(painted.length,0);
 assert.equal(idle,1);assert.equal(merged,1);assert.equal(polls,1);assert.equal(stopped,1);
 const markedBeforePaint=list.dataset.freshness==='current';
 if(BLOCK==='menu')closeSessionActionMenu();else{_renamingSid=null;if(typeof _resumePendingSidebarPaint==='function')_resumePendingSidebarPaint();}
 await new Promise(r=>setTimeout(r,0));
 assert.equal(painted[0]?.session_id,'new','release must repaint without another HTTP response');
 assert.equal(markedBeforePaint,false,'adoption is not paint');
 assert.equal(_sidebarPaintedValidator.etag,'new');assert.equal(list.dataset.freshness,'current');
})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace('BLOCK', json.dumps(blocker)))


def test_deferred_apply_rejection_is_observed():
    source = (ROOT/'static/sessions.js').read_text()
    fn = extract_function(source, '_schedulePendingSessionListApply')
    node(r'''
let callback,_pendingSessionListApplyTimer=0,_renderSessionListGen=1;
let _pendingSessionListPayload={gen:1,sessData:{},projData:{},unreadGen:0};
const SESSION_LIST_INTERACTION_IDLE_MS=0,_isSessionListUserInteracting=()=>false;
const setTimeout=fn=>{callback=fn;return 1},clearTimeout=()=>{};
let warnings=0;console.warn=()=>warnings++;
const _applySessionListPayload=()=>Promise.reject(new Error('paint failed'));
''' + fn + r'''
(async()=>{_schedulePendingSessionListApply();callback();await new Promise(setImmediate);
if(warnings!==1)throw Error('deferred rejection was not observed');})().catch(e=>{console.error(e);process.exitCode=1});
''')


def test_boot_status_has_locale_keys():
    source = (ROOT/'static/sessions.js').read_text()
    assert "note.textContent=t('session_updating_conversations')" in source
    locales = (ROOT/'static/i18n.js').read_text()
    assert locales.count('session_updating_conversations:') == locales.count('session_batch_move:')


def test_missing_source_cannot_certify_an_empty_regeneration_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(models, '_active_state_db_path', lambda: tmp_path/'absent.db')
    assert models.get_state_db_regeneration_tail_snapshot('a', 50) is None


def test_scalar_metadata_copy_uses_bulk_dictionary_clone():
    import inspect
    assert 'item = row.copy()' in inspect.getsource(models._copy_cli_sessions)


def test_bulk_copy_preserves_non_json_keys_shared_values_and_cycles():
    class Key:
        pass
    key = Key()
    row = {}
    shared = [row, key]
    row.update({'before': shared, key: shared, 'cycle': row})
    rows = [row, row]
    cloned = models._copy_cli_sessions(rows)
    assert cloned[0] is cloned[1] and cloned[0] is not row
    keys = list(cloned[0])
    assert keys[0] == 'before' and keys[2] == 'cycle'
    copied_key = keys[1]
    assert type(copied_key) is Key and copied_key is not key
    assert cloned[0]['cycle'] is cloned[0]
    assert cloned[0]['before'] is cloned[0][copied_key]
    assert cloned[0]['before'][0] is cloned[0]
    assert cloned[0]['before'][1] is copied_key


def test_wal_snapshot_change_refuses_with_real_source_path(tmp_path, monkeypatch):
    # Exercise the existing WAL regression's actual observation, not its absent
    # default HERMES_HOME path (which now correctly refuses on its own).
    from tests.test_issue6611_regeneration_authority import _FakeConn, _make_state_db
    db = _make_state_db(tmp_path, [{'role':'user','content':'p','timestamp':100}])
    monkeypatch.setattr(models, '_active_state_db_path', lambda: db)
    monkeypatch.setattr(models, 'open_state_db_readonly', lambda p: _FakeConn(sqlite3.connect(db)))
    assert models.get_state_db_regeneration_tail_snapshot('s1', 50) is None
