# Composer draft persistence

Composer autosaves use `sessions/<session_id>.draft`, a small JSON object with
exactly the draft payload exposed by the API (`text`, `files`, and any retained
legacy keys). The suffix deliberately does not match session discovery's
`*.json` glob. Draft saves do not serialize messages, rewrite an existing
session JSON, change conversation `updated_at`, or update the sidebar index.

`Session.composer_draft` resolves the standalone record on read, including for
cached sessions, independent full loads, metadata-only loads and duplicates.
Without a standalone record it falls back to the draft embedded in the legacy
session JSON. The first changed draft write creates the record; no transcript
migration or rewrite is required. An empty record is authoritative and must not
be unlinked on send: removing it could resurrect an old embedded draft.
Ordinary transcript saves may retain a compatibility snapshot of the draft;
that snapshot never overrides an existing standalone record.

A changed draft write flushes and fsyncs a uniquely named temporary file, then
atomically replaces the standalone record. A failed write leaves the previous
record authoritative and removes the temporary file. Corrupt standalone records
raise rather than silently resurrecting an older embedded draft. Atomic rename
and file fsync do not add a directory-fsync/power-loss guarantee beyond the
existing session writer.

Draft partial updates and deletion share a separate per-path thread lock. They
do not contend on the transcript writer lock. Cold draft requests use a
read-only draft-owner resolver that reads the metadata prefix without scene,
message-count or transcript recovery work. Unexpected legacy layouts with a
draft after the large arrays still require a read-only full parse to recover
that value; this never runs transcript self-healing or writes the transcript.
A bounded 128-entry metadata cache retains only draft-owner fields, keyed by
path plus inode, size, mtime and ctime. Incomplete legacy provenance is read
from the full JSON once per file revision rather than assuming ownership from
an incomplete prefix; no messages are retained in this cache.
New Chat shells have no session file until the first meaningful write: the first changed
draft creates the empty session identity once under the existing writer lock.
Unchanged shell requests remain unpersisted. Lock order for that initialization
and deletion is transcript lock before draft lock. Existing-session draft saves
use only the draft lock and recheck session existence before publication, so a
request resolved before deletion cannot recreate the deleted draft.

Lifecycle behavior is unchanged:

- Send clears via the existing client's empty draft POST, keeping an empty
  standalone record durable across reload and restart.
- Rename keeps the session ID and therefore the same draft record.
- Delete removes both the session JSON and the draft record under the draft
  lock; it retains the existing transcript lock, index and journal cleanup.
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
