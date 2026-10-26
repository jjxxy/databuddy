'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('atlasLineage', Object.freeze({
  list: () => ipcRenderer.invoke('atlas:lineage-list'),
  get: id => ipcRenderer.invoke('atlas:lineage-get', id)
}));
