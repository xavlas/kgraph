'use strict';
// Pure Node helpers (no Electron import) so they can be unit-tested: find Python, pick a free port, run the kgraph server.
const { spawn, execFile } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');

const MIN_PYTHON = [3, 10];

function pythonVersion(cmd) {
  return new Promise(resolve => {
    execFile(cmd, ['-c', 'import sys; print("%d.%d" % sys.version_info[:2])'], { timeout: 10000 }, (err, out) => {
      if (err) return resolve(null);
      const m = /^(\d+)\.(\d+)/.exec(String(out).trim());
      resolve(m ? [Number(m[1]), Number(m[2])] : null);
    });
  });
}

async function findPython(candidates = [process.env.KGRAPH_PYTHON, 'python3', 'python']) {
  for (const cmd of candidates.filter(Boolean)) {
    const v = await pythonVersion(cmd);
    if (v && (v[0] > MIN_PYTHON[0] || (v[0] === MIN_PYTHON[0] && v[1] >= MIN_PYTHON[1]))) return cmd;
  }
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

function probe(port) {
  return new Promise(resolve => {
    const req = http.get({ host: '127.0.0.1', port, path: '/api/meta', timeout: 1000 }, res => { res.resume(); resolve(res.statusCode === 200); });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

/** Starts `python -m kgraph serve` bound to 127.0.0.1. `ready` resolves when the API answers, rejects (with the log) otherwise. */
function startServer({ python, root, port, workdir, args = [], env = {}, timeoutMs = 25000 }) {
  const child = spawn(python, ['-m', 'kgraph', 'serve', '--host', '127.0.0.1', '--port', String(port), '--workdir', workdir, ...args], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: [root, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter), PYTHONUNBUFFERED: '1', ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  const keep = d => { log = (log + d).slice(-20000); };
  child.stdout.on('data', keep);
  child.stderr.on('data', keep);
  let exited = false;
  const exit = new Promise(resolve => child.on('exit', (code, sig) => { exited = true; resolve({ code, sig }); }));
  child.on('error', e => keep(`spawn error: ${e.message}\n`));

  const ready = (async () => {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      if (exited) throw new Error(`the kgraph server exited early:\n${log}`);
      if (await probe(port)) return port;
      await new Promise(r => setTimeout(r, 150));
    }
    throw new Error(`the kgraph server did not answer within ${timeoutMs / 1000}s:\n${log}`);
  })();

  const stop = () => new Promise(resolve => {
    if (exited) return resolve();
    const t = setTimeout(() => child.kill('SIGKILL'), 3000);
    exit.then(() => { clearTimeout(t); resolve(); });
    child.kill('SIGTERM');
  });
  return { child, port, ready, exit, stop, getLog: () => log };
}

module.exports = { findPython, freePort, probe, startServer, pythonVersion, MIN_PYTHON };
