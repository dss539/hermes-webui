"""Canonical JSON compatibility, source fences and independent compact images."""
import json
import os
import sqlite3

import pytest
from api import session_summary_index as index


@pytest.fixture(autouse=True)
def reset_index():
    index.clear()
    yield
    index.clear()


def test_index_changed_rows_external_edit_restart_and_delete(tmp_path):
    path = tmp_path/'_index.json'
    rows = [{'session_id': 'a', 'message_count': 99}, {'session_id': 'b', 'message_count': 3}]
    path.write_text(json.dumps(rows))
    assert index.read_index(path) == rows
    initial = index.counters()
    result = index.read_index(path)
    result[0]['message_count'] = -1
    assert index.read_index(path) == rows
    assert index.counters()['full_index_parses'] == initial['full_index_parses']
    with sqlite3.connect(tmp_path/'_sidebar.db') as c:
        before = dict(c.execute('SELECT session_id,index_row_json FROM rows'))
    rows[1]['message_count'] = 4
    path.write_text(json.dumps(rows))
    assert index.read_index(path) == rows
    with sqlite3.connect(tmp_path/'_sidebar.db') as c:
        after = dict(c.execute('SELECT session_id,index_row_json FROM rows'))
    assert before['a'] == after['a']
    assert before['b'] != after['b']
    index.clear()
    assert index.read_index(path) == rows
    assert index.counters()['index_disk_hits'] == 1
    rows.pop()
    path.write_text(json.dumps(rows))
    assert index.read_index(path) == rows
    with sqlite3.connect(tmp_path/'_sidebar.db') as c:
        assert c.execute("SELECT index_row_json FROM rows WHERE session_id='b'").fetchone() is None


def test_metadata_images_do_not_correct_compatibility_counts(tmp_path):
    path = tmp_path/'a.json'
    path.write_text('canonical bytes')
    calls = []
    def load():
        calls.append(True)
        return {'message_count': 2, 'user_message_count': 0, 'title': 'old'}
    first = index.metadata_compact(path, 99, load)
    first['title'] = 'mutated'
    assert index.metadata_compact(path, 99, load)['title'] == 'old'
    assert len(calls) == 1
    # A missing metadata count can depend on a separately changed index count.
    index.metadata_compact(path, 100, load)
    assert len(calls) == 2
    index_path = tmp_path/'_index.json'
    index_path.write_text(json.dumps([{'session_id': 'a', 'message_count': 99}]))
    assert index.read_index(index_path)[0]['message_count'] == 99
    assert index.metadata_compact(path, 99, load)['message_count'] == 2
    with sqlite3.connect(tmp_path/'_sidebar.db') as c:
        compatibility = c.execute('SELECT index_row_json FROM rows').fetchone()[0]
        assert json.loads(compatibility)['message_count'] == 99
    assert next(iter(index._store(tmp_path).metadata.values()))['message_count'] == 2


def test_only_changed_sidecar_is_loaded_and_replacement_detected(tmp_path):
    a, b = tmp_path/'a.json', tmp_path/'b.json'
    for path in (a,b):
        path.write_text('before')
    calls = []
    def load(path):
        calls.append(path.name)
        return {'title': path.read_text()}
    for path in (a,b):
        assert index.metadata_compact(path, 1, lambda: load(path))['title'] == 'before'
    replacement = tmp_path/'replacement'
    replacement.write_text('after!')
    os.replace(replacement, a)
    for path in (a,b):
        index.metadata_compact(path, 1, lambda: load(path))
    assert calls == ['a.json','b.json','a.json']
    a.unlink()
    assert index.metadata_compact(a, 1, lambda: None) is None


def test_mutation_during_load_not_blessed_as_new(tmp_path):
    path = tmp_path/'a.json'
    path.write_text('old')
    def raced():
        path.write_text('new')
        return {'title': 'old'}
    assert index.metadata_compact(path, 1, raced) == {'title':'old'}
    assert index.metadata_compact(path, 1, lambda: {'title':path.read_text()}) == {'title':'new'}


def test_corrupt_missing_derivative_and_failed_maintenance_preserve_canonical(tmp_path, monkeypatch):
    path = tmp_path/'_index.json'
    rows = [{'session_id':'a','message_count':3}]
    path.write_text(json.dumps(rows))
    (tmp_path/'_sidebar.db').write_bytes(b'corrupt derivative')
    assert index.read_index(path) == rows
    monkeypatch.setattr(index, '_store', lambda p: (_ for _ in ()).throw(OSError('derivative unavailable')))
    index.publish_index(path, rows)  # Successful canonical write must not fail.
    assert index.read_index(path) == rows
    assert path.read_text() == json.dumps(rows)


def test_canonical_corrupt_index_still_raises_for_existing_recovery(tmp_path):
    path = tmp_path/'_index.json'
    path.write_text('[')
    with pytest.raises(json.JSONDecodeError):
        index.read_index(path)
