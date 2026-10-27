const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto: crypto} = require('node:crypto');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function environment({monitoring = true, existing, count = 1, parallelism = 1, hold = false, stale = false, timeout = 1000} = {}) {
  const calls = [], created = [], changed = [], closed = [], pending = [], documents = new Map(), tabs = new Map();
  const local = {key: 'fixture', enabled: true}, session = {};
  let reads = 0, nextTabId = 2, onMessage;
  if (existing) {
    session.tab_ozon_0 = {id: 1, url: 'https://www.ozon.ru/product/1/'};
    tabs.set(1, {id: 1, url: existing});
    documents.set(1, 'legacy-document');
  }
  const jobs = Array.from({length: count}, (_, i) => ({id: i + 1, market: 'ozon', article: String(i + 1), variant: '', url: `https://www.ozon.ru/product/${i + 1}/`, lease: `lease${i}`, timeout_ms: timeout}));
  const storage = object => ({get: async () => ({...object}), set: async value => Object.assign(object, value), remove: async key => { delete object[key]; }});
  const listener = {addListener() {}};
  const context = vm.createContext({URL, AbortController, crypto, console,
    setTimeout: (fn, delay) => setTimeout(fn, delay === 100 ? 0 : delay), clearTimeout,
    fetch: async (url, options) => {
      assert.equal(new URL(url).origin, 'http://127.0.0.1:18765');
      calls.push({url, body: options.body ? JSON.parse(options.body) : null});
      const data = url.endsWith('/status') ? {protocol: 2, monitoring, parallelism, wb_price_kind: 'wb_wallet'} : url.endsWith('/next') ? {job: jobs.shift() || null} : {ok: true};
      return {ok: true, json: async () => data};
    },
    chrome: {
      storage: {local: storage(local), session: storage(session)},
      alarms: {onAlarm: listener, create() {}},
      runtime: {getManifest: () => ({version: '0.4.2'}), onInstalled: listener, onStartup: listener, onMessage: {addListener: fn => { onMessage = fn; }}},
      tabs: {
        // status intentionally stays loading: images must not hold up price collection.
        get: async id => ({...tabs.get(id), status: 'loading'}),
        create: async options => {
          const id = nextTabId++, tab = {id, url: options.url};
          tabs.set(id, tab); documents.set(id, `doc${id}`); created.push(options); return tab;
        },
        remove: async id => { closed.push(id); tabs.delete(id); },
        update: async (id, options) => { changed.push([id, options]); tabs.set(id, {id, url: options.url}); documents.set(id, 'new-document'); },
        reload: async id => { changed.push([id]); documents.set(id, 'new-document'); },
        sendMessage: async (id, message) => {
          if (message.type === 'paintwatch.ready') {
            reads++;
            return {documentId: stale && reads <= 3 ? '' : documents.get(id), url: tabs.get(id).url};
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
  return {context, calls, created, changed, closed, local, session, pending, settle, tabs, message: message => new Promise(reply => onMessage(message, {}, reply))};
}
test('background sends a leased result before images finish loading', async () => {
  const e = environment();
  await e.context.tick(); await e.settle();
  assert.equal(e.created.length, 1);
  assert.equal(e.created[0].active, false);
  const sent = e.calls.find(c => c.url.endsWith('/result')).body;
  assert.equal(sent.id, 1); assert.equal(sent.lease, 'lease0'); assert.equal(sent.price, 35000);
  assert.equal(e.closed.length, 1);
  assert.equal(e.tabs.size, 0);
  assert.equal(e.session.check_tab_0, undefined);
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
  assert.ok(e.tabs.has(1)); assert.ok(!e.closed.includes(1));
});
test('old checker tabs are cleaned on upgrade and new temporary tabs close after reading', async () => {
  const e = environment({existing: 'https://www.ozon.ru/product/1/', stale: true});
  await e.context.tick(); await e.settle();
  assert.equal(e.created.length, 1); assert.equal(e.changed.length, 0);
  assert.equal(e.closed.length, 2);
  assert.equal(e.tabs.size, 0);
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
  // The finished card is closed before its slot opens another one.
  assert.equal(e.created.length, 5);
  assert.equal(e.closed.length, 1);
  assert.equal(e.tabs.size, 4);
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
  assert.equal(e.closed.length, 1);
  assert.equal(e.tabs.size, 0);
  for (const release of e.pending.splice(0)) release();
  await sleep(2);
  assert.equal(e.calls.filter(c => c.url.endsWith('/result')).length, 1);
});

test('orphaned temporary tabs from a stopped service worker are closed', async () => {
  const e = environment({count: 0});
  e.session.check_tab_3 = {id: 91, url: 'https://www.ozon.ru/product/91/'};
  e.tabs.set(91, {id: 91, url: e.session.check_tab_3.url});
  await e.context.tick(); await e.settle();
  assert.ok(e.closed.includes(91));
  assert.equal(e.session.check_tab_3, undefined);
});
test('a checker tab navigated to another page by the user is left open', async () => {
  const e = environment({hold: true});
  await e.context.tick();
  for (let n = 0; e.pending.length < 1 && n < 100; n++) await sleep(2);
  const [id] = e.tabs.keys();
  e.tabs.set(id, {id, url: 'https://example.com/'});
  e.pending.shift()();
  await e.settle();
  assert.ok(e.tabs.has(id)); assert.equal(e.closed.length, 0);
  assert.equal(e.session.check_tab_0, undefined);
});

test('the popup can reopen a closed card as a normal tab for manual checking', async () => {
  const e = environment();
  await e.context.tick(); await e.settle();
  assert.equal(e.tabs.size, 0);
  await e.message({type: 'paintwatch.open'}); await sleep(2);
  assert.equal(e.tabs.size, 1);
  assert.equal(e.created.at(-1).active, true);
  assert.equal(e.created.at(-1).url, 'https://www.ozon.ru/product/1/');
  await e.context.tick(); await e.settle();
  assert.equal(e.tabs.size, 1);
});

test('diagnostics injects the current extractor and works without desktop or a content reply', async () => {
  const e = environment();
  e.context.chrome.tabs.query = async () => [{id: 81, url: 'https://www.wildberries.ru/catalog/708052242/detail.aspx'}];
  const injections = [];
  e.context.chrome.scripting = {executeScript: async options => {
    injections.push(options);
    return options.files ? [{frameId: 0}] : [{frameId: 0, result: {report_version: 1, market: 'wb', article: '708052242', matched: {price: 26100, price_kind: 'wb_wallet'}}}];
  }};
  e.context.fetch = async () => { throw new TypeError('Failed to fetch'); };
  const data = await e.message({type: 'paintwatch.diagnoseActive'});
  assert.equal(data.matched.price, 26100);
  assert.equal(data.extension_version, '0.4.2');
  assert.equal(injections.length, 2);
  assert.equal(injections[0].files[0], 'extractor.js');
  assert.equal(injections[0].target.tabId, 81);
  assert.equal(typeof injections[1].func, 'function');
  assert.equal(e.calls.length, 0); assert.equal(e.created.length, 0);
});
test('diagnostics rejects unsupported tabs and distinguishes denied script access', async () => {
  const e = environment();
  let injections = 0;
  e.context.chrome.tabs.query = async () => [{id: 81, url: 'https://example.com/'}];
  e.context.chrome.scripting = {executeScript: async () => { injections++; throw new Error('Denied'); }};
  assert.match((await e.message({type: 'paintwatch.diagnoseActive'})).error, /Откройте карточку/);
  assert.equal(injections, 0);
  e.context.chrome.tabs.query = async () => [{id: 81, url: 'https://www.wildberries.ru/catalog/708052242/detail.aspx'}];
  assert.match((await e.message({type: 'paintwatch.diagnoseActive'})).error, /Chrome не разрешил/);
});
test('an empty script result produces an actionable error rather than undefined', async () => {
  const e = environment();
  e.context.chrome.tabs.query = async () => [{id: 81, url: 'https://www.wildberries.ru/catalog/708052242/detail.aspx'}];
  e.context.chrome.scripting = {executeScript: async () => [{frameId: 0}]};
  assert.match((await e.message({type: 'paintwatch.diagnoseActive'})).error, /не вернул отчёт/);
});
test('desktop fetch failure explains the loopback connection and does not expose raw browser text', async () => {
  const e = environment();
  e.context.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(e.context.api('/v1/status'), /Нет связи с PaintWatch.*127.0.0.1:18765/);
  const response = await e.message({type: 'paintwatch.connect', key: 'fixture'});
  assert.equal(response.ok, false);
  assert.match(response.error, /Запустите PaintWatch.exe/);
  assert.ok(!response.error.includes('Failed to fetch'));
});
