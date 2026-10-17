# Composer draft persistence

Composer autosaves use `sessions/<session_id>.draft`, a small JSON object with
exactly the draft payload exposed by the API (`text`, `files`, and any retained
legacy keys). The suffix deliberately does not match session discovery's
`*.json` glob. Draft saves do not serialize messages, rewrite an existing
session JSON, change conversation `updated_at`, or update the sidebar index.

`Session.composer_draft` resolves the standalone record on read, including for
cached sessions, independent full loads, metadata-only loads and duplicates.
Its draft identity is pinned at construction and explicitly updated at permanent
compression rotation; temporary archived-snapshot ID rebinding does not change
which standalone record the property reads. A draft POST derives its path from
the validated request ID once and retains it through locking, fallback, merge
and publication, independently of any live Session identity changes.
Without a standalone record it falls back to the draft embedded in the legacy
session JSON. The first changed draft write creates the record; no transcript
migration or rewrite is required. An empty record is authoritative and must not
be unlinked on send: removing it could resurrect an old embedded draft.
Ordinary transcript saves may retain a compatibility snapshot of the draft;
that snapshot never overrides an existing standalone record.

A changed draft write flushes and fsyncs a uniquely named temporary file, then
atomically replaces the standalone record. A failed write leaves the previous
record authoritative and removes the temporary file. Corrupt, non-object or
unreadable standalone records are logged and treated as authoritative empty
values, so draft damage cannot block transcript save, compact or export.
Repeated read/quarantine warnings are limited per path and cause to once per
minute, with a bounded 256-signature cache that retains no exception objects.
A best-effort rename retains the bad bytes as `<session_id>.draft.corrupt`; this
quarantine also marks an empty draft on later reads, preventing legacy draft
resurrection. Quarantine never waits for another draft lock holder and retries
validation under its acquired lock before renaming, so it cannot move a healthy
concurrent replacement. A later successful draft save overrides the marker.
Atomic rename and file fsync do not add a directory-fsync/power-loss guarantee
beyond the existing session writer.

Draft partial updates and deletion share a separate per-path thread lock. They
do not contend on the transcript writer lock. Cold draft requests use a
read-only draft-owner resolver that reads the metadata prefix without scene,
message-count or transcript recovery work. Unexpected legacy layouts with a
draft after the large arrays still require a read-only full parse to recover
that value; this never runs transcript self-healing or writes the transcript.
A bounded 128-entry metadata cache retains only draft-owner fields, keyed by
path plus inode, size, mtime and ctime. Incomplete legacy provenance is read
from the full JSON once per file revision rather than assuming ownership from
an incomplete prefix; no messages are retained in this cache. Persisted owners
always use this resolver even when a full Session is cached, so an externally
replaced transcript supplies fresh legacy draft and provenance fields. Only a
never-persisted in-memory shell may provide a live fallback when the identity
file is absent; a saved or loaded object cannot recreate a removed identity.
New Chat shells have no session file until the first meaningful write: the first changed
draft creates the empty session identity once under the existing writer lock.
Unchanged shell requests remain unpersisted. Lock order for that initialization
and deletion is transcript lock before draft lock. Existing-session draft saves
use only the draft lock and recheck session existence before publication, so a
request resolved before deletion cannot recreate the deleted draft.

Lifecycle behavior:

- Send clears text via the existing client's text-only POST, retaining attachment
  references and a standalone record across reload and restart.
- Rename keeps the session ID and therefore the same draft record.
- Delete and bulk zero-message cleanup remove the session JSON, draft and any
  quarantined draft under the draft lock. Cleanup retains the existing policy
  of removing empty transcripts even when they have a nonempty draft. The
  transcript-lock-before-draft-lock order agrees with single-session deletion;
  bulk cleanup waits at most 100 ms for each transcript writer lock, leaves busy
  sessions untouched, and reports their count as `skipped` alongside `cleaned`.
  Draft unlink failures are logged without skipping later index/journal cleanup.
  Ephemeral, hidden-background and admission-rollback removals use the same
  file-removal helper.
- Duplicate reads the current draft and copies it independently into the new
  session's compatibility field. Subsequent draft writes get a separate record.
- Branch/fork and JSON import continue to create a draftless new session, as
  before. They do not inherit the source session's standalone record.
- JSON export explicitly resolves the current draft rather than using a stale
  object dictionary snapshot. Backups of live state must retain both files;
  exporting/importing a session keeps the existing draftless-import semantics.

The browser's 400 ms debounce, switch-time flush, restore rules, attachment
normalization and failed-send restoration are unchanged. Multiple tabs retain
the existing last-arrival-wins behavior. Locks coordinate threads in the single
WebUI server; they are not a multi-process partial-update transaction protocol.
