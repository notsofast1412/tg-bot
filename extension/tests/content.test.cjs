const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
async function collect({status = 200, body = '<html>Not JSON</html>', navigation = 200} = {}) {
  let callback, requests = 0;
  const url = 'https://www.wildberries.ru/catalog/708052242/detail.aspx';
  const context = vm.createContext({URL, URLSearchParams, AbortController,
    document: {body: {innerText: 'Краска'}, querySelector: () => null},
    performance: {getEntriesByType: () => [{responseStatus: navigation}]}, location: new URL(url),
    setTimeout: (fn, delay) => setTimeout(fn, delay === 1000 ? 0 : delay), clearTimeout,
    fetch: async (target, options) => {
      requests++;
      assert.equal(options.credentials, 'same-origin');
      return {status, ok: status < 400, url: 'https://www.wildberries.ru/__internal/u-card/cards/v4/detail', headers: {get: () => null}, text: async () => body};
    },
    chrome: {runtime: {onMessage: {addListener: fn => { callback = fn; }}}}
  });
  for (const file of ['extractor.js', 'content.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context);
  const result = await new Promise(resolve => callback({type: 'paintwatch.collect', job: {market: 'wb', article: '708052242', variant: '', wb_dest: '-1257786'}}, {}, resolve));
  return {result, requests};
}
test('a non-JSON API response is not automatically labelled as a CAPTCHA', async () => {
  const {result, requests} = await collect();
  assert.equal(result.code, 'unavailable');
  assert.match(result.error, /Резервный API WB/);
  assert.equal(requests, 1);
});
test('an actual API challenge is distinguished from an unsupported response', async () => {
  const {result} = await collect({body: '<script src="/__wbaas/challenges/antibot/browser-check.js"></script>'});
  assert.equal(result.code, 'access');
});
test('API rejection explicitly identifies the API stage', async () => {
  const {result, requests} = await collect({status: 403});
  assert.equal(result.code, 'access');
  assert.match(result.error, /Резервный API WB.*403/);
  assert.equal(requests, 1);
});
test('a denied main page stops before the API request', async () => {
  const {result, requests} = await collect({navigation: 403});
  assert.equal(result.code, 'access');
  assert.equal(requests, 0);
});
