"""Draft identity, legacy freshness and session removal regressions."""
from contextlib import contextmanager
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

import pytest
import api.composer_drafts as drafts
import api.models as models
import api.routes as routes
from tests.test_composer_draft_store import Handler, post, seed, store


def cleanup(monkeypatch):
    result = {}
    monkeypatch.setattr(routes, 'j', lambda h, payload, **kw: result.update(payload))
    routes._handle_sessions_cleanup(Handler(), {}, zero_only=True)
    return result


@pytest.mark.parametrize('temporary', [True, False], ids=['snapshot-rebind', 'compression-rotation'])
def test_inflight_draft_stays_with_requested_identity(store, monkeypatch, temporary):
    s = seed(store, draft={'text': 'original', 'files': ['original-file']})
    sid = s.session_id
    path = store / f'{sid}.json'
    other = models.Session(session_id='rotationtarget', workspace=str(store.parent))
    other.save()
    drafts.write_draft(other.path, {'text': 'other draft', 'files': ['other-file']})
    before = drafts.draft_path(other.path).read_bytes()
    ready, rotated, finish = threading.Event(), threading.Event(), threading.Event()
    real_lock = drafts.draft_lock
    errors = []

    def rotate():
        try:
            assert ready.wait(5)
            with routes._get_session_agent_lock(sid):
                s.session_id = other.session_id
                rotated.set()
                assert finish.wait(5)
                if temporary:
                    s.session_id = sid
        except BaseException as exc:
            errors.append(exc)

    @contextmanager
    def paused_lock(path):
        with real_lock(path):
            ready.set()
            assert rotated.wait(5)
            yield

    monkeypatch.setattr(drafts, 'draft_lock', paused_lock)
    writer = threading.Thread(target=rotate)
    writer.start()
    try:
        result = post(monkeypatch, sid, text='for original')
    finally:
        finish.set()
        writer.join(5)
    assert not writer.is_alive()
    assert not errors
    assert result['draft'] == {'text': 'for original', 'files': ['original-file']}
    assert json.loads(drafts.draft_path(path).read_text()) == result['draft']
    assert drafts.draft_path(other.path).read_bytes() == before


def test_property_ignores_temporary_snapshot_identity(store):
    s = seed(store)
    drafts.write_draft(s.path, {'text': 'own draft'})
    other = store / 'archived.json'
    drafts.write_draft(other, {'text': 'archive draft'})
    sid = s.session_id
    try:
        s.session_id = other.stem
        assert s.composer_draft == {'text': 'own draft'}
        assert s.compact()['composer_draft'] == {'text': 'own draft'}
    finally:
        s.session_id = sid


@pytest.mark.parametrize('source_signal', [None, 'source_tag', 'raw_source', 'session_source'])
def test_cached_owner_reads_replaced_legacy_metadata(store, monkeypatch, source_signal):
    s = seed(store, draft={'text': 'old', 'files': ['old-file']})
    raw = json.loads(s.path.read_text())
    raw['composer_draft'] = {'text': 'new', 'files': ['new-file']}
    raw['messages'].append({'role': 'assistant', 'content': 'external update', 'id': 'm2'})
    raw['message_count'] = len(raw['messages'])
    if source_signal:
        raw[source_signal] = 'subagent'
    replacement = store / 'replacement.tmp'
    replacement.write_text(json.dumps(raw))
    replacement.replace(s.path)
    before = s.path.read_bytes(), s.path.stat().st_mtime_ns
    if source_signal:
        assert 'view-only' in post(monkeypatch, s.session_id, text='edit', expected_status=400)['error']
        assert not drafts.draft_path(s.path).exists()
    else:
        owner = models.get_session_for_draft(s.session_id)
        assert owner.composer_draft['files'] == ['new-file']
        assert post(monkeypatch, s.session_id, text='edit')['draft'] == {'text': 'edit', 'files': ['new-file']}
    assert (s.path.read_bytes(), s.path.stat().st_mtime_ns) == before


def test_cleanup_removes_draft_with_empty_session(store, monkeypatch):
    s = seed(store, messages=False)
    post(monkeypatch, s.session_id, text='private unsent text')
    quarantine = s.path.with_suffix('.draft.corrupt')
    quarantine.write_text('previous corrupt bytes')
    assert cleanup(monkeypatch)['cleaned'] == 1
    assert not s.path.exists()
    assert not drafts.draft_path(s.path).exists()
    assert not quarantine.exists()


def test_cleanup_waits_for_draft_publication(store, monkeypatch):
    s = seed(store, messages=False)
    entered, resume, attempted = threading.Event(), threading.Event(), threading.Event()
    real_write = drafts.write_draft
    real_load = models.Session.load
    errors = []

    def paused_write(path, value):
        entered.set()
        assert resume.wait(5)
        real_write(path, value)

    def observed_load(*a, **kw):
        attempted.set()
        return real_load(*a, **kw)

    monkeypatch.setattr(drafts, 'write_draft', paused_write)
    monkeypatch.setattr(models.Session, 'load', observed_load)
    # Configure response hooks once, before both threads; do not race monkeypatch.
    body = {'session_id': s.session_id, 'text': 'last write'}
    monkeypatch.setattr(routes, 'read_body', lambda h: body)
    monkeypatch.setattr(routes, 'j', lambda *a, **kw: True)

    def run(fn):
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    saver = threading.Thread(target=lambda: run(lambda: routes.handle_post(Handler(), urlparse('/api/session/draft'))))
    remover = threading.Thread(target=lambda: run(lambda: routes._handle_sessions_cleanup(Handler(), {}, zero_only=True)))
    saver.start()
    assert entered.wait(5)
    remover.start()
    try:
        # A cleanup implementation may load only after taking the draft lock.
        attempted.wait(.1)
        still_exists = s.path.exists()
    finally:
        resume.set()
        saver.join(5)
        remover.join(5)
    assert not saver.is_alive() and not remover.is_alive()
    assert not errors
    assert still_exists, 'cleanup deleted transcript while a draft publication held the draft lock'
    assert not s.path.exists()
    assert not drafts.draft_path(s.path).exists()


def test_cleanup_before_resolved_draft_save_leaves_no_orphan(store, monkeypatch):
    s = seed(store, messages=False)
    post(monkeypatch, s.session_id, text='old draft')
    real_resolve = routes.get_session_for_draft
    resolved, resume = threading.Event(), threading.Event()
    errors = []
    result = {}

    def paused_resolve(sid):
        owner = real_resolve(sid)
        if threading.current_thread().name == 'draft-saver':
            resolved.set()
            assert resume.wait(5)
        return owner

    monkeypatch.setattr(routes, 'get_session_for_draft', paused_resolve)
    monkeypatch.setattr(routes, 'read_body', lambda h: {'session_id': s.session_id, 'text': 'late draft'})
    monkeypatch.setattr(routes, 'j', lambda *a, **kw: True)
    monkeypatch.setattr(routes, 'bad', lambda h, msg, status=400, **kw: result.update(status=status))

    def save():
        try:
            routes.handle_post(Handler(), urlparse('/api/session/draft'))
        except BaseException as exc:
            errors.append(exc)

    saver = threading.Thread(target=save, name='draft-saver')
    saver.start()
    assert resolved.wait(5)
    try:
        routes._handle_sessions_cleanup(Handler(), {}, zero_only=True)
    finally:
        resume.set()
        saver.join(5)
    assert not saver.is_alive()
    assert not errors
    assert result['status'] == 404
    assert not s.path.exists()
    assert not drafts.draft_path(s.path).exists()


def test_delete_draft_unlink_failure_does_not_skip_other_cleanup(store, monkeypatch):
    s = seed(store)
    post(monkeypatch, s.session_id, text='draft')
    called = []
    real_unlink = Path.unlink

    def failing_unlink(path, *a, **kw):
        if path == drafts.draft_path(s.path):
            raise PermissionError('draft unlink denied')
        return real_unlink(path, *a, **kw)

    monkeypatch.setattr(Path, 'unlink', failing_unlink)
    monkeypatch.setattr(routes, '_lookup_cli_session_metadata', lambda sid: {})
    monkeypatch.setattr(routes, '_worktree_retained_payload_for_session_id', lambda sid: {})
    monkeypatch.setattr(routes, '_is_messaging_session_id', lambda sid: False)
    monkeypatch.setattr(routes, '_record_webui_deleted_session_tombstone', lambda sid: called.append('tombstone'))
    monkeypatch.setattr(routes, 'prune_session_from_index', lambda sid: called.append('index'))
    monkeypatch.setattr(routes, 'read_body', lambda h: {'session_id': s.session_id})
    monkeypatch.setattr(routes, 'j', lambda *a, **kw: True)
    routes.handle_post(Handler(), urlparse('/api/session/delete'))
    assert 'tombstone' in called and 'index' in called
    assert not s.path.exists()


@pytest.mark.parametrize('reload', [False, True])
def test_missing_persisted_identity_is_not_an_unsaved_shell(store, monkeypatch, reload):
    s = seed(store)
    if reload:
        s = models.Session.load(s.session_id)
        models.SESSIONS[s.session_id] = s
    sid, path = s.session_id, s.path
    path.unlink()
    post(monkeypatch, sid, text='do not recreate', expected_status=404)
    assert not path.exists() and not drafts.draft_path(path).exists()


@pytest.mark.parametrize('removal', ['ephemeral-cancel', 'admission-rollback'])
def test_secondary_session_removal_cleans_draft(store, monkeypatch, removal):
    import api.streaming as streaming
    s = seed(store)
    post(monkeypatch, s.session_id, text='private draft')
    with routes._get_session_agent_lock(s.session_id):
        if removal == 'ephemeral-cancel':
            streaming._cleanup_ephemeral_cancelled_turn(s)
        else:
            routes._restore_chat_start_entry_sidecar((s.path, None, s.path.with_suffix('.json.bak'), False, None, False))
    assert not s.path.exists() and not drafts.draft_path(s.path).exists()
