# Local photo picker prototype

2026-10-15. This prototype adds a second image ingress path for the existing
owner-authenticated, single-photo MOCK workflow and has been deployed to the
existing Free MOCK service. A controlled ChatGPT check confirmed resource retrieval
but still failed to display the UI; see the resource compatibility evidence below.
Actual picker/bridge acceptance and the host's tool-message size limit remain
unverified. No new download hosts, credentials, storage service, model calls or
dependencies are needed. The prior attachment workflow and its recorded acceptance
are separate.

## Flow and boundaries

1. Discuss and save one or two visible fields with `plan_fields`.
2. Call `open_photo_picker` with the current session ID and revision. Its static
   `ui://photo-traits/photo-picker-v1.html` resource renders one JPEG file input.
3. The human selects a file and presses **傳送照片**. Only then does the view call
   `attach_photo_bytes` through the host's standard `tools/call` bridge.
4. Return to chat and call `preview_plan`. The human must check the sanitized
   preview and full field rubric before explicitly approving `analyze_photo`.
   Selecting or uploading a photo does not approve analysis.

The UI waits for a successful MCP Apps handshake and the authoritative tool
result; streamed/model-supplied input is not a session authority. Messages must
come from its parent window, and subsequent messages must match the handshake
origin. Opaque sandbox parents require a wildcard postMessage target. This is
host isolation, not cryptographic proof that a human selected the bytes.

`attach_photo_bytes` has `_meta.ui.visibility: ["app"]`. Hosts are responsible
for hiding it from the model and restricting app access. This metadata is not an
authorization boundary: a direct caller with the existing owner token and both
scopes can invoke it. The server independently checks the owner, `photo:analyze`
and `results:read`, planned MOCK mode, current revision, cancellation and expiry.
No token, signed URL or filename is passed into or returned from the view.

The HTTP transport still verifies RS256/issuer/audience/expiry/owner before
reading a body. Authenticated resources are fixed packaged HTML, not arbitrary
paths. Resource CSP declares no external connections, resources or frames.

| Boundary | Limit or behavior |
|---|---|
| JPEG bytes | 1–1,000,000; one image; existing grayscale/RGB marker validation and 2048 px side limit |
| Base64 | Strict, canonical standard base64; at most 1,333,336 characters; no URL/data URI or whitespace |
| Bytes RPC HTTP body | At most 1,350,000 bytes including the JSON envelope |
| All other RPC requests | Existing 100,000-byte limit; oversized requests rejected after bounded parsing |
| Connections/framing | Existing eight slots and 20-second socket timeout; no compressed/chunked request bodies or duplicate Content-Length |
| Bridge waiting | 15-second initialization, 60-second upload; no automatic retries |
| Storage | Only the sanitized JPEG is written; original/clean hashes retained; raw base64 and descriptors are not persisted by this server |
| Replacement | Current revision required; previous JPEG removed and confirmation invalidated; identical bytes are idempotent; completion rejects if the revision changed before its response snapshot |
| Retention | Original 30-minute session deadline; at most five minutes after first export; analysis/cancel photo purge and explicit clear unchanged |

All authenticated requests are buffered up to the outer 1,350,000-byte ceiling
before method classification, including requests later rejected by the old
100,000-byte limit. Eight admitted requests bound raw buffers to 10.8 MB, before
JSON/base64 copies and interpreter overhead. The higher ceiling is intentional
and only accepts `attach_photo_bytes` above the old limit.

The bytes path shares JPEG validation, metadata removal, revision checks,
replacement and cleanup with `attach_photo`; it never invokes the downloader.
JPEG validation is not a full pixel decoder. Removing orientation/ICC metadata
can change presentation, so the existing decoded preview and human check remain
necessary. Client references are released on completion, failure, expiry and
teardown; JavaScript garbage collection is not a secure memory-erasure guarantee.
Host-side request logging/retention is outside this prototype's server cleanup.

`attachments.py` and deployment configuration remain unchanged. The existing
approved exact hosts are `sdmntprjapaneast.oaiusercontent.com`,
`sdmntprcentralus.oaiusercontent.com`, `sdmntprukwest.oaiusercontent.com` and
`sdmntprwestus3.oaiusercontent.com`. They are not added as defaults. Other regions,
wildcards, redirects, private/reserved addresses and proxies remain disallowed.

## Repeatable local evidence

Use an existing Python 3.12+ environment with the repository's pinned HTTP OAuth
test extras. The UI harness additionally uses an installed Node executable and
only built-in modules; no package install or browser dependency is needed.

```sh
python scripts/dev.py init
python -m unittest tests.test_photo_picker -v
python scripts/dev.py check
```

Tests use generated 8×8 JPEGs, including a 1,000,000-byte fixture padded with legal
synthetic JPEG comments. OAuth tests generate ephemeral signing material in
memory and use loopback only. No real credential, photograph or model API is used.
Node executes the actual shipped inline UI script with a simulated DOM/parent
bridge; its success case forwards the upload to the real loopback OAuth/MCP
handler. The synthetic token belongs to the harness, not the UI sandbox.

Coverage includes malformed/noncanonical/oversized base64, JPEG bounds and
metadata removal, owner/scopes/revision/mode, no DNS or remote download, duplicate
and replacement behavior, concurrent replacement before an upload reply,
cancel/clear/expiry during validation, confirmation and
post-attempt cleanup, static resource authorization, old request limits and
framing. Bridge scenarios check origin/context, size before file read, explicit
single upload, duplicate-click prevention, teardown, cancelled selection, expiry,
errors/timeouts/late replies without retries, and missing host capabilities.

Evidence is labeled `SIMULATED_MCP_APPS_BRIDGE`. A pass proves this local
implementation and transport boundary, not native file dialog behavior, real
ChatGPT app visibility, real message-size support, mobile behavior or scientific
accuracy. If Node or OAuth extras are absent, their tests explicitly skip; a
partial run must not be reported as full bridge acceptance. Real metadata checks
are reported independently by `dev.py check` and never fetch missing data.

## Resource compatibility evidence, 2026-10-15

`resources/read` and `resources/list` accept optional object-valued request
`_meta`. It is ignored, never reflected or used as authority. Extra parameters,
non-object metadata, unknown/missing read URIs, insufficient scopes and the
original 100,000-byte resource request limit remain rejected. Three permanent
regression tests cover these boundaries.

The diagnostic build `51da15029dd52af1be2252fa042febf6f986b8f0` reached Live on
the existing Render Free MOCK service at 17:02:27 UTC. Public health returned
HTTP 200 with mode `mock` and authorization `configured`. Exactly one controlled
resource retry was performed in the existing synthetic test conversation at
17:03:41 UTC, without a new tool call, file selection, upload or analysis.

| Observation | Result |
|---|---|
| Server resource request at 17:03:42 UTC | `resources/read`, fixed picker URI, `_meta` present |
| Server dispatch | HTTP 200, RPC `ok`, 203 ms from handler entry; pending observation was 202 ms |
| ChatGPT widget fetch in browser Network | HTTP 200, 920 ms |
| Actual host UI | Displayed “無法顯示 PhotoTraits (MOCK)”; no usable picker or bridge acceptance established |

This confirms that the host sends request metadata and that retrieval succeeds
with the compatibility fix. It does not establish the cause of the earlier host
timeout or the remaining display failure. The existing fixture session had already
been cleared; this attempt was limited to the static UI resource, not upload or
session lifecycle acceptance. The host's pre-existing CSP developer setting was
observed but unchanged; no causal conclusion is drawn from it.

Temporary diagnostics were limited to the method, fixed URI/redaction, metadata
presence boolean, elapsed time and HTTP/RPC status, at most 12 requests within
30 minutes per process and 256 bytes per line. They emitted no raw request,
metadata, token, header, filename, photo, base64, session ID, field content or
signed URL. The observer and its temporary tests are removed in this cleanup;
normal HTTP request logging remains disabled. Nonsensitive findings are retained
here; local screenshots/check output stay ignored by Git.

## Next acceptance gate

The remaining gate is a scoped host rendering/bridge investigation, followed by
synthetic JPEG acceptance through a real ChatGPT host: maximum size, cancellation,
expired/stale sessions and the preview/approval transition. Confirm the app-only
tool is callable by the view and does not surface base64 to the model. A smaller
host limit must be handled explicitly, without enlarging the downloader allowlist
or introducing another upload service. No paid API use or security-policy change
is authorized by this evidence.

## Official contracts checked

- [OpenAI: Build a ChatGPT UI](https://developers.openai.com/plugins/build/chatgpt-ui):
  standard MCP Apps resource metadata and host messaging.
- [OpenAI: Build an MCP server](https://developers.openai.com/plugins/build/mcp-server):
  server authorization remains necessary for every action.
- [MCP Apps stable specification, 2026-01-26](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx):
  lifecycle, tool visibility and resource security contract.

These contracts do not establish account access or a guaranteed 1 MB end-to-end
ChatGPT upload limit.
