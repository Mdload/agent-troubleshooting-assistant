/* Verify Markdown tables, blockquotes, headings, hr, inline formatting and XSS safety. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const root = process.argv[2] || path.join(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'web/index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(page);

class Node {
  constructor(tag = '', text = '', type = 1) {
    this.nodeType = type;
    this.tagName = tag.toUpperCase();
    this.value = text;
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.className = '';
  }
  get childNodes() { return this.children; }
  get textContent() { return this.nodeType === 3 ? this.value : this.value + this.children.map(n => n.textContent).join(''); }
  set textContent(v) { if (this.nodeType === 3) { this.value = v; return; } this.value = ''; this.children = []; if (v) this.append(new Node('', v, 3)); }
  append(...items) {
    for (const item of items) {
      if (item.nodeType === 11) { this.append(...item.children); continue; }
      item.parentElement = this;
      this.children.push(item);
    }
  }
  replaceWith(fragment) {
    const p = this.parentElement, i = p.children.indexOf(this);
    const nodes = fragment.nodeType === 11 ? fragment.children : [fragment];
    for (const n of nodes) n.parentElement = p;
    p.children.splice(i, 1, ...nodes);
  }
  setAttribute(name, value) { this[name] = value; }
}

const document = {
  createElement: tag => new Node(tag),
  createTextNode: text => new Node('', text, 3),
  createDocumentFragment: () => new Node('', '', 11),
};
const sandbox = { document, URL, deviceName: d => d };
vm.createContext(sandbox);
vm.runInContext(page.slice(page.indexOf('function add('), page.indexOf('function tokenUsageNote(')), sandbox);
vm.runInContext(page.slice(page.indexOf('function observationLabel('), page.indexOf('// 卡片仅整理')), sandbox);

function all(n) { return [n, ...n.children.flatMap(all)]; }

const sample = [
  '> **结论**：局端主机当前持有 VIP，业务服务运行正常。',
  '',
  '### 检查结果',
  '',
  '| 设备 | Keepalived | Croc | 备注 |',
  '| :--- | :---: | ---: | --- |',
  '| **局端主机** | `active` | `active` | [文档](https://example.org/ha) |',
  '| 局端备机 | `active` | ~~stopped~~ | 待机 |',
  '',
  '---',
  '',
  '#### 下一步建议',
  '- 确认远端链路状态',
].join('\n');

const box = new Node('div');
sandbox.renderAnswer(box, sample, {});
const nodes = all(box);

assert.equal(nodes.filter(n => n.tagName === 'BLOCKQUOTE').length, 1);
assert.equal(nodes.filter(n => n.tagName === 'TABLE').length, 1);
assert.equal(nodes.filter(n => n.tagName === 'TH').length, 4);
assert.equal(nodes.filter(n => n.tagName === 'TD').length, 8);
assert.equal(nodes.filter(n => n.tagName === 'HR').length, 1);
assert.equal(nodes.filter(n => n.tagName === 'H3').length, 1);
assert.equal(nodes.filter(n => n.tagName === 'H4').length, 1);
assert.equal(nodes.filter(n => n.tagName === 'DEL').length, 1);
assert.equal(nodes.filter(n => n.tagName === 'A').length, 1);
assert.equal(nodes.filter(n => n.tagName === 'A')[0].href, 'https://example.org/ha');

const ths = nodes.filter(n => n.tagName === 'TH');
assert.equal(ths[1].style.textAlign, 'center');
assert.equal(ths[2].style.textAlign, 'right');

// XSS inside table cell must remain literal text
const xssBox = new Node('div');
sandbox.renderAnswer(xssBox, '| 列1 | 列2 |\n| --- | --- |\n| <img src=x onerror=alert(1)> | [bad](javascript:alert(1)) |', {});
assert.equal(all(xssBox).filter(n => n.tagName === 'IMG').length, 0);
assert.equal(all(xssBox).filter(n => n.tagName === 'A').length, 0);

console.log(JSON.stringify({ ui_markdown_checks: 4, passed: 4 }));
