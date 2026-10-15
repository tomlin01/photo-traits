# Local photo picker prototype

2026-10-15. This change is local and undeployed. It adds a second image ingress
path for the existing owner-authenticated, single-photo MOCK workflow. No new
download hosts, credentials, storage service, model calls or dependencies are
needed. Actual ChatGPT support and the host's tool-message size limit remain
unverified. The prior attachment workflow and its recorded acceptance are separate.

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

## Next acceptance gate

After separate deployment/plugin-refresh authorization, test the same synthetic
JPEG through a real ChatGPT host, including the maximum size, cancellation,
expired/stale sessions and the preview/approval transition. Confirm the host
accepts the new resource and app-only tool and does not surface base64 to the
model. A smaller host limit must be handled explicitly, without enlarging the
downloader allowlist or silently introducing another upload service. This local
phase performs no deployment, Git push, plugin refresh or paid API use.

## Official contracts checked

- [OpenAI: Build a ChatGPT UI](https://developers.openai.com/plugins/build/chatgpt-ui):
  standard MCP Apps resource metadata and host messaging.
- [OpenAI: Build an MCP server](https://developers.openai.com/plugins/build/mcp-server):
  server authorization remains necessary for every action.
- [MCP Apps stable specification, 2026-01-26](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx):
  lifecycle, tool visibility and resource security contract.

These contracts do not establish account access or a guaranteed 1 MB end-to-end
ChatGPT upload limit.
