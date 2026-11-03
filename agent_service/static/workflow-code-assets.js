'use strict';

/* Open only files listed in the manifest of the selected project workflow version. */
window.loadWorkflowCodeAssets = async function ({kind, projectId, flowId, digest, commit, expectedNodes, plan, api, isCurrent}) {
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
  if (manifest.files.some(file => typeof file?.path !== 'string' ||
      !/^(?:[\w-]+\/)*[\w-]+\.(?:json|sql|py)$/.test(file.path))) throw Error('流程文件路径无效');
  const files = new Map(manifest.files.map(file => [file.path, file]));
  const nodes = new Map(manifest.nodes.map(node => [node.id, node]));
  const invalidFile = node => !files.has(node.path) || (node.handler_path && !files.has(node.handler_path)) ||
    (node.paths != null && (!Array.isArray(node.paths) || node.paths.some(path => !files.has(path))));
  function validGroups(groups) {
    if (!Array.isArray(groups)) return false;
    const groupIds = new Set(groups.map(group => group.id)), covered = new Set();
    if (groupIds.size !== groups.length) return false;
    for (const group of groups) {
      if (!Array.isArray(group.deps) || !Array.isArray(group.nodeIds) || !group.nodeIds.length ||
          group.deps.some(id => !groupIds.has(id) || id === group.id)) return false;
      for (const id of group.nodeIds) {
        if (!nodes.has(id) || covered.has(id)) return false;
        covered.add(id);
      }
    }
    return covered.size === nodes.size;
  }
  if (nodes.size !== expectedNodes.length || nodes.size !== manifest.nodes.length ||
      expectedNodes.some(id => !nodes.has(id)) || files.size !== manifest.files.length ||
      manifest.nodes.some(invalidFile) || !validGroups(manifest.groups) ||
      (manifest.display_groups != null && !validGroups(manifest.display_groups))) throw Error('流程文件与步骤不匹配');
  if (manifest.detail_nodes != null) {
    const stages = new Set((plan?.nodes || []).filter(node => node.kind === 'table_workflow')
      .flatMap(node => (node.config?.plan?.stages || []).map(stage => node.id + ':' + stage.id)));
    if (!Array.isArray(manifest.detail_nodes) || stages.size !== manifest.detail_nodes.length) throw Error('加工步骤文件不匹配');
    for (const stage of manifest.detail_nodes) {
      const owner = nodes.get(stage.owner_id), paths = new Set(owner?.paths || []);
      if (stage.id !== stage.owner_id + ':' + stage.stage_id || !stages.delete(stage.id) || !owner ||
          invalidFile(stage) || !paths.has(stage.path) || (stage.paths || []).some(path => !paths.has(path))) throw Error('加工步骤文件不匹配');
      nodes.set(stage.id, stage);
    }
  } else if (kind === 'business' && globalThis.crypto?.subtle && typeof TextEncoder !== 'undefined') {
    // Older indexes store the real stage files under the parent. Reconstruct
    // their original versioned filenames and accept only exact manifest hits.
    const encoder = new TextEncoder();
    const tables = (plan?.nodes || []).filter(node => node.kind === 'table_workflow');
    await Promise.all(tables.flatMap(table => {
      const owner = nodes.get(table.id);
      if (!owner || !owner.path.endsWith('/rule.json')) return [];
      const paths = new Set(owner.paths || []);
      const runtime = [...paths].filter(path => path.startsWith('runtime/tables/') && path.endsWith('.py'));
      const folder = owner.path.slice(0, -'rule.json'.length) + 'stages/s_';
      return table.config.plan.stages.map(async rule => {
        const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(rule.id)));
        const suffix = Array.from(hash.subarray(0, 8), byte => byte.toString(16).padStart(2, '0')).join('');
        const base = folder + rule.id.slice(0, 8) + '_' + suffix;
        if (!paths.has(base + '.json')) return;
        const related = rule.kind === 'sql' && paths.has(base + '.sql') ? [base + '.sql', ...runtime] : runtime;
        nodes.set(table.id + ':' + rule.id, {path:base + '.json', paths:related});
      });
    }));

  }

  function fileList(container, entries, expandedFiles = [], initialFile = null) {
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
    let ticket = 0, selectedButton;
    async function openFile(file, button) {
      if (!isCurrent()) return;
      const mark = ++ticket;
      const current = () => mark === ticket && isCurrent() && viewer.isConnected;
      selectedButton?.setAttribute('aria-pressed', 'false');
      selectedButton = button;
      button.setAttribute('aria-pressed', 'true');
      viewer.textContent = '正在读取 ' + file.path + '…';
      // Move the visible code area into reach on the user's click. A delayed
      // server response must never pull someone back after they scroll away.
      viewer.scrollIntoView?.({block:'nearest',inline:'nearest'});
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
      } catch (_) {
        if (current()) viewer.textContent = '无法读取此版本的文件。请重新打开当前流程，再试一次。';
      }
    }
    let initialButton;
    for (const file of entries) {
      const button = doc.createElement('button');
      button.type = 'button';
      button.textContent = (file.label ? file.label + ' · ' : '') + file.path.split('/').pop();
      button.title = file.path;
      button.setAttribute('aria-pressed', 'false');
      button.addEventListener('click', () => openFile(file, button));
      if (file === initialFile) initialButton = button;
      parentFor(file.path).append(button);
    }
    if (initialButton) {
      container.append(viewer, links);
      openFile(initialFile, initialButton);
    } else container.append(links, viewer);
  }

  function showNode(container, id) {
    const node = nodes.get(id);
    if (!node) return false;
    const heading = container.ownerDocument.createElement('h6');
    heading.textContent = '节点源码与规则';
    container.append(heading);
    const paths = new Set([node.path, node.handler_path, ...(node.paths || [])]);
    const entries = Array.from(paths, path => files.get(path)).filter(Boolean);
    const queries = entries.filter(file => file.path.endsWith('.sql'));
    const initialFile = (queries.length === 1 ? queries[0] : null) || files.get(node.handler_path) || files.get(node.path);
    if (manifest.storage === 'derived') {
      const note = container.ownerDocument.createElement('p');
      note.textContent = '以下内容从该版本保存的流程定义还原。此版本未保存 Python 源码。';
      container.append(note);
    }
    const expandedFiles = entries.length <= 6 ? entries.map(file => file.path) : [initialFile.path];
    fileList(container, entries, expandedFiles, initialFile);
    return true;
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
  return {groups: manifest.display_groups ?? manifest.groups,
    usePlanGroups: kind === 'data' && manifest.display_groups == null,
    supportsStageContext: Array.isArray(manifest.detail_nodes), showNode, showProject};
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
