'use strict';
const { app, BrowserWindow, Menu, dialog, ipcMain, session, shell } = require('electron');
const fs = require('fs');
const path = require('path');
const { findPython, freePort, startServer } = require('./launcher');

// Dev: the Python package sits next to this folder. Packaged: electron-builder copies it to <resources>/kgraph.
const ROOT = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..');
let server = null;
let win = null;
let origin = null;

function openExternal(url) {
  try { const u = new URL(url); if (u.protocol === 'https:' || u.protocol === 'http:') shell.openExternal(url); } catch { /* ignore */ }
}

function fail(title, detail) {
  dialog.showErrorBox(title, detail);
  app.quit();
}

async function boot() {
  const python = await findPython();
  if (!python) {
    return fail('Python 3.10+ is required',
      'kgraph runs its analysis engine with Python (standard library only).\n\nInstall Python 3.10 or newer, or set KGRAPH_PYTHON to its path, then restart the app.');
  }
  const port = await freePort();
  const workdir = path.join(app.getPath('userData'), 'kgraph');
  fs.mkdirSync(workdir, { recursive: true });
  // KGRAPH_SCAN=<path|git url> : scan this repository at startup (used by launch.sh full so the app and Neo4j show the same repo)
  const extra = [];
  if (process.env.KGRAPH_SCAN) extra.push('--scan', process.env.KGRAPH_SCAN);
  if (process.env.KGRAPH_SCAN && process.env.KGRAPH_RULES === 'none') extra.push('--rules', 'none');
  server = startServer({ python, root: ROOT, port, workdir, args: extra });
  server.exit.then(({ code }) => {
    if (win && !app.isQuitting) dialog.showErrorBox('The kgraph server stopped', `Exit code ${code}.\n\n${server.getLog().slice(-1500)}`);
  });
  try { await server.ready; } catch (e) { return fail('Could not start the kgraph server', String(e.message || e).slice(0, 1800)); }
  origin = `http://127.0.0.1:${port}`;
  createWindow();
}

function createWindow() {
  win = new BrowserWindow({
    width: 1560, height: 960, minWidth: 1000, minHeight: 640, title: 'kgraph', backgroundColor: '#fafafc',
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false, sandbox: true, webSecurity: true },
  });
  win.webContents.on('will-navigate', (e, url) => { if (!url.startsWith(origin)) { e.preventDefault(); openExternal(url); } });
  win.webContents.setWindowOpenHandler(({ url }) => { openExternal(url); return { action: 'deny' }; });
  win.on('closed', () => { win = null; });
  win.loadURL(origin + '/');
}

ipcMain.handle('kgraph:pick-folder', async event => {
  if (!origin || !event.senderFrame || !event.senderFrame.url.startsWith(origin)) return null;       // only our own page
  const r = await dialog.showOpenDialog(win, { title: 'Choose the repository to analyse', properties: ['openDirectory'] });
  return r.canceled ? null : r.filePaths[0];
});
ipcMain.handle('kgraph:sample-path', event => {
  if (!origin || !event.senderFrame || !event.senderFrame.url.startsWith(origin)) return null;
  const p = path.join(ROOT, 'samples', 'shop');
  return fs.existsSync(p) ? p : null;
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => { if (win) { if (win.isMinimized()) win.restore(); win.focus(); } });
  app.whenReady().then(() => {
    session.defaultSession.setPermissionRequestHandler((_wc, _perm, cb) => cb(false));          // the UI needs no camera, mic, location…
    const template = Menu.buildFromTemplate([
      ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
      { role: 'fileMenu' }, { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
      { label: 'kgraph', submenu: [
        { label: 'Open the workspace folder', click: () => shell.openPath(path.join(app.getPath('userData'), 'kgraph')) },
        { label: 'Reload', accelerator: 'CmdOrCtrl+R', click: () => win && win.reload() },
      ] },
    ]);
    Menu.setApplicationMenu(template);
    boot().catch(e => fail('Unexpected error', String((e && e.stack) || e)));
  });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', () => { app.isQuitting = true; if (server) server.stop(); });
}
