const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function environment({monitoring = true, existing} = {}) {
  const calls = [], created = [], changed = [];
  const local = {key: 'fixture', enabled: true}, session = {};
  if (existing) session.ownedTabs = {ozon: {id: 1, url: 'https://www.ozon.ru/product/196525150/'}};
  let jobs = [{id: 7, market: 'ozon', article: '196525150', variant: '', url: 'https://www.ozon.ru/product/196525150/', lease: 'lease'}];
  const storage = object => ({get: async () => ({...object}), set: async value => Object.assign(object, value)});
  const listener = {addListener() {}};
  const context = vm.createContext({URL, AbortController, console,
    setTimeout: (fn, delay) => setTimeout(fn, delay < 5000 ? 0 : delay), clearTimeout,
    fetch: async (url, options) => {
      assert.equal(new URL(url).origin, 'http://127.0.0.1:18765');
      calls.push({url, body: options.body ? JSON.parse(options.body) : null});
      const data = url.endsWith('/status') ? {monitoring} : url.endsWith('/next') ? {job: jobs.shift() || null} : {ok: true};
      return {ok: true, json: async () => data};
    },
    chrome: {
      storage: {local: storage(local), session: storage(session)},
      alarms: {onAlarm: listener, create() {}},
      runtime: {onInstalled: listener, onStartup: listener, onMessage: listener},
      tabs: {
        get: async id => ({id, status: 'complete', url: existing || session.ownedTabs?.ozon?.url || 'https://www.ozon.ru/product/196525150/'}),
        create: async options => { created.push(options); return {id: 2, url: options.url}; },
        update: async (...args) => changed.push(args),
        reload: async (...args) => changed.push(args),
        sendMessage: async () => ({kind: 'visible', url: 'https://www.ozon.ru/product/196525150/', name: 'Paint', price: 35000, currency: 'RUB', price_kind: 'ozon_card'})
      }
    }
  });
  context.importScripts = name => vm.runInContext(fs.readFileSync(path.join(__dirname, '..', name), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8'), context);
  return {context, calls, created, changed, local};
}
test('background passes an issued job and its lease to the desktop application', async () => {
  const e = environment();
  await e.context.tick();
  assert.equal(e.created.length, 1);
  assert.equal(e.created[0].active, false);
  const sent = e.calls.find(c => c.url.endsWith('/result')).body;
  assert.equal(sent.id, 7);
  assert.equal(sent.lease, 'lease');
  assert.equal(sent.price, 35000);
});
test('paused desktop does not navigate or ask for a new job', async () => {
  const e = environment({monitoring: false});
  await e.context.tick();
  assert.equal(e.created.length, 0);
  assert.ok(!e.calls.some(c => c.url.endsWith('/next')));
});
test('a tab repurposed by the user is not reloaded or replaced', async () => {
  const e = environment({existing: 'https://example.com/'});
  await e.context.tick();
  assert.equal(e.created.length, 1);
  assert.equal(e.changed.length, 0);
});
