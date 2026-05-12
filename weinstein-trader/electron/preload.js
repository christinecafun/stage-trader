const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
  fetch: (path, method = 'GET', body = null) =>
    ipcRenderer.invoke('flask-fetch', { path, method, body }),

  getFlaskUrl: () => ipcRenderer.invoke('get-flask-url'),

  // SSE streaming via fetch in renderer (direct HTTP is fine since same machine)
  streamUrl: (path) => `http://127.0.0.1:5050${path}`,
});
