---
name: plan-photo-traits
description: Use PhotoTraits Local to define visible individual traits within one user-specified species, select local JPEG photos, confirm a dynamic field plan, and review separate model and human results. Not species classification or physical measurement without calibration.
---

# PhotoTraits Local

This is a local stdio MCP plugin with a separate loopback browser UI. It is not an installed ChatGPT attachment handler or embedded MCP App. Never imply that a source package has been installed. Never read, request, print or transmit an API key through the conversation or tools.

The optional `../../attachment_mock.py` entrypoint rehearses the `fileParams`
shape using generated JPEG fixtures only. It rejects real host download URLs and
is not in the supplied manifest. Report it as local QA, never as real attachment
support. It cannot start live mode or approve a plan.

## Conversation → explicit plan → user confirmation → execution

1. Call `status`. Explain the reported mode. `mock` produces only labeled placeholders, not model observations. This skill makes no extra API call to plan fields: use the host conversation.
2. Clarify only the missing essentials: the single species scope, research comparison, visible region, desired distinctions, and what should remain unknown. Do not change the species, meaning of labels, sharing, payment or publication scope without the user's decision. Melops and the earlier cheek/eye example are not defaults.
3. Propose a compact table of field name, visible definition, type, allowed categories or ordered levels, unit and unknown handling. Definitions must specify what is observable. Do not infer identity, sex, verified source labels, precise location, trap IDs or physical size. Physical measurements require calibration unavailable in this prototype; explain the unsupported case instead of renaming a score to mm/cm. Model confidence is not biological verification.
4. Create `new_session`, then call `plan_fields` with the current `revision` and only the proposed fields. The backend rejects unsupported fields and never appends quality, visibility, cheek or eye questions. Render the returned current definitions for the user. Provide the `local_ui_url` as a clickable local link; it is a session capability, not an API credential. Do not share it elsewhere.
5. In that local UI, the user selects JPEG files, reviews sanitized previews, optionally enters individual/observation labels and side, and clicks **確認本輪**. A chat attachment is not automatically available to this server. Do not scan the user's filesystem, ask another tool to read arbitrary paths, upload files on their behalf, automate the confirmation checkbox for them, or accept consent inferred from elapsed time. An explicit QA request may exercise these controls with unmistakable synthetic mock fixtures only.
6. Call `session_status` before execution. Field, purpose, species, photo-selection or relation edits invalidate confirmation. A stale revision/hash must be reloaded and reconfirmed; never reuse an earlier confirmation or budget.
7. Only when the user has requested execution and the backend reports `confirmed`, call `execute_confirmed` with its exact `plan_sha256`. It cannot supply confirmation or change payment mode. Repeat calls are idempotent and do not retry failures. Use `cancel_session` if requested. Report that an already in-flight live request may still complete and remains reserved.
8. Read status/results once completion is expected. Direct the user to independent human review and CSV download in the same local page. Preserve unknowns, disagreements, hashes, individual → observation → image relations and separate human/model columns. User labels are not PIT-verified; do not merge unknown individuals or create a train/test split here.

## Exact dynamic field schema

Each field has `name` (unique lower snake_case ≤40), `definition` (bounded text specifying visible criteria), `type`, `unit` and `unknown_rule`.

| Type | Unit | Required rubric | Unknown rule |
|---|---|---|---|
| `choice` | `category` | `choices`: 2–12 objects with `value` and `description`, including a user-reviewed `unknown` category | `choice_unknown`: hidden/ambiguous/unsupported visual evidence selects unknown |
| `predicate` | `model_probability` | No choices or levels; define one observable condition | `human_review_required`: API returns a numeric model estimate, not a nullable observation; human unknown stays unknown |
| `score` | `ordinal_index` | `levels`: 2–7 objects with `label` and `description`, ordered low to high | `human_review_required`: probability-weighted index starting at 0; no physical-unit or calibrated-probability interpretation |

If the requested semantics require automatic unknown for a predicate/score, offer a `choice` formulation with explicit unknown and obtain agreement. Do not silently add a visibility gate or treat 0.5 as unknown. Backend validation is a restrictive prototype guard, not a general biological verifier.

## Payment and installation boundaries

The supplied manifest always starts **mock**. Any previous local experiment does not authorize a new live run or deployment. Do not edit the manifest, start live flags, install/enable the plugin, change host permissions, deploy, upload a plugin, commit or push merely to complete this workflow.

Future live work needs a separate user decision naming selected data and a new budget, current official Decisions documentation/pricing, and explicit operator startup flags documented in `../../README.md`. The only permitted endpoint/model is `/v1/decisions` / `gpt-6-luna`. Credentials belong only in separately approved secure backend configuration; never treat an existing ignored file as authorization. No automatic retries. Unknown spending remains reserved. When anything is unsupported or disconnected, report the actual limitation and preserve local work.
