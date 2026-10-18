"""Review-4 DeepSeek order-admission probes (my copy only; never committed).

Production oracle = cbd7e993's api/agent_sessions.py executed from `git show`.
Every comparison is full-row JSON (values, types, key presence, list order).
"""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from api import agent_session_summaries as summaries
from api import agent_sessions as current
from api.agent_sessions import _as_score

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/sidebar_agent_schema.json').read_text())


def production_reader():
    oracle = {"__name__": "production_cbd7e993"}
    exec(compile(subprocess.check_output(
        ['git', 'show', 'cbd7e993:api/agent_sessions.py'], cwd=ROOT, text=True),
        'production_reader', 'exec'), oracle)
    return oracle['read_importable_agent_session_rows']


SCOPES = [
    {},
    {'include_sources': ('cron',), 'exclude_sources': None},
    {'include_sources': ('cli',), 'exclude_sources': None},
    {'exclude_sources': ('cron', 'webhook', 'kanban')},
    {'project_assignment': 'assigned'},
    {'project_assignment': 'unassigned'},
]
LIMITS = (1, 2, 5, 20, None)


def _ddl(mutation):
    """Return (sessions_sql, messages_sql, indexes) for a schema mutation."""
    tables = {name: sql for kind, name, sql in FIXTURE if kind == 'table'}
    indexes = [sql for kind, name, sql in FIXTURE if kind == 'index' and sql]
    sessions = tables['sessions']
    messages = tables['messages']
    if mutation == 'desc_pk':
        sessions = sessions.replace('id TEXT PRIMARY KEY', 'id TEXT PRIMARY KEY DESC', 1)
    elif mutation == 'unique_session_key':
        sessions = sessions.replace('session_key TEXT,', 'session_key TEXT UNIQUE,', 1)
    elif mutation == 'unique_message_uid':
        messages = messages.replace('message_uid TEXT,', 'message_uid TEXT UNIQUE,', 1)
    elif mutation == 'partial_unlisted_column':
        indexes = indexes + [
            "CREATE INDEX idx_sessions_visible ON sessions(title) WHERE hidden = 0"]
    elif mutation == 'null_keys':
        sessions = sessions.replace('started_at REAL NOT NULL', 'started_at REAL', 1)
        messages = messages.replace('timestamp REAL NOT NULL', 'timestamp REAL', 1)
    elif mutation == 'text_started_at':
        sessions = sessions.replace('started_at REAL NOT NULL', 'started_at TEXT', 1)
        messages = messages.replace('timestamp REAL NOT NULL', 'timestamp REAL', 1)
        messages = messages.replace('session_id TEXT NOT NULL REFERENCES sessions(id),',
                                    'session_id TEXT,', 1)
    return sessions, messages, indexes


def build(path, *, mutation=None, n=40, tied=False, analyze=False, start=1000.0,
          sources=('cli', 'cron', 'kanban', 'subagent'), text_keys=False):
    sessions, messages, indexes = _ddl(mutation)
    with sqlite3.connect(path) as c:
        c.execute(sessions)
        c.execute(messages)
        for sql in indexes:
            c.execute(sql)
        for i in range(n):
            sid = f'p{i:03d}'
            if text_keys:
                # numeric-looking text and an ISO-8601 text key in the same column
                started = '%d.0' % (1000 + i) if i % 2 else '2024-01-01T00:00:%02d' % i
            else:
                started = start if tied else start + i
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count)'
                      ' VALUES(?,?,?,?,?,?)',
                      (sid, sources[i % len(sources)], f'Title {i}', 'model', started, 1))
            if text_keys:
                c.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                          (sid, 'user', 'synthetic', None))
                continue
            stamp = 900.0 if tied else 900.0 + i
            c.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                      (sid, 'user', 'synthetic', stamp))
        if analyze:
            c.execute('ANALYZE')
    return path


@pytest.fixture(autouse=True)
def clear():
    summaries.clear()
    yield
    summaries.clear()


def compare(path, oracle, limits=LIMITS, scopes=SCOPES):
    mismatches = []
    summary_paths = 0
    fallbacks = 0
    for limit in limits:
        for scope in scopes:
            expected = oracle(path, limit=limit, **scope)
            before = summaries.counters()
            actual = current.read_importable_agent_session_rows(path, limit=limit, **scope)
            after = summaries.counters()
            summary_paths += after.get('summary_policy_passes', 0) - before.get('summary_policy_passes', 0)
            fallbacks += after.get('legacy_fallbacks', 0) - before.get('legacy_fallbacks', 0)
            if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
                mismatches.append((limit, scope,
                                   [r['id'] for r in actual], [r['id'] for r in expected]))
    return mismatches, summary_paths, fallbacks


def test_tie_free_canonical_uses_summary_and_matches(tmp_path):
    path = build(tmp_path / 'state.db', n=40, tied=False)
    oracle = production_reader()
    bad, summary_paths, fallbacks = compare(path, oracle)
    print('\ntie_free canonical: summary_paths', summary_paths, 'fallbacks', fallbacks,
          'mismatches', len(bad))
    assert not bad, bad[:4]
    assert fallbacks == 0
    assert summary_paths > 0


def test_tie_free_analyzed_matches(tmp_path):
    """Round-3 B1 was: ANALYZE + canonical DDL diverged. Now admission is data-driven."""
    path = build(tmp_path / 'state.db', n=40, tied=False, analyze=True)
    oracle = production_reader()
    bad, summary_paths, fallbacks = compare(path, oracle)
    print('\ntie_free analyzed: summary_paths', summary_paths, 'fallbacks', fallbacks,
          'mismatches', len(bad))
    assert not bad, bad[:4]
    assert fallbacks == 0


def test_tied_canonical_falls_back_and_matches(tmp_path):
    path = build(tmp_path / 'state.db', n=40, tied=True)
    oracle = production_reader()
    bad, summary_paths, fallbacks = compare(path, oracle)
    print('\ntied canonical: summary_paths', summary_paths, 'fallbacks', fallbacks,
          'mismatches', len(bad))
    assert not bad, bad[:4]
    assert fallbacks == len(LIMITS) * len(SCOPES)
    assert summaries.counters().get('tie_fallbacks', 0) == len(LIMITS) * len(SCOPES)


def test_tied_analyzed_falls_back_and_matches(tmp_path):
    path = build(tmp_path / 'state.db', n=40, tied=True, analyze=True)
    oracle = production_reader()
    bad, summary_paths, fallbacks = compare(path, oracle)
    print('\ntied analyzed: summary_paths', summary_paths, 'fallbacks', fallbacks,
          'mismatches', len(bad))
    assert not bad, bad[:4]
    assert fallbacks == len(LIMITS) * len(SCOPES)


def test_unbounded_ties_fall_back(tmp_path):
    """Astra round-3 B1b: unbounded reads must not reorder tied rows either."""
    path = build(tmp_path / 'state.db', n=30, tied=True)
    oracle = production_reader()
    expected = oracle(path, limit=None)
    actual = current.read_importable_agent_session_rows(path, limit=None)
    print('\nunbounded tied: fallbacks', summaries.counters().get('legacy_fallbacks', 0))
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert summaries.counters().get('legacy_fallbacks', 0) == 1


@pytest.mark.parametrize('mutation', ['desc_pk', 'unique_session_key', 'unique_message_uid',
                                      'partial_unlisted_column'])
def test_implicit_index_shapes_reject_summary_bounded_and_unbounded(tmp_path, mutation):
    path = build(tmp_path / 'state.db', mutation=mutation, n=20, tied=False)
    oracle = production_reader()
    for limit in (1, 5, None):
        expected = oracle(path, limit=limit)
        actual = current.read_importable_agent_session_rows(path, limit=limit)
        assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True), (mutation, limit)
        assert summaries.counters().get('summary_policy_passes', 0) == 0, (mutation, limit)
    print('\n', mutation, 'counters', summaries.counters())


def test_tie_appears_after_external_commit(tmp_path):
    """The cached tie_free decision must follow an external writer's commit."""
    path = build(tmp_path / 'state.db', n=12, tied=False)
    oracle = production_reader()
    first = current.read_importable_agent_session_rows(path, limit=3)
    assert summaries.counters().get('legacy_fallbacks', 0) == 0
    with sqlite3.connect(path) as writer:
        # Force a tie on K with an existing row (p001 has started_at=1001, msg 901).
        writer.execute("UPDATE messages SET timestamp=901.0 WHERE session_id='p002'")
    expected = oracle(path, limit=3)
    actual = current.read_importable_agent_session_rows(path, limit=3)
    print('\ntie after commit: ids', [r['id'] for r in actual], [r['id'] for r in expected],
          'counters', summaries.counters())
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert summaries.counters().get('tie_fallbacks', 0) == 1


def test_tie_removed_resumes_summary(tmp_path):
    path = build(tmp_path / 'state.db', n=12, tied=False)
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE messages SET timestamp=901.0 WHERE session_id='p002'")
    current.read_importable_agent_session_rows(path, limit=3)
    assert summaries.counters().get('tie_fallbacks', 0) == 1
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE messages SET timestamp=902.0 WHERE session_id='p002'")
    oracle = production_reader()
    expected = oracle(path, limit=5)
    actual = current.read_importable_agent_session_rows(path, limit=5)
    print('\ntie removed: counters', summaries.counters())
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert summaries.counters().get('tie_fallbacks', 0) == 1


def test_scope_subset_of_global_tie_is_conservative(tmp_path):
    """A tie outside the requested scope still forces the exact legacy SQL."""
    path = build(tmp_path / 'state.db', n=24, tied=False)
    with sqlite3.connect(path) as writer:
        # tie only between two cron rows
        writer.execute("UPDATE messages SET timestamp=900.0 WHERE session_id='p001'")  # cron
        writer.execute("UPDATE messages SET timestamp=900.0 WHERE session_id='p005'")  # cron
    oracle = production_reader()
    bad, summary_paths, fallbacks = compare(path, oracle, limits=(1, 5),
                                            scopes=[{'include_sources': ('cli',)}])
    print('\nscope subset: summary_paths', summary_paths, 'fallbacks', fallbacks)
    assert not bad, bad
    assert fallbacks == 2


def test_lineage_and_project_filters_match(tmp_path):
    """Compression lineage + project filters over the tie-free summary path."""
    path = build(tmp_path / 'state.db', n=14, tied=False)
    with sqlite3.connect(path) as c:
        c.execute('ALTER TABLE sessions ADD COLUMN project_id TEXT')
        # turn p002 -> p003 into a compression chain and assign a project to the tip
        c.execute("UPDATE sessions SET end_reason='compression', ended_at=1002.5 WHERE id='p002'")
        c.execute("UPDATE sessions SET parent_session_id='p002', project_id='proj-a' WHERE id='p003'")
        c.execute("UPDATE sessions SET project_id='proj-b' WHERE id='p009'")
        c.execute("UPDATE messages SET timestamp=2000.0 WHERE session_id='p003'")
    oracle = production_reader()
    scopes = [{}, {'project_assignment': 'assigned'}, {'project_assignment': 'unassigned'},
              {'project_assignment': 'assigned', 'project_ids': ('proj-a',)},
              {'project_assignment': 'assigned', 'project_ids': ('proj-b',)},
              {'include_sources': ('cli',)}]
    bad, summary_paths, fallbacks = compare(path, oracle, limits=(1, 2, 5, 50, None), scopes=scopes)
    print('\nlineage/project: summary_paths', summary_paths, 'fallbacks', fallbacks,
          'mismatches', len(bad))
    assert not bad, bad[:4]
    assert fallbacks == 0


def test_text_started_at_mixed_keys(tmp_path):
    """Non-REAL keys: both paths sort by _as_score, so they agree today.

    Distinct text K values collapse to one Python score (0.0), which is the
    residual plan-dependence noted in the review; this pins current equality.
    """
    path = build(tmp_path / 'state.db', mutation='text_started_at', n=8, text_keys=True)
    oracle = production_reader()
    expected = oracle(path, limit=None)
    actual = current.read_importable_agent_session_rows(path, limit=None)
    exp_ids = [r['id'] for r in expected]
    act_ids = [r['id'] for r in actual]
    scores = [_as_score(r.get('last_activity'), r.get('started_at')) for r in actual]
    print('\ntext keys: production', exp_ids, '\n           summary   ', act_ids,
          '\n           scores', scores, '\n           counters', summaries.counters())
    assert exp_ids == act_ids


def test_null_and_zero_keys_tie_in_the_effective_sort(tmp_path):
    """tie_free is checked on SQL K, but both paths sort by _as_score(K).

    A NULL key and a 0.0 key are distinct to the gate yet collapse to the same
    score, so the order among them still depends on each database's plan.
    """
    path = tmp_path / 'state.db'
    sessions, messages, indexes = _ddl('null_keys')
    with sqlite3.connect(path) as c:
        c.execute(sessions)
        c.execute(messages)
        for sql in indexes:
            c.execute(sql)
        for sid, started in (('n000', None), ('n001', 0.0), ('n002', 5.0)):
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count)'
                      ' VALUES(?,?,?,?,?,?)', (sid, 'cli', f'Title {sid}', 'model', started, 1))
            c.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                      (sid, 'user', 'x', None))
    oracle = production_reader()
    expected = oracle(path, limit=None)
    actual = current.read_importable_agent_session_rows(path, limit=None)
    exp = [r['id'] for r in expected]
    act = [r['id'] for r in actual]
    print('\nnull/zero: production', exp, 'summary', act,
          'scores', [_as_score(r.get('last_activity'), r.get('started_at')) for r in actual],
          'counters', summaries.counters())
    assert summaries.counters().get('tie_fallbacks', 0) == 0  # gate certifies
    assert _as_score(None, None) == _as_score(None, 0.0) == 0.0
    assert exp == act


def test_fallback_sql_is_production_sql(tmp_path, monkeypatch):
    """The tie fallback must execute literally production's statement."""
    import sqlite3 as sqlite3_module
    recorded = {}
    real_connect = sqlite3_module.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(lambda statement: recorded.setdefault('sql', []).append(statement))
        return conn

    monkeypatch.setattr(sqlite3_module, 'connect', recording_connect)
    path = build(tmp_path / 'state.db', n=30, tied=True)
    oracle = production_reader()

    recorded['sql'] = []
    oracle(path, limit=2)
    expected_sql = [s for s in recorded['sql'] if 'COALESCE(MAX(m.timestamp), s.started_at) DESC' in s]

    recorded['sql'] = []
    current.read_importable_agent_session_rows(path, limit=2)
    actual_sql = [s for s in recorded['sql'] if 'COALESCE(MAX(m.timestamp), s.started_at) DESC' in s]

    print('\nfallback statements: expected', len(expected_sql), 'actual', len(actual_sql))
    assert expected_sql and len(expected_sql) == len(actual_sql)
    assert [s.split() for s in actual_sql] == [s.split() for s in expected_sql]
    assert summaries.counters().get('tie_fallbacks', 0) == 1
