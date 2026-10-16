// Execute the shipped inline script in a simulated DOM/host, not a browser or
// real ChatGPT. Only the success case reaches the Python fixture's loopback MCP.
import assert from 'node:assert/strict';
import http from 'node:http';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
input = '';
const script = config.html.match(/<script>([\s\S]*?)<\/script>/)[1];
const jpeg = Buffer.from(config.jpeg, 'base64');
const hostOrigin = 'https://synthetic-host.invalid';
let rpcBytes = 0, mcpUploads = 0, mcpPreviews = 0, receivedPreview;
const checks = [];

function mcp(message) {
  const body = JSON.stringify(message);
  const size = Buffer.byteLength(body);
  if (message.params.name === 'attach_photo_bytes') { rpcBytes = size; mcpUploads++; }
  else mcpPreviews++;
  return new Promise((resolve, reject) => {
    const request = http.request({hostname: '127.0.0.1', port: config.port, path: '/mcp', method: 'POST',
      headers: {'Host': 'phototraits.example', 'Content-Type': 'application/json',
        'Content-Length': size, 'Authorization': 'Bearer ' + config.token}}, response => {
      let data = '';
      response.on('data', chunk => { data += chunk; });
      response.on('end', () => {
        if (response.statusCode !== 200) return reject(new Error('Loopback MCP rejected request'));
        const reply = JSON.parse(data);
        if (message.params.name === 'preview_plan') receivedPreview = reply.result;
        resolve(reply);
      });
    });
    request.on('error', () => reject(new Error('Loopback MCP failed')));
    request.end(body);
  });
}

function makeHost(options = {}) {
  let handler, now = Date.now(), timerId = 0;
  const timers = new Map(), sent = [], failures = [];
  // Minimal DOM with explicit fake decoding. Real browser decode is a separate
  // local acceptance check; this harness controls late/error/timeout outcomes.
  function element(tag = 'div') {
    return {tagName: tag, disabled: true, hidden: true, value: '', files: [],
      children: [], listeners: {}, ownText: '', src: '', naturalWidth: 8, naturalHeight: 8,
      set textContent(value) { this.ownText = String(value); this.children = []; },
      get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); },
      set innerHTML(value) { throw new Error('Untrusted HTML must never be interpreted'); },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.ownText = ''; this.children = children; },
      removeAttribute(name) { this[name] = ''; },
      addEventListener(type, callback) { this.listeners[type] = callback; },
      dispatch(type) { return this.listeners[type](); },
      decode() {
        assert.ok(this.src.startsWith('data:image/jpeg;base64,'));
        if (options.decode === 'fail') return Promise.reject(new Error('Synthetic decode failed'));
        if (options.decode === 'wait') return new Promise((resolve, reject) => { this.finishDecode = resolve; this.failDecode = reject; });
        if (options.decode === 'wrong-size') this.naturalWidth = 9;
        return Promise.resolve();
      }
    };
  }
  const elements = Object.fromEntries(['photo', 'upload', 'clear', 'status', 'preview', 'preview-image',
    'preview-details', 'preview-fields'].map(id => [id, element(id === 'preview-image' ? 'img' : 'div')]));
  const parent = {postMessage(message, target) {
    message = structuredClone(message);
    sent.push({message, target});
    if (message.method === 'ui/initialize') {
      assert.equal(message.params.protocolVersion, '2026-01-26');
      assert.deepEqual(message.params.appCapabilities.availableDisplayModes, ['inline']);
      if (!options.noInit) queueMicrotask(() => receive({jsonrpc: '2.0', id: message.id, result: {
        protocolVersion: '2026-01-26', hostInfo: {name: 'synthetic-only', version: '1'},
        hostCapabilities: options.noTools ? {} : {serverTools: {}}, hostContext: {displayMode: 'inline'}}}));
    } else if (message.method === 'ui/notifications/initialized' && !options.manualResult) {
      queueMicrotask(() => receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: options.picker || config.picker}));
    } else if (message.method === 'tools/call') {
      const name = message.params.name;
      assert.ok(['attach_photo_bytes', 'preview_plan'].includes(name), 'No approval/analysis tool in UI');
      assert.deepEqual(Object.keys(message.params.arguments).sort(), name === 'attach_photo_bytes'
        ? ['jpeg_base64', 'revision', 'session_id'] : ['session_id']);
      if (options.reply) {
        const result = options.reply(name, message);
        if (result) queueMicrotask(() => receive({jsonrpc: '2.0', id: message.id, result}));
        return;
      }
      if (options.transport === 'loopback') mcp(message).then(receive).catch(error => failures.push(error));
      else if (options.transport !== 'timeout') queueMicrotask(() => receive({jsonrpc: '2.0', id: message.id,
        result: {isError: true, content: [{type: 'text', text: 'Synthetic rejection'}]}}));
    }
  }};
  function receive(data, source = parent, origin = hostOrigin) { handler({data, source, origin}); }
  const context = vm.createContext({
    document: {getElementById: id => elements[id], createElement: element},
    window: {parent, addEventListener: (type, callback) => { assert.equal(type, 'message'); handler = callback; }},
    Date: class extends Date { static now() { return now; } },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, when: now + delay}); return id; },
    clearTimeout: id => timers.delete(id),
    Uint8Array, crypto: webcrypto,
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    btoa: value => Buffer.from(value, 'binary').toString('base64')
  });
  // No network, credential, Node, filesystem or storage API is exposed to the UI.
  vm.runInContext(script, context, {timeout: 2000, filename: 'photo_picker.html:inline'});
  function select(file = {size: jpeg.length, type: 'image/jpeg', arrayBuffer: async () =>
    jpeg.buffer.slice(jpeg.byteOffset, jpeg.byteOffset + jpeg.length)}) {
    elements.photo.files = [file]; elements.photo.value = 'synthetic-only.jpg';
    elements.photo.dispatch('change');
    return file;
  }
  function advance(ms) {
    now += ms;
    for (const [id, timer] of [...timers]) if (timer.when <= now) { timers.delete(id); timer.callback(); }
  }
  function teardown() { receive({jsonrpc: '2.0', id: 'teardown', method: 'ui/resource-teardown', params: {}}); }
  return {elements, sent, receive, select, advance, teardown, failures,
    calls: name => sent.filter(item => item.message.method === 'tools/call' && item.message.params.name === name),
    uploads: () => sent.filter(item => item.message.method === 'tools/call' && item.message.params.name === 'attach_photo_bytes')};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
async function until(predicate) {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw new Error('Synthetic host timed out waiting for UI state');
}

// Authoritative tool result is required; input arguments and other windows are
// never enough to enable the picker. The matched handshake pins the origin.
const success = makeHost({transport: 'loopback', manualResult: true});
success.receive({jsonrpc: '2.0', id: 1, result: {}}, {});
await tick();
assert.equal(success.elements.photo.disabled, true);
success.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-input', params: config.picker});
success.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: config.picker}, {});
success.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: config.picker}, undefined, 'https://other.invalid');
assert.equal(success.elements.photo.disabled, true);
success.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: config.picker});
assert.equal(success.elements.photo.disabled, false);
let reads = 0;
success.select({size: 1000001, type: 'image/jpeg', arrayBuffer: () => { reads++; }});
assert.equal(success.elements.upload.disabled, true);
assert.equal(reads, 0);
success.select({size: 8, type: 'image/png', arrayBuffer: () => { reads++; }});
assert.equal(success.elements.upload.disabled, true);
success.select();
assert.equal(success.uploads().length, 0);
const firstClick = success.elements.upload.dispatch('click');
await success.elements.upload.dispatch('click');
await until(() => !success.elements.preview.hidden);
assert.match(success.elements.status.textContent, /預覽已就緒/);
await firstClick;
assert.equal(success.uploads().length, 1);
assert.equal(success.uploads()[0].message.params.arguments.jpeg_base64, config.jpeg);
assert.equal(success.elements.photo.value, '');
assert.equal(success.elements.upload.disabled, true);
assert.equal(success.sent[0].target, '*');
assert.ok(success.sent.slice(1).every(item => item.target === hostOrigin));
assert.deepEqual(success.failures, []);
assert.equal(success.calls('preview_plan').length, 1);
assert.ok(receivedPreview);
const cleanImage = receivedPreview.content.find(item => item.type === 'image').data;
assert.notEqual(cleanImage, config.jpeg, 'Preview must be sanitized server bytes, not local selection');
assert.equal(success.elements['preview-image'].src, 'data:image/jpeg;base64,' + cleanImage);
for (const field of config.field_types.slice(0, 2)) assertField(success, field);
assert.match(success.elements['preview-details'].textContent, new RegExp(receivedPreview.structuredContent.plan.plan_sha256));
success.teardown();
assertCleared(success);
checks.push('received_preview_and_complete_fields');
assert.equal(success.elements.photo.disabled, true);
assert.equal(success.sent.at(-1).message.id, 'teardown');
checks.push('authoritative_context_and_origin', 'size_before_read', 'one_explicit_upload', 'one_megabyte_loopback_roundtrip', 'teardown');

const cancelled = makeHost();
await tick(); cancelled.select(); cancelled.elements.clear.dispatch('click');
assert.equal(cancelled.elements.photo.value, '');
assert.equal(cancelled.elements.upload.disabled, true);
assert.equal(cancelled.uploads().length, 0);
cancelled.select();
cancelled.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-cancelled', params: {}});
assert.equal(cancelled.elements.photo.value, '');
assert.equal(cancelled.elements.photo.disabled, true);
checks.push('local_selection_clear_and_host_cancel');

const failure = makeHost();
await tick(); failure.select(); await failure.elements.upload.dispatch('click');
assert.equal(failure.uploads().length, 1);
assert.equal(failure.elements.photo.value, '');
assert.equal(failure.elements.photo.disabled, true);
await failure.elements.upload.dispatch('click');
assert.equal(failure.uploads().length, 1);
checks.push('rejection_without_retry');

const timeout = makeHost({transport: 'timeout'});
await tick(); timeout.select(); const waiting = timeout.elements.upload.dispatch('click');
await tick(); timeout.advance(60001); await waiting;
assert.equal(timeout.uploads().length, 1);
assert.equal(timeout.elements.photo.disabled, true);
assert.equal(timeout.elements.photo.value, '');
timeout.receive({jsonrpc: '2.0', id: timeout.uploads()[0].message.id, result: config.picker});
assert.equal(timeout.elements.photo.disabled, true);
checks.push('timeout_and_late_reply_without_retry');

const expiry = makeHost();
await tick(); expiry.select(); expiry.advance(1800001);
await expiry.elements.upload.dispatch('click');
assert.equal(expiry.uploads().length, 0);
assert.equal(expiry.elements.photo.value, '');
assert.equal(expiry.elements.photo.disabled, true);
expiry.teardown();
assert.equal(expiry.sent.at(-1).message.id, 'teardown');
checks.push('expiry_before_send');

const interruptedRead = makeHost();
await tick();
let finishRead;
interruptedRead.select({size: jpeg.length, type: 'image/jpeg', arrayBuffer: () => new Promise(resolve => { finishRead = resolve; })});
const reading = interruptedRead.elements.upload.dispatch('click');
interruptedRead.teardown();
finishRead(jpeg.buffer.slice(jpeg.byteOffset, jpeg.byteOffset + jpeg.length));
await reading;
assert.equal(interruptedRead.uploads().length, 0);
assert.equal(interruptedRead.elements.photo.value, '');
checks.push('teardown_during_read');

for (const options of [{noTools: true}, {noInit: true}]) {
  const unsupported = makeHost(options);
  await tick(); unsupported.advance(15001); await tick();
  assert.equal(unsupported.elements.photo.disabled, true);
  assert.equal(unsupported.uploads().length, 0);
}
checks.push('missing_host_capability_or_handshake');

function assertCleared(host) {
  assert.equal(host.elements.preview.hidden, true);
  assert.equal(host.elements['preview-image'].src, '');
  assert.equal(host.elements['preview-fields'].textContent, '');
}
function assertField(host, field) {
  const text = host.elements['preview-fields'].textContent;
  for (const key of ['name', 'definition', 'type', 'unit', 'unknown_rule']) assert.ok(text.includes(field[key]), key);
  for (const option of field.choices || field.levels || []) {
    assert.ok(text.includes(option.value || option.label));
    assert.ok(text.includes(option.description));
  }
}
const pickerWithPhoto = {structuredContent: {...config.picker.structuredContent,
  has_photo: true, revision: receivedPreview.structuredContent.plan.revision}};
const baseRevision = pickerWithPhoto.structuredContent.revision;
function previewHost(options = {}, mutate) {
  const preview = structuredClone(receivedPreview);
  if (mutate) mutate(preview);
  return makeHost({picker: pickerWithPhoto, reply: () => preview, ...options});
}
// Existing photos are rendered from the server, once; no upload, polling or consent.
const existing = previewHost();
await until(() => !existing.elements.preview.hidden);
assert.equal(existing.uploads().length, 0);
assert.equal(existing.calls('preview_plan').length, 1);
existing.select();
assertCleared(existing);
assert.match(existing.elements.status.textContent, /尚未|已選取/);
existing.elements.clear.dispatch('click');
assertCleared(existing);
assert.equal(existing.calls('preview_plan').length, 1);
checks.push('existing_photo_once_and_local_selection_invalidates_display');

// Ordered scores and literal text must be fully visible, never executable markup.
// This is a rendering fixture, not a newly signed server plan: the client only
// displays the server plan digest; it independently hashes the image bytes.
const score = previewHost({}, reply => {
  reply.structuredContent.plan.fields = [structuredClone(config.field_types[2])];
  reply.structuredContent.plan.fields[0].definition = '<img onerror=alert(1)> Literal definition.';
});
await until(() => !score.elements.preview.hidden);
assert.ok(score.elements['preview-fields'].textContent.includes('<img onerror=alert(1)> Literal definition.'));
assertField(score, {...config.field_types[2], definition: '<img onerror=alert(1)> Literal definition.'});
score.teardown();
checks.push('complete_score_levels_and_text_only_rendering');

const invalid = [
  reply => { reply.isError = true; },
  reply => { reply.structuredContent.plan.session_id = 'b'.repeat(32); },
  reply => { reply.structuredContent.plan.revision--; },
  reply => { reply.structuredContent.plan.revision++; },
  reply => { reply.structuredContent.plan.plan_sha256 = 'invalid'; },
  reply => { reply.structuredContent.plan.mode = 'live'; },
  reply => { reply.structuredContent.plan.status = 'running'; },
  reply => { reply.structuredContent.plan.confirmed = true; },
  reply => { reply.structuredContent.plan.retention.expires_at_unix = 0; },
  reply => { reply.structuredContent.plan.photos = []; },
  reply => { reply.structuredContent.plan.photos[0].sha256 = '0'.repeat(64); },
  reply => { reply.structuredContent.plan.photos[0].dimensions = [2049, 8]; },
  reply => { reply.structuredContent.plan.fields[0].type = 'unrecognized'; },
  reply => { reply.structuredContent.plan.fields[0].name = null; },
  reply => { reply.structuredContent.plan.fields[0].unit = 'mm'; },
  reply => { reply.structuredContent.plan.fields[0].unknown_rule = 'guess'; },
  reply => { reply.structuredContent.plan.fields[0].choices.pop(); },
  reply => { reply.structuredContent.plan.fields[0].choices[1].value = 'present'; },
  reply => { reply.structuredContent.plan.fields[0].choices[1].value = null; },
  reply => { delete reply.structuredContent.plan.fields[0].definition; },
  reply => { reply.structuredContent.plan.fields[0].unexpected = 'hidden semantics'; },
  reply => { reply.structuredContent.plan.fields[1].name = reply.structuredContent.plan.fields[0].name; },
  reply => { reply.structuredContent.plan.fields = [structuredClone(config.field_types[2])]; reply.structuredContent.plan.fields[0].levels = []; },
  reply => { reply.content = reply.content.filter(item => item.type !== 'image'); },
  reply => { reply.content.push(reply.content.find(item => item.type === 'image')); },
  reply => { reply.content.find(item => item.type === 'image').mimeType = 'image/png'; },
  reply => { reply.content.find(item => item.type === 'image').data = '!!!!'; },
  reply => { reply.content.find(item => item.type === 'image').data += '\n'; },
  reply => { reply.content.find(item => item.type === 'image').data = 'A'.repeat(1333340); }
];
for (const mutate of invalid) {
  const host = previewHost({}, mutate);
  await until(() => host.elements.status.textContent.includes('預覽失敗'));
  assertCleared(host);
  assert.equal(host.elements.photo.disabled, true);
  assert.equal(host.calls('preview_plan').length, 1);
}
checks.push('invalid_plan_image_and_unknown_field_fail_closed');

for (const decode of ['fail', 'wrong-size', 'wait']) {
  const host = previewHost({decode});
  if (decode === 'wait') {
    await until(() => host.elements['preview-image'].finishDecode);
    assertClearedExceptPendingImage(host);
    host.advance(15001);
  }
  await until(() => host.elements.status.textContent.includes('預覽失敗'));
  assertCleared(host);
  assert.equal(host.calls('preview_plan').length, 1);
}
function assertClearedExceptPendingImage(host) {
  assert.equal(host.elements.preview.hidden, true);
  assert.equal(host.elements['preview-fields'].textContent, '');
  assert.ok(!host.elements.status.textContent.includes('預覽已就緒'));
}
checks.push('decode_failure_timeout_and_dimensions');

// New plan notifications during a delayed preview/decode must not be overwritten.
for (const during of ['rpc', 'decode', 'ready']) {
  const host = previewHost(during === 'rpc' ? {reply: () => null} : {decode: during === 'decode' ? 'wait' : undefined});
  if (during === 'rpc') await until(() => host.calls('preview_plan').length === 1);
  else if (during === 'decode') await until(() => host.elements['preview-image'].finishDecode);
  else await until(() => !host.elements.preview.hidden);
  const changed = {...pickerWithPhoto.structuredContent, revision: baseRevision + 1};
  host.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: {structuredContent: changed}});
  assertCleared(host);
  assert.equal(host.elements.photo.disabled, true);
  if (during === 'rpc') host.receive({jsonrpc: '2.0', id: host.calls('preview_plan')[0].message.id, result: receivedPreview});
  if (during === 'decode') host.elements['preview-image'].finishDecode();
  await tick();
  assertCleared(host);
  assert.equal(host.calls('preview_plan').length, 1);
}
checks.push('stale_revision_and_late_decode');

// clear_session intentionally has no revision. Terminal notifications must
// invalidate the card in every phase, including unresolved RPC/decode work.
for (const during of ['rpc', 'decode', 'ready']) {
  for (const status of ['cleared', 'clear_pending', 'cancelled']) {
    const host = previewHost(during === 'rpc' ? {reply: () => null} : {decode: during === 'decode' ? 'wait' : undefined});
    if (during === 'rpc') await until(() => host.calls('preview_plan').length === 1);
    else if (during === 'decode') await until(() => host.elements['preview-image'].finishDecode);
    else await until(() => !host.elements.preview.hidden);
    host.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: {structuredContent: {
      session_id: pickerWithPhoto.structuredContent.session_id, status}}});
    assertCleared(host);
    assert.equal(host.elements.photo.disabled, true);
    if (during === 'rpc') host.receive({jsonrpc: '2.0', id: host.calls('preview_plan')[0].message.id, result: receivedPreview});
    if (during === 'decode') host.elements['preview-image'].finishDecode();
    await tick(); assertCleared(host);
  }
}
checks.push('terminal_notification_without_revision_purges_all_phases');

const changedHash = previewHost();
await until(() => !changedHash.elements.preview.hidden);
changedHash.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: {structuredContent: {
  session_id: pickerWithPhoto.structuredContent.session_id, revision: baseRevision, plan_sha256: '0'.repeat(64)}}});
assertCleared(changedHash);
assert.equal(changedHash.elements.photo.disabled, true);

const previewTimeout = previewHost({reply: () => null});
await until(() => previewTimeout.calls('preview_plan').length === 1);
previewTimeout.advance(30001);
await tick();
assert.match(previewTimeout.elements.status.textContent, /預覽失敗/);
previewTimeout.receive({jsonrpc: '2.0', id: previewTimeout.calls('preview_plan')[0].message.id, result: receivedPreview});
await tick();
assertCleared(previewTimeout);
assert.equal(previewTimeout.calls('preview_plan').length, 1);
checks.push('changed_plan_hash_and_preview_timeout');

// Expiry/cancellation/teardown while decoding must purge even late-resolving pixels.
for (const event of ['expiry', 'cancel', 'teardown']) {
  const host = previewHost({decode: 'wait'});
  await until(() => host.elements['preview-image'].finishDecode);
  if (event === 'expiry') host.advance(1800001);
  else if (event === 'cancel') host.receive({jsonrpc: '2.0', method: 'ui/notifications/tool-cancelled', params: {}});
  else host.teardown();
  host.elements['preview-image'].finishDecode();
  await tick();
  assertCleared(host);
  assert.equal(host.elements.photo.disabled, true);
}
const visibleExpiry = previewHost();
await until(() => !visibleExpiry.elements.preview.hidden);
visibleExpiry.advance(1800001);
assertCleared(visibleExpiry);
assert.match(visibleExpiry.elements.status.textContent, /到期/);
checks.push('preview_expiry_cancellation_teardown_purge');

// An accepted upload followed by an unavailable preview is distinct from an
// uncertain upload. Neither path approves anything or automatically retries.
const previewFailure = makeHost({reply: name => name === 'attach_photo_bytes'
  ? pickerWithPhoto : {isError: true}});
await tick(); previewFailure.select(); await previewFailure.elements.upload.dispatch('click');
assert.match(previewFailure.elements.status.textContent, /已收到.*預覽失敗/);
assert.equal(previewFailure.uploads().length, 1);
assert.equal(previewFailure.calls('preview_plan').length, 1);
assertCleared(previewFailure);
checks.push('received_but_preview_failed_no_retry_or_approval');

process.stdout.write(JSON.stringify({evidence_kind: 'SIMULATED_MCP_APPS_BRIDGE',
  actual_chatgpt_verified: false, jpeg_bytes: jpeg.length, rpc_bytes: rpcBytes,
  mcp_uploads: mcpUploads, mcp_previews: mcpPreviews, checks}));
