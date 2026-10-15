# Temporary photo analysis and CSV return

This is a local implementation, not a deployed change or a persistent user dataset
service. Analyze the selected photo, review the separate model candidates and human
corrections, generate the CSV, and let the user save it. CSV bytes are generated in
memory and returned directly through MCP or the authenticated local download route.
An `export-generated` hash records generation only. Neither that event nor a browser
download click proves delivery, a Library write, or that the user saved the file.

## Lifetime and clear

- A session expires 30 minutes after creation. Edits, reads, cancellation, and
  additional activity do not reset that deadline.
- The first successful CSV generation shortens expiry to the earlier of the
  original deadline and five minutes after generation. Repeated exports and human
  corrections do not extend it. A transport or receipt-write failure after
  generation does not grant another window. Export before any result is rejected.
- `clear_session` requires the authenticated session owner and both existing OAuth
  scopes. The legacy local UI's clear route requires its session bearer token and
  same origin. The two interfaces use the same lifetime and cleanup implementation.
- Clear/expiry immediately closes the session to new work. With an active worker or
  attachment download it reports `clear_pending`, `server_files_removed: false`.
  Cancellation cannot recall an in-flight provider request. Cleanup runs after that
  work has stopped writing, including error paths; it never releases a budget hold.
- Idle files are deleted by an authenticated clear, a request-triggered cleanup, or
  a sweep every 30 seconds while the HTTP server runs. Access stops at the deadline;
  physical deletion can lag by one sweep, active work, or a storage failure. A
  stopped process cannot run a timer: the next startup/sweep handles owned orphans.
  A safety/IO failure remains pending and is retried; it is never reported as erased.

Successful cleanup removes temporary photos, plans, model responses/results,
corrections, confirmations and generation events from the session directory and
clears its in-process collections. Source measurements, pixels, estimates, model
candidates and human values are never collapsed into a single verified value.
The one-photo workflow still removes its sanitized JPEG after an attempt, failure,
replacement or idle cancellation. The legacy local picker may keep its sanitized
previews until session cleanup; it now has the same fixed expiry and clear control.

## Owned storage and restart behavior

New entrypoints select **`data/derived/phototraits-transient-v1`**, a dedicated,
Git-ignored temporary root. An existing root must have the exact ownership marker;
even an empty unmarked directory is refused. Creation uses private permissions.
Each new session has a random opaque directory name and a private ownership/lease
file held with a nonblocking POSIX `flock` for its lifetime.

Startup and periodic cleanup inspect only this marked root. An abandoned session
is deleted only after its lease can be exclusively acquired and its marker matches
its directory name. A live process/worker retains the lease, including across other
engine instances; no PID reuse, wall-clock age, or guessed stale lock is sufficient
to delete it. After a process dies its unrecoverable session can be deleted on the
next sweep immediately, even if the previous in-memory deadline was later.

Deletion uses directory descriptors and no-follow opens, verifies ownership and
the generated file layout, and refuses symlinks, hardlinks, unexpected files,
unknown subdirectories and replaced roots. Marker contents are fixed data, never
instructions or paths. Invalid/unmarked/partially created directories are left for
explicit operator review. A process killed during cleanup can leave an empty
unmarked directory; it is not automatically adopted. Filesystem tampering or IO
failures can therefore prevent automatic removal. This is deliberately conservative.
Local POSIX locking is required; shared/network filesystems and multi-host lease
semantics have not been validated.

The spending ledger must be outside the temporary root. Startup rejects a local
ledger configured inside it, and SQLite files are not part of the deletion
allowlist. Pending reservations and settled costs survive session deletion.
US$0.25 remains a conservative reservation estimate, not a proven billing ceiling;
the existing fail-closed accounting and live-mode gates are unchanged.

## Existing data and limits

No migration or cleanup is enabled for `data/derived/chatgpt-workflow`,
`data/derived/plugin-sessions`, `data/derived/attachment-mock-sessions`, previous
acceptance outputs, or any other preexisting output roots. **Deleting or migrating
existing real data still needs separate scope approval.** This implementation was
tested only against newly created disposable synthetic fixtures. It has not been
started against the new root in the actual project or deployed to Render.

Server cleanup does not erase ChatGPT conversation content, Library files, browser
downloads/caches, provider copies, backups or snapshots. It is ordinary application
deletion, not a guarantee of forensic erasure. The user must save the generated CSV
before the review window ends. No receipt certifies a saved user copy.

## Offline validation

Use Python 3.12+ and the existing HTTP-test dependencies; no new dependency is
required for cleanup. `scripts/dev.py init` is offline. `scripts/dev.py check` runs
the synthetic suite, then reports pinned real-metadata checks separately. The
retention tests use new temporary roots, generated JPEGs, mock transports and child
processes with synthetic files only. They cover expiry, first/repeated/failed export,
clear during active work, auth, crash/restart, competing instances, path/link safety,
budget survival, and candidate/human CSV separation. HTTP tests bind loopback only.
`scripts/accept_workflow.py` additionally returns and explicitly clears a synthetic
workflow; its saved evidence is an intentional test artifact, not user data storage.
