# Trait workflow overview

The single-photo workflow lives in `plugins/photo-traits-local/workflow.py`, using
the existing `server.py` / `scripts/traits.py` core. Its HTTP deployment is locked
by default in `render.yaml`; see [RENDER_MOCK.md](RENDER_MOCK.md).

When separately configured and authorized, the sequence is: define one species
scope and one or two visible fields, attach one selected JPEG, show the sanitized
preview and complete rubric, obtain explicit approval, make at most one attempt,
then preserve human corrections separately and export allowlisted CSV fields.
Tool arguments cannot establish the user's identity or independently prove consent.

Attachment hosts are denied unless explicitly allowlisted. For a structurally valid
HTTPS URL on an unlisted host, `attach_photo` returns an error containing only
`candidate_host` and `download_performed: false` to the authenticated session owner.
This check precedes DNS, download and session changes, so an existing photo and
preview confirmation remain intact. Malformed URLs receive a generic error. The
hostname is untrusted input for review, never permission to add it to the allowlist.

A changed image or field plan invalidates confirmation. Duplicate calls cannot
resend the same attempt; cancellation does not refund a request already in flight.
Unknown and unvalidated values remain unknown. Measurements from source records
are distinct from pixels and estimates. No photos, review tables or run receipts
are included in this source release.

The model core is fixed to Decisions API / `gpt-6-luna`; no new model integration
or live request is part of this deployment. Local legacy batch code additionally
requires explicit live/upload flags, documented prior-cost evidence and a bounded
budget. This does not authorize hosted live use. Current official contracts,
pricing, data scope, credentials and a durable spending ledger must be checked and
approved before any future model request.

The complete synthetic regression command is `python scripts/dev.py check` in the
prepared isolated environment. `python scripts/accept_workflow.py` adds a generated
fixture flow and writes ignored acceptance evidence. Real metadata checks, real
photos, model quality and ChatGPT host acceptance must be reported separately.
