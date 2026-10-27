const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto: crypto} = require('node:crypto');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function environment({monitoring = true, existing, count = 1, parallelism = 1, hold = false, stale = false, timeout = 1000} = {}) {
  const calls = [], created = [], changed = [], pending = [], documents = new Map(), tabs = new Map();
  const local = {key: 'fixture', enabled: true}, session = {};
  let reads = 0;
  if (existing) {
    session.tab_ozon_0 = {id: 1, url: 'https://www.ozon.ru/product/1/'};
    tabs.set(1, {id: 1, url: existing});
    documents.set(1, 'old-document');
  }
  const jobs = Array.from({length: count}, (_, i) => ({id: i + 1, market: 'ozon', article: String(i + 1), variant: '', url: `https://www.ozon.ru/product/${i + 1}/`, lease: `lease${i}`, timeout_ms: timeout}));
  const storage = object => ({get: async () => ({...object}), set: async value => Object.assign(object, value)});
  const listener = {addListener() {}};
  const context = vm.createContext({URL, AbortController, crypto, console,
    setTimeout: (fn, delay) => setTimeout(fn, delay === 100 ? 0 : delay), clearTimeout,
    fetch: async (url, options) => {
      assert.equal(new URL(url).origin, 'http://127.0.0.1:18765');
      calls.push({url, body: options.body ? JSON.parse(options.body) : null});
      const data = url.endsWith('/status') ? {protocol: 2, monitoring, parallelism} : url.endsWith('/next') ? {job: jobs.shift() || null} : {ok: true};
      return {ok: true, json: async () => data};
    },
    chrome: {
      storage: {local: storage(local), session: storage(session)},
      alarms: {onAlarm: listener, create() {}},
      runtime: {onInstalled: listener, onStartup: listener, onMessage: listener},
      tabs: {
        // status intentionally stays loading: images must not hold up price collection.
        get: async id => ({...tabs.get(id), status: 'loading'}),
        create: async options => {
          const id = tabs.size + 2, tab = {id, url: options.url};
          tabs.set(id, tab); documents.set(id, `doc${id}`); created.push(options); return tab;
        },
        update: async (id, options) => { changed.push([id, options]); tabs.set(id, {id, url: options.url}); documents.set(id, 'new-document'); },
        reload: async id => { changed.push([id]); documents.set(id, 'new-document'); },
        sendMessage: async (id, message) => {
          if (message.type === 'paintwatch.ready') {
            reads++;
            return {documentId: stale && reads <= 3 ? 'old-document' : documents.get(id), url: tabs.get(id).url};
          }
          assert.notEqual(message.job.documentId, 'old-document');
          const result = {kind: 'visible', url: tabs.get(id).url, name: 'Paint', price: 35000, currency: 'RUB', price_kind: 'ozon_card'};
          if (hold) return new Promise(resolve => pending.push(() => resolve(result)));
          return result;
        }
      }
    }
  });
  context.importScripts = name => vm.runInContext(fs.readFileSync(path.join(__dirname, '..', name), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'background.js'), 'utf8'), context);
  // Exercise the scheduler explicitly; never leave real polling timers behind in tests.
  vm.runInContext('schedule = () => {}', context);
  const settle = async () => { await vm.runInContext('Promise.all([...active.values()])', context); };
  return {context, calls, created, changed, local, pending, settle};
}
test('background sends a leased result before images finish loading', async () => {
  const e = environment();
  await e.context.tick(); await e.settle();
  assert.equal(e.created.length, 1);
  assert.equal(e.created[0].active, false);
  const sent = e.calls.find(c => c.url.endsWith('/result')).body;
  assert.equal(sent.id, 1); assert.equal(sent.lease, 'lease0'); assert.equal(sent.price, 35000);
  const claim = e.calls.find(c => c.url.endsWith('/next')).body;
  assert.equal(claim.protocol, 2); assert.equal(claim.slot, 0); assert.ok(claim.client);
});
test('paused desktop does not navigate or ask for a new job', async () => {
  const e = environment({monitoring: false});
  await e.context.tick(); await e.settle();
  assert.equal(e.created.length, 0);
  assert.ok(!e.calls.some(c => c.url.endsWith('/next')));
});
test('a tab repurposed by the user is not reloaded or replaced', async () => {
  const e = environment({existing: 'https://example.com/'});
  await e.context.tick(); await e.settle();
  assert.equal(e.created.length, 1); assert.equal(e.changed.length, 0);
});
test('reload waits for a fresh document instead of accepting the previous card DOM', async () => {
  const e = environment({existing: 'https://www.ozon.ru/product/1/', stale: true});
  await e.context.tick(); await e.settle();
  assert.equal(e.created.length, 0); assert.equal(e.changed.length, 1);
  assert.equal(e.calls.filter(c => c.url.endsWith('/result')).length, 1);
});
test('four cards really overlap and a free slot refills while three remain blocked', async () => {
  const e = environment({count: 8, parallelism: 4, hold: true});
  await e.context.tick();
  for (let n = 0; e.pending.length < 4 && n < 100; n++) await sleep(2);
  assert.equal(e.pending.length, 4);
  assert.equal(e.created.length, 4);
  await Promise.all([e.context.tick(), e.context.tick()]);
  assert.equal(e.calls.filter(c => c.url.endsWith('/next')).length, 4);
  e.pending.shift()();
  for (let n = 0; !e.calls.some(c => c.url.endsWith('/result')) && n < 100; n++) await sleep(2);
  await sleep(2); await e.context.tick();
  for (let n = 0; e.pending.length < 4 && n < 100; n++) await sleep(2);
  assert.equal(e.pending.length, 4);
  assert.equal(e.calls.filter(c => c.url.endsWith('/next')).length, 5);
  // Reuse of the first slot reloads its own tab, rather than opening 200 tabs.
  assert.equal(e.created.length, 4);
  for (const release of e.pending.splice(0)) release();
  await e.settle();
});

test('a hung card is bounded by its timeout and reports its lease only once', async () => {
  const e = environment({hold: true, timeout: 30});
  await e.context.tick(); await e.settle();
  const results = e.calls.filter(c => c.url.endsWith('/result'));
  assert.equal(results.length, 1);
  assert.equal(results[0].body.lease, 'lease0');
  assert.match(results[0].body.error, /Время проверки/);
  for (const release of e.pending.splice(0)) release();
  await sleep(2);
  assert.equal(e.calls.filter(c => c.url.endsWith('/result')).length, 1);
});
