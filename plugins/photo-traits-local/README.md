# PhotoTraits source package

This package contains the one-photo workflow and its shared local review core.
The checked-in Render entrypoint is `workflow_http.py --locked`; this template
exposes health only and rejects every MCP tool, even with a bearer token. The
existing owner-authenticated MOCK service uses a separately configured start
command. See [configuration and acceptance status](../../docs/RENDER_MOCK.md).

## Components

- `workflow.py`: one user-confirmed photo, one or two visible fields, separate
  candidate/human values, cancellation and CSV export.
- `attachments.py`: bounded HTTPS downloads from explicitly approved exact hosts;
  no redirects, private addresses, forwarded credentials or arbitrary file paths.
- `workflow_http.py`: RS256 OAuth transport, plus explicit health-only locked startup.
- `server.py` / `ui.html`: the shared core and optional loopback review interface.
- `attachment_mock.py`, `capabilities_server.py`, `capabilities_http.py`, and
  `descriptor_probe.py`: synthetic/diagnostic sources retained for regression tests.
  They are not launched by the Render start command.

All ten workflow tools require a verified principal outside locked mode. OAuth
configuration must supply issuer, canonical HTTPS resource and an approved immutable
subject. Missing configuration cannot create an anonymous workflow. Attachment hosts
are empty by default. There is no hosted live CLI option or cloud spending ledger.

## Local manifest template

`mcp.json` is a source template, not an installed or enabled integration. Its
`/absolute/path/to/photo-traits` value is a placeholder. Before separately authorized
local use, replace it in an ignored/local copy with an explicitly selected project
path, and use an installed Python 3.12+ executable. Never put keys in the manifest.
The manifest starts mock mode only. Render does not read this stdio manifest.

## Tests

Use the root README's isolated environment instructions and run
`python scripts/accept_workflow.py`. Complete acceptance checks dependency versions,
all regressions and a generated gray JPEG workflow, with scripted human-value
fixtures and ignored synthetic CSV evidence. It does not register a plugin, connect
a host account, download a real photo, access a real key or call a model.

One actual ChatGPT photo completed the MOCK flow with explicit human confirmation,
separate review and Library CSV delivery on 2026-10-14. This recorded host run is
separate from the synthetic acceptance script; full web/desktop/iOS acceptance
remains pending. A model-supplied boolean is not proof of human consent. Do not
publish or enable an integration merely because tests pass.

## Biological and payment boundaries

Use the conversation to define visible field meanings; do not default to Melops
or append fields the user did not choose. Unknowns remain unknown. No unsupported
physical size, source sex validation, individual identification or species inference.

Live work is outside this release. It requires separate explicit approval for
selected data and budget, current official Decisions documentation/pricing, secure
backend credentials and a durable spending ledger. A Free service's ephemeral
filesystem cannot enforce a cumulative spending limit across restarts.
