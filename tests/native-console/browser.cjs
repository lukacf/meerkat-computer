// Real installed Computer package -> real broker/worker -> native Console.
// The model is scripted; no Console events, tool results or screenshots are forged.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');

let input, consoleRoot, chromium, createSandboxServer;
const safeEnv = Object.fromEntries(['HOME', 'PATH', 'TMPDIR', 'LANG', 'PLAYWRIGHT_BROWSERS_PATH']
  .filter(key => process.env[key]).map(key => [key, process.env[key]]));
const processes = [];
let browser, page, proxy, sandbox, backendUrl, shutdownWorker;
const requests = [];
const pageErrors = [];
const childLogs = [];

const VIEW_IDS = ['computer-navigate', 'computer-observe'];
const POLL_DEADLINE_MS = 5000;
const POLL_GAP_MS = 8000;

function previewCalls(records, heldAt, cutoffAt = Infinity) {
  return records.filter(record => {
    if (record.path !== '/console/mcp-apps/call-tool' ||
        record.startedAt < heldAt || record.startedAt > cutoffAt) return false;
    // A request can be observed between its headers and its JSON body arriving.
    try { return JSON.parse(record.body).name === 'computer_preview'; }
    catch { return false; }
  });
}

function validatePolling(records, { heldAt, checkedAt, cutoffAt = checkedAt,
    settled = false, requireAfter60 = false }) {
  const calls = previewCalls(records, heldAt, cutoffAt);
  for (const call of calls) {
    assert(Number.isFinite(call.startedAt), 'poll start timestamp is invalid');
    if (call.finishedAt === null) {
      assert(!settled, 'poll response is incomplete');
      assert(checkedAt - call.startedAt < POLL_DEADLINE_MS, 'poll response exceeded deadline');
      continue;
    }
    assert(Number.isFinite(call.finishedAt), 'poll completion timestamp is invalid');
    assert.equal(call.status, 200, 'poll response must succeed');
    const latency = call.finishedAt - call.startedAt;
    assert(latency >= 0 && latency < POLL_DEADLINE_MS, 'poll latency must be nonnegative and bounded');
    assert(call.finishedAt <= checkedAt, 'poll completion cannot be in the future');
  }
  const completions = calls.filter(call => Number.isFinite(call.finishedAt));
  for (const id of VIEW_IDS) {
    const view = completions.filter(call => JSON.parse(call.body).toolCallId === id)
      .sort((a, b) => a.finishedAt - b.finishedAt);
    if (settled) assert(view.length >= 2, `view ${id} needs at least two completed polls`);
    let previous = heldAt;
    for (const call of view) {
      assert(call.finishedAt - previous < POLL_GAP_MS, `view ${id} stopped completing polls`);
      previous = call.finishedAt;
    }
    assert(checkedAt - previous < POLL_GAP_MS, `view ${id} stopped completing polls`);
    if (requireAfter60) assert(view.some(call => call.startedAt > heldAt + 60000 &&
      call.finishedAt > heldAt + 60000), `view ${id} needs a completed fresh poll after sixty seconds`);
  }
  return completions;
}

function appendLog(log, channel, chunk) {
  const text = chunk.toString();
  log[channel] = (log[channel] + text).slice(-1000000);
  // Flush while the process runs as well as during teardown, so a parent timeout
  // still leaves child diagnostics. Each log has an ordered append queue.
  log.pending = log.pending.then(() => fs.appendFile(log.path, text, { mode: 0o600 }))
    .catch(error => { log.writeError = error.message; });
}

async function persistEvidence({ output, records, errors, logs, failure,
    cleanupErrors = [], result, screenshotPage }) {
  const evidence = { failure: failure ? { message: failure.message, stack: failure.stack } : null,
    cleanupErrors, screenshot: null };
  if (failure && screenshotPage) {
    try {
      await screenshotPage.screenshot({ path: path.join(output, 'failure.png'),
        fullPage: true, timeout: 5000 });
      evidence.screenshot = { path: 'failure.png' };
    } catch (error) { evidence.screenshot = { error: error.message }; }
  }
  await Promise.all(logs.map(log => log.pending));
  for (const log of logs) if (log.writeError) {
    const message = `log ${log.marker}: ${log.writeError}`;
    if (!cleanupErrors.includes(message)) cleanupErrors.push(message);
  }
  const write = (name, value) => fs.writeFile(path.join(output, name),
    JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  await Promise.all([
    write('requests.json', records),
    write('page-errors.json', errors),
    write('processes.json', logs.map(log => ({ marker: log.marker, pid: log.child.pid,
      exitCode: log.child.exitCode, signal: log.child.signalCode,
      log: path.basename(log.path), writeError: log.writeError || null }))),
  ]);
  if (failure || cleanupErrors.length) {
    // Preserve a screenshot captured before the browser was closed when the final
    // teardown flush has no live page left to capture.
    if (!screenshotPage) {
      try { evidence.screenshot = JSON.parse(await fs.readFile(path.join(output, 'failure.json'))).screenshot; }
      catch { /* This can be a failure first discovered during cleanup. */ }
    }
    await write('failure.json', evidence);
    await write('result.json', { passed: false, ...evidence });
  } else if (result) await write('result.json', result);
}

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject); server.listen(0, '127.0.0.1', resolve);
  });
  return `http://127.0.0.1:${server.address().port}`;
}
async function eventually(check, label, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const result = await check();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${label}`);
}
async function launch(binary, env, marker) {
  const child = spawn(binary, [], { cwd: input.runtimeState,
    env: { ...safeEnv, ...env }, stdio: ['ignore', 'pipe', 'pipe'] });
  processes.push(child);
  const log = { marker, child, stdout: '', stderr: '', pending: Promise.resolve(),
    path: path.join(input.output, marker + '.log') };
  childLogs.push(log);
  child.stdout.on('data', chunk => appendLog(log, 'stdout', chunk));
  child.stderr.on('data', chunk => appendLog(log, 'stderr', chunk));
  child.on('error', error => appendLog(log, 'stderr', error.message));
  return eventually(() => {
    const line = log.stdout.split('\n').find(line => line.startsWith(marker + ' '));
    if (line) return JSON.parse(line.slice(marker.length + 1));
    if (child.exitCode !== null || child.signalCode !== null)
      throw new Error(`Fixture ${marker} exited before readiness`);
    return null;
  }, marker, 60000);
}
async function json(base, route, body, headers = {}) {
  const response = await fetch(base + route, {
    signal: AbortSignal.timeout(5000),
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  assert.equal(response.status, 200, `Unexpected HTTP status on ${route}`);
  const value = await response.json();
  assert(!value.error, `RPC ${body?.method || route}: ${JSON.stringify(value.error)}`);
  return value;
}
async function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  await new Promise(resolve => {
    const timer = setTimeout(() => { child.kill('SIGKILL'); resolve(); }, 3000);
    child.once('exit', () => { clearTimeout(timer); resolve(); });
  });
}
async function close(server) {
  server?.closeAllConnections();
  if (server?.listening) await new Promise(resolve => server.close(resolve));
}

async function main() {
  const brokerState = path.join(input.runtimeState, 'computer');
  await fs.mkdir(brokerState, { recursive: true });
  const broker = await launch(input.brokerBinary, {
    COMPUTER_FIXTURE_STATE: brokerState,
    COMPUTER_FIXTURE_PYTHON: input.python,
  }, 'COMPUTER_FIXTURE_READY');
  const operator = JSON.parse(await fs.readFile(path.join(brokerState, 'operator.json'), 'utf8'));
  shutdownWorker = async () => {
    const response = await fetch(`${broker.url}/v1/computers/${broker.computer_id}/stop`, {
      method: 'POST', headers: { authorization: `Bearer ${operator.token}` },
      signal: AbortSignal.timeout(15000),
    });
    assert.equal(response.status, 200, 'fixture worker must stop through the broker');
  };
  const binding = JSON.parse(await fs.readFile(broker.binding_file, 'utf8'));
  const controlHeaders = { authorization: `Bearer ${binding.token}` };
  const assets = new Map(await Promise.all([
    ['/console', 'index.html', 'text/html'],
    ['/console/assets/console-app.js', 'console-app.js', 'text/javascript'],
    ['/console/assets/console-app.css', 'console-app.css', 'text/css'],
  ].map(async ([url, file, type]) => [url, {
    type, bytes: await fs.readFile(path.join(consoleRoot, 'dist', file)),
  }])));
  proxy = http.createServer((req, res) => {
    const route = new URL(req.url, 'http://localhost').pathname;
    const asset = assets.get(route === '/console/' ? '/console' : route);
    if (asset && req.method === 'GET') {
      res.writeHead(200, { 'content-type': asset.type }); res.end(asset.bytes); return;
    }
    if (!backendUrl) { res.writeHead(503); res.end(); return; }
    const target = new URL(req.url, backendUrl);
    const record = { method: req.method, path: target.pathname, body: '', status: null,
      startedAt: Date.now(), finishedAt: null };
    requests.push(record);
    req.on('data', chunk => { if (record.body.length < 65536) record.body += chunk; });
    const upstream = http.request(target, { method: req.method, headers: { ...req.headers, host: target.host } }, response => {
      record.status = response.statusCode;
      if (response.statusCode >= 400) {
        record.errorBody = '';
        response.on('data', chunk => {
          if (record.errorBody.length < 4096) record.errorBody += chunk;
        });
      }
      response.on('end', () => { record.finishedAt = Date.now(); });
      res.writeHead(response.statusCode, response.headers);
      response.on('error', error => res.destroy(error));
      res.on('close', () => response.destroy());
      response.pipe(res);
    });
    upstream.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end(); });
    req.pipe(upstream);
  });
  const base = await listen(proxy);
  sandbox = createSandboxServer({ allowedHostOrigins: [base] });
  const sandboxUrl = `${await listen(sandbox)}/sandbox.html`;
  const consumer = await launch(input.consumerBinary, {
    COMPUTER_CONSUMER_STATE: path.join(input.runtimeState, 'consumer'),
    COMPUTER_FIXTURE_PYTHON: input.python,
    COMPUTER_BROKER_URL: broker.url,
    COMPUTER_BINDING_FILE: broker.binding_file,
    COMPUTER_SANDBOX_URL: sandboxUrl,
  }, 'COMPUTER_CONSUMER_READY');
  backendUrl = consumer.url;
  await eventually(async () => {
    const value = await json(base, '/console/rpc', {
      jsonrpc: '2.0', id: 1, method: 'meta/wait_ready', params: { timeout_ms: 1000 },
    });
    return value.result?.timeout === false && value.result.ready?.length === 1;
  }, 'native identity ready', 60000);
  const denied = await fetch(base + '/__acceptance/rpc', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 0, method: 'meta/retire',
      params: { identity: 'router:main' } }), signal: AbortSignal.timeout(5000),
  });
  assert.equal(denied.status, 404, 'fixture must not expose the general runtime RPC dispatcher');
  // These narrow fixture reads use the released owning identity runtime.
  // Console still owns every application resolution, tool call and resource read.
  const baseline = await json(base, '/__acceptance/completion');
  assert(baseline?.completion_cursor, 'native completion cursor is available');
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1360, height: 1600 } });
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(base + '/console', { waitUntil: 'domcontentloaded' });
  await page.getByTestId('sidebar-agent:router:main').click();
  await page.getByTestId('chat-composer:router:main').fill('[computer-native] Inspect the computer.');
  const sentAt = Date.now();
  await page.locator('button:has-text("Send"):visible, .cc-composer__send-btn:visible').first().click();
  const apps = [0, 1].map(index => page.frameLocator('iframe[title="Interactive tool result"]')
    .nth(index).frameLocator('iframe'));
  for (const app of apps) await app.locator('#image').waitFor({ state: 'visible', timeout: 30000 });
  const firstRenderMs = Date.now() - sentAt;
  const initial = await eventually(async () => {
    const state = await json(base, '/__acceptance/state'); return state.held ? state : null;
  }, 'model held after real observation');
  const saved = await json(broker.url, '/v1/preview', undefined, controlHeaders);
  assert.equal(saved.status, 'available');
  for (const app of apps) {
    await eventually(async () => (await app.locator('#image').getAttribute('src')) ===
      `data:image/png;base64,${saved.frame.data}`, 'saved screenshot reused');
    assert((await app.locator('#age').textContent()).includes('Last agent screenshot'));
  }
  const heldAt = Date.now();
  await page.screenshot({ path: path.join(input.output, 'native-preview.png'), fullPage: true });
  const refreshAt = Date.now();
  await apps[0].getByRole('button', { name: 'Refresh' }).click();
  await eventually(async () => !(await apps[0].getByRole('button', { name: 'Refresh' }).isDisabled()), 'refresh during model turn', 5000);
  const refreshMs = Date.now() - refreshAt;
  // The installed package has two independent real pollers. Cross the host's
  // action timeout when requested; default keeps local smoke inexpensive.
  while (Date.now() - heldAt < input.pollMs) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    const current = await json(base, '/__acceptance/state');
    assert.equal(current.held, true);
    assert.deepEqual(current.requests, initial.requests, 'App polls changed model requests');
    for (const app of apps) assert(!(await app.locator('#image').isHidden()), 'preview stopped polling');
    validatePolling(requests, { heldAt, checkedAt: Date.now() });
  }
  // Freeze the already-started request set and drain it while the model remains
  // held. Later automatic polls cannot hide an unfinished response in this set.
  const cutoffAt = Date.now();
  await eventually(() => {
    validatePolling(requests, { heldAt, cutoffAt, checkedAt: Date.now() });
    return previewCalls(requests, heldAt, cutoffAt).every(call => Number.isFinite(call.finishedAt));
  }, 'all admitted polls complete before model release', POLL_DEADLINE_MS);
  const pollCalls = validatePolling(requests, { heldAt, cutoffAt, checkedAt: Date.now(),
    settled: true, requireAfter60: input.pollMs > 60000 });
  const beforeRelease = await json(base, '/__acceptance/state');
  assert.equal(beforeRelease.held, true);
  assert.deepEqual(beforeRelease.requests, initial.requests);
  const heldMs = Date.now() - heldAt;
  const resolves = requests.filter(r => r.path === '/console/mcp-apps/resolve' && r.status === 200);
  assert(resolves.some(r => JSON.parse(r.body).toolCallId === 'computer-navigate'));
  assert(resolves.some(r => JSON.parse(r.body).toolCallId === 'computer-observe'));
  assert(resolves.every(r => JSON.parse(r.body).identity === 'router:main'));
  await json(base, '/__acceptance/release', {});
  await page.locator('[data-testid^="chat-turn:router:main:"] .cc-markdown-document')
    .getByText('Computer acceptance complete.', { exact: true }).waitFor({ timeout: 15000 });
  // Final text is streamed before native run finalization. Wait for the native
  // completion cursor and committed invocation access, not an arbitrary delay.
  const completed = await eventually(async () => {
    const value = await json(base, '/__acceptance/completion', baseline.completion_cursor);
    if (value?.outcome === 'timed_out') return null;
    assert.equal(value?.outcome, 'completed', 'native run completed successfully');
    assert.equal(value.completion_cursor.epoch, baseline.completion_cursor.epoch);
    assert(value.completion_cursor.turns > baseline.completion_cursor.turns);
    return value;
  }, 'native run completion cursor', 15000);
  await fs.writeFile(path.join(input.output, 'completion.json'), JSON.stringify({
    before: baseline, after: completed,
  }, null, 2) + '\n');
  for (const locator of resolves.map(row => JSON.parse(row.body))) {
    await eventually(async () => (await fetch(base + '/console/mcp-apps/resolve', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(locator), signal: AbortSignal.timeout(5000),
    })).status === 200, 'committed native invocation is resolvable', 15000);
  }
  await page.reload({ waitUntil: 'domcontentloaded' });
  for (const app of apps) await app.locator('#image').waitFor({ state: 'visible', timeout: 15000 });
  await page.getByTestId('chat-composer:router:main').fill('Confirm the prior result.');
  await page.locator('button:has-text("Send"):visible, .cc-composer__send-btn:visible').first().click();
  const after = await eventually(async () => {
    const value = await json(base, '/__acceptance/state');
    return value.requests.length > initial.requests.length ? value : null;
  }, 'fresh model request after reload');
  const modelWire = JSON.stringify(after.requests);
  for (const marker of ['meerkat-computer/preview', 'Computer preview updated.'])
    assert(!modelWire.includes(marker), `${marker} leaked into model requests`);
  assert(!after.requests.some(r => r.tools?.some(t => t.name === 'computer_preview')));
  const finalPreview = await json(broker.url, '/v1/preview', undefined, controlHeaders);
  assert.equal(finalPreview.frame.observation_id, saved.frame.observation_id,
    'reload/polling must not take new screenshots or replay actions');
  await json(broker.url, '/v1/lease/release', {}, controlHeaders);
  for (const app of apps) await app.locator('#image').waitFor({ state: 'hidden', timeout: 6000 });
  assert.equal(pageErrors.length, 0, 'browser page errors');
  const result = { passed: true, model: 'scripted choices, native execution',
    package: input.package, isolation: 'local process fixture; not VM evidence',
    views: 2, firstRenderMs, refreshMs, pollingHeldMs: heldMs,
    polls: pollCalls.length, maxPollMs: Math.max(...pollCalls.map(r => r.finishedAt - r.startedAt)),
    reloadWithoutReplay: true, revokedBindingClearsPreview: true,
    privatePreviewAbsentFromModel: true, modelRequests: after.requests.length,
    metaRelease: input.metaRelease, metaRevision: input.metaRevision,
    coreRevision: input.coreRevision, coreVersion: input.coreVersion };
  return result;
}
async function run() {
  let failure, result;
  const cleanupErrors = [];
  const persist = (screenshotPage, includeResult = true) => input && persistEvidence({ output: input.output,
    records: requests, errors: pageErrors, logs: childLogs, failure, cleanupErrors,
    result: includeResult ? result : undefined, screenshotPage });
  try {
    input = JSON.parse(process.argv[2]);
    consoleRoot = path.join(input.metaSource, 'console');
    ({ chromium } = require(path.join(consoleRoot, 'node_modules/playwright')));
    ({ createSandboxServer } = require(path.join(consoleRoot, 'mcp-apps-sandbox.cjs')));
    result = await main();
  } catch (error) {
    failure = error;
    console.error(error.message);
    process.exitCode = 1;
  } finally {
    // Capture the failed UI before closing it and persist partial network evidence
    // before attempting cleanup. One failed cleanup must not skip later steps.
    try { await persist(failure ? page : undefined, false); }
    catch (error) { cleanupErrors.push(`initial evidence: ${error.message}`); }
    for (const [name, cleanup] of [
      ['browser', () => browser?.close()],
      ['worker', () => shutdownWorker?.()],
      ...processes.reverse().map(child => [`child ${child.pid}`, () => stop(child)]),
      ['proxy', () => close(proxy)], ['sandbox', () => close(sandbox)],
    ]) {
      try { await cleanup(); }
      catch (error) { cleanupErrors.push(`${name}: ${error.message}`); }
    }
    if (cleanupErrors.length) process.exitCode = 1;
    try { await persist(); }
    catch (error) { console.error(`Final evidence could not be saved: ${error.message}`); process.exitCode = 1; }
  }
  if (!process.exitCode && result) console.log(JSON.stringify(result));
}

// Pure contract checks run without native binaries, browser packages or credentials.
async function selfTest() {
  const call = (id, startedAt, finishedAt = startedAt + 100) => ({
    path: '/console/mcp-apps/call-tool',
    body: JSON.stringify({ name: 'computer_preview', toolCallId: id }),
    startedAt, finishedAt, status: 200,
  });
  const healthy = [];
  for (let at = 1000; at <= 65000; at += 2000)
    for (const id of VIEW_IDS) healthy.push(call(id, at));
  const options = { heldAt: 1000, checkedAt: 67000, settled: true, requireAfter60: true };
  assert.equal(validatePolling(healthy, options).length, 66);
  assert.throws(() => validatePolling([call(VIEW_IDS[0], 1000, null)],
    { heldAt: 1000, checkedAt: 2000, settled: true }), /incomplete/);
  assert.throws(() => validatePolling([call(VIEW_IDS[0], 1000, 900)],
    { heldAt: 1000, checkedAt: 2000 }), /nonnegative/);
  assert.throws(() => validatePolling([call(VIEW_IDS[0], 1000, 6000)],
    { heldAt: 1000, checkedAt: 7000 }), /bounded/);
  assert.throws(() => validatePolling([call(VIEW_IDS[0], 1000, null)],
    { heldAt: 1000, checkedAt: 6000 }), /deadline/);
  assert.throws(() => validatePolling(healthy.filter(row => row.startedAt < 6000), options), /stopped/);
  assert.throws(() => validatePolling(healthy.filter(row => row.startedAt <= 57000),
    { ...options, checkedAt: 62000 }), /after sixty/);
  assert.throws(() => validatePolling(healthy.filter(row => row.startedAt < 15000 ||
    row.startedAt > 30000), options), /stopped/);
  assert.throws(() => validatePolling(healthy.filter(row =>
    JSON.parse(row.body).toolCallId === VIEW_IDS[0]), options), /at least two/);
  assert.throws(() => validatePolling(healthy.map((row, index) => index ? row :
    { ...row, status: 500 }), options), /must succeed/);
  assert.equal(validatePolling([], { heldAt: 1000, checkedAt: 2000 }).length, 0);

  const output = await fs.mkdtemp(path.join(require('node:os').tmpdir(), 'computer-evidence-unit-'));
  try {
    const log = { marker: 'fixture', child: { pid: 123, exitCode: null, signalCode: null },
      stdout: '', stderr: '', pending: Promise.resolve(), path: path.join(output, 'fixture.log') };
    appendLog(log, 'stdout', 'READY\n');
    await log.pending;
    appendLog(log, 'stderr', 'post-readiness failure\n');
    const records = [{ path: '/console/mcp-apps/call-tool', finishedAt: null }];
    const errors = ['fixture page error'];
    const failure = new Error('fixture failure');
    const screenshotPage = { screenshot: async options => {
      assert.equal(options.timeout, 5000);
      await fs.writeFile(options.path, 'unit screenshot fixture');
    } };
    const read = async name => JSON.parse(await fs.readFile(path.join(output, name)));
    await persistEvidence({ output, records, errors, logs: [log], failure, screenshotPage });
    assert((await fs.readFile(log.path, 'utf8')).includes('post-readiness failure'));
    assert.deepEqual(await read('requests.json'), records);
    assert.deepEqual(await read('page-errors.json'), errors);
    assert.equal((await read('result.json')).passed, false);
    assert.deepEqual((await read('failure.json')).screenshot, { path: 'failure.png' });
    assert.equal((await fs.stat(path.join(output, 'failure.json'))).mode & 0o777, 0o600);
    log.child.exitCode = 1;
    await persistEvidence({ output, records, errors, logs: [log], failure,
      cleanupErrors: ['cleanup fixture'] });
    assert.deepEqual((await read('failure.json')).screenshot, { path: 'failure.png' });
    assert.equal((await read('processes.json'))[0].exitCode, 1);
    await persistEvidence({ output, records, errors, logs: [log], failure,
      screenshotPage: { screenshot: async () => { throw new Error('capture unavailable'); } } });
    assert.deepEqual((await read('failure.json')).screenshot, { error: 'capture unavailable' });
  } finally { await fs.rm(output, { recursive: true, force: true }); }
  console.log('Native harness contract self-test passed: 11 polling cases and 9 evidence checks.');
}

module.exports = { validatePolling, previewCalls, persistEvidence, appendLog };
if (require.main === module) {
  if (process.argv[2] === '--self-test') selfTest().catch(error => {
    console.error(error); process.exitCode = 1;
  });
  // Worker descendants inherit stdio. After final evidence is flushed, exit so
  // the Python controller can reap its whole owned process group promptly.
  else void run().then(() => process.exit(process.exitCode || 0));
}
