const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const url = 'https://www.wildberries.ru/catalog/518397236/detail.aspx?size=716573024';
const source = 'https://www.wildberries.ru/__internal/u-card/cards/v4/detail';
const product = {id: 518397236, name: 'Краска', totalQuantity: 52, token: 'PRIVATE', sizes: [
  {optionId: 716573024, price: {product: 26300, basic: 51600, wallet: 25500}, stocks: [{qty: 52}], payload: 'PRIVATE'}]};
let listener, navigationListener, nextJob = {id: 'job', url, issued_at: Date.now() / 1000 - 0.1};
let currentUrl = 'https://www.wildberries.ru/catalog/999/detail.aspx';
let activations = [], posts = [], timers = [], typedNavigations = [], trace = [], claimedUrls = new Map();
const context = {URL, AbortSignal, TextDecoder, Uint8Array, atob, crypto: require('node:crypto').webcrypto,
  PAINTBOT_KEY: 'LOCAL_KEY', setInterval() {}, clearTimeout() {},
  setTimeout(fn) {timers.push(fn); return timers.length;},
  chrome: {devtools: {inspectedWindow: {tabId: 7}, network: {
    onRequestFinished: {addListener(fn) {listener = fn;}},
    onNavigated: {addListener(fn) {navigationListener = fn;}}}},
    tabs: {get: async () => ({url: currentUrl}),
      update: async (id, value) => {assert.equal(value.url, undefined); assert.equal(value.active, true); activations.push(id);},
      reload: async () => {throw new Error('Browser API reload must not be used');}}},
  fetch: async (endpoint, options) => {
    assert(endpoint.startsWith('http://127.0.0.1:8765/'));
    assert.equal(options.headers.Authorization, 'Bearer LOCAL_KEY');
    if (endpoint.endsWith('/result')) posts.push(JSON.parse(options.body));
    if (endpoint.endsWith('/progress')) trace.push(JSON.parse(options.body));
    if (endpoint.endsWith('/navigate')) {
      const value = JSON.parse(options.body);
      assert.equal(value.url, undefined);
      currentUrl = claimedUrls.get(value.job_id);
      typedNavigations.push(currentUrl);
    }
    const job = nextJob;
    if (options.method === 'GET') {
      nextJob = null;
      if (job) claimedUrls.set(job.id, job.url);
    }
    return {ok: true, json: async () => options.method === 'GET' ? {job} : {ok: true}};
  }};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../browser-extension/devtools.js'), 'utf8') +
  '\nglobalThis.helpers = {publicProduct, poll};', context);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  await tick(); await tick();
  assert.equal(activations.length, 1);
  assert.equal(typedNavigations.length, 1);
  assert.equal(typedNavigations[0], url);
  // A cancelled/replaced server job must not be silently claimed and lost.
  nextJob = {id: 'replacement-job', url, issued_at: Date.now() / 1000 - 0.01};
  await context.helpers.poll();
  await tick();
  assert.equal(activations.length, 2);
  assert.equal(typedNavigations.length, 2);
  assert.equal(typedNavigations[1], url);
  navigationListener(url);
  const clean = context.helpers.publicProduct({products: [product]}, '518397236');
  assert.equal(clean.sizes[0].price.product, 26300);
  assert(!JSON.stringify(clean).includes('PRIVATE'));
  assert(!JSON.stringify(clean).includes('wallet'));
  function entry(extra = {}) {
    return {startedDateTime: new Date().toISOString(), request: {url: source + '?nm=518397236&dest=1259570991'},
      response: {status: 200, headers: []}, getContent: fn => fn(JSON.stringify({products: [product]}), ''), ...extra};
  }
  listener(entry({response: {status: 200, headers: [{name: 'Age', value: '121'}]}}));
  listener(entry({request: {url: 'https://evil.test/cards/v4/detail?nm=518397236'}}));
  listener(entry({startedDateTime: '2000-01-01T00:00:00Z'}));
  assert.equal(timers.length, 0);
  listener(entry());
  assert.equal(timers.length, 1);
  timers.at(-1)();
  await tick(); await tick();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].job_id, 'replacement-job');
  assert.equal(posts[0].page_url, url);
  assert.equal(posts[0].products[0].sizes[0].price.product, 26300);
  assert(!JSON.stringify(posts).includes('PRIVATE'));
  assert(!JSON.stringify(posts).includes('LOCAL_KEY'));
  assert(trace.some(e => e.event === 'navigation_requested'));
  assert(trace.some(e => e.event === 'navigated'));
  assert(trace.some(e => e.event === 'products_received'));
  assert(!JSON.stringify(trace).includes('PRIVATE'));
})().catch(error => {console.error(error); process.exitCode = 1;});
