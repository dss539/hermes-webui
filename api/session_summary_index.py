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
_FORMAT_VERSION = 3


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
        self.signature = self.index = None
        self.generation = -1
        self.entries = {}
        self.order = []
        self.metadata = OrderedDict()
        try:
            # Only this disposable WebUI derivative is writable.
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.execute('PRAGMA synchronous=NORMAL')
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS meta (singleton INTEGER PRIMARY KEY,
                    format_version INTEGER, index_signature TEXT,
                    generation INTEGER NOT NULL, index_order TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rows (session_id TEXT PRIMARY KEY,
                    index_row_json TEXT NOT NULL);
            ''')
            if ({row[1] for row in self.conn.execute('PRAGMA table_info(meta)')} !=
                    {'singleton', 'format_version', 'index_signature', 'generation', 'index_order'} or
                    {row[1] for row in self.conn.execute('PRAGMA table_info(rows)')} !=
                    {'session_id', 'index_row_json'}):
                raise ValueError('unsupported WebUI summary schema')
            self.conn.execute('INSERT OR IGNORE INTO meta VALUES (1,?,NULL,0,\'[]\')', (_FORMAT_VERSION,))
            self.conn.commit()
            self.conn.execute('BEGIN')
            try:
                self.sync()
            finally:
                self.conn.rollback()
        except Exception:
            self.conn.close()
            raise

    def sync(self):
        """Observe generation AND row images in the caller's SQLite transaction."""
        version, saved, generation, order = self.conn.execute(
            'SELECT format_version,index_signature,generation,index_order FROM meta WHERE singleton=1').fetchone()
        if version != _FORMAT_VERSION:
            raise ValueError('unsupported WebUI summary index')
        if generation != self.generation:
            entries = {sid: (json.loads(encoded), encoded) for sid, encoded in
                       self.conn.execute('SELECT session_id,index_row_json FROM rows')}
            order = json.loads(order)
            if len(order) != len(entries) or set(order) != entries.keys():
                raise ValueError('inconsistent WebUI summary order')
            self.entries, self.order = entries, order
            self.generation = generation
            self.signature = self.index = None
            _COUNTERS['generation_reloads'] += 1
        return saved

    def publish(self, path, rows, signature, *, updates=None, order=None, previous_source=None):
        if signature is None or path_revision(path) != signature:
            return
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            saved = self.sync()
            # A targeted writer knows which canonical rows it replaced. Trust
            # that delta only if the derivative certifies the exact input file
            # the writer read; external canonical rewrites require a full walk.
            incremental = (updates is not None and order is not None and previous_source is not None
                           and saved == json.dumps(previous_source))
            if incremental:
                if len(order) != len(set(order)) or not set(order) <= self.entries.keys() | updates.keys():
                    raise ValueError('invalid compatibility index delta')
                source_rows = updates.values()
                _COUNTERS['delta_publishes'] += 1
            else:
                source_rows = rows
                order = []
                _COUNTERS['full_publishes'] += 1
            changed = []
            seen = set()
            for row in source_rows:
                _COUNTERS['index_rows_examined'] += 1
                image = _image(row)
                sid = str(image['session_id'])
                if sid in seen:
                    raise ValueError('duplicate compatibility index ids')
                seen.add(sid)
                if not incremental:
                    order.append(sid)
                old = self.entries.get(sid)
                if old is not None and _same_image(image, old[0]):
                    continue
                encoded = json.dumps(image, ensure_ascii=True)
                if len(encoded) > 65536:
                    raise ValueError('unbounded compatibility row; use legacy JSON reader')
                changed.append((sid, copy.deepcopy(image), encoded))
                _COUNTERS['index_rows_encoded'] += 1
            deleted = self.entries.keys() - set(order)
            if path_revision(path) != signature:
                return
            self.conn.executemany('DELETE FROM rows WHERE session_id=?', [(sid,) for sid in deleted])
            self.conn.executemany('''INSERT INTO rows VALUES (?,?)
                ON CONFLICT(session_id) DO UPDATE SET index_row_json=excluded.index_row_json''',
                [(sid, encoded) for sid, image, encoded in changed])
            self.conn.execute('UPDATE meta SET index_signature=?,generation=generation+1,index_order=? WHERE singleton=1',
                              (json.dumps(signature), json.dumps(order)))
            self.conn.commit()
            generation = self.generation + 1
            # If installation of the process-local delta fails after commit,
            # the next transaction must reload the committed row map.
            self.generation = -1
            self.signature = self.index = None
            for sid in deleted:
                del self.entries[sid]
            for sid, image, encoded in changed:
                self.entries[sid] = (image, encoded)
            self.order = order
            self.generation = generation
            # Materialize the complete list only when a reader needs it, not on
            # the message-save path. Order is one O(ids) image, not N SQL writes
            # whenever an updated conversation moves to the top.
            self.signature = self.index = None
            _COUNTERS['index_rows_changed'] += len(changed)
        finally:
            self.conn.rollback()


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
                raise
            # Only recognized corrupt/old disposable files, never canonical JSON.
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
            store.conn.execute('BEGIN')
            try:
                saved = store.sync()
                if signature is not None and saved == json.dumps(signature):
                    if signature == store.signature:
                        _COUNTERS['index_hits'] += 1
                    else:
                        store.index = [store.entries[sid][0] for sid in store.order]
                        store.signature = signature
                        _COUNTERS['index_disk_hits'] += 1
                    if path_revision(path) == signature:
                        return copy.deepcopy(store.index)
            finally:
                store.conn.rollback()
            rows = json.loads(path.read_bytes())
            _COUNTERS['full_index_parses'] += 1
            if not isinstance(rows, list):
                raise ValueError('index must be a list')
            store.publish(path, rows, signature)
            if path_revision(path) == signature:
                store.index = [store.entries[sid][0] for sid in store.order]
                store.signature = signature
            return [_image(row) for row in rows]
    except Exception:
        _COUNTERS['index_fallbacks'] += 1
        return json.loads(path.read_bytes())


def publish_index(path, rows, expected_source=None, *, updates=None, order=None, previous_source=None):
    """Best effort after canonical replace, under existing writer serialization.

    Bind rows to the temporary file's pre-publication identity/mtime/size. The
    optional delta also names the exact previous canonical file; never apply a
    changed-id hint across an external rewrite or a missing derivative.
    """
    try:
        with _LOCK:
            signature = path_revision(path)
            if signature is None or signature[:3] + signature[4:] != expected_source:
                return
            _store(Path(path).parent).publish(Path(path), rows, signature,
                updates=updates, order=order, previous_source=previous_source)
    except Exception:
        _COUNTERS['publish_failures'] += 1
        _LOG.debug('WebUI summary maintenance failed; JSON retained', exc_info=True)


def metadata_compact(path, index_message_count, loader):
    """Cache precisely the old metadata-only compact, not a saved full compact."""
    path = Path(path)
    signature = path_revision(path)
    if signature is None:
        return loader()
    key = (signature, index_message_count)
    try:
        with _LOCK:
            store = _store(path.parent)
            hit = store.metadata.get(path.stem)
            if hit is not None and hit[0] == key:
                store.metadata.move_to_end(path.stem)
                _COUNTERS['metadata_hits'] += 1
                return copy.deepcopy(hit[1])
        row = loader()
        _COUNTERS['metadata_loads'] += 1
        if row is None or path_revision(path) != signature:
            return row
        with _LOCK:
            store = _store(path.parent)
            # Exactly one generation per stem: replacement and eviction are O(1).
            store.metadata[path.stem] = (key, copy.deepcopy(row))
            store.metadata.move_to_end(path.stem)
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
