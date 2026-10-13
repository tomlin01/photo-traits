# Locked MOCK deployment

The initial Render deployment serves public health checks only. `--locked` never
constructs a workflow engine, reads OAuth configuration, binds a research workspace,
reads a model key, or creates an attachment downloader. Every MCP initialization,
tool listing and tool call is rejected, including requests with bearer tokens.
The CLI has no live mode. Removing `--locked` still requires explicit, valid OAuth
configuration and does not enable model spending.

## Reviewed service settings

Use a Python Web Service on the **Free** instance plan, the dedicated
`codex/render-mock` branch, and the values in the repository's `render.yaml`.
The Python version is pinned in `.python-version`. Automatic deployment is off.
No environment groups, secrets, API keys, attachment hosts, database, persistent
disk, payment method, paid add-on or upgrade are required for this locked service.
The Blueprint is a settings reference; committing it does not create a service.

The start command binds `0.0.0.0` and Render's `$PORT`. The public checks are:

| Request | Expected response |
| --- | --- |
| `GET /healthz` | 200; `status: ok`, `mode: mock`, `authorization: locked` |
| `HEAD /healthz` | 200; no body |
| `POST /mcp` with initialize, tools/list or tools/call | 401, with or without a token |
| `GET /mcp` | 401 |
| `GET /.well-known/oauth-protected-resource` | 503; no invented OAuth issuer |
| File paths, results, photos and unknown routes | 404 |

Use no credentials or photos for these checks. A public health response does not
mean that OAuth, ChatGPT attachment handling or the photo analysis flow has passed
host acceptance. They require separate configuration and acceptance.

## Publishing boundary

Review every file and every reachable commit before pushing. A safe release may
need a fresh branch from the existing remote main and a file-level copy of reviewed
code, with a GitHub noreply commit identity. Do not merge or cherry-pick a private
local baseline into that branch. Preserve the original branch and working tree.

Exclude `.env*`, `.local`, research photos/data/results and local approval or probe
evidence. Machine-specific local plugin manifests must not be published unchanged.
Synthetic fixtures and tests must be identified as such; they do not validate
biological labels or real model quality.

Cloud live mode remains blocked until a separately approved durable spending
ledger and OAuth setup exist. The Free filesystem is ephemeral; an in-memory or
local file counter cannot enforce spending across restarts.

## Official references checked 2026-10-13

- [Web services, port binding and TLS](https://render.com/docs/web-services)
- [Free instance limits and ephemeral storage](https://render.com/docs/free)
- [Python version selection](https://render.com/docs/python-version)
- [Blueprint configuration](https://render.com/docs/blueprint-spec)

Free services sleep after 15 minutes of inactivity, and the workspace shares
750 free instance hours per month. Actual deployment status, service URL and plan
must be verified in Render; this document is not evidence of a completed deployment.
