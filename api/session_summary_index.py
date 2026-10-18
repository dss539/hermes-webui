"""Source-validated WebUI JSON summary index. JSON remains canonical.

The compatibility index, metadata-only compact and guarded save-time compact
are separate images. Never substitute a full-save count for a metadata count.
Only bounded compact/policy fields are retained, never transcripts or drafts.
"""
from collections import Counter, OrderedDict
import copy
import json
import logging
from pathlib import Path
import sqlite3
import threading

from api.sidebar_revision import path_revision, sidecar_revision

_LOCK = threading.RLock()
_STORES = OrderedDict()
_COUNTERS = Counter()
_LOG = logging.getLogger(__name__)


def _image(row):
    from api.route_session_list_cache import _SIDEBAR_SESSION_RESPONSE_FIELDS
    private = {'source', 'actual_message_count', 'manual_title', 'platform', 'user_id',
               'chat_id', 'chat_type', 'thread_id', 'session_key', 'origin_chat_id',
               'origin_user_id', 'end_reason', 'ended_at'}
    image = {key: value for key, value in row.items()
             if key in _SIDEBAR_SESSION_RESPONSE_FIELDS or key in private}
    if 'pending_user_message' in row:
        image['pending_user_message'] = bool(row['pending_user_message'])
    if len(json.dumps(image, ensure_ascii=False)) > 65536:
        raise ValueError('unbounded compatibility row; use legacy JSON reader')
    return image


class _Store:
    def __init__(self, directory):
        self.path = directory / '_sidebar.db'
        self.conn = sqlite3.connect(self.path, timeout=.05, check_same_thread=False)
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS meta (singleton INTEGER PRIMARY KEY, format_version INTEGER, revision INTEGER, index_signature TEXT,
                epoch TEXT, floor_revision INTEGER NOT NULL DEFAULT 0, ready INTEGER NOT NULL DEFAULT 0, namespace_signature TEXT);
            INSERT OR IGNORE INTO meta(singleton,format_version,revision,epoch) VALUES (1,1,0,hex(randomblob(16)));
            CREATE TABLE IF NOT EXISTS rows (session_id TEXT PRIMARY KEY, updated_seq INTEGER, deleted INTEGER DEFAULT 0,
                file_signature TEXT, index_row_json TEXT, index_position INTEGER,
                sidecar_row_json TEXT, saved_compact_json TEXT, policy_inputs_json TEXT);
        ''')
        if self.conn.execute('SELECT format_version FROM meta WHERE singleton=1').fetchone() != (1,):
            self.conn.close()
            raise ValueError('unsupported WebUI summary index')
        self.signature = None
        self.index = None
        self.metadata = OrderedDict()
        self.namespace = self.conn.execute('SELECT namespace_signature FROM meta WHERE singleton=1').fetchone()[0]

    def reconcile_namespace(self, directory):
        namespace = sidecar_revision(directory)
        encoded = json.dumps(namespace)
        if encoded == self.namespace:
            return
        old = dict(json.loads(self.namespace)) if self.namespace else {}
        current = dict(json.loads(encoded))
        with self.conn:
            self.conn.execute('UPDATE meta SET revision=revision+1,namespace_signature=? WHERE singleton=1', (encoded,))
            revision = self.conn.execute('SELECT revision FROM meta WHERE singleton=1').fetchone()[0]
            for name in old.keys() | current.keys():
                if name.startswith('_') or old.get(name) == current.get(name):
                    continue
                sid = name[:-5]
                signature = json.dumps(current.get(name)) if name in current else None
                # Keep only images whose recorded bytes are still canonical.
                self.conn.execute('''UPDATE rows SET updated_seq=?,deleted=?,
                    sidecar_row_json=CASE WHEN file_signature=? THEN sidecar_row_json ELSE NULL END,
                    saved_compact_json=CASE WHEN file_signature=? THEN saved_compact_json ELSE NULL END,
                    file_signature=CASE WHEN file_signature=? THEN file_signature ELSE NULL END WHERE session_id=?''',
                    (revision, int(name not in current), signature, signature, signature, sid))
        self.namespace = encoded
        _COUNTERS['namespace_reconciliations'] += 1

    def publish(self, path, rows, signature):
        if signature is None or path_revision(path) != signature:
            return
        # JSON encoding is only paid on a real index edit, never each refresh.
        rows = [_image(row) for row in rows]
        images = [(str(row['session_id']), json.dumps(row, ensure_ascii=False), position)
                  for position, row in enumerate(rows)]
        if len({row[0] for row in images}) != len(images):
            raise ValueError('duplicate compatibility index ids')
        with self.conn:
            revision = self.conn.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1 RETURNING revision').fetchone()[0]
            old = {row[0]: row[1:] for row in self.conn.execute('SELECT session_id,index_row_json,index_position FROM rows WHERE index_row_json IS NOT NULL')}
            ids = {row[0] for row in images}
            for sid in old.keys() - ids:
                self.conn.execute('UPDATE rows SET index_row_json=NULL,index_position=NULL,updated_seq=?,deleted=1 WHERE session_id=?', (revision, sid))
            for sid, image, position in images:
                if old.get(sid) != (image, position):
                    self.conn.execute('''INSERT INTO rows(session_id,updated_seq,index_row_json,index_position,deleted) VALUES(?,?,?,?,0)
                        ON CONFLICT(session_id) DO UPDATE SET updated_seq=excluded.updated_seq,index_row_json=excluded.index_row_json,index_position=excluded.index_position,deleted=0''', (sid, revision, image, position))
                    _COUNTERS['index_rows_changed'] += 1
            self.conn.execute('UPDATE meta SET index_signature=?,ready=1 WHERE singleton=1', (json.dumps(signature),))
        if path_revision(path) == signature:
            self.signature = signature
            self.index = copy.deepcopy(rows)


def _store(directory):
    key = str(Path(directory).resolve())
    store = _STORES.get(key)
    if store is None or not store.path.exists():
        if store is not None:
            store.conn.close()
        store = _Store(Path(key))
        _STORES[key] = store
    _STORES.move_to_end(key)
    while len(_STORES) > 32:
        _STORES.popitem(last=False)[1].conn.close()
    return store


def read_index(path):
    """Return independent index rows, validating source bytes on every read."""
    path = Path(path)
    try:
        with _LOCK:
            store = _store(path.parent)
            store.reconcile_namespace(path.parent)
            signature = path_revision(path)
            if signature is not None and signature == store.signature:
                _COUNTERS['index_hits'] += 1
                return copy.deepcopy(store.index)
            saved = store.conn.execute('SELECT index_signature FROM meta WHERE singleton=1').fetchone()[0]
            if signature is not None and saved == json.dumps(signature):
                rows = [json.loads(row[0]) for row in store.conn.execute('SELECT index_row_json FROM rows WHERE index_row_json IS NOT NULL ORDER BY index_position')]
                if path_revision(path) == signature:
                    store.index = rows
                    store.signature = signature
                    _COUNTERS['index_disk_hits'] += 1
                    return copy.deepcopy(rows)
            rows = json.loads(path.read_bytes())
            _COUNTERS['full_index_parses'] += 1
            if not isinstance(rows, list):
                raise ValueError('index must be a list')
            rows = [_image(row) for row in rows]
            store.publish(path, rows, signature)
            return rows
    except Exception:
        _COUNTERS['index_fallbacks'] += 1
        # Preserve missing/corrupt-index recovery and malformed-row behavior.
        return json.loads(path.read_bytes())


def publish_index(path, rows):
    """Best effort after canonical replace, under existing writer serialization."""
    try:
        with _LOCK:
            _store(Path(path).parent).publish(Path(path), rows, path_revision(path))
    except Exception:
        _COUNTERS['publish_failures'] += 1
        _LOG.debug('WebUI summary maintenance failed; JSON retained', exc_info=True)


def publish_saved(path, compact, expected_identity):
    """Keep guarded save facts separate; never replace compatibility metadata.

    The temporary file's inode was captured before publication. If another
    same-session save won the replacement race, do not bless this older image.
    Failure is derivative-only and cannot turn a successful save into a failure.
    """
    try:
        compact = _image(compact)
        with _LOCK:
            signature = path_revision(path)
            if signature is None or signature[:2] != expected_identity:
                return
            store = _store(Path(path).parent)
            with store.conn:
                store.conn.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
                revision = store.conn.execute('SELECT revision FROM meta WHERE singleton=1').fetchone()[0]
                store.conn.execute('''INSERT INTO rows(session_id,updated_seq,file_signature,saved_compact_json) VALUES(?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET updated_seq=excluded.updated_seq,sidecar_row_json=CASE WHEN file_signature=excluded.file_signature THEN sidecar_row_json ELSE NULL END,file_signature=excluded.file_signature,saved_compact_json=excluded.saved_compact_json''',
                    (Path(path).stem, revision, json.dumps(signature), json.dumps(compact, ensure_ascii=False)))
    except Exception:
        _COUNTERS['publish_failures'] += 1
        _LOG.debug('Saved summary maintenance failed; canonical JSON retained', exc_info=True)


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
            with store.conn:
                store.conn.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
                revision = store.conn.execute('SELECT revision FROM meta WHERE singleton=1').fetchone()[0]
                store.conn.execute('''INSERT INTO rows(session_id,updated_seq,file_signature,sidecar_row_json) VALUES(?,?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET updated_seq=excluded.updated_seq,saved_compact_json=CASE WHEN file_signature=excluded.file_signature THEN saved_compact_json ELSE NULL END,file_signature=excluded.file_signature,sidecar_row_json=excluded.sidecar_row_json''',
                    (path.stem, revision, json.dumps(signature), json.dumps(row, ensure_ascii=False)))
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
