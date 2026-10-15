// Execute the shipped inline script in a simulated DOM/host, not a browser or
// real ChatGPT. Only the success case reaches the Python fixture's loopback MCP.
import assert from 'node:assert/strict';
import http from 'node:http';
import vm from 'node:vm';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const config = JSON.parse(input);
input = '';
const script = config.html.match(/<script>([\s\S]*?)<\/script>/)[1];
const jpeg = Buffer.from(config.jpeg, 'base64');
const hostOrigin = 'https://synthetic-host.invalid';
let rpcBytes = 0, mcpUploads = 0;
const checks = [];

function mcp(message) {
  const body = JSON.stringify(message);
  rpcBytes = Buffer.byteLength(body);
  mcpUploads++;
  return new Promise((resolve, reject) => {
    const request = http.request({hostname: '127.0.0.1', port: config.port, path: '/mcp', method: 'POST',
      headers: {'Host': 'phototraits.example', 'Content-Type': 'application/json',
        'Content-Length': rpcBytes, 'Authorization': 'Bearer ' + config.token}}, response => {
      let data = '';
      response.on('data', chunk => { data += chunk; });
      response.on('end', () => {
        if (response.statusCode !== 200) return reject(new Error('Loopback MCP rejected request'));
        resolve(JSON.parse(data));
      });
    });
    request.on('error', () => reject(new Error('Loopback MCP failed')));
    request.end(body);
  });
}

function makeHost(options = {}) {
  let handler, now = Date.now(), timerId = 0;
  const timers = new Map(), sent = [], failures = [];
  const elements = Object.fromEntries(['photo', 'upload', 'clear', 'status'].map(id => [id, {
    disabled: true, textContent: '', value: '', files: [], listeners: {},
    addEventListener(type, callback) { this.listeners[type] = callback; },
    dispatch(type) { return this.listeners[type](); }
  }]));
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
      queueMicrotask(() => receive({jsonrpc: '2.0', method: 'ui/notifications/tool-result', params: config.picker}));
    } else if (message.method === 'tools/call') {
      assert.equal(message.params.name, 'attach_photo_bytes');
      assert.deepEqual(Object.keys(message.params.arguments).sort(), ['jpeg_base64', 'revision', 'session_id']);
      if (options.transport === 'loopback') mcp(message).then(receive).catch(error => failures.push(error));
      else if (options.transport !== 'timeout') queueMicrotask(() => receive({jsonrpc: '2.0', id: message.id,
        result: {isError: true, content: [{type: 'text', text: 'Synthetic rejection'}]}}));
    }
  }};
  function receive(data, source = parent, origin = hostOrigin) { handler({data, source, origin}); }
  const context = vm.createContext({
    document: {getElementById: id => elements[id]},
    window: {parent, addEventListener: (type, callback) => { assert.equal(type, 'message'); handler = callback; }},
    Date: class extends Date { static now() { return now; } },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, when: now + delay}); return id; },
    clearTimeout: id => timers.delete(id),
    Uint8Array, btoa: value => Buffer.from(value, 'binary').toString('base64')
  });
  // No network, credential, Node, filesystem or storage API is exposed to the UI.
  vm.runInContext(script, context, {timeout: 2000});
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
    uploads: () => sent.filter(item => item.message.method === 'tools/call')};
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
await until(() => success.elements.status.textContent.includes('照片已暫存'));
await firstClick;
assert.equal(success.uploads().length, 1);
assert.equal(success.uploads()[0].message.params.arguments.jpeg_base64, config.jpeg);
assert.equal(success.elements.photo.value, '');
assert.equal(success.elements.upload.disabled, true);
assert.equal(success.sent[0].target, '*');
assert.ok(success.sent.slice(1).every(item => item.target === hostOrigin));
assert.deepEqual(success.failures, []);
success.teardown();
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

process.stdout.write(JSON.stringify({evidence_kind: 'SIMULATED_MCP_APPS_BRIDGE',
  actual_chatgpt_verified: false, jpeg_bytes: jpeg.length, rpc_bytes: rpcBytes,
  mcp_uploads: mcpUploads, checks}));
