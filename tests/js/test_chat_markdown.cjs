const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(tag = '#text', text = '') { this.tagName = tag; this.children = []; this.value = text; this.attributes = {}; }
  append(...nodes) { this.children.push(...nodes.map(n => typeof n === 'string' ? new Element('#text', n) : n)); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  set textContent(v) { this.children = []; this.value = String(v); }
  get textContent() { return this.value + this.children.map(n => n.textContent).join(''); }
  set innerHTML(v) { throw new Error('HTML injection must never be used'); }
}
const context = vm.createContext({ document: { createElement: tag => new Element(tag), createTextNode: t => new Element('#text', t) }, URL });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../miniflow/ui/static/chat-markdown.js'), 'utf8'), context);
const render = text => context.claudeMarkdown(text);
const all = (root, tag) => [root, ...root.children.flatMap(n => all(n, '*'))].filter(n => tag === '*' || n.tagName === tag);

test('readable headings, emphasis, lists and inline code', () => {
  const root = render('## Status\n\nReview **these blocks** before running.\n\n- **`parse_rss`**\n- *Filter items*\n\n1. Review\n2. Approve');
  assert.equal(all(root, 'h3')[0].textContent, 'Status');
  assert.equal(all(root, 'ul').length, 1);
  assert.equal(all(root, 'ol').length, 1);
  assert.equal(all(root, 'li').length, 4);
  assert.equal(all(root, 'code')[0].textContent, 'parse_rss');
  assert.equal(all(root, 'strong')[0].textContent, 'these blocks');
  assert.equal(all(root, 'em')[0].textContent, 'Filter items');
});
test('tables keep rows and headings; code containing a pipe stays in one cell', () => {
  const root = render('| Block | Purpose |\n|---|---|\n| `a|b` | Parse feeds |\n| Filter | Remove duplicates |');
  assert.equal(all(root, 'table').length, 1);
  assert.equal(all(root, 'th').length, 2);
  assert.equal(all(root, 'td').length, 4);
  assert.equal(all(root, 'code')[0].textContent, 'a|b');
});
test('complete and still-streaming fences preserve exact code and diagram spacing', () => {
  const root = render('```python\nprint("<script>")\n  next()\n```\n\n```text\na   → b\n      ↓');
  assert.equal(all(root, 'pre').length, 2);
  assert.equal(all(root, 'pre')[0].textContent, 'print("<script>")\n  next()');
  assert.equal(all(root, 'pre')[1].textContent, 'a   → b\n      ↓');
  assert.equal(all(root, 'script').length, 0);
});
test('remote links work; javascript and raw HTML remain harmless text', () => {
  const root = render('[Source](https://example.org/feed)\n\n[Attack](javascript:alert(1)) <img src=x onerror=alert(1)> <script>alert(1)</script>');
  const links = all(root, 'a');
  assert.equal(links.length, 1);
  assert.equal(links[0].href, 'https://example.org/feed');
  assert.equal(links[0].rel, 'noopener noreferrer');
  assert.equal(all(root, 'img').length, 0);
  assert.equal(all(root, 'script').length, 0);
  assert.match(root.textContent, /<img/);
});
test('quotes, nested lists, partial delimiters and long messages remain readable', () => {
  const root = render('> **Note:** review first\n\n- First\n  - Nested\n- Last\n\nUnfinished **bold\n\n' + 'x'.repeat(100000));
  assert.equal(all(root, 'blockquote').length, 1);
  assert.equal(all(root, 'ul').length, 2);
  assert.match(root.textContent, /Unfinished \*\*bold/);
  assert.ok(root.textContent.endsWith('x'.repeat(100000)));
});

test('bare underscore identifiers keep their spelling', () => {
  const root = render('fetch_and_parse and filter_opportunities stay unchanged.');
  assert.equal(root.textContent, 'fetch_and_parse and filter_opportunities stay unchanged.');
  assert.equal(all(root, 'em').length, 0);
});
