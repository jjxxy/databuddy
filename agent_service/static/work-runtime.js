/* Background work visibility. Commands and credentials stay out of the UI. */
(() => {
  'use strict';
  const b = window.atlasBusiness, dialog = document.querySelector('#work-progress-dialog');
  if (!b || !dialog) return;
  const list = dialog.querySelector('[data-work-list]'), notice = dialog.querySelector('[data-work-notice]');
  let generation = 0, timer = null, loading = false;
  const labels = {submitting:'正在连接',queued:'排队中',preparing:'正在准备',starting:'正在启动',running:'正在执行',
    succeeded:'执行成功',failed:'执行失败',timed_out:'执行超时',cancelled:'已停止',unknown:'执行结果待核对'};
  function node(tag, text, className) {
    const value = document.createElement(tag); value.textContent = text;
    if (className) value.className = className;
    return value;
  }
  function reset() {
    generation++; loading = false; clearTimeout(timer); timer = null;
    list.replaceChildren(); notice.textContent = ''; if (dialog.open) dialog.close();
  }
  async function load() {
    const id = b.getState().projectId, mark = generation;
    if (!id || loading || !dialog.open) return;
    loading = true;
    try {
      const value = await b.api('/projects/' + encodeURIComponent(id) + '/work-environment');
      if (mark !== generation || !dialog.open || b.getState().projectId !== id) return;
      notice.textContent = value.configured ? '后台执行不依赖此页面。停止当前工作请使用对话中的停止按钮；生成的文件在“成果”中查看。' : '此工作空间尚未开启代码执行，请联系管理员。已有的数据工作流与办公文件功能仍可使用。';
      const open = new Set([...list.querySelectorAll('details[open]')].map(item => item.dataset.execution));
      list.replaceChildren();
      for (const execution of value.executions || []) {
        const item = node('article', '', 'work-execution');
        const heading = node('div', '', 'work-execution-heading');
        heading.append(node('strong', labels[execution.state] || '等待核对'));
        heading.append(node('span', new Date(execution.created_at).toLocaleString('zh-CN', {hour12:false}), 'dialog-description'));
        item.append(heading);
        const count = execution.output_files?.length || 0;
        item.append(node('p', '工作目录共 ' + count + ' 个文件', 'dialog-description'));
        if (execution.connection_notice) item.append(node('p', execution.connection_notice));
        if (execution.error) item.append(node('p', '这次执行尚未完成，请在对话中查看原因，核对后继续处理。', 'error'));
        const output = [execution.exit_code == null ? '' : '退出状态：' + execution.exit_code, execution.error, execution.stdout, execution.stderr].filter(Boolean).join('\n');
        if (output) {
          const details = document.createElement('details'); details.dataset.execution = execution.id; details.open = open.has(execution.id);
          details.append(node('summary', '查看执行输出'), node('pre', output, 'work-execution-log')); item.append(details);
        }
        list.append(item);
      }
      if (!value.executions?.length) list.append(node('p', '还没有云端代码执行记录。直接在对话中说明要完成的工作即可。', 'dialog-description'));
    } catch (error) {
      if (mark === generation && dialog.open) notice.textContent = error.message;
    } finally {
      if (mark === generation) { loading = false; if (dialog.open) timer = setTimeout(load, 2000); }
    }
  }
  document.querySelector('#work-progress').onclick = () => {
    if (!b.getState().projectId) return b.toast('请先打开一个项目');
    reset(); dialog.showModal(); load();
  };
  dialog.querySelector('[data-work-close]').onclick = () => reset();
  dialog.addEventListener('close', () => { generation++; clearTimeout(timer); timer = null; loading = false; });
  dialog.querySelector('[data-work-artifacts]').onclick = () => { reset(); window.atlasShell?.openArtifacts('work').catch(error => b.toast(error.message)); };
  window.atlasWorkRuntime = {reset, projectChanged:reset};
})();
