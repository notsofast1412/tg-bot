const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto: crypto} = require('node:crypto');
const {FakeClock, flush} = require('./fake_clock.cjs');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function environment({monitoring = true, existing, count = 1, parallelism = 1, hold = false, stale = false, timeout = 1000, clock, local: initialLocal = {}} = {}) {
  const calls = [], created = [], changed = [], closed = [], pending = [], ruleUpdates = [], rules = new Map(), documents = new Map(), tabs = new Map();
  const local = {key: 'fixture', enabled: true, ...initialLocal}, session = {};
  let reads = 0, nextTabId = 2, onMessage;
  if (existing) {
    session.tab_ozon_0 = {id: 1, url: 'https://www.ozon.ru/product/1/'};
    tabs.set(1, {id: 1, url: existing});
    documents.set(1, 'legacy-document');
  }
  const jobs = Array.from({length: count}, (_, i) => ({id: i + 1, market: 'ozon', article: String(i + 1), variant: '', url: `https://www.ozon.ru/product/${i + 1}/`, lease: `lease${i}`, timeout_ms: timeout}));
  const storage = object => ({get: async () => ({...object}), set: async value => Object.assign(object, value), remove: async keys => { for (const key of [].concat(keys)) delete object[key]; }});
  const listener = {addListener() {}};
  const context = vm.createContext({URL, AbortController, crypto, console,
    ...(clock ? {Date: clock.Date} : {}),
    setTimeout: clock ? clock.setTimeout : (fn, delay) => setTimeout(fn, delay === 100 ? 0 : delay), clearTimeout: clock ? clock.clearTimeout : clearTimeout,
    fetch: async (url, options) => {
      assert.equal(new URL(url).origin, 'http://127.0.0.1:18765');
      calls.push({url, body: options.body ? JSON.parse(options.body) : null});
      const data = url.endsWith('/status') ? {protocol: 2, monitoring, parallelism, wb_price_kind: 'wb_wallet', availability_status: true} : url.endsWith('/next') ? {job: jobs.shift() || null} : {ok: true};
      return {ok: true, json: async () => data};
    },
    chrome: {
      storage: {local: storage(local), session: storage(session)},
      alarms: {onAlarm: listener, create() {}},
      runtime: {getManifest: () => ({version: '0.4.9'}), getURL: name => `chrome-extension://paintwatch/${name}`, onInstalled: listener, onStartup: listener, onMessage: {addListener: fn => { onMessage = fn; }}},
      declarativeNetRequest: {
        getSessionRules: async () => [...rules.values()],
        updateSessionRules: async options => {
          ruleUpdates.push(JSON.parse(JSON.stringify(options)));
          for (const id of options.removeRuleIds || []) rules.delete(id);
          for (const rule of options.addRules || []) rules.set(rule.id, JSON.parse(JSON.stringify(rule)));
        }
      },
      tabs: {
        // status intentionally stays loading: images must not hold up price collection.
        get: async id => {
          if (!tabs.has(id)) throw new Error('No tab');
          return {...tabs.get(id), status: 'loading'};
        },
        query: async () => [...tabs.values()],
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
  return {context, calls, created, changed, closed, local, session, pending, ruleUpdates, rules, settle, tabs, message: message => new Promise(reply => onMessage(message, {}, reply))};
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
  assert.equal(data.extension_version, '0.4.9');
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
test('a sold-out Ozon search redirect reports availability and closes the same owned tab', async () => {
  const e = environment();
  const create = e.context.chrome.tabs.create;
  e.context.chrome.tabs.create = async options => {
    const tab = await create(options);
    e.tabs.set(tab.id, {id: tab.id, url: 'https://www.ozon.ru/search/?text=paint&product_id=1'});
    return tab;
  };
  const send = e.context.chrome.tabs.sendMessage;
  e.context.chrome.tabs.sendMessage = async (id, message) => message.type === 'paintwatch.ready' ? send(id, message) : {
    kind: 'sold_out', availability: 'out_of_stock', evidence: 'ozon_sold_out_search', url: e.tabs.get(id).url
  };
  await e.context.tick(); await e.settle();
  const sent = e.calls.find(c => c.url.endsWith('/result')).body;
  assert.equal(sent.kind, 'sold_out'); assert.equal(sent.price, undefined); assert.equal(sent.error, undefined);
  assert.equal(e.created.length, 1); assert.equal(e.closed.length, 1); assert.equal(e.tabs.size, 0);
  assert.match(e.local.status, /товар закончился/);
});
test('a redirect during collection is retried only in the new document for the original SKU', async () => {
  const e = environment();
  const send = e.context.chrome.tabs.sendMessage;
  let attempts = 0;
  e.context.chrome.tabs.sendMessage = async (id, message) => {
    if (message.type === 'paintwatch.ready') {
      const state = await send(id, message);
      return attempts ? {...state, documentId: 'redirect-document'} : state;
    }
    attempts++;
    if (attempts === 1) {
      e.tabs.set(id, {id, url: 'https://www.ozon.ru/search/?product_id=1'});
      throw new Error('The message port closed before a response was received');
    }
    assert.equal(message.job.documentId, 'redirect-document');
    return {kind: 'sold_out', availability: 'out_of_stock', evidence: 'ozon_sold_out_search', url: e.tabs.get(id).url};
  };
  await e.context.tick(); await e.settle();
  assert.equal(attempts, 2); assert.equal(e.created.length, 1);
  assert.equal(e.calls.filter(c => c.url.endsWith('/result')).length, 1);
  assert.equal(e.calls.find(c => c.url.endsWith('/result')).body.kind, 'sold_out');
  assert.equal(e.closed.length, 1);
});
test('orphaned sold-out tabs close, but unrelated search pages remain open', async () => {
  const e = environment({count: 0});
  for (const [slot, id, url] of [[0, 90, 'https://www.ozon.ru/search/?product_id=1'], [1, 91, 'https://www.ozon.ru/search/?product_id=2'], [2, 92, 'https://www.ozon.ru/search/?text=paint']]) {
    e.session[`check_tab_${slot}`] = {id, url: 'https://www.ozon.ru/product/1/'};
    e.tabs.set(id, {id, url});
  }
  await e.context.tick(); await e.settle();
  assert.deepEqual(e.closed, [90]); assert.ok(e.tabs.has(91)); assert.ok(e.tabs.has(92));
});
test('a desktop lacking availability support asks for the application update before claiming a job', async () => {
  const e = environment();
  e.context.fetch = async () => ({ok: true, json: async () => ({protocol: 2, wb_price_kind: 'wb_wallet', monitoring: true, parallelism: 4})});
  const response = await e.message({type: 'paintwatch.connect', key: 'fixture'});
  assert.equal(response.ok, false); assert.match(response.error, /0\.4\.5/); assert.equal(e.created.length, 0);
});
test('lightweight mode blocks only images and media in owned marketplace tabs and clears its rules', async () => {
  const e = environment();
  await e.context.tick(); await e.settle();
  const rule = e.ruleUpdates.find(update => update.addRules)?.addRules[0];
  assert.deepEqual(rule.condition.resourceTypes, ['image', 'media']);
  assert.deepEqual(rule.condition.initiatorDomains, ['ozon.ru', 'wildberries.ru']);
  assert.equal(rule.condition.tabIds.length, 1);
  assert.deepEqual(rule.condition.tabIds, e.closed);
  assert.equal(rule.action.type, 'block');
  assert.equal(e.rules.size, 0);
  assert.equal(Object.keys(e.session).filter(key => key.startsWith('check_tab_')).length, 0);
});
test('lightweight mode can be disabled and ordinary manual tabs receive no rules', async () => {
  const e = environment();
  e.local.lightweight = false;
  await e.context.tick(); await e.settle();
  assert.equal(e.ruleUpdates.length, 0);
  await e.message({type: 'paintwatch.open'}); await flush();
  assert.equal(e.created.at(-1).active, true);
  assert.equal(e.rules.size, 0);
});
test('orphaned media rules are removed even if session tab ownership was lost', async () => {
  const e = environment({count: 0});
  e.rules.set(101, {id: 101});
  e.rules.set(102, {id: 102});
  await e.context.tick(); await e.settle();
  assert.equal(e.rules.size, 0);
});
test('a reused user tab loses its media rule and remains open', async () => {
  const e = environment({hold: true});
  await e.context.tick(); await flush();
  assert.equal(e.pending.length, 1); assert.equal(e.rules.size, 1);
  const [id] = e.tabs.keys();
  e.tabs.set(id, {id, url: 'https://example.com/'});
  e.pending.shift()(); await e.settle();
  assert.equal(e.rules.size, 0); assert.ok(e.tabs.has(id)); assert.equal(e.closed.length, 0);
});
test('late rule installation after timeout cannot overwrite the next slot owner or its rule', async () => {
  const clock = new FakeClock();
  const e = environment({count: 2, hold: true, timeout: 100, clock});
  const update = e.context.chrome.declarativeNetRequest.updateSessionRules;
  let release, first = true;
  e.context.chrome.declarativeNetRequest.updateSessionRules = async options => {
    if (options.addRules && first) {
      first = false;
      await new Promise(resolve => { release = resolve; });
    }
    return update(options);
  };
  await e.context.tick(); await flush();
  assert.equal(typeof release, 'function');
  await clock.advance(100); await e.settle();
  assert.equal(e.closed.length, 1);
  await e.context.tick(); await flush();
  assert.equal(e.pending.length, 1);
  const [activeId] = e.tabs.keys();
  const activeRule = [...e.rules.values()][0].id;
  release(); await flush();
  assert.equal(e.rules.size, 1); assert.ok(e.rules.has(activeRule));
  assert.equal(e.session[`check_tab_${activeId}`].id, activeId);
  assert.ok(e.tabs.has(activeId));
  e.pending.shift()(); await e.settle();
  assert.equal(e.rules.size, 0); assert.equal(e.tabs.size, 0);
});
test('no next-job requests are sent when the desktop reports no eligible work', async () => {
  const e = environment();
  e.context.fetch = async url => {
    assert.ok(url.endsWith('/status'));
    return {ok: true, json: async () => ({protocol: 2, wb_price_kind: 'wb_wallet', availability_status: true, monitoring: true, parallelism: 6, ready: 0})};
  };
  await e.context.tick(); await e.settle();
  assert.equal(e.created.length, 0);
});

const CLEANUP_INTERVAL = 30 * 60 * 1000;
const keeperUrl = 'chrome-extension://paintwatch/idle.html';
function extraTabs(e) {
  e.tabs.set(100, {id: 100, windowId: 1, url: 'https://example.com/', pinned: true});
  e.tabs.set(101, {id: 101, windowId: 2}); // No permission to read this tab's URL is needed.
}
test('after thirty minutes all profile tabs close except one idle tab and the timer repeats', async () => {
  const clock = new FakeClock(), e = environment({clock, count: 0, monitoring: false});
  extraTabs(e);
  await e.context.tick();
  const deadline = e.local.cleanupNextAt;
  assert.equal(deadline, clock.now + CLEANUP_INTERVAL);
  await clock.advance(CLEANUP_INTERVAL - 1); await e.context.tick();
  assert.equal(e.closed.length, 0);
  await clock.advance(1); await e.context.tick();
  assert.equal(e.tabs.size, 1);
  const [keeper] = e.tabs.values();
  assert.equal(keeper.url, keeperUrl);
  assert.ok(e.closed.includes(100)); assert.ok(e.closed.includes(101));
  assert.ok(!e.closed.includes(keeper.id));
  assert.equal(e.local.cleanupNextAt, clock.now + CLEANUP_INTERVAL);
  e.tabs.set(102, {id: 102, url: 'https://www.ozon.ru/search/'});
  await clock.advance(CLEANUP_INTERVAL); await e.context.tick();
  assert.equal(e.tabs.size, 1); assert.ok(e.tabs.has(keeper.id));
  assert.equal(e.created.length, 1); assert.ok(e.closed.includes(102));
  assert.ok(!e.calls.some(call => call.url.endsWith('/next')));
});
test('cleanup drains existing checks without claiming more jobs and resumes after the sweep', async () => {
  const clock = new FakeClock(), e = environment({clock, count: 3, parallelism: 2, hold: true});
  await e.context.tick(); await flush();
  assert.equal(e.pending.length, 2);
  extraTabs(e);
  e.local.cleanupNextAt = clock.now;
  for (let n = 0; n < 3; n++) await e.context.tick();
  assert.equal(e.calls.filter(call => call.url.endsWith('/next')).length, 2);
  assert.equal(e.closed.length, 0); assert.equal(e.tabs.size, 4);
  assert.match(e.local.status, /проверки приостановлены/);
  const resultCountsAtRemoval = [], remove = e.context.chrome.tabs.remove;
  e.context.chrome.tabs.remove = async id => {
    if (id === 100 || id === 101) resultCountsAtRemoval.push(e.calls.filter(call => call.url.endsWith('/result')).length);
    await remove(id);
  };
  for (const release of e.pending.splice(0)) release();
  await e.settle(); await e.context.tick();
  assert.deepEqual(resultCountsAtRemoval, [2, 2]);
  assert.equal(e.tabs.size, 1);
  assert.equal(e.calls.filter(call => call.url.endsWith('/next')).length, 2);
  assert.ok(e.calls.filter(call => call.url.endsWith('/result')).every(call => !call.body.error));
  await e.context.tick(); await flush();
  assert.ok(e.calls.filter(call => call.url.endsWith('/next')).length > 2);
  assert.equal(e.pending.length, 1);
  e.pending.shift()(); await e.settle();
  assert.equal(e.tabs.size, 1);
});
test('restart preserves the cleanup deadline and waits for persisted desktop leases', async () => {
  const clock = new FakeClock(), before = environment({clock, monitoring: false});
  await before.context.tick();
  const e = environment({clock, count: 0, local: {...before.local}});
  extraTabs(e);
  await e.context.tick(); await e.settle();
  assert.equal(e.local.cleanupNextAt, before.local.cleanupNextAt);
  const claims = e.calls.filter(call => call.url.endsWith('/next')).length;
  await clock.advance(CLEANUP_INTERVAL);
  const fetch = e.context.fetch;
  let leased = 2;
  e.context.fetch = async (...args) => {
    const response = await fetch(...args);
    if (!args[0].endsWith('/status')) return response;
    const data = await response.json();
    return {ok: true, json: async () => ({...data, active: leased})};
  };
  await e.context.tick();
  assert.equal(e.closed.length, 0);
  assert.equal(e.calls.filter(call => call.url.endsWith('/next')).length, claims);
  leased = 0;
  await e.context.tick();
  assert.equal(e.tabs.size, 1);
  assert.equal([...e.tabs.values()][0].url, keeperUrl);
});
test('disabled automatic cleanup does not sweep tabs and enabling it starts a fresh interval', async () => {
  const clock = new FakeClock(), e = environment({clock, count: 0, monitoring: false, local: {autoCleanup: false, cleanupNextAt: clock.now - 1}});
  extraTabs(e);
  await e.context.tick();
  assert.equal(e.tabs.size, 2); assert.equal(e.local.cleanupNextAt, undefined);
  e.local.autoCleanup = true;
  await e.context.tick();
  assert.equal(e.local.cleanupNextAt, clock.now + CLEANUP_INTERVAL);
  assert.equal(e.closed.length, 0);
});
test('manual cleanup works with auto cleanup disabled and never turns on stopped monitoring', async () => {
  const e = environment({count: 0, monitoring: false, local: {autoCleanup: false}});
  extraTabs(e);
  const result = await e.message({type: 'paintwatch.cleanup'});
  assert.equal(result.ok, true);
  await flush();
  assert.equal(e.tabs.size, 1); assert.equal(e.local.autoCleanup, false);
  assert.equal(e.local.cleanupRequested, false);
  await e.context.tick();
  assert.ok(e.calls.every(call => call.url.endsWith('/status')));
  assert.equal(e.local.cleanupNextAt, undefined);
});
test('unpaired extension rejects a manual sweep without touching tabs', async () => {
  const e = environment({local: {enabled: false}});
  extraTabs(e);
  const result = await e.message({type: 'paintwatch.cleanup'});
  assert.match(result.error, /подключите/);
  assert.equal(e.tabs.size, 2); assert.equal(e.closed.length, 0);
});
test('keeper creation failure preserves all old tabs and prevents new checks until retry succeeds', async () => {
  const e = environment({local: {cleanupRequested: true}});
  extraTabs(e);
  const create = e.context.chrome.tabs.create;
  e.context.chrome.tabs.create = async () => { throw new Error('Chrome unavailable'); };
  await e.context.tick();
  assert.equal(e.tabs.size, 2); assert.equal(e.closed.length, 0);
  assert.equal(e.local.cleanupRequested, true);
  assert.match(e.local.status, /проверки остаются на паузе/);
  assert.ok(!e.calls.some(call => call.url.endsWith('/next')));
  e.context.chrome.tabs.create = create;
  await e.context.tick();
  assert.equal(e.tabs.size, 1); assert.equal(e.local.cleanupRequested, false);
});
test('partial close failure retries without ever requesting removal of the last tab', async () => {
  const e = environment({local: {cleanupRequested: true}, count: 0});
  extraTabs(e);
  const remove = e.context.chrome.tabs.remove;
  let denied = true;
  const countsBeforeRemove = [];
  e.context.chrome.tabs.remove = async id => {
    countsBeforeRemove.push(e.tabs.size);
    if (id === 101 && denied) throw new Error('Busy');
    await remove(id);
  };
  await e.context.tick();
  assert.equal(e.tabs.size, 2); assert.ok(e.tabs.has(101));
  assert.equal(e.local.cleanupRequested, true);
  assert.ok(!e.calls.some(call => call.url.endsWith('/next')));
  denied = false;
  await e.context.tick();
  assert.equal(e.tabs.size, 1); assert.equal(e.created.length, 1);
  assert.ok(countsBeforeRemove.every(count => count >= 2));
  assert.equal(e.local.cleanupRequested, false);
});
test('desktop disconnect leaves cleanup pending and checks paused until connection recovers', async () => {
  const e = environment({local: {cleanupRequested: true}, count: 0});
  extraTabs(e);
  const fetch = e.context.fetch;
  e.context.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await e.context.tick();
  assert.equal(e.tabs.size, 2); assert.equal(e.local.cleanupRequested, true);
  assert.match(e.local.status, /проверки остаются на паузе/);
  e.context.fetch = fetch;
  await e.context.tick();
  assert.equal(e.tabs.size, 1); assert.equal(e.local.cleanupRequested, false);
});
test('hung checks finish by their existing timeout before cleanup and are reported only once', async () => {
  const clock = new FakeClock(), e = environment({clock, hold: true, timeout: 100});
  await e.context.tick(); await flush();
  e.local.cleanupNextAt = clock.now;
  await e.context.tick();
  assert.equal(e.closed.length, 0);
  await clock.advance(100); await e.settle();
  await e.context.tick();
  assert.equal(e.tabs.size, 1);
  assert.equal(e.calls.filter(call => call.url.endsWith('/result')).length, 1);
  e.pending.shift()(); await flush();
  assert.equal(e.calls.filter(call => call.url.endsWith('/result')).length, 1);
});
test('a delayed tab creation after the sweep is closed even with an unavailable URL', async () => {
  const clock = new FakeClock(), e = environment({clock, timeout: 100});
  const create = e.context.chrome.tabs.create;
  let releaseCreate;
  e.context.chrome.tabs.create = options => options.url === keeperUrl ? create(options) : new Promise(resolve => {
    releaseCreate = async () => {
      const tab = await create(options);
      e.tabs.set(tab.id, {id: tab.id});
      resolve(tab);
    };
  });
  await e.context.tick(); await flush();
  assert.equal(typeof releaseCreate, 'function');
  await clock.advance(100); await e.settle();
  e.local.cleanupRequested = true;
  await e.context.tick();
  assert.equal(e.tabs.size, 1);
  await releaseCreate(); await flush();
  assert.equal(e.tabs.size, 1);
  assert.equal([...e.tabs.values()][0].url, keeperUrl);
  assert.equal(e.calls.filter(call => call.url.endsWith('/result')).length, 1);
});
test('checker cleanup also recognizes the pending product URL before navigation completes', async () => {
  const e = environment({hold: true});
  await e.context.tick(); await flush();
  const [id] = e.tabs.keys();
  e.tabs.set(id, {id, url: '', pendingUrl: 'https://www.ozon.ru/product/1/'});
  e.pending.shift()(); await e.settle();
  assert.equal(e.tabs.size, 0); assert.deepEqual(e.closed, [id]);
});
