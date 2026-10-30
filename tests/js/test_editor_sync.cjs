const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const context = vm.createContext({ Studio: { flow: { steps: {} } } });
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../miniflow/ui/static/editor-sync.js'), 'utf8'), context);
const changes = (a, b) => context.studioFlowChanges(a, b);
// values made inside the vm context have their own Array prototype; compare plain copies
const plain = (value) => JSON.parse(JSON.stringify(value));

const before = {
  description: 'feed',
  steps: {
    read: { block: 'read_csv', settings: { path: 'a.csv' }, position: { x: 10, y: 10 } },
    keep: { block: 'filter_rows', settings: { rows: '{{ read }}', where: 'x > 1' } },
    log: { block: 'log', after: ['keep'] },
  },
};

test('added, changed and removed steps; moving a step is not a change', () => {
  const after = {
    description: 'feed',
    steps: {
      read: { block: 'read_csv', settings: { path: 'a.csv' }, position: { x: 400, y: 90 } },
      keep: { block: 'filter_rows', settings: { rows: '{{ read }}', where: 'x > 2' } },
      save: { block: 'write_csv', settings: { rows: '{{ keep }}' } },
    },
  };
  const c = changes(before, after);
  assert.deepEqual(plain([...c.added]), ['save']);
  assert.deepEqual(plain([...c.changed.entries()]), [['keep', ['where']]]);
  assert.deepEqual(plain(c.removed.map((r) => r.name)), ['log']);
  assert.deepEqual(plain([...c.addedWires]), ['keep→save']);
  assert.deepEqual(plain(c.removedWires), ['keep→log']);
  assert.deepEqual(plain(c.flow), []);
  assert.equal(context.studioChangeSummary(c),
    '1 step added · 1 step changed · 1 step removed · 2 connections changed');
});

test('connections from {{ step }} and after both count; flow-level fields are listed', () => {
  const after = JSON.parse(JSON.stringify(before));
  after.description = 'feed v2';
  after.steps.log.after = [];
  after.steps.log.settings = { message: '{{ read }}' };
  const c = changes(before, after);
  assert.deepEqual(plain([...c.addedWires]), ['read→log']);
  assert.deepEqual(plain(c.removedWires), ['keep→log']);
  assert.deepEqual(plain(c.changed.get('log')), ['after', 'message']);
  assert.deepEqual(plain(c.flow), ['description']);
});

test('a brand-new flow marks every step as added', () => {
  const c = changes({ steps: {} }, before);
  assert.deepEqual(plain([...c.added]), ['read', 'keep', 'log']);
  assert.equal(c.changed.size, 0);
  assert.equal(context.studioChangeCount(changes(before, before)), 0);
  // fields the editor fills in with empty values are not changes
  const filled = { ...before, tests: [], params: {}, schedules: [] };
  assert.equal(context.studioChangeCount(changes(before, filled)), 0);
});

test('saves by an agent are attributed to Claude', () => {
  assert.equal(context.studioSyncAuthor({ message: 'add a filter (agent)' }), 'Claude');
  assert.equal(context.studioSyncAuthor({ message: 'tidy' }), 'Another editor');
});
