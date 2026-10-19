"""Conservative bounded-order compatibility for Agent 6ff5c7eec5.

SQLite's aggregate tie order is plan-defined. Only the canonical index layout
(including its title-uniqueness migration), also recorded in the project-owned
production-shape fixture, is certified. Any addition, removal, changed column,
partial predicate or uniqueness falls back to the original SQL. Never add an
explicit tie-break or infer safety from a few matching index names.
"""
import re

_DDL = '''
CREATE INDEX idx_sessions_source ON sessions(source);
CREATE INDEX idx_sessions_source_id ON sessions(source, id);
CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX idx_sessions_session_key ON sessions(session_key, started_at DESC);
CREATE INDEX idx_sessions_gateway_peer ON sessions(source, user_id, chat_id, chat_type, thread_id, started_at DESC);
CREATE INDEX idx_sessions_handoff_state ON sessions(handoff_state, started_at);
CREATE INDEX idx_sessions_system_prompt_hash ON sessions(system_prompt_hash);
CREATE INDEX idx_sessions_tool_names ON sessions(tool_names);
CREATE INDEX idx_sessions_effective_activity ON sessions(COALESCE(last_activity_at, started_at) DESC, started_at DESC);
CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions(title) WHERE title IS NOT NULL;
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX idx_messages_session_id ON messages(session_id, id);
CREATE INDEX idx_messages_assistant_calls_by_session ON messages(session_id) WHERE role = 'assistant' AND tool_calls IS NOT NULL;
CREATE INDEX idx_messages_platform_msg_id ON messages(session_id, platform_message_id) WHERE platform_message_id IS NOT NULL;
CREATE INDEX idx_messages_session_active ON messages(session_id, active, timestamp);
CREATE INDEX idx_messages_display_page ON messages(session_id, display_order, active DESC, id DESC) WHERE active = 1 OR compacted = 1;
CREATE INDEX idx_messages_display_backfill ON messages(session_id) WHERE (display_order IS NULL OR display_identity IS NULL) AND (active = 1 OR compacted = 1);
CREATE INDEX idx_messages_display_identity ON messages(session_id, display_identity, display_order) WHERE display_identity IS NOT NULL AND (active = 1 OR compacted = 1);
CREATE INDEX idx_messages_active_null ON messages(active) WHERE active IS NULL;
CREATE INDEX idx_messages_session_uid ON messages(session_id, message_uid) WHERE active = 1;
'''


def _normalize(sql):
    # Deliberately conservative textual equality. Do not strip whitespace from
    # quoted predicates (which would equate different string literals).
    return re.sub(r'\s+', ' ', sql.strip().rstrip(';'))


_EXPECTED = frozenset(_normalize(sql) for sql in _DDL.split(';') if sql.strip())


def supports_bounded_order(source):
    indexes = list(source.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name IN ('sessions','messages')"))
    explicit = frozenset(_normalize(sql) for name, sql in indexes if sql is not None)
    implicit = {name for name, sql in indexes if sql is None}
    return explicit == _EXPECTED and implicit == {'sqlite_autoindex_sessions_1'}
