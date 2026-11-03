'use strict';

/* Open only files listed in the manifest of the selected project workflow version. */
window.loadWorkflowCodeAssets = async function ({kind, projectId, flowId, digest, commit, expectedNodes, api, isCurrent}) {
  const encode = encodeURIComponent;
  const endpoint = '/projects/' + encode(projectId) + (kind === 'data' ? '/data-workflows/' : '/business-flows/') + encode(flowId);
  const query = new URLSearchParams({expected_digest: digest});
  if (commit) query.set('commit', commit);
  const identityMatches = value => value?.flow_id === flowId && value.digest === digest;
  const manifest = await api(endpoint + '/code-assets?' + query);
  const assetCommit = manifest?.commit ?? null;
  if ((commit && assetCommit !== commit) || (assetCommit !== null && !/^[a-f0-9]{40}$/.test(assetCommit))) throw Error('流程文件版本不一致');
  if (assetCommit) query.set('commit', assetCommit);
  if (!identityMatches(manifest) || !Array.isArray(manifest.files) || !Array.isArray(manifest.nodes) || !Array.isArray(manifest.groups)) {
    throw Error('流程文件版本不一致');
  }
  const files = new Map(manifest.files.map(file => [file.path, file]));
  const nodes = new Map(manifest.nodes.map(node => [node.id, node]));
  const groupIds = new Set(manifest.groups.map(group => group.id));
  const covered = new Set();
  const invalidFile = node => !files.has(node.path) || (node.handler_path && !files.has(node.handler_path)) ||
    (node.paths != null && (!Array.isArray(node.paths) || node.paths.some(path => !files.has(path))));
  if (nodes.size !== expectedNodes.length || nodes.size !== manifest.nodes.length ||
      expectedNodes.some(id => !nodes.has(id)) || files.size !== manifest.files.length ||
      manifest.nodes.some(invalidFile) ||
      groupIds.size !== manifest.groups.length || manifest.groups.some(group => {
        if (!Array.isArray(group.deps) || !Array.isArray(group.nodeIds) || !group.nodeIds.length ||
            group.deps.some(id => !groupIds.has(id) || id === group.id)) return true;
        for (const id of group.nodeIds) {
          if (!nodes.has(id) || covered.has(id)) return true;
          covered.add(id);
        }
        return false;
      }) || covered.size !== nodes.size) throw Error('流程文件与步骤不匹配');

  function fileList(container, entries, expandedFiles = []) {
    const doc = container.ownerDocument;
    const links = doc.createElement('div'), viewer = doc.createElement('div');
    links.className = 'workflow-file-links';
    viewer.className = 'workflow-file-viewer';
    viewer.setAttribute('aria-live', 'polite');
    const folders = new Map([['', links]]);
    function parentFor(path) {
      let prefix = '', parent = links;
      for (const name of path.split('/').slice(0, -1)) {
        prefix += name + '/';
        if (!folders.has(prefix)) {
          const directory = doc.createElement('details'), summary = doc.createElement('summary');
          summary.textContent = name + '/';
          directory.open = expandedFiles.some(file => file.startsWith(prefix));
          directory.append(summary);
          parent.append(directory);
          folders.set(prefix, directory);
        }
        parent = folders.get(prefix);
      }
      return parent;
    }
    let ticket = 0;
    for (const file of entries) {
      const button = doc.createElement('button');
      button.type = 'button';
      button.textContent = (file.label ? file.label + ' · ' : '') + file.path.split('/').pop();
      button.title = file.path;
      button.addEventListener('click', async () => {
        const mark = ++ticket;
        const current = () => mark === ticket && isCurrent() && viewer.isConnected;
        viewer.textContent = '正在读取 ' + file.path + '…';
        try {
          const params = new URLSearchParams(query);
          params.set('path', file.path);
          const value = await api(endpoint + '/code-asset?' + params);
          if (!current()) return;
          if (!identityMatches(value) || (value.commit ?? null) !== assetCommit || value.path !== file.path || typeof value.content !== 'string') throw Error();
          const caption = doc.createElement('strong'), pre = doc.createElement('pre');
          caption.textContent = file.path;
          pre.tabIndex = 0;
          pre.textContent = value.content;
          viewer.replaceChildren(caption, pre);
          viewer.scrollIntoView?.({block: 'nearest'});
        } catch (_) {
          if (current()) viewer.textContent = '无法读取此版本的文件。请重新打开当前流程，再试一次。';
        }
      });
      parentFor(file.path).append(button);
    }
    container.append(links, viewer);
  }

  function showNode(container, id) {
    const node = nodes.get(id);
    if (!node) return;
    const heading = container.ownerDocument.createElement('h6');
    heading.textContent = '节点文件';
    container.append(heading);
    const paths = new Set([node.path, node.handler_path, ...(node.paths || [])]);
    const entries = Array.from(paths, path => files.get(path)).filter(Boolean);
    fileList(container, entries, entries.length <= 6 ? entries.map(file => file.path) : [node.path, node.handler_path].filter(Boolean));
  }

  function showProject(container) {
    const doc = container.ownerDocument;
    const disclosure = doc.createElement('details'), summary = doc.createElement('summary');
    disclosure.className = 'workflow-project-files';
    summary.textContent = '项目流程文件（' + files.size + '）';
    const note = doc.createElement('p');
    note.textContent = manifest.storage === 'git' ? '文件来自当前选择的 Git 版本。' : '根据当前保存的流程还原文件视图。';
    disclosure.append(summary, note);
    fileList(disclosure, files.values());
    container.append(disclosure);
  }
  return {groups: manifest.groups, showNode, showProject};
};

window.renderWorkflowNodeIO = function (container, id, {kind, workflow, plan, projectId, flowId, run, version, isCurrent}) {
  const shell = window.nexusShell;
  if (!shell) return;
  const validId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(value);
  function action(label, open) {
    const button = container.ownerDocument.createElement('button');
    const message = container.ownerDocument.createElement('span');
    button.type = 'button';
    button.textContent = label;
    message.setAttribute('role', 'status');
    button.onclick = async () => {
      const current = () => isCurrent() && container.contains(button) && button.isConnected;
      if (!current()) return;
      button.disabled = true;
      message.textContent = ' 正在打开…';
      try {
        await open();
        if (current()) message.textContent = '';
      } catch (_) {
        if (current()) message.textContent = ' 无法打开，请核对该来源或成果是否仍可访问。';
      } finally {
        if (current()) button.disabled = false;
      }
    };
    container.append(button, message);
  }
  if (kind === 'data') {
    const source = Object.hasOwn(workflow?.document?.plan?.inputs || {}, id) && workflow.document.bindings?.[id]?.source_id;
    if (validId(source) && typeof shell.openSource === 'function') {
      action('查看当前绑定的来源', () => shell.openSource(source));
    }
    return;
  }
  const node = plan?.nodes?.find(node => node.id === id), pin = node?.config;
  if (typeof shell.openSource === 'function' && validId(pin?.source_id) && Number.isSafeInteger(pin.revision) && pin.revision > 0) {
    if (node.kind === 'source_table' && /^[a-f0-9]{64}$/.test(pin.content_hash || '')) {
      action('查看固定版本的来源原件 · 版本 ' + pin.revision, () => shell.openSource(pin.source_id, pin.revision, pin.content_hash));
    }
    if (node.kind === 'api_capture') {
      action('查看接口连接 · 配置版本 ' + pin.revision, () => shell.openSource(pin.source_id, pin.revision));
    }
  }
  if (!node || !version?.digest || !version.commit || run?.plan_digest !== version.digest ||
      run.commit !== version.commit || run.flow_id !== flowId || typeof shell.openArtifact !== 'function') return;
  const step = run.steps?.find(item => item.node_id === id), artifact = step?.artifact;
  if (step?.status !== 'succeeded' || !validId(artifact?.id) || typeof artifact.name !== 'string') return;
  const base = '/api/projects/' + encodeURIComponent(projectId) + '/artifacts/' + encodeURIComponent(artifact.id);
  action('查看本步成果 · ' + artifact.name, () => shell.openArtifact({
    id: artifact.id, name: artifact.name, preview_url: base + '/preview', download_url: base + '/download'
  }));
};
