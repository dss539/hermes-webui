"""Review-3 DeepSeek independent probes (my copy only; never committed)."""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from api import agent_session_summaries as summaries
from api import agent_sessions as current

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/sidebar_agent_schema.json').read_text())

CANONICAL = 'canonical'
MUTATIONS = [
    CANONICAL,
    'analyzed',
    'extra_index',
    'missing_index',
    'wrong_columns',
    'partial',
    'unique',
    'no_messages_index',
    'no_messages_table',
    'added_column',
]


def production_reader():
    oracle = {"__name__": "production_cbd7e993"}
    exec(compile(subprocess.check_output(
        ['git', 'show', 'cbd7e993:api/agent_sessions.py'], cwd=ROOT, text=True),
        'production_reader', 'exec'), oracle)
    return oracle['read_importable_agent_session_rows']


def _apply(mutation, c):
    if mutation == 'analyzed':
        c.execute('ANALYZE')
    elif mutation == 'extra_index':
        c.execute('CREATE INDEX surprise ON sessions(title,source)')
    elif mutation == 'missing_index':
        c.execute('DROP INDEX idx_sessions_source_id')
    elif mutation == 'wrong_columns':
        c.executescript('DROP INDEX idx_sessions_source_id;'
                        'CREATE INDEX idx_sessions_source_id ON sessions(id,source)')
    elif mutation == 'partial':
        c.executescript("DROP INDEX idx_sessions_source_id;"
                        "CREATE INDEX idx_sessions_source_id ON sessions(source,id) WHERE source='cli'")
    elif mutation == 'unique':
        c.executescript('DROP INDEX idx_sessions_source_id;'
                        'CREATE UNIQUE INDEX idx_sessions_source_id ON sessions(source,id)')
    elif mutation == 'no_messages_index':
        c.execute('DROP INDEX idx_messages_session')
    elif mutation == 'no_messages_table':
        c.execute('DROP TABLE messages')
    elif mutation == 'added_column':
        c.execute('ALTER TABLE sessions ADD COLUMN extra_flag INTEGER DEFAULT 0')


def build_db(path, mutation=CANONICAL, *, count=60, tied=True):
    with sqlite3.connect(path) as c:
        for kind, name, sql in FIXTURE:
            if kind == 'table':
                c.execute(sql)
        for kind, name, sql in FIXTURE:
            if kind == 'index' and sql:
                c.execute(sql)
        sources = ('cli', 'cron', 'kanban', 'subagent')
        for i in range(count):
            started = 1000.0 if tied else 1000.0 + i
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count) '
                      'VALUES(?,?,?,?,?,?)',
                      (f'p{i:02d}', sources[i % 4], f'Title {i}', 'model', started, 1))
            stamp = 900.0 if tied else 900.0 + i
            c.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                      (f'p{i:02d}', 'user', 'synthetic', stamp))
        _apply(mutation, c)
    return path


SCOPES = [
    {},
    {'include_sources': ('cron',), 'exclude_sources': None},
    {'include_sources': ('cli',), 'exclude_sources': None},
    {'exclude_sources': ('cron', 'webhook', 'kanban')},
    {'project_assignment': 'assigned'},
    {'project_assignment': 'unassigned'},
]

LIMITS = (1, 2, 3, 5, 20, 60, 200)


@pytest.fixture(autouse=True)
def clear():
    summaries.clear()
    yield
    summaries.clear()


@pytest.mark.parametrize('mutation', MUTATIONS)
def test_bounded_equivalence_and_path(tmp_path, mutation):
    path = build_db(tmp_path / 'state.db', mutation)
    oracle = production_reader()
    mismatches = []
    used_summary = 0
    used_legacy = 0
    for limit in LIMITS:
        for scope in SCOPES:
            expected = oracle(path, limit=limit, **scope)
            before = summaries.counters()
            actual = current.read_importable_agent_session_rows(path, limit=limit, **scope)
            after = summaries.counters()
            used_summary += after.get('summary_policy_passes', 0) - before.get('summary_policy_passes', 0)
            used_legacy += after.get('legacy_fallbacks', 0) - before.get('legacy_fallbacks', 0)
            if json.dumps(actual, sort_keys=True) != json.dumps(expected, sort_keys=True):
                mismatches.append((limit, scope,
                                   [r['id'] for r in actual], [r['id'] for r in expected]))
    print('\n', mutation, 'comparisons', len(LIMITS) * len(SCOPES),
          'legacy_paths', used_legacy, 'summary_paths', used_summary)
    assert not mismatches, (mutation, mismatches[:6])


@pytest.mark.parametrize('mutation', MUTATIONS)
def test_unbounded_equivalence(tmp_path, mutation):
    """Unbounded reads always use the summary path; check tie order anyway."""
    path = build_db(tmp_path / 'state.db', mutation)
    oracle = production_reader()
    expected = oracle(path, limit=None)
    actual = current.read_importable_agent_session_rows(path, limit=None)
    print('\n', mutation, 'unbounded ids equal', [r['id'] for r in actual] == [r['id'] for r in expected])
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
