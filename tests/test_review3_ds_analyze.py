"""Isolate the ANALYZE/statistics bypass of the bounded-order guard."""
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from api import agent_session_summaries as summaries
from api import agent_sessions as current

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/sidebar_agent_schema.json').read_text())


def production_reader():
    oracle = {"__name__": "production_cbd7e993"}
    exec(compile(subprocess.check_output(
        ['git', 'show', 'cbd7e993:api/agent_sessions.py'], cwd=ROOT, text=True),
        'production_reader', 'exec'), oracle)
    return oracle['read_importable_agent_session_rows']


def build(tmp_path, *, analyze, count=60):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'state.db'
    with sqlite3.connect(path) as c:
        for kind, name, sql in FIXTURE:
            if kind == 'table':
                c.execute(sql)
        for kind, name, sql in FIXTURE:
            if kind == 'index' and sql:
                c.execute(sql)
        for i in range(count):
            c.execute('INSERT INTO sessions(id,source,title,model,started_at,message_count) '
                      'VALUES(?,?,?,?,?,?)',
                      (f'p{i:02d}', ('cli', 'cron', 'kanban', 'subagent')[i % 4], f'T{i}', 'm', 1000.0, 1))
            c.execute('INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)',
                      (f'p{i:02d}', 'user', 'x', 900.0))
        if analyze:
            c.execute('ANALYZE')
    return path


@pytest.fixture(autouse=True)
def clear():
    summaries.clear()
    yield
    summaries.clear()


def ids(rows):
    return [r['id'] for r in rows]


def test_statistics_blind_spot(tmp_path):
    oracle = production_reader()
    for analyze in (False, True):
        path = build(tmp_path / ('a' if analyze else 'n'), analyze=analyze)
        base = ids(oracle(path, limit=2))
        summaries.clear()
        tip_summary = ids(current.read_importable_agent_session_rows(path, limit=2))
        summaries.clear()
        # Reject the layout with an extra index so the exact legacy SQL runs on
        # the source. Production is then measured on that same file, so both
        # builds see the identical schema/plan.
        with sqlite3.connect(path) as c:
            c.execute('CREATE INDEX surprise ON sessions(title,source)')
        summaries.clear()
        base_same = ids(oracle(path, limit=2))
        summaries.clear()
        tip_legacy = ids(current.read_importable_agent_session_rows(path, limit=2))
        print('\nanalyze=', analyze, 'base=', base, 'tip_summary=', tip_summary,
              'base(same file, forced-fallback schema)=', base_same,
              'tip_legacy=', tip_legacy)
        assert base == tip_summary
        assert base_same == tip_legacy, 'legacy SQL on the source must reproduce production'
        assert summaries.counters()['tie_fallbacks'] == 1


def test_guard_sees_only_indexes(tmp_path):
    path = build(tmp_path / 'x', analyze=True)
    with summaries.summary_connection(path, ordered=True) as c:
        assert not getattr(c, 'sidebar_summary', False)
        assert c.execute('SELECT COUNT(*) FROM sqlite_stat1').fetchone()[0] > 0
    assert summaries.counters()['tie_fallbacks'] == 1
