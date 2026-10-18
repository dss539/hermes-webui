"""Review-3 DeepSeek derivative/index probes (my copy only)."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from api import models
from api import session_summary_index as index
from api import sidebar_revision as revision


@pytest.fixture(autouse=True)
def clear(monkeypatch):
    # These probes deliberately assign model globals. Record their originals
    # before setup so neither synthetic paths nor the fake persisted-id reader
    # can survive into later files in a suite shard.
    for name in ('SESSION_DIR', 'SESSION_INDEX_FILE', '_persisted_session_ids_snapshot',
                 '_PERSISTED_SESSION_IDS_CACHE'):
        monkeypatch.setattr(models, name, getattr(models, name))
    monkeypatch.setattr(models, 'SESSIONS', {})
    index.clear()
    yield
    index.clear()


def db_state(tmp_path):
    with closing(sqlite3.connect(tmp_path / '_sidebar.db')) as c:
        rows = c.execute('SELECT session_id,index_row_json FROM rows ORDER BY session_id').fetchall()
        meta = c.execute('SELECT format_version,index_signature,generation,index_order FROM meta').fetchone()
    return rows, meta


def canonical_images(rows):
    return [index._image(r) for r in rows]


def fake_sessions(path):
    models.SESSION_DIR = path
    models.SESSION_INDEX_FILE = path / '_index.json'
    models.SESSIONS.clear()


def write_sidecars(path, count):
    for i in range(count):
        (path / f's{i}.json').write_text(json.dumps({
            'session_id': f's{i}', 'title': f'T{i}', 'updated_at': 1000 + i,
            'message_count': 1, 'messages': [{'role': 'user', 'content': 'x'}],
        }))


def test_delta_publish_matches_canonical_and_db(tmp_path):
    write_sidecars(tmp_path, 25)
    fake_sessions(tmp_path)
    models._write_session_index(updates=None)
    canonical = json.loads((tmp_path / '_index.json').read_text())
    assert index.read_index(tmp_path / '_index.json') == canonical_images(canonical)
    rows, meta = db_state(tmp_path)
    assert len(rows) == 25 and meta[0] == 3
    assert set(json.loads(meta[3])) == {r['session_id'] for r in canonical}

    row = dict(canonical[3])
    row['title'] = 'changed'
    row['updated_at'] = 99999
    row['message_count'] = 2.0

    class Changed:
        session_id = row['session_id']

        def compact(self):
            return row

    before = index.counters().get('index_rows_examined', 0)
    models._write_session_index(updates=[Changed()])
    examined = index.counters().get('index_rows_examined', 0) - before
    canonical = json.loads((tmp_path / '_index.json').read_text())
    actual = index.read_index(tmp_path / '_index.json')
    rows, meta = db_state(tmp_path)
    print('\n delta examined', examined, 'generation', meta[2], 'rows', len(rows))
    assert actual == canonical_images(canonical)
    assert json.loads(meta[3]) == [r['session_id'] for r in canonical]
    assert {sid for sid, _ in rows} == {r['session_id'] for r in canonical}
    assert examined == 1
    # restart
    index.clear()
    assert index.read_index(tmp_path / '_index.json') == canonical_images(canonical)


def test_delta_reclaims_deleted_and_respects_external_rewrite(tmp_path):
    write_sidecars(tmp_path, 6)
    fake_sessions(tmp_path)
    models._write_session_index(updates=None)
    canonical = json.loads((tmp_path / '_index.json').read_text())
    # External writer drops one row from canonical without publishing.
    external = [r for r in canonical if r['session_id'] != 's2']
    (tmp_path / '_index.json').write_text(json.dumps(external))
    assert index.read_index(tmp_path / '_index.json') == canonical_images(external)
    rows, meta = db_state(tmp_path)
    assert {sid for sid, _ in rows} == {r['session_id'] for r in external}
    # A delta save right after must still walk the canonical file fully.
    row = dict(external[0])
    row['title'] = 'after-external'

    class Changed:
        session_id = row['session_id']

        def compact(self):
            return row

    models._write_session_index(updates=[Changed()])
    canonical = json.loads((tmp_path / '_index.json').read_text())
    assert index.read_index(tmp_path / '_index.json') == canonical_images(canonical)
    rows, meta = db_state(tmp_path)
    assert {sid for sid, _ in rows} == {r['session_id'] for r in canonical}
    assert all(e['session_id'] != 's2' for e in canonical)


def test_prune_then_read(tmp_path):
    write_sidecars(tmp_path, 4)
    fake_sessions(tmp_path)
    models._write_session_index(updates=None)
    models.prune_session_from_index('s1')
    canonical = json.loads((tmp_path / '_index.json').read_text())
    assert index.read_index(tmp_path / '_index.json') == canonical_images(canonical)
    rows, _ = db_state(tmp_path)
    assert {sid for sid, _ in rows} == {r['session_id'] for r in canonical}


def test_two_stores_coherent(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B'}]
    path.write_text(json.dumps(rows))
    index.read_index(path)
    store_a = index._store(tmp_path)
    # Simulated second process: independent store object over the same file.
    store_b = index._Store(tmp_path)
    rows_b = [{'session_id': 'b', 'title': 'B2'}, {'session_id': 'c', 'title': 'C'}]
    path.write_text(json.dumps(rows_b))
    signature_b = revision.path_revision(path)
    store_b.publish(path, rows_b, signature_b)
    # A must observe B's committed generation, not its stale map.
    assert index.read_index(path) == canonical_images(rows_b)
    rows_db, _ = db_state(tmp_path)
    assert {sid for sid, _ in rows_db} == {'b', 'c'}
    # A publishes a full walk that deletes b: no resurrection after restart.
    rows_a = [{'session_id': 'd', 'title': 'D'}]
    path.write_text(json.dumps(rows_a))
    signature_a = revision.path_revision(path)
    store_a.publish(path, rows_a, signature_a)
    assert index.read_index(path) == canonical_images(rows_a)
    index.clear()
    assert index.read_index(path) == canonical_images(rows_a)
    rows_db, _ = db_state(tmp_path)
    assert {sid for sid, _ in rows_db} == {'d'}, rows_db


def test_failed_local_install_reloads_from_db(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B'}]
    path.write_text(json.dumps(rows))
    index.read_index(path)
    store = index._store(tmp_path)

    class Boom(dict):
        def __setitem__(self, key, value):
            raise RuntimeError('simulated crash installing the process-local delta')

    store.entries = Boom(store.entries)
    more = [{'session_id': 'a', 'title': 'A2'}, {'session_id': 'b', 'title': 'B'}]
    path.write_text(json.dumps(more))
    signature = revision.path_revision(path)
    try:
        store.publish(path, more, signature)
    except RuntimeError:
        pass
    store.entries = dict(store.entries)
    # The committed transaction must be visible; the crashed in-memory map must not win.
    assert index.read_index(path) == canonical_images(more)


def test_format2_file_rebuilds(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}]
    path.write_text(json.dumps(rows))
    with closing(sqlite3.connect(tmp_path / '_sidebar.db')) as c:
        c.executescript('''
            CREATE TABLE meta (singleton INTEGER PRIMARY KEY, format_version INTEGER, index_signature TEXT);
            CREATE TABLE rows (session_id TEXT PRIMARY KEY, index_row_json TEXT NOT NULL, index_position INTEGER NOT NULL);
            INSERT INTO meta VALUES (1,2,'stale');
            INSERT INTO rows VALUES ('zzz','{"session_id":"zzz","title":"STALE"}',0);
        ''')
        c.commit()
    assert index.read_index(path) == canonical_images(rows)
    assert index.counters().get('derivative_rebuilds') == 1
    rows_db, meta = db_state(tmp_path)
    assert {sid for sid, _ in rows_db} == {'a'} and meta[0] == 3


@pytest.mark.parametrize('count', [355, 3000, 8000])
def test_delta_publish_cost(tmp_path, count):
    path = tmp_path / '_index.json'
    rows = [{'session_id': str(i), 'title': str(i), 'updated_at': i, 'message_count': i}
            for i in range(count)]
    path.write_text(json.dumps(rows))
    index.read_index(path)
    models.SESSION_DIR = tmp_path
    models.SESSION_INDEX_FILE = path
    models.SESSIONS.clear()
    models._persisted_session_ids_snapshot = lambda: {str(i) for i in range(count)}
    changed = {'session_id': '0', 'title': 'x', 'updated_at': count + 1, 'message_count': 1}

    class Changed:
        session_id = '0'

        def compact(self):
            return changed

    import os
    models._persisted_session_ids_snapshot = lambda: {str(i) for i in range(count)}
    before = index.counters().get('index_rows_examined', 0)
    start = time.perf_counter()
    models._write_session_index(updates=[Changed()])
    elapsed = (time.perf_counter() - start) * 1000
    examined = index.counters().get('index_rows_examined', 0) - before
    # read cost after the save
    start = time.perf_counter()
    for _ in range(3):
        index.read_index(path)
    read_ms = (time.perf_counter() - start) * 1000 / 3
    print('\n count', count, 'save+delta ms %.1f' % elapsed, 'examined', examined, 'read ms %.2f' % read_ms)
    assert examined == 1


def test_delta_adds_new_and_drops_removed_rows(tmp_path):
    write_sidecars(tmp_path, 3)
    fake_sessions(tmp_path)
    models._write_session_index(updates=None)
    import json as _json
    new_row = {'session_id': 'snew', 'title': 'N', 'updated_at': 5000, 'message_count': 1}

    class New:
        session_id = 'snew'

        def compact(self):
            return new_row

    models._write_session_index(updates=[New()])
    canonical = _json.loads((tmp_path / '_index.json').read_text())
    assert [r['session_id'] for r in canonical][0] == 'snew'
    assert index.read_index(tmp_path / '_index.json') == canonical_images(canonical)
    # Sidecar disappearance prunes the row through the same delta publish.
    (tmp_path / 's1.json').unlink()
    models._write_session_index(updates=[])
    canonical = _json.loads((tmp_path / '_index.json').read_text())
    assert all(r['session_id'] != 's1' for r in canonical)
    assert index.read_index(tmp_path / '_index.json') == canonical_images(canonical)
    rows, meta = db_state(tmp_path)
    assert {sid for sid, _ in rows} == {r['session_id'] for r in canonical}
    assert _json.loads(meta[3]) == [r['session_id'] for r in canonical]
