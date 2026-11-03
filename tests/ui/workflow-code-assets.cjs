const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');

async function run() {
  const dom = new JSDOM('<div id="panel"></div>', {runScripts: 'outside-only', url: 'https://app.test/'});
  const w = dom.window;
  try {
    for (const file of ['workflow-code-assets', 'business-flow-view', 'workbench-dag', 'workflow-overview', 'chat-workflows']) {
      w.eval(fs.readFileSync('agent_service/static/' + file + '.js', 'utf8'));
    }
    const business = 'bf_' + 'b'.repeat(40), data = 'wf_' + 'd'.repeat(40);
    const digest = 'e'.repeat(64), commit = 'a'.repeat(40);
    const projectId = 'p_ui', nodes = [
      {id: 'load', kind:'source_table', config:{source_id:'file1',revision:2,content_hash:digest}, title: '读取销售数据', depends_on: []},
      {id: 'sum', title: '核对金额', depends_on: ['load']},
      {id: 'ppt', title: '编制汇报', depends_on: ['sum']}
    ];
    const plan = {inputs: {sales: {meaning: '销售记录'}}, stages: [{id: 'calc', kind: 'aggregate', input: 'sales'}], output: 'calc'};
    const latestRun={id:'run1',flow_id:business,plan_digest:digest,commit,steps:[{node_id:'ppt',status:'succeeded',artifact:{id:'art1',name:'汇报材料.pptx',preview_url:'https://bad.invalid/trap'}}]};
    const requested = [], tick = () => new Promise(resolve => setTimeout(resolve, 0));
    let sourceArgs, resultArgs;
    w.nexusShell = {openSource: async (...args) => {sourceArgs = args}, openArtifact: async value => {resultArgs = value}};
    let finish, delay = false, mismatch = false, historical = false;
    function manifest(id) {
      const ids = id === business ? nodes.map(n => n.id) : ['sales', 'calc', '__overview_result__'];
      const rules = ids.map(i => ({id: i, path: 'nodes/' + i + '.json', handler_path: historical ? null : 'runtime/' + i + '.py'}));
      for(const node of rules.slice(0,2))node.paths=historical?[]:['shared/math/money.py'];
      if(id===data)rules[1].paths.push('nodes/calc.sql');
      const paths = new Set(rules.flatMap(n => [n.path,n.handler_path,...(n.paths||[])].filter(Boolean)).concat(['overview.json','io/bindings.json']));
      const files = Array.from(paths).map(path => ({path, label: path.endsWith('.py') ? '运行代码' : '规则文件'}));
      return {flow_id: id, digest, commit: historical ? null : commit, storage: historical ? 'derived' : 'git', files, nodes: rules, groups: [
        {id: 'g1', label: '核对本期收入', nodeIds: ids.slice(0,2), deps: []},
        {id: 'g2', label: '交付成果', nodeIds: ids.slice(2), deps: ['g1']}
      ]};
    }
    async function api(url) {
      const u = new URL(url, 'https://app.test'), parts = u.pathname.split('/'), kind = parts[3], id = parts[4], action = parts[5];
      if (!id) return {items: [{id: kind === 'business-flows' ? business : data, name: kind}]};
      if (!action) return id === business ? {id, candidate_document:{nodes}, candidate_digest:digest, candidate_commit:commit,latest_run:latestRun} :
        {workflow_id:id, draft_sha256:digest, document:{plan,bindings:{sales:{source_id:'file1'}}}};
      assert.equal(u.searchParams.get('expected_digest'), digest);
      const m = manifest(id);
      if (action === 'code-assets') return m;
      assert.equal(action, 'code-asset');
      assert.equal(u.searchParams.get('commit'), m.commit);
      const path = u.searchParams.get('path');
      assert(m.files.some(file => file.path === path));
      requested.push(path);
      if (delay) await new Promise(resolve => {finish = resolve});
      return {flow_id:id, digest: mismatch ? 'x' : digest, commit:m.commit, path, content:'<script>unsafe()</script>\n' + path};
    }
    const panel = w.document.querySelector('#panel'), state = {projectId,viewEpoch:1};
    const app = w.createChatWorkflowPanel({container:panel, api, getState:()=>state});
    await app.refresh(true);
    const sel = panel.querySelector('select');
    const choose = async kind => {sel.value = kind + ':' + (kind === 'business' ? business : data); sel.dispatchEvent(new w.Event('change')); await tick()};
    for (const kind of ['business', 'data']) {
      await choose(kind);
      const id = kind === 'business' ? 'sum' : 'calc';
      const overview = panel.querySelector('.workflow-overview-graph');
      assert.match(overview.textContent, /核对本期收入/);
      const beforeGroup=requested.length;
      overview.querySelector('button').click();
      assert.equal(panel.querySelector('.workflow-step-files'),null);
      assert.equal(requested.length,beforeGroup,'opening a group must not fetch any node files');
      assert.equal(app.getSelectedWorkflow().node_id,undefined);
      const buttons=panel.querySelectorAll('.workflow-group-graph button');
      assert.equal(buttons.length,2,'keyboard-accessible graph fallback exposes only this group');
      const sourceId=kind==='business'?'load':'sales';
      buttons[0].click();await tick();
      const section=()=>panel.querySelector('.workflow-step-files');
      assert.equal(panel.querySelectorAll('.workflow-step-files').length,1);
      assert.equal(app.getSelectedWorkflow().node_id,sourceId);
      assert.equal(requested.length,beforeGroup+1);
      assert.match(section().querySelector('pre').textContent,new RegExp('runtime/'+sourceId+'\\.py'));
      assert.equal(section().querySelector('button[title="nodes/'+id+'.json"]'),null);
      const shared=section().querySelector('button[title="shared/math/money.py"]');
      assert(shared.parentElement.open);
      shared.parentElement.open=false;
      const sourceButton=[...section().querySelectorAll('button')].find(b=>b.textContent.includes(kind==='business'?'固定版本的来源原件':'当前绑定的来源'));
      sourceButton.click();await tick();
      assert.deepEqual(sourceArgs,kind==='business'?['file1',2,digest]:['file1']);

      // An old node file request may finish after another selection in the same version.
      delay=true;shared.click();
      const finishOld=finish;
      delay=false;buttons[1].click();await tick();
      const expected=kind==='data'?'nodes/calc.sql':'runtime/sum.py';
      assert.equal(section().querySelector('pre').textContent,'<script>unsafe()</script>\n'+expected);
      finishOld();await tick();
      assert.equal(section().querySelector('pre').textContent,'<script>unsafe()</script>\n'+expected);
      assert.equal(panel.querySelectorAll('.workflow-step-files').length,1);
      assert.equal(section().querySelector('button[title="nodes/'+sourceId+'.json"]'),null);
      assert.deepEqual([...section().querySelectorAll('.workflow-file-links summary')].map(s=>s.textContent),['nodes/','runtime/','shared/','math/']);
      assert(section().querySelector('button[title="shared/math/money.py"]').parentElement.open);
      assert(![...section().querySelectorAll('button')].some(b=>b.textContent.includes('来源原件')||b.textContent.includes('绑定的来源')));
      assert.equal(app.getSelectedWorkflow().node_id,id,'clicking a graph node binds that node to chat');
      const file=section().querySelector('button[title="runtime/'+id+'.py"]');
      file.click();await tick();
      const viewer=section().querySelector('pre');
      assert.equal(viewer.textContent,'<script>unsafe()</script>\nruntime/'+id+'.py');
      assert.equal(viewer.querySelector('script'),null);
      panel.querySelector('.workflow-overview-detail').click();
      assert.equal(app.getSelectedWorkflow().node_id,undefined,'switching diagrams clears the prior step');
      const full = panel.querySelector(kind === 'business' ? '.flow-graph' : '.workflow-graph-scroll');
      full.querySelectorAll('g[role=button]')[2].dispatchEvent(new w.Event('click'));
      assert.equal(app.getSelectedWorkflow().node_id,kind==='business'?'ppt':undefined,'full graph selects executable nodes only');
      const detail = panel.querySelector(kind === 'business' ? '.flow-graph-detail' : '.workflow-graph-detail');
      detail.querySelector('.workflow-file-links button').click(); await tick();
      assert.match(detail.querySelector('pre').textContent, kind === 'business' ? /ppt\.json/ : /__overview_result__\.json/);
      if(kind==='business'){
        const link=[...detail.querySelectorAll('button')].find(b=>b.textContent.includes('查看本步成果'));
        link.click();await tick();
        assert.equal(resultArgs.preview_url,'/api/projects/p_ui/artifacts/art1/preview');
      }
      const tree = panel.querySelector('.workflow-project-files');
      assert.match(tree.textContent, /Git/);
      const io = tree.querySelector('button[title="io/bindings.json"]');
      mismatch = true; io.click(); await tick();
      assert.match(tree.textContent, /无法读取此版本/);
      assert.equal(tree.querySelector('pre'), null);
      mismatch = false;
    }
    delay = true;
    panel.querySelector('.workflow-project-files button').click();
    await choose('business'); finish(); await tick();
    assert.equal(panel.querySelector('.workflow-file-viewer pre'), null);
    delay = false; historical = true;
    await choose('data');
    assert.match(panel.querySelector('.workflow-project-files').textContent, /还原/);
    assert.equal(panel.querySelector('button[title^="runtime/"]'), null);
    // A legacy definition without Python source still shows its recoverable rule.
    panel.querySelector('.workflow-overview-graph button').click();
    panel.querySelector('.workflow-group-graph button').click();await tick();
    assert.match(panel.querySelector('.workflow-step-files pre').textContent,/nodes\/sales\.json/);
    console.log('project chat drilldown passed for both DAG engines, exact file contents, version rejection, stale reads and derived history');
  } finally {dom.window.close()}
}
run().catch(err => {console.error(err);process.exitCode=1});
