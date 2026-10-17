"""Small atomic composer records, independent of transcript persistence."""
from collections import OrderedDict
import json
import logging
import os
import threading
import time
import weakref
from pathlib import Path

_LOCKS = weakref.WeakValueDictionary()
_LOCKS_LOCK = threading.Lock()
_WARNING_TIMES = OrderedDict()
_WARNING_LOCK = threading.Lock()


def _warn_draft_error(path, message, error):
    # Keep traceback cost/noise bounded on polling reads of an unquarantinable
    # record. Store signatures, not exceptions/tracebacks, and cap the cache.
    key = (str(path), message, type(error), str(error))
    now = time.monotonic()
    with _WARNING_LOCK:
        previous = _WARNING_TIMES.get(key)
        if previous is not None and now - previous < 60:
            return
        _WARNING_TIMES[key] = now
        _WARNING_TIMES.move_to_end(key)
        if len(_WARNING_TIMES) > 256:
            _WARNING_TIMES.popitem(last=False)
    logging.getLogger(__name__).warning(message, path, exc_info=True)


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


def _read_record(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid composer draft record')
    return value


def read_draft(session_path: Path, fallback=None) -> dict:
    path = draft_path(session_path)
    corrupt = path.with_suffix('.draft.corrupt')
    try:
        return _read_record(path)
    except FileNotFoundError:
        # The quarantine is also an authoritative-empty marker. Renaming a bad
        # record must not make the next read resurrect an embedded legacy draft.
        if corrupt.exists():
            return {}
        return fallback if isinstance(fallback, dict) else {}
    except (OSError, ValueError, UnicodeError) as exc:
        _warn_draft_error(path, 'Unreadable composer draft %s; treating as empty', exc)
    # Property reads can occur while a cache/index lock is held. Never wait for
    # a draft writer (which may need that lock) just to quarantine a bad record.
    lock = draft_lock(session_path)
    if not lock.acquire(blocking=False):
        return {}
    try:
        try:
            # A concurrent atomic write may already have repaired this record.
            return _read_record(path)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError):
            try:
                path.replace(corrupt)
            except OSError as exc:
                _warn_draft_error(path, 'Failed to quarantine composer draft %s', exc)
            return {}
    finally:
        lock.release()


def remove_session_files(session_path: Path) -> None:
    """Remove an identity and its draft together; caller owns transcript lock.

    Keep draft unlink failure isolated from the remaining session cleanup.
    """
    session_path = Path(session_path)
    with draft_lock(session_path):
        session_path.unlink(missing_ok=True)
        for path in (draft_path(session_path), session_path.with_suffix('.draft.corrupt')):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning('Failed to unlink composer draft %s', path, exc_info=True)


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
