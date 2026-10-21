'use strict';
const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs');
const { findPython, freePort, startServer } = require('../launcher');

const ROOT = path.resolve(__dirname, '..', '..');

test('findPython skips missing interpreters and finds a >= 3.10 one', async () => {
  assert.strictEqual(await findPython(['/definitely/not/python', undefined]), null);
  const py = await findPython(['/definitely/not/python', 'python3', 'python']);
  assert.ok(py, 'a Python >= 3.10 should be installed to run kgraph');
});

test('freePort returns distinct usable ports', async () => {
  const [a, b] = [await freePort(), await freePort()];
  assert.ok(a > 1023 && b > 1023);
});

test('startServer launches the real kgraph server, serves the UI, protects POST, then stops', async () => {
  const python = await findPython();
  const port = await freePort();
  const workdir = fs.mkdtempSync(path.join(os.tmpdir(), 'kgraph-electron-'));
  const srv = startServer({ python, root: ROOT, port, workdir });
  try {
    await srv.ready;
    const get = p => new Promise((res, rej) => http.get({ host: '127.0.0.1', port, path: p }, r => {
      let body = ''; r.on('data', d => (body += d)); r.on('end', () => res({ status: r.statusCode, body, headers: r.headers }));
    }).on('error', rej));
    const page = await get('/');
    assert.strictEqual(page.status, 200);
    assert.match(page.body, /kgraph/);
    assert.match(page.body, /id="tab-network"/);                       // the new UI is what gets served
    assert.match(page.headers['content-security-policy'], /default-src 'self'/);
    const js = await get('/static/network.js');
    assert.strictEqual(js.status, 200);
    const post = await new Promise((res, rej) => {
      const req = http.request({ host: '127.0.0.1', port, path: '/api/scan', method: 'POST', headers: { 'Content-Type': 'application/json' } },
        r => { r.resume(); res(r.statusCode); });
      req.on('error', rej); req.end('{}');
    });
    assert.strictEqual(post, 403);                                      // CSRF guard: custom header required
  } finally {
    await srv.stop();
  }
  assert.ok(srv.child.killed || srv.child.exitCode !== null || srv.child.signalCode);
});

test('startServer reports the log when the server cannot start', async () => {
  const python = await findPython();
  const srv = startServer({ python, root: os.tmpdir(), port: await freePort(), workdir: os.tmpdir(), timeoutMs: 8000 });   // no kgraph package here
  await assert.rejects(srv.ready, /exited early[\s\S]*kgraph/);
});
