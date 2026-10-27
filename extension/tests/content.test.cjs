const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto: crypto} = require('node:crypto');
async function collect({status = 200, body = '<html>Not JSON</html>', navigation = 200, visible = false, retryAfter = null, mutate = false, documentId, mutationDelay = 5, seller = false, timeout = 200} = {}) {
  let callback, requests = 0;
  const url = 'https://www.wildberries.ru/catalog/708052242/detail.aspx';
  let observer, detected = visible;
  const price = {kind: 'visible', price: 38000, price_kind: 'wb_product', currency: 'RUB', name: 'Краска'};
  const context = vm.createContext({URL, URLSearchParams, AbortController, crypto,
    MutationObserver: class { constructor(fn) { observer = fn; } observe() { if (mutate) setTimeout(() => { detected = true; observer(); }, mutationDelay); } disconnect() {} },
    document: {body: {innerText: 'Краска'}, querySelector: () => null},
    performance: {getEntriesByType: () => [{responseStatus: navigation}]}, location: new URL(url),
    setTimeout, clearTimeout,
    fetch: async (target, options) => {
      requests++;
      assert.equal(options.credentials, 'same-origin');
      return {status, ok: status < 400, url: 'https://www.wildberries.ru/__internal/u-card/cards/v4/detail', headers: {get: name => name === 'retry-after' ? retryAfter : null}, text: async () => body};
    },
    chrome: {runtime: {onMessage: {addListener: fn => { callback = fn; }}}}
  });
  for (const file of ['extractor.js', 'content.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context);
  context.PaintWatchExtract.visible = () => detected ? price : null;
  const result = await new Promise(resolve => callback({type: 'paintwatch.collect', job: {market: 'wb', article: '708052242', variant: '', wb_dest: '-1257786', documentId, timeout_ms: timeout, deadline: Date.now() + timeout, seller: seller ? '123' : ''}}, {}, resolve));
  return {result, requests};
}
test('a non-JSON API response is not automatically labelled as a CAPTCHA', async () => {
  const {result, requests} = await collect();
  assert.equal(result.code, 'unavailable');
  assert.match(result.error, /Резервный API WB/);
  assert.equal(requests, 1);
});
test('an API challenge does not prove a main-page CAPTCHA', async () => {
  const {result} = await collect({body: '<script src="/__wbaas/challenges/antibot/browser-check.js"></script>'});
  assert.equal(result.code, 'unavailable');
});
test('API rejection explicitly identifies the API stage', async () => {
  const {result, requests} = await collect({status: 403});
  assert.equal(result.code, 'unavailable');
  assert.match(result.error, /Резервный API WB.*403/);
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
