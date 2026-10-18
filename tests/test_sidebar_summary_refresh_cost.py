"""Exact reconciliation hot-loop and cold client preparation regressions."""
from pathlib import Path
import sqlite3
import subprocess

import pytest
from api import agent_session_summaries as summaries


@pytest.fixture
def db(tmp_path):
    summaries.clear()
    p = tmp_path/'state.db'
    with sqlite3.connect(p) as c:
        c.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,title TEXT,model TEXT,message_count INTEGER,started_at REAL);
          CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role,content,timestamp REAL);
          CREATE INDEX idx_messages_session ON messages(session_id,timestamp);
          CREATE TABLE locks(n);
          INSERT INTO sessions VALUES('a','cli','A','m',2,1);
          INSERT INTO messages VALUES(1,'a','user','one',1),(2,'a','assistant','two',2);''')
    yield p
    summaries.clear()


def test_reconciliation_does_not_allocate_typed_copies_of_unchanged_rows(db, monkeypatch):
    with summaries.summary_connection(db): pass
    with sqlite3.connect(db) as c: c.execute('INSERT INTO locks VALUES(1)')
    def forbidden(row):
        pytest.fail('reconciliation allocated a typed tuple copy')
    monkeypatch.setattr(summaries, '_fact_key', forbidden)
    with summaries.summary_connection(db) as c:
        assert c.execute('SELECT actual_user_message_count FROM _sidebar_message_stats').fetchone() == (1,)
    assert summaries.counters()['last_changed_rows'] == 0


def test_canonical_roles_do_not_pay_lower_function_per_message(db):
    calls=[]
    with summaries.summary_connection(db) as c:
        c.sidebar_source.create_function('lower',1,lambda v:calls.append(v) or v.lower())
    with sqlite3.connect(db) as c: c.execute('INSERT INTO locks VALUES(1)')
    with summaries.summary_connection(db): pass
    assert calls == [], 'canonical user/assistant roles redundantly lowercased'


@pytest.mark.parametrize('role', ['user','assistant','USER','UsEr','user\x00',' user','user ',None,b'user',b'USER',7,1.5,'üser','system','tool'])
def test_role_fast_path_preserves_sql_lower_semantics(db, role):
    with sqlite3.connect(db) as c:
        c.execute('UPDATE messages SET role=? WHERE id=1',(role,))
        expected=c.execute("SELECT COUNT(CASE WHEN LOWER(role)='user' THEN 1 END) FROM messages").fetchone()
    with summaries.summary_connection(db) as c:
        assert c.execute('SELECT actual_user_message_count FROM _sidebar_message_stats').fetchone() == expected


@pytest.mark.parametrize('left,right', [((1,),(1.0,)),((0.0,),(-0.0,)),((b'a',),('a',)),(None,()),((1,),()),((None,),(None,)),((2.0,),(2.0,))])
def test_typed_equality_matches_original_fact_key(left,right):
    assert summaries._facts_equal(left,right) == (summaries._fact_key(left)==summaries._fact_key(right))


def test_webui_derivative_uses_wal_and_rebuilds_from_unchanged_json(tmp_path):
    import json
    from api import session_summary_index as index
    index.clear()
    path=tmp_path/'_index.json'
    path.write_text(json.dumps([{'session_id':'a','title':'A','message_count':2}]))
    canonical=path.read_bytes()
    try:
        expected=index.read_index(path)
        with index._LOCK:
            store=index._store(tmp_path)
            assert store.conn.execute('PRAGMA journal_mode').fetchone() == ('wal',)
            assert store.conn.execute('PRAGMA synchronous').fetchone() == (1,)
        index.clear()
        (tmp_path/'_sidebar.db').unlink()
        assert index.read_index(path) == expected
        assert path.read_bytes() == canonical
    finally:
        index.clear()


def section(start,end):
    source=(Path.cwd()/'static/sessions.js').read_text()
    return source[source.index(start):source.index(end,source.index(start))]


def node(script):
    p=subprocess.run(['node','-e',script],capture_output=True,text=True)
    assert p.returncode == 0,p.stderr


def test_empty_lineage_cache_skips_collapse_but_nonempty_cache_is_pruned():
    node("""const assert=require('node:assert/strict');
const _lineageReportCache=new Map(),_lineageReportInflight=new Map();let collapses=0;
const _sidebarLineageKeyForRow=s=>s.session_id,_isChildSession=()=>false;
const _lineageReportCacheKey=(s,key)=>key;
const _collapseSessionLineageForSidebar=rows=>{collapses++;return rows;};
"""+section('function _pruneLineageReportCacheToVisibleSessions(', 'function _lineageReportCacheKey(')+"""
_pruneLineageReportCacheToVisibleSessions([{session_id:'a'}]);assert.equal(collapses,0);
_lineageReportCache.set('a',{});_lineageReportCache.set('stale',{});
_lineageReportInflight.set('stale',{});
_pruneLineageReportCacheToVisibleSessions([{session_id:'a'}]);assert.equal(collapses,1);
assert.deepEqual([..._lineageReportCache.keys()],['a']);assert.equal(_lineageReportInflight.size,0);
""")


def test_old_date_labels_reuse_formatter_without_changing_output():
    node("""const assert=require('node:assert/strict');
const _serverNowMs=()=>Date.now();const cache=new Map();let builds=0;
function _intlFormatter(locale,opts){const key=JSON.stringify(opts);if(!cache.has(key)){builds++;cache.set(key,new Intl.DateTimeFormat(locale,opts));}return cache.get(key);}
"""+section('function _formatSessionDate(', 'function _formatRelativeSessionTime(')+"""
const now=Date.parse('2026-10-18T12:00:00Z');
for(let i=0;i<30;i++){
 const stamp=Date.parse((i%2?'2025':'2026')+'-01-10T12:00:00Z');
 const d=new Date(stamp),options={month:'short',day:'numeric'};
 if(d.getFullYear()!==new Date(now).getFullYear()) options.year='numeric';
 assert.equal(_formatSessionDate(stamp,now),d.toLocaleDateString(undefined,options));
}
assert.equal(builds,2,'date labels bypassed the shared formatter');
assert.equal(_formatSessionDate(NaN,now),'Invalid Date');
""")
