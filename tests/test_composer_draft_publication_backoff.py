"""Exercise the real Windows replace retry branch without a Windows host."""
from tests.test_composer_draft_profile_races import env as env
import json
import threading


def test_windows_draft_retry_does_not_block_unrelated_owner_save(env, monkeypatch):
    e = env
    original = e.owner()
    unrelated = e.m.Session(session_id='unrelated-owner', profile='default')
    ready, attempted, done = threading.Event(), threading.Event(), threading.Event()
    errors, delays, blocked = [], [], []
    real_os, real_time = e.m.os, e.m.time
    real_publish = e.m._replace_session_sidecar
    draft_path = e.m._session_draft_path(original.session_id)
    attempts = []

    class OSProxy:
        name = 'nt'
        def __getattr__(self, name):
            return getattr(real_os, name)
        def replace(self, src, dst):
            if dst == draft_path:
                attempts.append(True)
                if len(attempts) < 5:
                    raise PermissionError('synthetic Windows sharing violation')
            return real_os.replace(src, dst)

    class TimeProxy:
        def __getattr__(self, name):
            return getattr(real_time, name)
        def sleep(self, delay):
            delays.append(delay)
            ready.set()
            assert attempted.wait(5)
            if len(delays) == 1:
                blocked.append(not done.wait(.1))

    def publish(tmp, path, sid):
        if sid == unrelated.session_id:
            attempted.set()
        return real_publish(tmp, path, sid)

    def writer():
        try:
            assert ready.wait(5)
            unrelated.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(e.m, 'os', OSProxy())
    monkeypatch.setattr(e.m, 'time', TimeProxy())
    monkeypatch.setattr(e.m, '_replace_session_sidecar', publish)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        status, payload = e.call(text='ordinary draft')
    finally:
        ready.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    actual = dict(status=status, delays=delays, total_requested_backoff=sum(delays),
                  unrelated_owner_blocked=blocked, eventually_saved=unrelated.path.exists())
    print('WINDOWS_BACKOFF_RESULT', json.dumps(actual))
    assert status == 200
    assert blocked == [False], actual
