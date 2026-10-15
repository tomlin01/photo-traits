# Decisions and cumulative budget — 2026-10-14

This stage is local and synthetic only. No real photo or credential was read,
no model call was made, and no push, merge, deployment, account change or paid
infrastructure was performed. The running Render service remains authenticated
MOCK. **A production durable binding is still required; live startup is blocked.**

## Current official contract and prices

Checked on 2026-10-14 without an API key:

- [Decisions guide](https://developers.openai.com/api/docs/guides/decisions):
  `POST https://api.openai.com/v1/decisions`, `gpt-6-luna`, text/image input and
  named predicate, choice and score questions. US$0.10 per million input tokens;
  no output, cache-read or cache-write charges. Regional and long-context
  multipliers apply. No Responses fallback.
- [Model limits](https://developers.openai.com/api/docs/models/gpt-6-luna):
  maximum input 922,000 tokens, long-context input multiplier 2, regional premium
  10%. [Pricing](https://developers.openai.com/api/docs/pricing) separately covers
  processing tiers. Use the Decisions-specific price, not generative output rates.
- Official OpenAPI `POST /v1/decisions`, read through the documentation connector:
  request/response examples match the existing adapter. One resource reference
  URL returned 404; current official SDK types provided additional confirmation:
  [request](https://github.com/openai/openai-python/blob/main/src/openai/types/decision_create_params.py),
  [image](https://github.com/openai/openai-python/blob/main/src/openai/types/decision_input_image_param.py),
  [answers/usage](https://github.com/openai/openai-python/blob/main/src/openai/types/decision.py).
  These are dated checks of mutable sources, not added SDK dependencies.

Requests contain only `model`, `input`, `questions`: one sanitized JPEG data URL
with `detail: high`, fixed safety instructions and exactly the confirmed questions.
No attachment URL, filename, metadata, owner identity, source sex or measured
length enters the request. The fixed-origin transport retains verified TLS, no
proxy, no redirects, one attempt, safe errors and no automatic retries.

Decisions has no documented `max_output_tokens` parameter. The conservative
input rate is `0.10 × 2 × 1.10 = US$0.22 / million tokens`. The entire documented
input ceiling costs `922000 × 0.22 / 1000000 = US$0.20284`. Each call reserves
**US$0.25**, without guessing image tokens. This is a conservative reservation
estimate, not an expected photo price or a proven provider billing ceiling.
The US$5 ledger limit constrains modeled accounting; it is **not a hard guarantee
that actual provider charges cannot exceed US$5**. Prices, billable usage and durable
persistence still need independent verification before live activation. Existing
JPEG byte/dimension guards remain in force.

`checked_pricing()` refuses unknown/changed prices or limits, future dates and
evidence older than seven days. Updating the date alone is not a price review.
Account-specific access and actual usage/billing are **not verified** by public
docs or synthetic tests and must be checked at the separately authorized stage.

## One project ledger

`plugins/photo-traits-local/budget.py` adds one standard-library module and an
injectable `BudgetStore` contract. No frontend/tool can initialize or refill it.

- Fixed project identity, cumulative **US$5** accounting limit. New sessions,
  directories, restarts and authorization labels do not grant another US$5.
- Existing pinned ledger identity and explicit audited opening spend required.
  Missing state never defaults to zero. Provisioning is an explicit offline
  primitive, never part of startup, init, tool handling or request execution.
- Integer nano-USD; SQLite `BEGIN IMMEDIATE`, synchronous `EXTRA`, full filesystem
  sync, existing-file `mode=rw`, integrity and identity validation. A successful
  reservation acknowledgment occurs only after commit.
- Atomically reserve US$0.25 against opening spend + settled upper charges + all
  unresolved holds. Every attempt ID is permanently unique; even a settled attempt
  cannot be sent again. Store request and price digests, not photos or credentials.
- Complete, consistent usage settles at the conservative rate for all input tokens,
  without subtracting cache counters. Persist token count, upper cost and sanitized
  response checksum. Identical settlement is idempotent; conflicting evidence is
  rejected. Token-based upper charges are not independently verified invoices.
- Timeouts, HTTP errors, lost acknowledgments, crashes, invalid replies and storage
  outages retain committed holds. No expiry, cancellation refund, automatic retry,
  refill or reset. An uncertain reservation commit prevents sending.

Safe response evidence is written before settlement. Failed settlement keeps the
hold and safe response, while exported candidates remain unknown. Reconciliation
requires evidence and a separately reviewed operator step. Photo/session cleanup
never refunds budget. Legacy batch live execution is disabled because its per-run
accounting could bypass the total. The old boolean durability bypass is removed.

## Deployment decision still required

Read-only Render inspection on 2026-10-14 found one project with one resource:
`photo-traits-mock`, Free Python service, deployed at `5e8d239`. No database was
listed. Its Disk page requires paid compute. The smallest paid Compute option
was **US$7/month**, plus disk charges. No plan was selected/saved and no credential
or environment-variable page was opened.

The simplest option for this small single-instance app is paid Render compute
with a persistent disk holding **only the ledger**, avoiding another database,
client dependency and database credential. Disk price, taxes and other charges
still require a current provider quote; the disk price was not established here.
Hosting is a separate recurring expense, not authorized by the US$5 model budget.
[Render disks](https://render.com/docs/disks) preserve only files beneath the
actual mount, support one instance and prevent zero-downtime deploys. The current
[Free filesystem](https://render.com/docs/free) cannot enforce this budget.

`SQLiteBudgetStore.ensure_ready(for_live=True)` deliberately raises. The reference
survives local process restarts, not loss of an ephemeral disk. There is no boolean
or environment switch to assert durability. A future reviewed binding must verify
the provisioned mount, stable ledger path and separately pinned ledger ID, reject
missing/replaced storage, and test a real restart/redeploy. It must never initialize
state at startup. All processes share one ledger. A separate transactional
backend is an alternative only if multiple instances become necessary; none has
been provisioned or connected.

Keep photos and sessions in the dedicated [temporary session root](TRANSIENT_SESSIONS.md),
separate from the ledger. Lost ephemeral sessions are not automatically resumed;
committed holds remain. Restoring an old valid database snapshot is not detectable
by this local reference. Backup restore, migration, loss or suspected rollback
requires disabling live execution and reconciling charges before resuming. Spend
made outside this ledger cannot be bounded by this application.

## Verification

Use an existing Python 3.12+ environment. This machine's default `python3` was
3.11; the existing `.local/workflow-venv` is Python 3.13.7. With that interpreter:

```bash
python scripts/dev.py init
python scripts/dev.py check
python scripts/accept_workflow.py
```

HTTP tests use local loopback servers, which need local-listener permission in
restricted sandboxes. Fixtures and transports are synthetic; no real keys, photos
or paid endpoints are used. Tests cover thread/process concurrency, committed and
uncommitted process crashes, reopen, exhaustion, duplicate attempts, missing or
corrupt storage, lock timeout, stale/unknown prices, lost commit acknowledgments,
transport errors, settlement failure and separate CSV human corrections.

Positive fixtures use `mode: synthetic`, `model_called: false` and
`SYNTHETIC_NOT_MODEL_OUTPUT`. They cannot fall through to the production transport
or key loader. Raw allowlisted evidence, candidates and human corrections stay
separate. Refusals and unsupported/ambiguous traits remain null/unknown; no
physical size is inferred without calibration. This does not establish model
quality or biological accuracy. Existing metadata tests are preserved; absent
pinned metadata is separately SKIP, without downloading it.

Owner RS256 JWT, scopes, the two exact approved attachment hosts, HTTPS/public-IP
pinning, no redirects, image sanitization, one-photo/two-field limits, fresh human
confirmation and photo cleanup remain. Auth, SSRF and locked startup regressions
run as before. The budget-only stage left `render.yaml`, `workflow_http.py` and
attachment code unchanged. The subsequent local cleanup stage updates session
lifecycle and test-fixture storage; the Render template and live-startup gates
remain unchanged.

## Exact deferred activation steps

1. Approve the persistence option and its separate recurring hosting cost before
   provisioning it. Model-budget approval does not authorize hosting charges.
2. Audit all new-project opening spend, provision the one ledger once, pin its
   identity outside session storage, and implement the durable binding above.
   Test actual restart/deployment persistence with synthetic holds. Never create
   a fresh ledger to recover missing budget state.
3. Separately approve push/deployment and explicit live-startup wiring. Preserve
   owner JWT, approved hosts, short photo retention, Auto-Deploy OFF and PR
   Previews OFF. The locked template must not silently replace private settings.
4. Recheck official Decisions prices, limits, data-retention terms and account
   access. Only after separate credential approval use the official secure backend
   key workflow. No keys in chat/source/frontend. Key presence was not inspected.
5. Reconfirm the exact photo, single-species scope, one or two visible fields and
   rubrics, OpenAI Decisions / `gpt-6-luna` transmission, **one attempt**, US$0.25
   per-call reservation estimate and the remaining cumulative US$5 accounting
   balance. Explain that these are not provider-enforced billing caps. Show the
   sanitized preview. No additional smoke test or automatic retry is authorized.
6. Make only the separately approved call, reconcile usage, record human review
   and export CSV. Missing usage/timeout retains the hold and stops for an audit.

## Local result recorded 2026-10-14

Python 3.13.7: repeatable init passed; `scripts/accept_workflow.py` reran init and
`dev.py check`, with **141 synthetic tests passed, no test skips**, plus generated
MOCK lifecycle/CSV acceptance. Real pinned metadata was separately **SKIP** because
it was absent; no download occurred. `git diff --check` passed. The original
worktree remains on `codex/local-baseline` with its prior changes preserved;
implementation edits are in the sanitized checkout on `codex/decisions-live`.
At that stage, no commit, push, merge or deployment was made. Its acceptance report
and fixture CSV are in ignored `.local/workflow-acceptance-*` and
`data/derived/workflow-acceptance-*`. The later cleanup acceptance keeps its synthetic
CSV in the ignored evidence directory and explicitly clears the temporary session.
