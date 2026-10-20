"""Owner concurrency and exclusive-publication admission ordering."""
import threading

import pytest

from tests.test_composer_draft_profile_races import env as env


def test_waiting_exclusive_publication_precedes_new_owner(env, monkeypatch):
    gate = env.m._DraftOwnerPublicationGate()
    waiting, late_waiting = threading.Event(), threading.Event()
    committed, release, late_done = threading.Event(), threading.Event(), threading.Event()
    errors, order = [], []
    real_wait = gate._condition.wait

    def wait(timeout=None):
        if threading.current_thread().name == 'exclusive':
            waiting.set()
        elif threading.current_thread().name == 'late-owner':
            late_waiting.set()
        return real_wait(timeout)

    monkeypatch.setattr(gate._condition, 'wait', wait)

    def commit():
        try:
            with gate.exclusive():
                order.append('exclusive')
                committed.set()
                assert release.wait(5)
        except BaseException as exc:
            errors.append(exc)

    def owner():
        try:
            with gate.shared():
                order.append('owner')
                late_done.set()
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(target=commit, name='exclusive')
    reader = threading.Thread(target=owner, name='late-owner')
    try:
        with gate.shared():
            writer.start()
            assert waiting.wait(5)
            reader.start()
            assert late_waiting.wait(5)
            assert not committed.is_set() and not late_done.is_set()
        assert committed.wait(5)
        assert not late_done.is_set()
    finally:
        release.set()
        writer.join(6)
        if reader.ident is not None:
            reader.join(6)
    assert not writer.is_alive() and not reader.is_alive() and not errors
    assert order == ['exclusive', 'owner']


@pytest.mark.parametrize('mode', ['shared', 'exclusive'])
def test_publication_gate_releases_after_failed_attempt(env, mode):
    gate = env.m._DraftOwnerPublicationGate()
    with pytest.raises(PermissionError):
        with getattr(gate, mode)():
            raise PermissionError('synthetic sharing violation')
    completed = threading.Event()

    def publish():
        with gate.exclusive():
            completed.set()
        with gate.shared():
            completed.set()

    thread = threading.Thread(target=publish, daemon=True)
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and completed.is_set()
