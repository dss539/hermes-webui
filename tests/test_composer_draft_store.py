"""Draft persistence must be independent of transcript writes and locks."""
import io
import json
import threading
from urllib.parse import urlparse

import pytest
import api.models as models
import api.routes as routes
from api.models import Session


class Handler:
    command = 'POST'
    headers = {}
    client_address = ('127.0.0.1', 12345)
    wfile = io.BytesIO()

    def _safe_webui_print(self, *args):
        pass


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / 'sessions'
    path.mkdir()
    monkeypatch.setattr(models, 'SESSION_DIR', path)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', path / '_index.json')
    monkeypatch.setattr(routes, 'SESSION_DIR', path)
    monkeypatch.setattr(routes, 'SESSION_INDEX_FILE', path / '_index.json')
    monkeypatch.setattr(routes, '_is_subagent_child_session_id', lambda sid: False)
    monkeypatch.setattr(routes, '_check_csrf', lambda handler: True)
    models.SESSIONS.clear()
    yield path
    models.SESSIONS.clear()


def post(monkeypatch, sid, *, expected_status=200, **fields):
    body = {'session_id': sid, **fields}
    result = {}
    monkeypatch.setattr(routes, 'read_body', lambda h: body)
    def respond(h, payload, status=200, **kwargs):
        result.update(status=status, payload=payload)
        return True
    monkeypatch.setattr(routes, 'j', respond)
    monkeypatch.setattr(routes, 'bad', lambda h, msg, status=400, **kw: respond(h, {'error': msg}, status))
    routes.handle_post(Handler(), urlparse('/api/session/draft'))
    assert result['status'] == expected_status, result
    return result['payload']


def seed(store, *, draft=None, messages=True):
    s = Session(session_id='drafttest001', title='Draft test', workspace=str(store.parent),
                messages=[{'role': 'user', 'content': 'Retain transcript', 'id': 'm1'}] if messages else [],
                composer_draft=draft or {})
    s.save()
    models.SESSIONS[s.session_id] = s
    return s


def test_draft_save_never_rewrites_transcript(store, monkeypatch):
    s = seed(store)
    before = s.path.read_bytes(), s.path.stat().st_mtime_ns, s.updated_at
    def no_save(*a, **k):
        pytest.fail('draft save called Session.save')
    monkeypatch.setattr(Session, 'save', no_save)
    result = post(monkeypatch, s.session_id, text='unsent', files=[{'name': 'a.txt'}])
    assert result['draft'] == {'text': 'unsent', 'files': [{'name': 'a.txt'}]}
    assert (s.path.read_bytes(), s.path.stat().st_mtime_ns, s.updated_at) == before


def test_draft_does_not_acquire_transcript_lock(store, monkeypatch):
    s = seed(store)
    class ForbiddenLock:
        def __enter__(self):
            pytest.fail('draft waited on transcript lock')
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(routes, '_get_session_agent_lock', lambda sid: ForbiddenLock())
    post(monkeypatch, s.session_id, text='typing during transcript save')


def test_draft_roundtrip_reload_and_independent_object(store, monkeypatch):
    s = seed(store, draft={'text': 'legacy'})
    stale = Session.load(s.session_id)
    expected = {'text': 'new draft \u2603', 'files': [{'name': 'notes.md', 'size': 123}]}
    post(monkeypatch, s.session_id, **expected)
    assert stale.composer_draft == expected
    models.SESSIONS.clear()
    loaded = Session.load(s.session_id)
    assert loaded.composer_draft == expected
    assert Session.load_metadata_only(s.session_id).compact()['composer_draft'] == expected


def test_legacy_draft_fallback_and_durable_clear(store, monkeypatch):
    old = {'text': 'legacy draft', 'files': ['file.txt']}
    s = seed(store, draft=old)
    assert Session.load(s.session_id).composer_draft == old
    post(monkeypatch, s.session_id, text='', files=[])
    # A late transcript save may contain an older snapshot; the clear wins.
    raw = json.loads(s.path.read_text())
    raw['composer_draft'] = old
    s.path.write_text(json.dumps(raw))
    models.SESSIONS.clear()
    assert Session.load(s.session_id).composer_draft == {'text': '', 'files': []}


def test_partial_updates_and_duplicate_noop(store, monkeypatch):
    s = seed(store, draft={'text': 'legacy', 'files': ['keep']})
    assert post(monkeypatch, s.session_id, text='changed')['draft'] == {'text': 'changed', 'files': ['keep']}
    paths = {p: p.stat().st_mtime_ns for p in store.iterdir()}
    assert post(monkeypatch, s.session_id, text='changed')['unchanged'] is True
    assert {p: p.stat().st_mtime_ns for p in store.iterdir()} == paths


def test_unsaved_shell_draft_survives_reload(store, monkeypatch):
    s = Session(session_id='unsaveddraft', workspace=str(store.parent))
    models.SESSIONS[s.session_id] = s
    post(monkeypatch, s.session_id, text='first input', files=[])
    models.SESSIONS.clear()
    assert Session.load(s.session_id).composer_draft == {'text': 'first input', 'files': []}


def test_unsaved_unchanged_shell_stays_unpersisted(store, monkeypatch):
    s = Session(session_id='unsaveddraft', workspace=str(store.parent))
    models.SESSIONS[s.session_id] = s
    assert post(monkeypatch, s.session_id)['unchanged']
    assert not s.path.exists()


@pytest.mark.parametrize('failure', ['replace', 'fsync'])
def test_atomic_failure_keeps_previous_draft_and_cleans_temp(store, monkeypatch, failure):
    import api.composer_drafts as drafts
    s = seed(store)
    post(monkeypatch, s.session_id, text='durable')
    def failed_write(*args):
        raise OSError('injected draft failure')
    monkeypatch.setattr(drafts.os, failure, failed_write)
    with pytest.raises(OSError, match='injected'):
        post(monkeypatch, s.session_id, text='not committed')
    assert s.composer_draft == {'text': 'durable'}
    assert not list(store.glob('*.tmp.*'))


def test_transcript_save_cannot_revert_a_draft_saved_during_serialization(store, monkeypatch):
    s = seed(store, draft={'text': 'old'})
    original_dumps = models.json.dumps
    entered = threading.Event()
    resume = threading.Event()
    def paused_dumps(value, *a, **kw):
        if isinstance(value, dict) and 'messages' in value:
            entered.set()
            assert resume.wait(5)
        return original_dumps(value, *a, **kw)
    monkeypatch.setattr(models.json, 'dumps', paused_dumps)
    errors = []
    def save():
        try:
            with routes._get_session_agent_lock(s.session_id):
                s.save()
        except BaseException as exc:
            errors.append(exc)
    writer = threading.Thread(target=save)
    writer.start()
    assert entered.wait(5)
    try:
        post(monkeypatch, s.session_id, text='saved while transcript lock held')
    finally:
        resume.set()
        writer.join(5)
    assert not writer.is_alive()
    assert not errors
    assert json.loads(s.path.read_text())['composer_draft'] == {'text': 'old'}
    assert Session.load(s.session_id).composer_draft == {'text': 'saved while transcript lock held'}


def test_cold_modern_draft_save_does_not_load_transcript(store, monkeypatch):
    import builtins
    from pathlib import Path
    s = seed(store)
    # A modern header is small even when the transcript body is large.
    s.messages[0]['content'] = 'large transcript body ' * 100_000
    s.save()
    transcript = s.path
    models.SESSIONS.clear()
    models._draft_owner_metadata.cache_clear()
    real_open, real_io_open = builtins.open, io.open
    real_read_text, real_read_bytes = Path.read_text, Path.read_bytes
    reads = []

    class BoundedTranscript:
        def __init__(self, stream):
            self.stream = stream
            self.total = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, size=-1):
            if size < 0:
                pytest.fail('draft save read full transcript directly')
            self.total += size
            if self.total > models._METADATA_PREFIX_FIRST_STAGE_BYTES:
                pytest.fail('draft save exceeded modern metadata read budget')
            reads.append(size)
            return self.stream.read(size)
        def __iter__(self):
            pytest.fail('draft save iterated full transcript')
        def readline(self, *args):
            pytest.fail('draft save scanned transcript lines')
        def readlines(self, *args):
            pytest.fail('draft save read all transcript lines')

    def guard_open(opener):
        def guarded(path, *a, **kw):
            stream = opener(path, *a, **kw)
            return BoundedTranscript(stream) if isinstance(path, (str, Path)) and Path(path) == transcript else stream
        return guarded

    def guard_full_read(reader):
        def guarded(path, *a, **kw):
            if path == transcript:
                pytest.fail('draft save read full transcript directly')
            return reader(path, *a, **kw)
        return guarded

    def no_full_load(*a, **kw):
        pytest.fail('draft save loaded transcript')
    monkeypatch.setattr(Session, 'load', no_full_load)
    monkeypatch.setattr(Path, 'read_text', guard_full_read(real_read_text))
    monkeypatch.setattr(Path, 'read_bytes', guard_full_read(real_read_bytes))
    monkeypatch.setattr(builtins, 'open', guard_open(real_open))
    monkeypatch.setattr(io, 'open', guard_open(real_io_open))
    post(monkeypatch, s.session_id, text='cold metadata only')
    assert reads, 'cold request did not exercise the instrumented metadata read'


def test_validation_and_partial_file_update(store, monkeypatch):
    s = seed(store)
    result = post(monkeypatch, s.session_id, text='x' * 60_000, files=list(range(70)))
    assert result['draft'] == {'text': 'x' * 50_000, 'files': list(range(50))}
    result = post(monkeypatch, s.session_id, files='wrong type')
    assert result['draft'] == {'text': 'x' * 50_000, 'files': []}
    result = post(monkeypatch, s.session_id, text=123)
    assert result['draft'] == {'text': '', 'files': []}


def test_legacy_layout_draft_request_never_runs_transcript_self_heal(store, monkeypatch):
    sid = 'legacydraft01'
    # Old/custom layouts may put the draft after the transcript and omit counts.
    raw = {'session_id': sid, 'title': 'Legacy', 'workspace': str(store.parent),
           'messages': [{'role': 'assistant', 'content': 'partial', 'partial': True}] * 2,
           'composer_draft': {'text': 'legacy', 'files': ['keep']}}
    path = store / f'{sid}.json'
    path.write_text(json.dumps(raw))
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(Session, 'save', lambda *a, **k: pytest.fail('legacy transcript rewritten'))
    post(monkeypatch, sid, text='new')
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert models.get_session_for_draft(sid).composer_draft == {'text': 'new', 'files': ['keep']}
    assert sid not in models.SESSIONS  # never cache an empty transcript stub


@pytest.mark.parametrize('source_signal', ['source_tag', 'raw_source', 'session_source'])
def test_sidecar_subagent_remains_view_only(store, monkeypatch, source_signal):
    s = seed(store)
    setattr(s, source_signal, 'subagent')
    s.save()
    models.SESSIONS.clear()
    result = post(monkeypatch, s.session_id, text='not allowed', expected_status=400)
    assert 'view-only' in result['error']
    assert not s.path.with_suffix('.draft').exists()


@pytest.mark.parametrize('source_signal', ['source_tag', 'raw_source', 'session_source'])
def test_legacy_provenance_after_messages_remains_view_only(store, monkeypatch, source_signal):
    sid = 'legacysource'
    raw = {'session_id': sid, 'title': 'Legacy', 'workspace': str(store.parent),
           'composer_draft': {'text': 'legacy'}, 'messages': [], source_signal: 'subagent'}
    (store / f'{sid}.json').write_text(json.dumps(raw))
    assert 'view-only' in post(monkeypatch, sid, text='no', expected_status=400)['error']


def test_draft_owner_cache_does_not_cache_messages_and_invalidates_on_replace(store, monkeypatch):
    s = seed(store, draft={'text': 'old'})
    models.SESSIONS.clear()
    owner = models.get_session_for_draft(s.session_id)
    assert owner.messages == []
    assert owner._loaded_metadata_only
    # Atomic sidecar replacement changes the signature even if size is equal.
    raw = json.loads(s.path.read_text())
    raw['composer_draft'] = {'text': 'new'}
    raw['source_tag'] = 'subagent'
    replacement = store / 'replacement.tmp'
    replacement.write_text(json.dumps(raw))
    replacement.replace(s.path)
    assert models.get_session_for_draft(s.session_id).composer_draft == {'text': 'new'}
    assert 'view-only' in post(monkeypatch, s.session_id, text='no', expected_status=400)['error']


def test_invalid_and_missing_session_do_not_create_drafts(store, monkeypatch):
    post(monkeypatch, '../unsafe', text='no', expected_status=400)
    post(monkeypatch, 'notfound001', text='no', expected_status=404)
    assert not list(store.glob('*.draft'))
