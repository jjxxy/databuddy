'use strict';
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('nexusLineage', Object.freeze({
  list: () => ipcRenderer.invoke('nexus:lineage-list'),
  get: id => ipcRenderer.invoke('nexus:lineage-get', id)
}));
