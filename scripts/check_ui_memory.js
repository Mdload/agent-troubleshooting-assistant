/* Execute the Agent v2 memory panel with synthetic API responses and a DOM fixture. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const source = fs.readFileSync(path.join(__dirname, '../web/memory-ui.js'), 'utf8');
function element(tag = 'div', textContent = '') {
  return {
    tag, textContent, children: [], hidden: false, open: false, checked: false,
    append(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    addEventListener() {},
  };
}

function fixture(api) {
  const elements = new Map();
  const toolbar = element();
  const sandbox = {
    accountEpoch: 1, accountLoading: false, accountExpired: false, api,
    current: () => ({id: 'example-chat'}),
    document: {querySelector: () => toolbar},
    el(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    add(parent, tag, text) {
      const child = element(tag, text);
      parent.append(child);
      return child;
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  sandbox.el('memory-dialog').open = true;
  return sandbox;
}

async function main() {
  const requests = [];
  const state = {
    granted_devices: [{id: 'remote-primary', label: 'Example device'}],
    summary: 'Example summary',
    memories: [{kind: 'correction', id: 'example-memory', title: 'Example correction', content: '<example text>'}],
  };
  const view = fixture(async (url, payload) => {
    requests.push({url, payload});
    return {agent_memory: payload ? {...state, granted_devices: []} : state};
  });
  await view.loadTaskMemory();
  assert.equal(view.el('agent-conv-summary').textContent, state.summary);
  assert.equal(view.el('agent-memory-list').children[0].children[1].textContent, '<example text>');
  assert.equal(view.el('memory-consent').hidden, true);
  assert.deepEqual(Object.keys(view.selectedTaskMemory()), []);
  const revoke = view.el('agent-granted-devices').children[0].children[0];
  await revoke.onclick();
  assert.equal(requests[0].url, '/api/conversations/example-chat/memory');
  assert.equal(requests[1].payload.action, 'revoke_device');
  assert.equal(requests[1].payload.device, 'remote-primary');
  assert.match(view.el('agent-granted-devices').children[0].textContent, /暂未授权/);

  let finish;
  const stale = fixture(() => new Promise(resolve => { finish = resolve; }));
  const pending = stale.loadTaskMemory();
  stale.accountEpoch++;
  stale.resetTaskMemory();
  finish({agent_memory: state});
  await pending;
  assert.equal(stale.el('agent-memory-list').children.length, 0);
  console.log(JSON.stringify({ui_memory_checks: 3, passed: 3, model_api_calls: 0, live_device_reads: 0}));
}

main().catch(error => { console.error(error); process.exitCode = 1; });
