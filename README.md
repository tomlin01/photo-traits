# photo-traits

Extract visible individual traits within one user-specified species, with separate
model candidates and human review. This is not species classification, identity
verification, or physical measurement without calibration. Melops is a provisional
metadata source, not a fixed product requirement.

## Locked MOCK deployment

The Render configuration starts Python with `--locked`. Only `/healthz` succeeds;
all MCP initialization, tool listing and tool calls are rejected. No workflow
engine, model key, attachment downloader or research workspace is opened in this
mode. OAuth and cloud live inference are disabled.

See [deployment settings and checks](docs/RENDER_MOCK.md) and `render.yaml` for the
Free Python service, dedicated branch, manual deploys and pinned Python version.
No photos, research data, CSV results, credentials or local account configuration
are included in this repository. Deployment settings are not proof that a service
has been created or that ChatGPT host acceptance has passed.

## Local validation

Use Python 3.12 or newer. Core metadata and local mock paths use the standard
library. Complete HTTP OAuth tests additionally use the pinned RS256 dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r plugins/photo-traits-local/requirements-http.txt
.venv/bin/python scripts/dev.py init
.venv/bin/python scripts/dev.py check
.venv/bin/python scripts/accept_workflow.py
```

`init` itself never installs packages, downloads data, reads credentials or calls
an API. `check` runs synthetic regressions and separately checks pinned metadata
if already present; absent real data is explicitly reported as SKIP. Acceptance
creates ignored synthetic evidence only. A mock test pass is not model accuracy,
biological validation, or web/desktop/iOS host acceptance.

## Data and workflow rules

- Preserve individual → observation → image relations. Keep recaptures, sides and
  crops together when splitting by individual. Untagged labels are not PIT-confirmed.
- Keep field-measured millimetres separate from pixels and model estimates. Missing
  or unvalidated source sex stays unknown. Confidence is not biological verification.
- Flag ambiguous observations; never silently merge or average them. Human values
  and model candidates remain separate, including disagreements and unknowns.
- Export only allowlisted fields. Exclude coordinates, trap identifiers, locations
  and photo metadata. Metadata downloads are pinned and capped at 5,000,000 bytes;
  image archives are never downloaded by setup.

[Source provenance](docs/SOURCES.md), [field dictionary](docs/FIELD_DICTIONARY.md),
[review protocol](docs/REVIEW_PROTOCOL.md), and [workflow overview](docs/TRAITS_WORKFLOW.md)
document these boundaries. The source-code package and its configurable local
manifest are described in [the package README](plugins/photo-traits-local/README.md).

The existing model core uses Decisions API / `gpt-6-luna`. Real model calls require
separate data-scope and budget approval, current official documentation, secure
backend key setup and durable spending controls. Cloud live startup is unavailable.

Source data is CC BY 4.0: retain attribution, version and changes when sharing
approved derivatives. The source data licence does not license this code;
a code licence has not yet been selected.
