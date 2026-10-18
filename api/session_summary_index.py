"""Source-validated WebUI index cache; canonical JSON is never replaced.

Only allow-list index images are persisted. Metadata compacts are an independent
bounded memory cache: their historical counts must not inherit index counts.
"""
from collections import Counter, OrderedDict
import copy
import json
import logging
from pathlib import Path
import sqlite3
import threading

from api.sidebar_revision import path_revision

_LOCK = threading.RLock()
_STORES = OrderedDict()
_COUNTERS = Counter()
_LOG = logging.getLogger(__name__)
_FORMAT_VERSION = 2


def _image(row):
    from api.route_session_list_cache import _SIDEBAR_SESSION_RESPONSE_FIELDS
    private = {'source', 'actual_message_count', 'manual_title', 'platform', 'user_id',
               'chat_id', 'chat_type', 'thread_id', 'session_key', 'origin_chat_id',
               'origin_user_id', 'end_reason', 'ended_at'}
    image = {key: value for key, value in row.items()
             if key in _SIDEBAR_SESSION_RESPONSE_FIELDS or key in private}
    if 'pending_user_message' in row:
        image['pending_user_message'] = bool(row['pending_user_message'])
    return image


def _same_image(left, right):
    # Ordinary equality conflates JSON integers/reals/bools and signed zero.
    if type(left) is not type(right) or left != right:
        return False
    if isinstance(left, dict):
        return all(_same_image(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return all(_same_image(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and left == 0:
        return left.hex() == right.hex()
    return True


class _Store:
    def __init__(self, directory):
        self.path = directory / '_sidebar.db'
        self.conn = sqlite3.connect(self.path, timeout=.05, check_same_thread=False)
        try:
            # Only this disposable WebUI derivative is writable.
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=NORMAL')
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS meta (singleton INTEGER PRIMARY KEY,
                    format_version INTEGER, index_signature TEXT);
                CREATE TABLE IF NOT EXISTS rows (session_id TEXT PRIMARY KEY,
                    index_row_json TEXT NOT NULL, index_position INTEGER NOT NULL);
            ''')
            if ({row[1] for row in self.conn.execute('PRAGMA table_info(meta)')} !=
                    {'singleton', 'format_version', 'index_signature'} or
                    {row[1] for row in self.conn.execute('PRAGMA table_info(rows)')} !=
                    {'session_id', 'index_row_json', 'index_position'}):
                raise ValueError('unsupported WebUI summary schema')
            version = self.conn.execute('SELECT format_version FROM meta WHERE singleton=1').fetchone()
            if version is None:
                self.conn.execute('INSERT INTO meta VALUES (1,?,NULL)', (_FORMAT_VERSION,))
                self.conn.commit()
            elif version != (_FORMAT_VERSION,):
                raise ValueError('unsupported WebUI summary index')
            # Keep the actual encoded images once, not a full SELECT per save.
            self.entries = {sid: (json.loads(encoded), encoded, position)
                            for sid, encoded, position in self.conn.execute(
                                'SELECT session_id,index_row_json,index_position FROM rows')}
        except Exception:
            self.conn.close()
            raise
        self.signature = None
        self.index = None
        self.metadata = OrderedDict()

    def publish(self, path, rows, signature):
        if signature is None or path_revision(path) != signature:
            return
        entries = {}
        images = []
        changed = []
        for position, row in enumerate(rows):
            image = _image(row)
            sid = str(image['session_id'])
            if sid in entries:
                raise ValueError('duplicate compatibility index ids')
            old = self.entries.get(sid)
            if old is not None and _same_image(image, old[0]):
                image, encoded = old[:2]
            else:
                encoded = json.dumps(image, ensure_ascii=True)
                if len(encoded) > 65536:
                    raise ValueError('unbounded compatibility row; use legacy JSON reader')
                image = copy.deepcopy(image)
                _COUNTERS['index_rows_encoded'] += 1
            entry = (image, encoded, position)
            entries[sid] = entry
            images.append(image)
            if old is None or old[1:] != entry[1:]:
                changed.append((sid, encoded, position))
        with self.conn:
            self.conn.executemany('DELETE FROM rows WHERE session_id=?',
                                  [(sid,) for sid in self.entries.keys() - entries.keys()])
            self.conn.executemany('''INSERT INTO rows VALUES (?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET index_row_json=excluded.index_row_json,
                    index_position=excluded.index_position''', changed)
            self.conn.execute('UPDATE meta SET index_signature=? WHERE singleton=1', (json.dumps(signature),))
        _COUNTERS['index_rows_changed'] += len(changed)
        self.entries = entries
        if path_revision(path) == signature:
            self.signature = signature
            self.index = images


def _store(directory):
    key = str(Path(directory).resolve())
    store = _STORES.get(key)
    if store is None or not store.path.exists():
        if store is not None:
            store.conn.close()
            _STORES.pop(key, None)
        try:
            store = _Store(Path(key))
        except (sqlite3.DatabaseError, ValueError) as exc:
            if isinstance(exc, sqlite3.DatabaseError) and getattr(exc, 'sqlite_errorcode', None) not in (
                    sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                # Busy/permission/I/O failures are not corrupt-cache evidence.
                raise
            # Rebuild a corrupt/old disposable file ONCE. Permission/open/source
            # failures still take the canonical fallback, never touch JSON.
            for suffix in ('', '-wal', '-shm'):
                Path(key, '_sidebar.db' + suffix).unlink(missing_ok=True)
            store = _Store(Path(key))
            _COUNTERS['derivative_rebuilds'] += 1
        _STORES[key] = store
    _STORES.move_to_end(key)
    while len(_STORES) > 32:
        _STORES.popitem(last=False)[1].conn.close()
    return store


def read_index(path):
    """Return independent allow-list images, validating source on every read.

    This is the sidebar-metadata-only API, not an arbitrary canonical index
    reader: exclude transcripts/drafts and retain only a pending-text boolean.
    """
    path = Path(path)
    try:
        with _LOCK:
            store = _store(path.parent)
            signature = path_revision(path)
            if signature is not None and signature == store.signature:
                _COUNTERS['index_hits'] += 1
                return copy.deepcopy(store.index)
            saved = store.conn.execute('SELECT index_signature FROM meta WHERE singleton=1').fetchone()[0]
            if signature is not None and saved == json.dumps(signature):
                rows = [entry[0] for entry in sorted(store.entries.values(), key=lambda entry: entry[2])]
                if path_revision(path) == signature:
                    store.index = rows
                    store.signature = signature
                    _COUNTERS['index_disk_hits'] += 1
                    return copy.deepcopy(rows)
            rows = json.loads(path.read_bytes())
            _COUNTERS['full_index_parses'] += 1
            if not isinstance(rows, list):
                raise ValueError('index must be a list')
            store.publish(path, rows, signature)
            return [_image(row) for row in rows]
    except Exception:
        _COUNTERS['index_fallbacks'] += 1
        # Preserve missing/corrupt-index recovery and malformed-row behavior.
        return json.loads(path.read_bytes())


def publish_index(path, rows, expected_source=None):
    """Best effort after canonical replace, under existing writer serialization.

    Bind input rows to the temporary file's pre-publication identity/mtime/size.
    A later external writer must not bless our older rows under its new stamp.
    """
    try:
        with _LOCK:
            signature = path_revision(path)
            if signature is None or signature[:3] + signature[4:] != expected_source:
                return
            _store(Path(path).parent).publish(Path(path), rows, signature)
    except Exception:
        _COUNTERS['publish_failures'] += 1
        _LOG.debug('WebUI summary maintenance failed; JSON retained', exc_info=True)


def metadata_compact(path, index_message_count, loader):
    """Cache precisely the old metadata-only compact, not a saved full compact."""
    path = Path(path)
    signature = path_revision(path)
    if signature is None:
        return loader()
    key = (path.stem, signature, index_message_count)
    try:
        with _LOCK:
            store = _store(path.parent)
            hit = store.metadata.get(key)
            if hit is not None:
                store.metadata.move_to_end(key)
                _COUNTERS['metadata_hits'] += 1
                return copy.deepcopy(hit)
        row = loader()
        _COUNTERS['metadata_loads'] += 1
        if row is None or path_revision(path) != signature:
            return row
        with _LOCK:
            store = _store(path.parent)
            # Only the current canonical generation is useful for this id.
            for old in [old for old in store.metadata if old[0] == path.stem]:
                store.metadata.pop(old)
            store.metadata[key] = copy.deepcopy(row)
            while len(store.metadata) > 2000:
                store.metadata.popitem(last=False)
        return row
    except Exception:
        _COUNTERS['metadata_fallbacks'] += 1
        return loader()


def counters():
    with _LOCK:
        return dict(_COUNTERS)


def clear():
    with _LOCK:
        for store in _STORES.values():
            store.conn.close()
        _STORES.clear()
        _COUNTERS.clear()
