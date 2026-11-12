const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function popup(reply) {
  const calls = [], saved = [], elements = {};
  for (const id of ['key', 'status', 'lightweight', 'autoCleanup', 'cleanup', 'connect', 'check', 'open', 'diagnose', 'report']) {
    elements[id] = {value: '', textContent: '', hidden: true, focus() {}, select() { this.selected = true; }};
  }
  const context = vm.createContext({document: {getElementById: id => elements[id]},
    chrome: {storage: {local: {get: async () => ({}), set: async value => {saved.push(JSON.parse(JSON.stringify(value)));}, remove: async key => {saved.push({removed: key});}}, onChanged: {addListener() {}}},
      runtime: {sendMessage: async message => { calls.push(message); return reply; }}}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'popup.js'), 'utf8'), context);
  return {elements, calls, saved};
}
test('diagnostic button requests a fresh report from the background and selects its text', async () => {
  const e = popup({report_version: 1, market: 'wb', matched: {price: 26100, price_kind: 'wb_wallet'}, extension_version: '0.4.9'});
  await e.elements.diagnose.onclick();
  assert.equal(e.calls[0].type, 'paintwatch.diagnoseActive');
  assert.equal(JSON.parse(e.elements.report.value).matched.price, 26100);
  assert.equal(e.elements.report.selected, true);
});
test('an undefined diagnostic response shows a recovery instruction', async () => {
  const e = popup(undefined);
  await e.elements.diagnose.onclick();
  assert.notEqual(e.elements.report.value, 'undefined');
  assert.match(e.elements.report.value, /Расширение не вернуло отчёт/);
  assert.match(e.elements.report.value, /Приложение для диагностики не требуется/);
});
test('an undefined connection response is handled without a TypeError', async () => {
  const e = popup(undefined);
  await e.elements.connect.onclick();
  assert.match(e.elements.status.textContent, /Расширение не ответило/);
});
test('the media-saving checkbox defaults on and saves an explicit opt-out', async () => {
  const e = popup({ok: true});
  await Promise.resolve();
  assert.equal(e.elements.lightweight.checked, true);
  e.elements.lightweight.checked = false;
  await e.elements.lightweight.onchange();
  assert.deepEqual(e.saved, [{lightweight: false}]);
  assert.match(e.elements.status.textContent, /новым проверкам/);
});
test('automatic cleanup defaults on and disabling it resets its saved deadline', async () => {
  const e = popup({ok: true});
  await Promise.resolve();
  assert.equal(e.elements.autoCleanup.checked, true);
  e.elements.autoCleanup.checked = false;
  await e.elements.autoCleanup.onchange();
  assert.deepEqual(e.saved, [{autoCleanup: false}, {removed: 'cleanupNextAt'}]);
  assert.match(e.elements.status.textContent, /выключена/);
  assert.equal(e.calls[0].type, 'paintwatch.tick');
});
test('cleanup button requests a paused sweep and displays a rejected request', async () => {
  const e = popup({ok: true});
  await e.elements.cleanup.onclick();
  assert.equal(e.calls[0].type, 'paintwatch.cleanup');
  assert.match(e.elements.status.textContent, /проверки приостанавливаются/);
  const rejected = popup({error: 'Сначала подключите расширение'});
  await rejected.elements.cleanup.onclick();
  assert.match(rejected.elements.status.textContent, /Сначала подключите/);
});
