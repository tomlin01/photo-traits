# MOCK configuration and acceptance baseline

This 2026-10-14 record covers the existing authenticated MOCK service at
`https://photo-traits-mock.onrender.com`, running code `5e8d239`. The recorded
service uses the Free plan and manual deployments. Its private runtime settings
are separate from the safe repository template. This local documentation/config
commit does not deploy, change provider settings or enable model calls.

## Safe checked-in defaults

`render.yaml` uses the dedicated `codex/render-mock` branch, Free Python service,
pinned `.python-version`, automatic deployment off, `0.0.0.0` and Render's `$PORT`.
Its start command retains `--locked` and contains no account-specific discovery
values. Applying this template to the existing service would lock the workflow;
it is a baseline reference, not a replacement for its private runtime settings.

`--locked` never constructs a workflow engine, reads authorization environment
variables, binds a research workspace, reads a model key or creates an attachment
downloader. It needs no credentials or attachment hosts. Every MCP initialization,
tool listing and tool call is rejected, including requests with bearer tokens.
The CLI has no live mode.

Optional `--discovery-issuer` and `--discovery-resource` arguments must be supplied
together with `--locked`. Use private configuration values, preserve the issuer's
trailing slash and the resource's `/mcp` path. Discovery publishes those URLs,
the two scopes and header bearer method; it does not unlock a tool or configure
a verifier. The committed template omits both arguments.

Expected locked responses:

| Request | Expected response |
| --- | --- |
| `GET /healthz` | 200; `status: ok`, `mode: mock`, `authorization: locked` |
| `HEAD /healthz` | 200; no body |
| `POST /mcp` with initialize, tools/list or tools/call | 401, with or without a token |
| `GET /mcp` | 401 |
| `GET /.well-known/oauth-protected-resource` or its `/mcp` suffix | 503 by default; 200 only with both explicit discovery arguments |
| `HEAD` for either metadata route | Same status as GET; no body |
| File paths, results, photos and unknown routes | 404 |

## Existing owner-authenticated MOCK settings

The authenticated start path requires these backend environment variables. Values
below are placeholders only; actual issuer, audience/resource and owner values
stay in private configuration. No email, client secret, token or key belongs here.

| Variable | Placeholder / meaning |
| --- | --- |
| `PHOTOTRAITS_OAUTH_ISSUER` | `https://issuer.example/`; exact approved issuer, with trailing slash |
| `PHOTOTRAITS_RESOURCE` | `https://service.example/mcp`; canonical resource and JWT audience |
| `PHOTOTRAITS_ALLOWED_SUB` | `<approved-immutable-owner-subject>`; exact approved owner |

All ten tools require a verified RS256 JWT and the appropriate `photo:analyze`
or `results:read` scope. Anonymous requests, invalid tokens and other owners are
rejected. Missing configuration fails startup; discovery alone grants no access.

Reference start command for the already authorized authenticated MOCK setup;
applying it is a separate operator action, not part of local init or this commit:

```bash
python plugins/photo-traits-local/workflow_http.py --workspace . --bind 0.0.0.0 --port "$PORT" \
  --attachment-host sdmntprjapaneast.oaiusercontent.com \
  --attachment-host sdmntprcentralus.oaiusercontent.com
```

These are exactly the two approved attachment hosts. The default allowlist is
empty; do not add wildcards or an automatic fallback for a new host. Downloads
retain HTTPS-only, no-redirect, public-IP-only, DNS/IP pinning and TLS hostname
verification guards, with the existing JPEG size limit. Signed URLs, photo bytes
and attachment identifiers are private data, not configuration examples.

## Acceptance status recorded 2026-10-14

| Layer | Evidence and limit |
| --- | --- |
| Local code regressions | All 123 synthetic tests passed again during this baseline check, with unchanged application code from `5e8d239` and no test skips. Synthetic fixtures are not real-data or model validation. |
| Local source metadata | SKIP: no pinned raw metadata file in the isolated checkout; no download performed. This is separate from the 123 passing synthetic tests. |
| Runtime configuration | 10 download guard checks and 16 external HTTPS checks passed in the prior configuration acceptance, including anonymous/invalid-token/wrong-owner rejection; not rerun by local init. |
| Actual ChatGPT tool flow | One selected photo and two choice fields completed `new_session` → `plan_fields` → `attach_photo` → `preview_plan` → explicit user confirmation → one `analyze_photo` → completed `session_status` → explicit user review → `review_values` → `export_csv`. |
| Output | `model_called: false`, cost US$0, `MOCK_NOT_MODEL_OUTPUT`; raw candidates remained `null` / `mock_unknown`, with separate human values. The two-row CSV was validated and delivered through native Library. No photo, values or Library identifier is reproduced here. |
| Photo cleanup | The `finally` path deletes temporary photos and synthetic tests assert their absence. Actual remote filesystem deletion was **not independently checked**: status reports metadata only, and Free service shell access was unavailable without an upgrade. No upgrade was performed. |
| Still pending | Full cross-surface web/desktop/iOS acceptance, live API use and scientific accuracy have **not passed**. |

To reproduce local software checks, follow the root README's Python 3.12+ and
pinned-dependency setup, then run `python scripts/accept_workflow.py`. It runs
`scripts/dev.py init` and `scripts/dev.py check`, plus a synthetic MOCK lifecycle
and CSV check. Use the selected environment's Python. No external service or
real photo is contacted; no model key is read. Raw metadata verification is a
separate step in `check`, explicitly SKIP when the pinned file is absent. The
script's host-unverified message describes its local synthetic scope; it does
not reproduce or supersede the recorded ChatGPT run above.

## Publishing boundary

Review every file and every reachable commit before pushing. A safe release may
need a fresh branch from the existing remote main and a file-level copy of reviewed
code, with a GitHub noreply commit identity. Do not merge or cherry-pick a private
local baseline into that branch. Preserve the original branch and working tree.

Exclude `.env*`, `.local` evidence, keys, tokens, private emails/owner IDs, actual
photos, image/session hashes or IDs and personal CSV output. Use explicit file
staging and the existing GitHub noreply identity; never force-add ignored data.
Machine-specific manifests and account settings stay private. Preserve prior
commits and unrelated working-tree changes; a local commit is not permission to
push, merge, redeploy, share research data or enable live inference.

Cloud live mode remains blocked until a durable reserve and hard spending limit
survive restarts, a user-entered key is securely configured in the backend, and
the user explicitly authorizes the selected photo/model transmission and bounded
call count. The existing new-spend ceiling is US$5; this MOCK baseline spends
US$0 and authorizes no paid changes. Ephemeral local counters cannot enforce the
cumulative limit. Current official API documentation and the secure key setup
workflow must be checked before any later integration.

## Official references checked 2026-10-13

- [Web services, port binding and TLS](https://render.com/docs/web-services)
- [Free instance limits and ephemeral storage](https://render.com/docs/free)
- [Python version selection](https://render.com/docs/python-version)
- [Blueprint configuration](https://render.com/docs/blueprint-spec)

These are dated references, not a fresh provider check. Future deployment changes
require separate authorization and current verification of the service and plan.
