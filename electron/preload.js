'use strict';
// Minimal, explicit bridge (contextIsolation on, no Node in the page): only two capabilities are exposed.
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('kgraphDesktop', {
  pickFolder: () => ipcRenderer.invoke('kgraph:pick-folder'),
  samplePath: () => ipcRenderer.invoke('kgraph:sample-path'),
});
