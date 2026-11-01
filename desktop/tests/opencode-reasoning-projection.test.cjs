const test = require('node:test');
const assert = require('node:assert/strict');

test('Atlas request projection preserves assistant reasoning and native tool pairing without exposing text in metrics', async () => {
  const {projectRequest} = await import('../../agent_service/opencode_context.mjs');
  const reasoning = 'fixture-sensitive-reasoning-测试';
  const longReceipt = (id, extra = {}) => ({
    role: 'tool', tool_call_id: 'call_' + id,
    content: JSON.stringify({observation: {step_id: id, tool: 'write_file', status: 'done', arguments: {},
      observation: {path: 'work/report.json', data: 'x'.repeat(12000), ...extra}}}),
  });
  const assistant = {role: 'assistant', content: null, reasoning_content: reasoning,
    tool_calls: [{id: 'call_first', type: 'function', function: {name: 'atlas_step', arguments: '{"kind":"tool"}'}}]};
  const secondAssistant = {role: 'assistant', content: null, reasoning_content: '',
    tool_calls: [{id: 'call_second', type: 'function', function: {name: 'atlas_step', arguments: '{}'}}]};
  const body = {model: 'fixture', messages: [assistant, longReceipt('first'),
    secondAssistant, longReceipt('second'), longReceipt('latest')]};
  const original = structuredClone(body);
  const result = projectRequest(body, {underPressure: true});
  assert.equal(result.metrics.compacted_receipts, 1);
  assert.deepEqual(body, original);
  assert.deepEqual(result.body.messages[0], assistant);
  assert.deepEqual(result.body.messages[2], secondAssistant);
  assert.equal(result.body.messages[1].tool_call_id, 'call_first');
  assert.equal(result.body.messages[3].tool_call_id, 'call_second');
  assert.equal(JSON.parse(result.body.messages[1].content).observation.observation.not_full_result, true);
  assert.equal(JSON.stringify(result.metrics).includes(reasoning), false);
});
