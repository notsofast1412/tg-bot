const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto: crypto} = require('node:crypto');
const {FakeClock, flush} = require('./fake_clock.cjs');
async function collect({market = 'ozon', status = 200, body = '<html>Not JSON</html>', navigation = 200, visible = false, soldOut = false, redirect = false, retryAfter = null, mutate = false, documentId, mutationDelay = 5, seller = false, timeout = 200} = {}) {
  let callback, requests = 0;
  const article = market === 'wb' ? '708052242' : '196525150';
  const url = market === 'wb' ? `https://www.wildberries.ru/catalog/${article}/detail.aspx` : redirect ? `https://www.ozon.ru/search/?product_id=${article}` : `https://www.ozon.ru/product/${article}/`;
  let observer, detected = visible;
  const price = {kind: 'visible', price: market === 'wb' ? 26100 : 38000, price_kind: market === 'wb' ? 'wb_wallet' : 'ozon_card', currency: 'RUB', name: 'Краска'};
  const context = vm.createContext({URL, URLSearchParams, AbortController, crypto,
    MutationObserver: class { constructor(fn) { observer = fn; } observe() { if (mutate) setTimeout(() => { detected = true; observer(); }, mutationDelay); } disconnect() {} },
    document: {body: {innerText: 'Краска'}, querySelector: () => null},
    performance: {getEntriesByType: () => [{responseStatus: navigation}]}, location: new URL(url),
    setTimeout, clearTimeout,
    fetch: async (target, options) => {
      requests++;
      assert.equal(options.credentials, 'same-origin');
      return {status, ok: status < 400, url: market === 'wb' ? 'https://www.wildberries.ru/__internal/u-card/cards/v4/detail' : 'https://www.ozon.ru/api/composer-api.bx/page/json/v2', headers: {get: name => name === 'retry-after' ? retryAfter : null}, text: async () => body};
    },
    chrome: {runtime: {onMessage: {addListener: fn => { callback = fn; }}}}
  });
  for (const file of ['extractor.js', 'content.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context);
  context.PaintWatchExtract.visible = () => detected ? price : null;
  context.PaintWatchExtract.soldOut = () => soldOut && detected ? {kind: 'sold_out', availability: 'out_of_stock', evidence: redirect ? 'ozon_sold_out_search' : 'product_sold_out_heading'} : null;
  const result = await new Promise(resolve => callback({type: 'paintwatch.collect', job: {market, article, variant: '', wb_dest: '-1257786', documentId, timeout_ms: timeout, deadline: Date.now() + timeout, seller: seller ? '123' : ''}}, {}, resolve));
  return {result, requests};
}
test('a non-JSON API response is not automatically labelled as a CAPTCHA', async () => {
  const {result, requests} = await collect();
  assert.equal(result.code, 'unavailable');
  assert.match(result.error, /Резервный API OZON/);
  assert.equal(requests, 1);
});
test('an API challenge does not prove a main-page CAPTCHA', async () => {
  const {result} = await collect({body: '<script src="/__wbaas/challenges/antibot/browser-check.js"></script>'});
  assert.equal(result.code, 'unavailable');
});
test('API rejection explicitly identifies the API stage', async () => {
  const {result, requests} = await collect({status: 403});
  assert.equal(result.code, 'unavailable');
  assert.match(result.error, /Резервный API OZON.*403/);
  assert.equal(requests, 1);
});
test('a denied main page stops before the API request', async () => {
  const {result, requests} = await collect({navigation: 403});
  assert.equal(result.code, 'access');
  assert.equal(requests, 0);
});

test('visible price returns immediately without an API request', async () => {
  const {result, requests} = await collect({visible: true});
  assert.equal(result.price, 38000); assert.equal(requests, 0);
});
test('price rendered after a DOM mutation is collected without polling or API fallback', async () => {
  const {result, requests} = await collect({mutate: true});
  assert.equal(result.price, 38000); assert.equal(requests, 0);
});
test('positive Retry-After is respected without a ten-minute floor', async () => {
  const {result} = await collect({status: 429, retryAfter: '15'});
  assert.equal(result.retry_after, 15); assert.equal(result.code, 'rate_limit');
});
test('a collector request for an old document cannot read a new document', async () => {
  const {result, requests} = await collect({visible: true, documentId: 'old-document'});
  assert.match(result.error, /Документ/); assert.equal(requests, 0);
});

test('a price appearing after API 403 is still collected within the overall deadline', async () => {
  const {result, requests} = await collect({status: 403, mutate: true, mutationDelay: 70});
  assert.equal(requests, 1);
  assert.equal(result.kind, 'visible'); assert.equal(result.price, 38000);
});
test('a pinned seller is not silently bypassed by a visible price without seller identity', async () => {
  const {result} = await collect({status: 403, visible: true, seller: true});
  assert.equal(result.code, 'unavailable'); assert.equal(result.price, undefined);
});

test('WB reads a wallet price without requesting the non-wallet API', async () => {
  const {result, requests} = await collect({market: 'wb', visible: true});
  assert.equal(result.price, 26100); assert.equal(result.price_kind, 'wb_wallet');
  assert.equal(requests, 0);
});
test('WB waits for its wallet price and still never requests the ordinary-price API', async () => {
  const {result, requests} = await collect({market: 'wb', mutate: true, mutationDelay: 70});
  assert.equal(result.price, 26100); assert.equal(requests, 0);
});
test('WB does not substitute an API product price if no wallet price appears', async () => {
  const {result, requests} = await collect({market: 'wb'});
  assert.equal(result.code, 'unavailable');
  assert.match(result.error, /WB Кошельком/);
  assert.equal(result.price, undefined); assert.equal(requests, 0);
});
test('a confirmed sold-out redirect returns immediately without seller or price requirements', async () => {
  const {result, requests} = await collect({redirect: true, soldOut: true, visible: true, seller: true});
  assert.equal(result.kind, 'sold_out'); assert.equal(result.availability, 'out_of_stock');
  assert.equal(result.price, undefined); assert.equal(result.error, undefined); assert.equal(requests, 0);
});
test('a sold-out notice appearing later completes through the DOM observer without API fallback', async () => {
  const {result, requests} = await collect({redirect: true, soldOut: true, mutate: true, mutationDelay: 70});
  assert.equal(result.kind, 'sold_out'); assert.equal(requests, 0);
});
test('a search redirect alone does not prove that a product is sold out', async () => {
  const {result, requests} = await collect({redirect: true});
  assert.equal(result.kind, undefined); assert.equal(result.code, 'unavailable');
  assert.match(result.error, /отсутствие исходного товара не подтверждено/); assert.equal(requests, 0);
});
test('access restrictions are not downgraded to a sold-out result', async () => {
  const {result, requests} = await collect({redirect: true, soldOut: true, visible: true, navigation: 403});
  assert.equal(result.code, 'access'); assert.equal(result.kind, undefined); assert.equal(requests, 0);
});
function observedCollector() {
  const clock = new FakeClock();
  let callback, observer, reads = 0, price = null, result, disconnected = false;
  const context = vm.createContext({URL, URLSearchParams, AbortController, crypto, Date: clock.Date,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    document: {body: {innerText: 'Краска'}, documentElement: {}, querySelector: () => null},
    location: new URL('https://www.wildberries.ru/catalog/708052242/detail.aspx'),
    performance: {getEntriesByType: () => [{responseStatus: 200}]},
    MutationObserver: class {constructor(fn) {observer = fn;} observe() {} disconnect() {disconnected = true;}},
    fetch: () => {throw new Error('Unexpected marketplace API request');},
    chrome: {runtime: {onMessage: {addListener: fn => {callback = fn;}}}}
  });
  for (const file of ['extractor.js', 'content.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context);
  context.PaintWatchExtract.visible = () => {reads++; return price;};
  context.PaintWatchExtract.soldOut = () => null;
  callback({type: 'paintwatch.collect', job: {market: 'wb', article: '708052242', variant: '', timeout_ms: 1000, deadline: clock.now + 1000}}, {}, value => {result = value;});
  return {
    clock, reads: () => reads, result: () => result, disconnected: () => disconnected,
    mutate: (available = false) => {
      if (available) price = {kind: 'visible', price: 27800, price_kind: 'wb_wallet', name: 'Краска', currency: 'RUB'};
      if (!disconnected) observer();
    }
  };
}
test('a burst of a thousand mutations is coalesced without a thousand price scans', async () => {
  const e = observedCollector();
  for (let n = 0; n < 1000; n++) e.mutate();
  assert.equal(e.reads(), 3);
  await e.clock.advance(49);
  e.mutate(true);
  await e.clock.advance(1);
  assert.equal(e.result().price, 27800); assert.equal(e.reads(), 4);
  assert.equal(e.disconnected(), true); assert.equal(e.clock.timers.size, 0);
});
test('the first price mutation is still collected immediately', async () => {
  const e = observedCollector();
  e.mutate(true); await flush();
  assert.equal(e.result().price, 27800); assert.equal(e.clock.timers.size, 0);
});
test('a price arriving just before the deadline is flushed instead of being missed', async () => {
  const e = observedCollector();
  e.mutate(); await e.clock.advance(925);
  e.mutate(true); await e.clock.advance(25);
  assert.equal(e.result().price, 27800); assert.equal(e.clock.timers.size, 0);
  const reads = e.reads(); await e.clock.advance(500);
  assert.equal(e.reads(), reads);
});
