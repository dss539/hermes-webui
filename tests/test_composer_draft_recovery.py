"""Corrupt draft containment and GET/delete boundary coverage."""
import threading
from urllib.parse import urlparse

import pytest
import api.composer_drafts as drafts
import api.models as models
import api.routes as routes
from tests.test_composer_draft_store import Handler, post, seed, store


def get_draft(monkeypatch, sid, expected_status=200):
    result = {}
    monkeypatch.setattr(routes, 'j', lambda h, payload, status=200, **kw: result.update(status=status, payload=payload))
    monkeypatch.setattr(routes, 'bad', lambda h, msg, status=400, **kw: result.update(status=status, error=msg))
    handler = Handler()
    handler.command = 'GET'
    routes.handle_post(handler, urlparse('/api/session/draft?session_id=' + sid))
    assert result['status'] == expected_status, result
    return result


@pytest.mark.parametrize('payload', [b'broken json', b'[]', b'\xff'])
@pytest.mark.parametrize('first_read', ['save', 'compact', 'get'])
def test_corrupt_draft_does_not_block_session_io_or_resurrect_legacy(store, monkeypatch, payload, first_read):
    s = seed(store, draft={'text': 'obsolete secret'})
    path = drafts.draft_path(s.path)
    path.write_bytes(payload)
    if first_read == 'save':
        s.save()
    elif first_read == 'compact':
        assert s.compact()['composer_draft'] == {}
    else:
        assert get_draft(monkeypatch, s.session_id)['payload']['draft'] == {}
    assert s.composer_draft == {}
    assert path.with_suffix('.draft.corrupt').read_bytes() == payload
    assert s.compact()['composer_draft'] == {}
    assert get_draft(monkeypatch, s.session_id)['payload']['draft'] == {}
    s.save()
    models.SESSIONS.clear()
    assert models.Session.load(s.session_id).composer_draft == {}
    assert get_draft(monkeypatch, s.session_id)['payload']['draft'] == {}
    assert post(monkeypatch, s.session_id, text='recovered')['draft'] == {'text': 'recovered'}


@pytest.mark.parametrize('failure', ['read', 'quarantine'])
def test_unreadable_draft_and_failed_quarantine_are_contained(store, monkeypatch, failure):
    from pathlib import Path
    s = seed(store, draft={'text': 'must not return'})
    path = drafts.draft_path(s.path)
    path.write_text('invalid')
    real_read, real_replace = Path.read_text, Path.replace

    def denied_read(target, *a, **kw):
        if target == path:
            raise PermissionError('draft read denied')
        return real_read(target, *a, **kw)

    def denied_replace(target, *a, **kw):
        if target == path:
            raise PermissionError('quarantine denied')
        return real_replace(target, *a, **kw)

    if failure == 'read':
        monkeypatch.setattr(Path, 'read_text', denied_read)
    else:
        monkeypatch.setattr(Path, 'replace', denied_replace)
    assert s.composer_draft == {}
    assert s.compact()['composer_draft'] == {}
    s.save()
    assert get_draft(monkeypatch, s.session_id)['payload']['draft'] == {}


def test_failed_quarantine_warnings_are_rate_limited_by_path_and_cause(store, monkeypatch, caplog):
    from pathlib import Path
    from types import SimpleNamespace
    import logging

    clock = [0.0]
    monkeypatch.setattr(drafts, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    s = seed(store, draft={'text': 'must not return'})
    path = drafts.draft_path(s.path)
    path.write_text('invalid')
    real_replace = Path.replace
    cause = ['quarantine denied']

    def denied_replace(target, *a, **kw):
        if target.suffix == '.draft':
            raise PermissionError(cause[0])
        return real_replace(target, *a, **kw)

    monkeypatch.setattr(Path, 'replace', denied_replace)
    caplog.set_level(logging.WARNING, logger=drafts.__name__)
    for _ in range(5):
        assert drafts.read_draft(s.path, {'text': 'obsolete'}) == {}
    warnings = [r for r in caplog.records if r.name == drafts.__name__]
    assert len(warnings) == 2  # one read failure and one quarantine failure
    assert all(r.exc_info for r in warnings)

    # A different cause or path must still surface its first warning.
    cause[0] = 'new quarantine failure'
    assert drafts.read_draft(s.path) == {}
    warnings = [r for r in caplog.records if r.name == drafts.__name__]
    assert len(warnings) == 3
    other = store / 'otherdraft.json'
    drafts.draft_path(other).write_text('invalid')
    assert drafts.read_draft(other) == {}
    warnings = [r for r in caplog.records if r.name == drafts.__name__]
    assert len(warnings) == 5

    clock[0] = 59.0
    assert drafts.read_draft(s.path) == {}
    assert len([r for r in caplog.records if r.name == drafts.__name__]) == 5
    clock[0] = 60.0
    assert drafts.read_draft(s.path) == {}
    assert len([r for r in caplog.records if r.name == drafts.__name__]) == 7

    # Suppression must not suppress recovery or resurrect the legacy fallback.
    drafts.write_draft(s.path, {'text': 'repaired'})
    assert s.composer_draft == {'text': 'repaired'}


def test_corrupt_property_read_does_not_wait_for_draft_lock(store):
    s = seed(store, draft={'text': 'obsolete'})
    path = drafts.draft_path(s.path)
    path.write_text('invalid')
    held, release = threading.Event(), threading.Event()

    def hold():
        with drafts.draft_lock(s.path):
            held.set()
            assert release.wait(5)

    writer = threading.Thread(target=hold)
    writer.start()
    assert held.wait(5)
    try:
        assert s.composer_draft == {}
        assert path.exists()  # quarantine is best effort while another writer owns it
    finally:
        release.set()
        writer.join(5)
    assert not writer.is_alive()
    assert s.composer_draft == {}
    assert path.with_suffix('.draft.corrupt').exists()


def test_get_draft_rejects_unsafe_id(store, monkeypatch):
    get_draft(monkeypatch, '../unsafe', expected_status=400)


def test_get_draft_cold_and_missing(store, monkeypatch):
    s = seed(store)
    post(monkeypatch, s.session_id, text='cold draft')
    models.SESSIONS.clear()
    assert get_draft(monkeypatch, s.session_id)['payload']['draft'] == {'text': 'cold draft'}
    get_draft(monkeypatch, 'missingdraft', expected_status=404)


def test_delete_before_inflight_draft_lock_cannot_recreate_record(store, monkeypatch):
    s = seed(store)
    post(monkeypatch, s.session_id, text='old')
    sid, path = s.session_id, s.path
    at_lock, resume = threading.Event(), threading.Event()
    real_lock = drafts.draft_lock
    captured, errors = [], []

    def gated_lock(path):
        if threading.current_thread().name == 'late-draft':
            at_lock.set()
            assert resume.wait(5)
        return real_lock(path)

    monkeypatch.setattr(drafts, 'draft_lock', gated_lock)
    monkeypatch.setattr(routes, 'read_body', lambda h: {'session_id': sid, 'text': 'late'})
    monkeypatch.setattr(routes, 'j', lambda h, payload, status=200, **kw: captured.append((status, payload)))
    monkeypatch.setattr(routes, 'bad', lambda h, msg, status=400, **kw: captured.append((status, msg)))
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda sid: {})
    monkeypatch.setattr(routes, '_worktree_retained_payload_for_session_id', lambda sid: {})
    monkeypatch.setattr(routes, '_is_messaging_session_id', lambda sid: False)
    monkeypatch.setattr(routes, '_record_webui_deleted_session_tombstone', lambda sid: None)

    def save():
        try:
            routes.handle_post(Handler(), urlparse('/api/session/draft'))
        except BaseException as exc:
            errors.append(exc)

    saver = threading.Thread(target=save, name='late-draft')
    saver.start()
    assert at_lock.wait(5)
    try:
        routes.handle_post(Handler(), urlparse('/api/session/delete'))
    finally:
        resume.set()
        saver.join(5)
    assert not saver.is_alive() and not errors
    assert captured[-1][0] == 404
    assert not path.exists() and not drafts.draft_path(path).exists()
