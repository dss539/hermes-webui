"""Review-4 DeepSeek derivative generation/no-op publish probes (my copy only)."""
import json
import os
import sqlite3
from contextlib import closing

import pytest

from api import session_summary_index as index
from api import sidebar_revision as revision


@pytest.fixture(autouse=True)
def clear():
    index.clear()
    yield
    index.clear()


def db_state(tmp_path):
    with closing(sqlite3.connect(tmp_path / '_sidebar.db')) as c:
        rows = c.execute('SELECT session_id,index_row_json FROM rows ORDER BY session_id').fetchall()
        meta = c.execute('SELECT format_version,index_signature,generation,index_order FROM meta').fetchone()
    return rows, meta


def images(rows):
    return [index._image(r) for r in rows]


def write(path, rows):
    path.write_text(json.dumps(rows))


def test_noop_republish_keeps_generation_and_updates_signature(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B'}]
    write(path, rows)
    assert index.read_index(path) == images(rows)
    _, before = db_state(tmp_path)
    first_sig = json.loads(before[1])

    # Same canonical bytes, new file identity (a rewrite, e.g. a save that
    # wrote identical content): only the source fence may move.
    write(path, rows)
    store = index._store(tmp_path)
    new_sig = revision.path_revision(path)
    assert json.dumps(new_sig) != json.dumps(first_sig)
    store.publish(path, rows, new_sig)
    _, after = db_state(tmp_path)
    print('\nno-op publish: generation', before[2], '->', after[2],
          'signature changed', before[1] != after[1])
    assert after[2] == before[2], 'identical images must not bump the generation'
    assert json.loads(after[1]) == list(new_sig)
    assert json.loads(after[3]) == ['a', 'b']
    assert index.read_index(path) == images(rows)


def test_noop_republish_does_not_force_other_process_reload(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B'}]
    write(path, rows)
    index.read_index(path)
    other = index._Store(tmp_path)          # second process's loaded map
    before = index.counters().get('generation_reloads', 0)
    write(path, rows)
    index._store(tmp_path).publish(path, rows, revision.path_revision(path))
    # Second process observes the new signature without reloading rows.
    saved = other.sync()
    after = index.counters().get('generation_reloads', 0)
    print('\nother process reloads', after - before, 'saved is new', saved == json.dumps(revision.path_revision(path)))
    assert after == before
    assert other.entries.keys() == {'a', 'b'}


def test_image_change_with_same_order_bumps_generation(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B'}]
    write(path, rows)
    index.read_index(path)
    _, before = db_state(tmp_path)
    other = index._Store(tmp_path)
    other.sync()

    changed = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B2'}]
    write(path, changed)
    index._store(tmp_path).publish(path, changed, revision.path_revision(path))
    rows_db, after = db_state(tmp_path)
    other.sync()
    print('\nimage change: generation', before[2], '->', after[2],
          'other title', other.entries['b'][0].get('title'))
    assert after[2] == before[2] + 1, 'a real row change must invalidate other processes'
    assert json.loads(after[3]) == ['a', 'b']
    assert other.entries['b'][0].get('title') == 'B2'


def test_reorder_bumps_generation_once(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B'}]
    write(path, rows)
    index.read_index(path)
    _, before = db_state(tmp_path)
    other = index._Store(tmp_path)
    other.sync()

    reordered = [{'session_id': 'b', 'title': 'B'}, {'session_id': 'a', 'title': 'A'}]
    write(path, reordered)
    index._store(tmp_path).publish(path, reordered, revision.path_revision(path))
    _, after = db_state(tmp_path)
    other.sync()
    print('\nreorder: generation', before[2], '->', after[2], 'order', other.order)
    assert after[2] == before[2] + 1
    assert other.order == ['b', 'a']
    assert index.read_index(path) == images(reordered)


def test_delete_with_unchanged_order_bumps_generation(tmp_path):
    path = tmp_path / '_index.json'
    rows = [{'session_id': 'a', 'title': 'A'}, {'session_id': 'b', 'title': 'B'}]
    write(path, rows)
    index.read_index(path)
    _, before = db_state(tmp_path)
    other = index._Store(tmp_path)
    other.sync()
    kept = [{'session_id': 'a', 'title': 'A'}]
    write(path, kept)
    index._store(tmp_path).publish(path, kept, revision.path_revision(path))
    rows_db, after = db_state(tmp_path)
    other.sync()
    print('\ndelete: generation', before[2], '->', after[2], 'other entries', list(other.entries))
    assert after[2] == before[2] + 1
    assert {sid for sid, _ in rows_db} == {'a'}
    assert list(other.entries) == ['a']


def test_metadata_two_variants_coexist(tmp_path):
    path = tmp_path / '_index.json'
    write(path, [{'session_id': 'a', 'title': 'A', 'message_count': 3}])
    calls = []

    def loader():
        calls.append(1)
        return {'session_id': 'a', 'message_count': 3, 'extra': len(calls)}

    first = index.metadata_compact(path, None, loader)
    second = index.metadata_compact(path, 3, loader)
    third = index.metadata_compact(path, None, loader)
    fourth = index.metadata_compact(path, 3, loader)
    print('\nmetadata variants: loads', len(calls), 'hits', index.counters().get('metadata_hits'))
    assert len(calls) == 2, 'both variants must stay cached under one stem'
    assert third == first and fourth == second
    assert index.counters().get('metadata_hits') == 2


def test_metadata_new_generation_drops_both_variants(tmp_path):
    path = tmp_path / '_index.json'
    write(path, [{'session_id': 'a', 'title': 'A'}])
    loads = []

    def loader():
        loads.append(1)
        return {'session_id': 'a', 'title': 'v%d' % len(loads)}

    index.metadata_compact(path, None, loader)
    index.metadata_compact(path, 4, loader)
    write(path, [{'session_id': 'a', 'title': 'A2'}])
    fresh = index.metadata_compact(path, None, loader)
    print('\nnew generation:', fresh, 'loads', len(loads))
    assert fresh['title'] == 'v3'
    assert len(loads) == 3
