"""Ordered tie fallback must tolerate a briefly locked state.db like production.

Derived from the round-5 review probe: a real external writer holds a
rollback-journal EXCLUSIVE lock for 300 ms at the moment the fallback opens
its read-only handle. Production (cbd7e993) uses SQLite's default 5 s busy
timeout and returns rows; the branch must do the same, with identical rows.
No SQLite error, SQL or row is simulated.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import threading
import time

import pytest
from api import agent_sessions as current, agent_session_summaries as summaries
from tests.test_sidebar_repair3 import build

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def clear():
    summaries.clear()
    yield
    summaries.clear()


def _production_reader():
    scope = {'__name__': 'busy_timeout_production'}
    source = subprocess.check_output(
        ['git', 'show', 'cbd7e993:api/agent_sessions.py'], cwd=ROOT, text=True)
    exec(compile(source, 'production.py', 'exec'), scope)
    return scope['read_importable_agent_session_rows']


def _locked_read(path, reader, monkeypatch, limit):
    real_connect = sqlite3.connect
    ready = threading.Event()
    threads, errors, seen = [], [], {}

    def writer():
        try:
            with closing(real_connect(path)) as connection:
                connection.execute('BEGIN EXCLUSIVE')
                ready.set()
                time.sleep(.30)
                connection.rollback()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))
            ready.set()

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        if kwargs.get('uri') and 'mode=ro' in str(args[0]):
            seen['busy_timeout_ms'] = connection.execute('PRAGMA busy_timeout').fetchone()[0]
            thread = threading.Thread(target=writer)
            threads.append(thread)
            thread.start()
            assert ready.wait(3)
        return connection

    with monkeypatch.context() as m:
        m.setattr(sqlite3, 'connect', connect)
        rows = reader(path, limit=limit)
    for thread in threads:
        thread.join(4)
        assert not thread.is_alive()
    assert len(threads) == 1 and not errors
    return rows, seen['busy_timeout_ms']


@pytest.mark.parametrize('limit', [1, 2, 5])
def test_tie_fallback_waits_for_locked_db_like_production(tmp_path, monkeypatch, limit):
    path = build(tmp_path / 'state.db', keys=(10, 10, 20))
    with closing(sqlite3.connect(path)) as c:
        assert c.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    production = _production_reader()
    expected = production(path, limit=limit)
    assert expected
    # Warm the summary reader; the tied keys force the ordered fallback.
    assert current.read_importable_agent_session_rows(path, limit=limit) == expected
    assert summaries.counters()['tie_fallbacks'] >= 1

    rows, busy_ms = _locked_read(path, current.read_importable_agent_session_rows, monkeypatch, limit)
    prod_rows, prod_busy_ms = _locked_read(path, production, monkeypatch, limit)
    assert busy_ms == prod_busy_ms == 5000
    assert rows == prod_rows == expected
