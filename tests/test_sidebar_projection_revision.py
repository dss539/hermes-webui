"""Exact projection and refresh-queue regressions for sidebar latency."""

import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from api import agent_sessions, models, routes, route_session_list_cache as cache


def test_revision_detects_in_place_update_delete_and_replacement(tmp_path, monkeypatch):
    # Simulate colliding stat/MAX(rowid) probes; only a persistent commit
    # revision can observe UPDATE/DELETE in this case.
    monkeypatch.setattr(models, '_path_stat_cache_key', lambda p: ('same',))
    monkeypatch.setattr(models, '_sqlite_content_fingerprint', lambda p: ('same',))
    sqlite_revision = models._sqlite_file_stat_cache_key
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE rows (id INTEGER PRIMARY KEY, title TEXT)')
        c.execute("INSERT INTO rows VALUES(1,'first')")
    before = sqlite_revision(db)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE rows SET title='other' WHERE id=1")
    after = sqlite_revision(db)
    assert before != after
    assert sqlite_revision(db) == after
    with sqlite3.connect(db) as c:
        c.execute('DELETE FROM rows')
    assert sqlite_revision(db) != after
    db.unlink()
    with sqlite3.connect(db) as c:
        c.execute('CREATE TABLE replacement (id INTEGER)')
    assert sqlite_revision(db) != after


def test_route_stamp_observes_sidecar_only_edit_and_project_change(monkeypatch, tmp_path):
    session_dir = tmp_path / 'sessions'
    session_dir.mkdir()
    sidecar = session_dir / 's1.json'
    sidecar.write_text('{"title":"first"}')
    projects = tmp_path / 'projects.json'
    projects.write_text('[]')
    monkeypatch.setattr(routes, 'SESSION_DIR', session_dir)
    monkeypatch.setattr(models, 'PROJECTS_FILE', projects)
    monkeypatch.setattr(routes, '_active_state_db_path', lambda: tmp_path / 'missing.db')
    monkeypatch.setattr(routes, '_active_stream_ids', lambda: set())
    key = routes._session_list_cache_key('default', False, True, False, False)
    before = routes._session_list_cache_source_stamp(key)
    stat = sidecar.stat()
    sidecar.write_text('{"title":"other"}')
    os.utime(sidecar, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    after = routes._session_list_cache_source_stamp(key)
    assert before != after
    projects.write_text('[{"project_id":"new"}]')
    assert routes._session_list_cache_source_stamp(key) != after


def test_source_change_during_builder_cannot_validate_stale_projection(monkeypatch):
    cache._session_list_cache_clear()
    revision = [0]
    monkeypatch.setattr(cache, '_session_list_cache_resolved_source_stamp', lambda k: tuple(revision))
    key = routes._session_list_cache_key('default', False, False, False, False)
    calls = []
    def builder():
        calls.append(revision[0])
        payload = {'sessions': [{'session_id': str(revision[0])}]}
        if revision[0] == 0:
            revision[0] = 1
        return payload
    result = routes._get_cached_session_list_payload(key=key, builder=builder)
    assert result['sessions'][0]['session_id'] == '1'
    assert calls == [0, 1]
    assert cache._session_list_cache_get(key)[0] == result


def test_indexed_projection_avoids_wide_join_and_retains_exact_counts(monkeypatch, tmp_path):
    db = tmp_path / 'state.db'
    with sqlite3.connect(db) as c:
        c.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,model TEXT,message_count INTEGER,started_at REAL,source TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,timestamp REAL);
            CREATE INDEX idx_messages_session ON messages(session_id,timestamp);
            INSERT INTO sessions VALUES('a','Alpha',NULL,0,1,'cli');
            INSERT INTO sessions VALUES('b','Beta',NULL,0,1,'cli');
            INSERT INTO messages VALUES(1,'a','USER',3),(2,'a','assistant',4),(3,'b','user',4);
        ''')
    statements = []
    original = agent_sessions.open_state_db_readonly
    def connect(*args, **kwargs):
        c = original(*args, **kwargs)
        c.set_trace_callback(statements.append)
        return c
    monkeypatch.setattr(agent_sessions, 'open_state_db_readonly', connect)
    indexed = agent_sessions.read_importable_agent_session_rows(db, limit=20)
    assert [r['id'] for r in indexed] == ['a', 'b']
    assert [r['actual_message_count'] for r in indexed] == [2, 1]
    assert [r['actual_user_message_count'] for r in indexed] == [1, 1]
    assert not any('LEFT JOIN messages m ON' in q for q in statements)
    with sqlite3.connect(db) as c:
        c.execute('DROP INDEX idx_messages_session')
    fallback = agent_sessions.read_importable_agent_session_rows(db, limit=20)
    assert fallback == indexed


@pytest.mark.parametrize('relative', [
    'sessions/_index.json', 'sessions/one.json', 'settings.json',
    'projects.json', 'gateway.json', 'home/config.yaml',
    'home/cron/jobs.json', 'home/webui_state/last_workspace.txt',
    'home/webui_state/workspaces.json', 'last_workspace.txt', 'workspaces.json',
])
def test_cache_stamp_covers_each_file_input(tmp_path, monkeypatch, relative):
    (tmp_path / 'sessions').mkdir()
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('first')
    home = tmp_path / 'home'
    db = home / 'state.db'
    monkeypatch.setattr(routes, 'SESSION_DIR', tmp_path / 'sessions')
    monkeypatch.setattr(routes, 'SETTINGS_FILE', tmp_path / 'settings.json')
    monkeypatch.setattr(models, 'PROJECTS_FILE', tmp_path / 'projects.json')
    monkeypatch.setattr(routes, '_gateway_session_metadata_path', lambda: tmp_path / 'gateway.json')
    monkeypatch.setattr(routes, '_active_state_db_path', lambda: db)
    monkeypatch.setattr(routes, '_active_stream_ids', lambda: set())
    monkeypatch.setattr(models, '_all_profiles_cli_contexts', lambda: ([(home, db, 'default')], ()))
    key = routes._session_list_cache_key('default', True, True, False, False)
    cache._session_list_cache_clear()
    cache._session_list_cache_set(key, {'sessions': [{'session_id': 'one'}]})
    before = routes._session_list_cache_source_stamp(key)
    target.write_text('other')
    assert routes._session_list_cache_source_stamp(key) != before
    assert cache._session_list_cache_get(key)[0] is None


def test_sqlite_revision_readers_are_bounded_and_readonly(tmp_path, monkeypatch):
    from collections import OrderedDict
    from api import sidebar_revision as revisions
    readers = OrderedDict()
    monkeypatch.setattr(revisions, '_READERS', readers)
    monkeypatch.setattr(revisions, '_MAX_READERS', 2)
    try:
        values = []
        for n in range(3):
            p = tmp_path / f'{n}.db'
            with sqlite3.connect(p) as c:c.execute('CREATE TABLE rows(id INTEGER)')
            values.append(revisions.sqlite_revision(p))
            assert len(readers) <= 2
        assert str(tmp_path / '0.db') not in readers
        for entry in readers.values():
            assert not entry[2].in_transaction
            with pytest.raises(sqlite3.OperationalError, match='readonly'):
                entry[2].execute('INSERT INTO rows VALUES(1)')
        # Reopening an evicted source cannot alias its old data_version key.
        assert revisions.sqlite_revision(tmp_path / '0.db') != values[0]
    finally:
        for entry in readers.values():entry[2].close()


def test_same_tick_refresh_burst_fetches_once_and_later_call_still_wins():
    source = (Path(routes.__file__).parents[1] / 'static/sessions.js').read_text()
    start = source.index('async function _drainRenderSessionListQueue(')
    end = source.index('// ── Gateway session SSE', start)
    queue = source[start:end]
    merge_start = source.index('function _mergeRenderSessionListOptions(')
    merge_end = source.index('\nfunction ', merge_start + 10)
    script = '''
const assert=require('node:assert/strict');
let _renderSessionListInFlight=null,_renderSessionListQueuedRequest=null,_renderSessionListGen=0;
let requests=[],release=null,painted=[];
async function _runRenderSessionListRefresh(opts,gen){
 requests.push({opts,gen});await new Promise(r=>release=r);
 if(gen===_renderSessionListGen)painted.push(gen);
}
''' + source[merge_start:merge_end] + '\n' + queue + '''
(async()=>{
 const one=renderSessionList({deferWhileInteracting:true});
 const two=renderSessionList({deferWhileInteracting:false});
 const three=renderSessionList({deferWhileInteracting:true});
 await Promise.resolve();
 assert.equal(requests.length,1,'same-tick burst must dispatch once');
 assert.equal(requests[0].gen,3);
 assert.equal(requests[0].opts.deferWhileInteracting,false);
 const four=renderSessionList({deferWhileInteracting:true});
 release();await Promise.resolve();await Promise.resolve();
 assert.equal(requests.length,2,'later invalidation must dispatch again');
 assert.equal(requests[1].gen,4);release();
 await Promise.all([one,two,three,four]);assert.deepEqual(painted,[4]);
 assert.equal(_renderSessionListInFlight,null);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
