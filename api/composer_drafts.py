"""Small atomic composer records, independent of transcript persistence."""
import json
import os
import threading
import weakref
from pathlib import Path

_LOCKS = weakref.WeakValueDictionary()
_LOCKS_LOCK = threading.Lock()


def draft_lock(session_path: Path):
    # Weak ownership keeps locks alive for holders/waiters, not every past chat.
    key = str(session_path)
    with _LOCKS_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def draft_path(session_path: Path) -> Path:
    # Not *.json: session discovery must never mistake a draft for a session.
    return session_path.with_suffix('.draft')


def read_draft(session_path: Path, fallback=None) -> dict:
    try:
        value = json.loads(draft_path(session_path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return fallback if isinstance(fallback, dict) else {}
    if not isinstance(value, dict):
        raise ValueError('Invalid composer draft record')
    return value


def write_draft(session_path: Path, draft: dict) -> None:
    path = draft_path(session_path)
    tmp = path.with_suffix(f'.draft.tmp.{os.getpid()}.{threading.get_ident()}')
    # ASCII escapes preserve ordinary Unicode and lone provider surrogates.
    payload = json.dumps(draft, ensure_ascii=True)
    try:
        with open(tmp, 'w', encoding='utf-8') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
