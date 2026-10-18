"""WebUI-owned raw sidebar facts; Agent databases are strictly read-only.

There is no exact row revision in old Agent schemas. A changed commit therefore
requires a complete relevant-fact reconciliation, NOT a guessed max-id delta.
Only changed derivative rows are replaced. Unchanged commits do no fact reads.
All SQL policy/candidate selection stays in the existing readers.
"""
from collections import Counter, OrderedDict
from contextlib import closing, contextmanager
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
import time

from api.agent_sessions import state_db_readonly_uri

_LOG = logging.getLogger(__name__)
_LOCK = threading.RLock()
_READERS = OrderedDict()
_MAX_READERS = 32
_SERIAL = 0
_COUNTERS = Counter()
_COLUMNS = (
    'id', 'source', 'title', 'model', 'message_count', 'started_at',
    'ended_at', 'end_reason', 'parent_session_id', 'model_config',
    'session_source', 'user_id', 'chat_id', 'chat_type', 'thread_id',
    'session_key', 'origin_chat_id', 'origin_user_id', 'platform',
    'project_id', 'archived',
)


class UnsupportedSummary(Exception):
    """Use the legacy reader rather than approximate an unknown SQL schema."""


class UnsupportedOrder(UnsupportedSummary):
    """Keep raw facts, but preserve plan-dependent tie order on the source."""


class _SummaryConnection(sqlite3.Connection):
    sidebar_summary = True
    sidebar_source: sqlite3.Connection
    sidebar_reader: '_Reader'


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _fact_key(row):
    if row is None:
        return None
    # Python equates 1 and 1.0 (and +0.0/-0.0), while the wire contract does not.
    # Keep values and types in separate tuples: map(type, ...) runs in C and
    # avoids one Python allocation per column on every component-cache lookup.
    return (tuple(row), tuple(map(type, row)),
            tuple(value.hex() for value in row if isinstance(value, float)))


def _facts_equal(left, right):
    # Native tuple comparison rejects changed values without allocating typed
    # copies. Equal numeric values still need their storage type and zero sign.
    if left != right:
        return False
    if left is None:
        return True
    return all(type(a) is type(b) and
               (type(a) is not float or a != 0 or
                math.copysign(1, a) == math.copysign(1, b))
               for a, b in zip(left, right))


def counters():
    with _LOCK:
        return dict(_COUNTERS)


def clear():
    """Release bounded readers, including source handles; also useful in tests."""
    with _LOCK:
        for reader in _READERS.values():
            reader.close()
        _READERS.clear()
        _COUNTERS.clear()


class _Reader:
    def __init__(self, path, identity):
        global _SERIAL
        self.path = path
        self.identity = identity
        self.source = sqlite3.connect(
            state_db_readonly_uri(path), uri=True, timeout=.05,
            check_same_thread=False,
        )
        self.summary = None
        self.version = None
        self.schema = None
        self.rows = {}
        self.stats = {}
        self.components = OrderedDict()
        _SERIAL += 1
        self.serial = _SERIAL
        self.revision = 0
        self.watcher_revision = 0

    def close(self):
        self.source.close()
        if self.summary is not None:
            self.summary.close()

    def _initialize(self):
        source = self.source
        shape = list(source.execute('PRAGMA table_info(sessions)'))
        names = {row[1] for row in shape}
        required = {'id', 'source', 'title', 'model', 'message_count', 'started_at'}
        ddl = source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()
        if (not required <= names or not ddl or
                any(word in ddl[0].upper() for word in ('COLLATE', 'WITHOUT ROWID', 'GENERATED')) or
                len([row for row in shape if row[5]]) != 1 or
                not any(row[1] == 'id' and row[2].upper() == 'TEXT' and row[5] == 1 for row in shape)):
            raise UnsupportedSummary('nonstandard sessions schema')
        # Implicit constraints are not replayed by the disposable table builder.
        # Validate the actual key, not the sqlite_autoindex name: DESC/collation
        # are absent from table_info. Unknown UNIQUE/PK indexes use legacy SQL.
        implicit = [(name, origin) for _, name, _, origin, _ in source.execute('PRAGMA index_list(sessions)')
                    if origin != 'c']
        if len(implicit) != 1 or implicit[0][1] != 'pk':
            raise UnsupportedSummary('unsupported implicit sessions index')
        key = [row for row in source.execute('PRAGMA index_xinfo(' + _quote(implicit[0][0]) + ')') if row[5]]
        if len(key) != 1 or key[0][2:6] != ('id', 0, 'BINARY', 1):
            raise UnsupportedSummary('unsupported sessions primary key')
        self.columns = [name for name in _COLUMNS if name in names]
        self.message_shape = list(source.execute('PRAGMA table_info(messages)'))
        self.message_columns = {row[1] for row in self.message_shape}
        message_ddl = source.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
        if self.message_shape and (
                not message_ddl or any(word in message_ddl[0].upper() for word in ('COLLATE', 'WITHOUT ROWID', 'GENERATED')) or
                not {'session_id', 'id', 'role', 'timestamp'} <= self.message_columns or
                not any(row[1] == 'session_id' and row[2].upper() == 'TEXT' for row in self.message_shape) or
                not any(row[1] == 'timestamp' and row[2].upper() == 'REAL' for row in self.message_shape) or
                len([row for row in self.message_shape if row[5]]) != 1 or
                any(row[3] != 'c' for row in source.execute('PRAGMA index_list(messages)')) or
                not any(row[1] == 'id' and row[2].upper() == 'INTEGER' and row[5] == 1 for row in self.message_shape)):
            # COUNT(id) and COUNT(*) coincide only for the canonical rowid alias.
            # REAL affinity also prevents equal 1/1.0 MAX ties returning different
            # storage classes in scalar-indexed versus grouped consumer queries.
            raise UnsupportedSummary('legacy messages schema')
        # Finish source reads outside the disposable-build exception boundary.
        indexes = []
        for name, sql in source.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='sessions' AND sql IS NOT NULL"):
            index_columns = [row[2] for row in source.execute('PRAGMA index_info(' + _quote(name) + ')')]
            if index_columns and all(col in self.columns for col in index_columns):
                indexes.append(sql)
        messages_index_present = self.message_shape and any(
            row[1] == 'idx_messages_session' for row in source.execute('PRAGMA index_list(messages)'))
        local = None
        try:
            local = sqlite3.connect(':memory:', factory=_SummaryConnection, check_same_thread=False)
            definitions = []
            for row in shape:
                if row[1] in self.columns:
                    # No new affinity conversion: values already have the source
                    # column's affinity. Preserve id lookup and source rowid order.
                    definitions.append(_quote(row[1]) + ' ' + row[2] + (' PRIMARY KEY' if row[1] == 'id' else ''))
            local.execute('CREATE TABLE sessions (' + ','.join(definitions) + ')')
            # index_info omits predicate dependencies; all local DDL failures
            # below are unsupported, never evidence of unreadable Agent data.
            for sql in indexes:
                local.execute(sql)
            local.execute('CREATE TABLE _sidebar_message_stats (session_id TEXT PRIMARY KEY, actual_message_count, actual_user_message_count, last_activity)')
            if self.message_shape:
                # Shape only, never fabricated message rows. Deny reads below so
                # a missed integration cannot silently consume an empty history.
                local.execute('CREATE TABLE messages (' + ','.join(_quote(row[1]) for row in self.message_shape) + ')')
                if messages_index_present:
                    local.execute('CREATE INDEX idx_messages_session ON messages(session_id,timestamp)')
            local.set_authorizer(lambda action, table, column, db, trigger:
                                 sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'messages'
                                 else sqlite3.SQLITE_OK)
            local.sidebar_source = source
            local.sidebar_reader = self
        except Exception as exc:
            if local is not None:
                local.close()
            raise UnsupportedSummary('disposable summary construction failed') from exc
        if self.summary is not None:
            self.summary.close()
        self.summary = local
        self.rows = {}
        self.stats = {}

    def refresh(self, *, ordered=False):
        source = self.source
        source.row_factory = None
        version = source.execute('PRAGMA data_version').fetchone()[0]
        schema = source.execute('PRAGMA schema_version').fetchone()[0]
        if self.summary is not None and version == self.version and schema == self.schema:
            _COUNTERS['unchanged'] += 1
            if ordered and not self.tie_free:
                raise UnsupportedOrder('plan-dependent tied order')
            return
        start = time.perf_counter()
        start_wall = time.time()
        source.execute('BEGIN')
        try:
            schema = source.execute('PRAGMA schema_version').fetchone()[0]
            if schema != self.schema or self.summary is None:
                # Validate the source shape before any order certification.
                self._initialize()
                self.schema = schema
                self.watcher_revision += 1
                _COUNTERS['bootstraps'] += 1
            rows = dict((row[1], row) for row in source.execute(
                'SELECT rowid,' + ','.join(_quote(c) for c in self.columns) + ' FROM sessions ORDER BY rowid'))
            if len(rows) > 65536:
                raise UnsupportedSummary('source exceeds sidebar raw-row budget')
            stats = {}
            if self.message_shape:
                stats = dict((row[0], row[1:]) for row in source.execute(
                    "SELECT session_id, COUNT(*), COUNT(*) FILTER (WHERE role='user' OR "
                    "(role!='assistant' AND LOWER(role)='user')), MAX(timestamp) "
                    'FROM messages GROUP BY session_id') if row[0] in rows)
            # No hashes or latest-message assumptions. Equality of the exact
            # relevant facts, not identity of individual transcript rows, is the
            # requirement. Content titles use the original SQL on this snapshot.
            changed = {sid for sid in self.rows.keys() | rows.keys()
                       if not _facts_equal(self.rows.get(sid), rows.get(sid))
                       or not _facts_equal(self.stats.get(sid), stats.get(sid))}
            source_position = self.columns.index('source') + 1
            def watched(row):
                return row is not None and row[source_position] is not None and row[source_position] not in ('cron', 'webui')
            if any(watched(self.rows.get(sid)) or watched(rows.get(sid)) for sid in changed):
                self.watcher_revision += 1
            local = self.summary
            try:
                with local:
                    local.executemany('DELETE FROM sessions WHERE id=?', [(sid,) for sid in changed])
                    local.executemany('DELETE FROM _sidebar_message_stats WHERE session_id=?', [(sid,) for sid in changed])
                    live = [sid for sid in changed if sid in rows]
                    if live:
                        local.executemany('INSERT INTO sessions(rowid,' + ','.join(_quote(c) for c in self.columns) + ') VALUES (' + ','.join('?' for _ in range(len(self.columns) + 1)) + ')',
                                          [rows[sid] for sid in live])
                        local.executemany('INSERT INTO _sidebar_message_stats VALUES (?,?,?,?)',
                                          [(sid, *stats[sid]) for sid in live if sid in stats])
            except Exception as exc:
                raise UnsupportedSummary('disposable summary update failed') from exc
            # Conservative across every scope and candidate/refill window. The
            # final SQL key is activity (or started_at without messages). If it
            # is unique over all eligible rows, the candidate tuple starting
            # with that key is unique too. SQLite, not Python, defines equality
            # here (including numeric storage classes and NULL groups).
            self.tie_free = local.execute('''
                SELECT 1 FROM sessions s
                LEFT JOIN _sidebar_message_stats ms ON ms.session_id=s.id
                WHERE s.source IS NOT NULL
                GROUP BY COALESCE(ms.last_activity,s.started_at)
                HAVING COUNT(*)>1 LIMIT 1
            ''').fetchone() is None
            self.rows = rows
            self.stats = stats
            self.version = version  # Never restamp a pre-commit snapshot as newer.
            self.revision += 1  # Includes content-only edits used by goal titles.
            _COUNTERS['full_fact_rereads'] += 1
            _COUNTERS['changed_rows'] += len(changed)
            _COUNTERS['fact_rows_read'] += len(rows)
            _COUNTERS['message_aggregate_passes'] += bool(self.message_shape)
            _COUNTERS['last_changed_rows'] = len(changed)
            _COUNTERS['last_fact_ms'] = (time.perf_counter() - start) * 1000
            if os.getenv('HERMES_WEBUI_SIDEBAR_DIAGNOSTICS') == '1':
                print('[sidebar-summary] ' + json.dumps({'event': 'reconcile', 'reader': self.serial,
                      'revision': self.revision, 'start': start_wall, 'rows': len(rows), 'changed_rows': len(changed),
                      'full_fact_rereads': 1, 'ms': _COUNTERS['last_fact_ms']}), flush=True)
        finally:
            source.rollback()
        if ordered and not self.tie_free:
            raise UnsupportedOrder('plan-dependent tied order')


@contextmanager
def summary_connection(db_path, *, ordered=False):
    """Serialize source adoption and SQL consumers; never retain a transaction.

    Ordered reads (bounded or not) require tie-free effective activity keys.
    Unordered lookup/watcher consumers can still use tied raw facts.
    On unsupported schemas/failure callers use their original read-only reader.
    Exceptions from the consumer are not caught/replayed here.
    """
    path = Path(db_path).resolve()
    reader = None
    fallback = None
    order_fallback = False
    with _LOCK:
        try:
            st = path.stat()
            identity = (st.st_dev, st.st_ino)
            reader = _READERS.get(str(path))
            if reader is not None and reader.identity != identity:
                _READERS.pop(str(path)).close()
                reader = None
            if reader is None:
                reader = _Reader(path, identity)
                _READERS[str(path)] = reader
            reader.refresh(ordered=ordered)
            _READERS.move_to_end(str(path))
            while len(_READERS) > _MAX_READERS or sum(len(entry.rows) for entry in _READERS.values()) > 65536:
                _READERS.popitem(last=False)[1].close()
                _COUNTERS['evictions'] += 1
        except UnsupportedOrder:
            order_fallback = True
            _READERS.move_to_end(str(path))
            while len(_READERS) > _MAX_READERS or sum(len(entry.rows) for entry in _READERS.values()) > 65536:
                _READERS.popitem(last=False)[1].close()
                _COUNTERS['evictions'] += 1
            _COUNTERS['tie_fallbacks'] += 1
            _COUNTERS['legacy_fallbacks'] += 1
        except UnsupportedSummary:
            old = _READERS.pop(str(path))
            # The already-open source is strictly read-only. Reuse it for the
            # legacy consumer instead of opening a second handle on every read.
            fallback = old.source
            if old.summary is not None:
                old.summary.close()
            reader = None
            _COUNTERS['legacy_fallbacks'] += 1
            _LOG.debug('Unsupported sidebar summary shape; exact legacy reader', exc_info=True)
        except (OSError, sqlite3.Error):
            old = _READERS.pop(str(path), None)
            if old is not None:
                old.close()
            _COUNTERS['source_read_failures'] += 1
            # Preserve the original single read-only open/failure contract.
            # In particular do not retry an open failure with another handle.
            raise
        if order_fallback or reader is None:
            if os.getenv('HERMES_WEBUI_SIDEBAR_DIAGNOSTICS') == '1':
                # Existing opt-in diagnostics: numeric process counters only,
                # never source paths, session identifiers or row contents.
                print('[sidebar-summary] ' + json.dumps({'event': 'fallback',
                      'tie_fallbacks': _COUNTERS['tie_fallbacks'],
                      'legacy_fallbacks': _COUNTERS['legacy_fallbacks']}), flush=True)
        if order_fallback:
            # Production opens a new read-only handle for each ordered read.
            # The retained fact reader can have stale planner statistics after
            # external ANALYZE even though data_version detects the commit.
            # Keep it for raw facts, but never use it for plan-sensitive ties.
            with closing(sqlite3.connect(state_db_readonly_uri(path), uri=True,
                                         timeout=.05, check_same_thread=False)) as conn:
                yield conn
        elif reader is None:
            assert fallback is not None
            fallback.row_factory = None
            with closing(fallback) as conn:
                yield conn
        else:
            reader.summary.row_factory = None
            reader.source.row_factory = None
            yield reader.summary


def project_rows(conn, rows, project, normalize, visible):
    """Reuse exact query-local connected components, including noncontinuation edges.

    Parent titles/root ids affect children across source boundaries too. A key
    includes every raw value AND candidate input order, not just a root id or a
    revision. Components outside this query never leak into a bounded window.
    """
    if not getattr(conn, 'sidebar_summary', False):
        _COUNTERS['legacy_raw_projections'] += 1
        result = project(rows)
        for row in result:
            row.pop('model_config', None)
        return [item for row in result if visible(item := normalize(row))]
    reader = conn.sidebar_reader
    by_id = {row['id']: row for row in rows}
    parents = {sid: sid for sid in by_id}

    def root(sid):
        while parents[sid] != sid:
            parents[sid] = parents[parents[sid]]
            sid = parents[sid]
        return sid

    for row in rows:
        parent = row.get('parent_session_id')
        if parent in by_id:
            parents[root(row['id'])] = root(parent)
    components = {}
    ranks = {}
    for rank, row in enumerate(rows):
        ranks[row['id']] = rank
        components.setdefault(root(row['id']), []).append(row)
    result = []
    for component in components.values():
        key = tuple((tuple(row), _fact_key(tuple(row.values()))) for row in component)
        cached = reader.components.get(key)
        if cached is None:
            projected = project([dict(row) for row in component])
            for row in projected:
                row.pop('model_config', None)
            cached = [item for row in projected if visible(item := normalize(row))]
            _COUNTERS['components_rederived'] += 1
            _COUNTERS['physical_rows_rederived'] += len(component)
            if len(component) <= 2048:
                reader.components[key] = cached
                while len(reader.components) > 16384:
                    reader.components.popitem(last=False)
        else:
            reader.components.move_to_end(key)
            _COUNTERS['component_hits'] += 1
        result.extend(dict(row) for row in cached)
    # The old global stable sort preserves raw root input rank for tied activity.
    from api.agent_sessions import _as_score
    result.sort(key=lambda row: (-_as_score(row.get('last_activity'), row.get('started_at')),
                                ranks[row.get('_lineage_root_id') or row['id']]))
    _COUNTERS['summary_policy_passes'] += 1
    return result


def source_revision(db_path):
    """An exact, shared generation for the watcher; no raw projection/hash."""
    with summary_connection(db_path) as conn:
        if not getattr(conn, 'sidebar_summary', False):
            return None
        return (conn.sidebar_reader.serial, conn.sidebar_reader.watcher_revision)
