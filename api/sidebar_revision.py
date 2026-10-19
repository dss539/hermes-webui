"""Bounded, read-only SQLite commit revisions for sidebar projections."""

import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from api.agent_sessions import state_db_readonly_uri

_LOCK = threading.Lock()
_READERS = OrderedDict()
_MAX_READERS = 32
_GENERATION = 0


def path_revision(path):
    try:
        st = Path(path).stat()
        return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
    except FileNotFoundError:
        return None
    except OSError:
        return ('unavailable', time.monotonic_ns())


def sidecar_revision(session_dir):
    """Detect in-place edits as well as additions/removals, without JSON reads."""
    try:
        with os.scandir(session_dir) as entries:
            return tuple(sorted(
                (e.name, path_revision(e.path))
                for e in entries if e.name.endswith('.json')
            ))
    except FileNotFoundError:
        return ()
    except OSError:
        return ('unavailable', time.monotonic_ns())


def webui_revision(session_dir, projects_file):
    parent = Path(session_dir).parent
    return (
        sidecar_revision(session_dir), path_revision(projects_file),
        path_revision(parent / 'last_workspace.txt'),
        path_revision(parent / 'workspaces.json'),
    )


def profile_revision(home):
    home = Path(home)
    return tuple(path_revision(home / relative) for relative in (
        'config.yaml', 'cron/jobs.json',
        'webui_state/last_workspace.txt', 'webui_state/workspaces.json',
    ))


def sqlite_revision(path):
    """Observe commits on the SAME connection; never retain a read transaction.

    Fresh connections' data_version values cannot be compared. A bounded reader
    per file makes UPDATE/DELETE commits observable too, even when file stats and
    MAX(rowid) collide. File replacement gets a new monotonic generation. Failure
    returns a unique marker, so unavailable sources cannot validate old entries.
    """
    global _GENERATION
    with _LOCK:
        key = str(Path(path).absolute())
        entry = _READERS.get(key)
        try:
            stat = Path(key).stat()
            identity = (stat.st_dev, stat.st_ino)
            if entry is None or entry[0] != identity:
                if entry is not None:
                    entry[2].close()
                    _READERS.pop(key, None)
                conn = sqlite3.connect(
                    state_db_readonly_uri(Path(key)), uri=True,
                    timeout=0.01, check_same_thread=False,
                )
                _GENERATION += 1
                entry = (identity, _GENERATION, conn)
                _READERS[key] = entry
            version = entry[2].execute('PRAGMA data_version').fetchone()[0]
            _READERS.move_to_end(key)
            while len(_READERS) > _MAX_READERS:
                _READERS.popitem(last=False)[1][2].close()
            return (entry[1], version)
        except FileNotFoundError:
            old = _READERS.pop(key, None)
            if old is not None:
                old[2].close()
            return None
        except (OSError, sqlite3.Error):
            old = _READERS.pop(key, None)
            if old is not None:
                old[2].close()
            _GENERATION += 1
            return ('unavailable', _GENERATION)
