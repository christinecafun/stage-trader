const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const http = require('http');

let mainWindow;
let flaskProcess;
const FLASK_PORT = 5050;
const FLASK_URL = `http://127.0.0.1:${FLASK_PORT}`;

// ── Kill any process on Flask port ───────────────────────────────────────────
function freePort(port) {
  try {
    const { execSync } = require('child_process');
    if (process.platform === 'win32') {
      execSync(`for /f "tokens=5" %a in ('netstat -aon ^| findstr :${port}') do taskkill /F /PID %a`, { stdio: 'ignore' });
    } else {
      execSync(`fuser -k ${port}/tcp 2>/dev/null || true`, { stdio: 'ignore' });
    }
  } catch {}
}

// ── Start Flask backend ───────────────────────────────────────────────────────
function startFlask() {
  freePort(FLASK_PORT);
  const backendPath = path.join(__dirname, '..', 'backend');
  const python = process.platform === 'win32' ? 'python' : 'python3';

  flaskProcess = spawn(python, ['app.py'], {
    cwd: backendPath,
    env: { ...process.env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  flaskProcess.stdout.on('data', (d) => console.log('[Flask]', d.toString().trim()));
  flaskProcess.stderr.on('data', (d) => console.error('[Flask]', d.toString().trim()));
  flaskProcess.on('exit', (code) => console.log(`[Flask] exited with code ${code}`));
}

// ── Wait for Flask to be ready ────────────────────────────────────────────────
function waitForFlask(retries = 30) {
  return new Promise((resolve, reject) => {
    const attempt = (n) => {
      http.get(`${FLASK_URL}/api/status`, (res) => {
        resolve();
      }).on('error', () => {
        if (n <= 0) return reject(new Error('Flask did not start in time'));
        setTimeout(() => attempt(n - 1), 500);
      });
    };
    attempt(retries);
  });
}

// ── Create window ─────────────────────────────────────────────────────────────
async function createWindow() {
  startFlask();

  try {
    await waitForFlask();
  } catch (e) {
    console.error('Flask startup timeout — opening anyway');
  }

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    title: 'Weinstein Trader',
    backgroundColor: '#1e1e1e',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    titleBarStyle: 'hidden',
    titleBarOverlay: {
      color: '#1e1e1e',
      symbolColor: '#c9a84c',
      height: 32,
    },
  });

  mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));

  mainWindow.on('closed', () => { mainWindow = null; });
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (flaskProcess) flaskProcess.kill();
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

// ── IPC: proxy fetch to Flask (avoids CORS in renderer) ──────────────────────
ipcMain.handle('flask-fetch', async (event, { path: urlPath, method = 'GET', body }) => {
  return new Promise((resolve, reject) => {
    const options = {
      hostname: '127.0.0.1',
      port: FLASK_PORT,
      path: urlPath,
      method,
      headers: { 'Content-Type': 'application/json' },
    };
    const req = http.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try { resolve({ ok: true, data: JSON.parse(data), status: res.statusCode }); }
        catch { resolve({ ok: true, data, status: res.statusCode }); }
      });
    });
    req.on('error', (e) => reject(e));
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
});

// Expose Flask base URL to renderer
ipcMain.handle('get-flask-url', () => FLASK_URL);
