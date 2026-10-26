importScripts('extractor.js');
const BASE = 'http://127.0.0.1:18765';
let busy = false;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function api(path, body) {
  const {key} = await chrome.storage.local.get('key');
  if (!key) throw new Error('Вставьте код подключения из PaintWatch');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(BASE + path, {method: body === undefined ? 'GET' : 'POST',
      headers: {Authorization: `Bearer ${key}`, 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: controller.signal});
    if (!response.ok) throw new Error(response.status === 403 ? 'Код подключения не принят' : 'Данные задания не приняты приложением');
    return await response.json();
  } finally { clearTimeout(timer); }
}
async function showStatus(status) {
  await chrome.storage.local.set({status, updated: Date.now()});
}
async function openJob(job) {
  const {ownedTabs = {}} = await chrome.storage.session.get('ownedTabs');
  const owned = ownedTabs[job.market];
  let tab;
  if (owned) {
    try {
      const existing = await chrome.tabs.get(owned.id);
      const old = PaintWatchExtract.ref(existing.url), expected = PaintWatchExtract.ref(owned.url);
      if (old.market === expected.market && old.article === expected.article && old.variant === expected.variant) tab = existing;
    } catch { /* The user closed or repurposed the tab. */ }
  }
  if (tab) {
    const old = PaintWatchExtract.ref(tab.url);
    if (old.market === job.market && old.article === job.article && old.variant === job.variant) await chrome.tabs.reload(tab.id, {bypassCache: true});
    else await chrome.tabs.update(tab.id, {url: job.url});
  } else tab = await chrome.tabs.create({url: job.url, active: false});
  ownedTabs[job.market] = {id: tab.id, url: job.url};
  await chrome.storage.session.set({ownedTabs, owned: ownedTabs[job.market]});
  for (let i = 0; i < 25; i++) {
    await pause(1000);
    if ((await chrome.tabs.get(tab.id)).status === 'complete') return tab.id;
  }
  throw new Error('Карточка не загрузилась вовремя');
}
async function tick() {
  if (busy) return;
  const {enabled} = await chrome.storage.local.get('enabled');
  if (!enabled) return;
  busy = true;
  let job;
  try {
    for (let index = 0; index < 5; index++) {
      const status = await api('/v1/status');
      if (!status.monitoring) { await showStatus('Связь есть. Включите мониторинг в PaintWatch'); return; }
      ({job} = await api('/v1/next', {}));
      if (!job) { await showStatus('Подключено. Ожидаем следующую проверку'); return; }
      await showStatus(`Проверяется ${job.market}: ${job.article}`);
      const tab = await openJob(job);
      const result = await chrome.tabs.sendMessage(tab, {type: 'paintwatch.collect', job});
      if (!result || typeof result !== 'object') throw new Error('Расширение не смогло прочитать страницу');
      await api('/v1/result', {...result, id: job.id, lease: job.lease});
      await showStatus(result.error || `Данные ${job.article} переданы. Результат смотрите в PaintWatch`);
      job = null;
      await pause(3000);
    }
  } catch (e) {
    if (job) {
      try { await api('/v1/result', {id: job.id, lease: job.lease, url: job.url, error: 'Не удалось прочитать карточку. Проверьте вкладку браузера'}); } catch { /* Keep lease and recover by timeout. */ }
    }
    await showStatus(e.message || 'Нет связи с PaintWatch. Проверьте, что приложение запущено');
  } finally { busy = false; }
}
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'paintwatch') tick(); });
chrome.runtime.onInstalled.addListener(() => chrome.alarms.create('paintwatch', {periodInMinutes: 1}));
chrome.runtime.onStartup.addListener(() => { chrome.alarms.create('paintwatch', {periodInMinutes: 1}); tick(); });
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.type === 'paintwatch.connect') {
    chrome.storage.local.set({key: message.key.trim(), enabled: true}).then(async () => {
      chrome.alarms.create('paintwatch', {periodInMinutes: 1});
      try { await api('/v1/status'); await showStatus('Подключено'); reply({ok: true}); tick(); }
      catch { reply({ok: false, error: 'Нет связи. Проверьте код и запустите PaintWatch'}); }
    });
    return true;
  }
  if (message.type === 'paintwatch.tick') { tick(); reply({ok: true}); }
  if (message.type === 'paintwatch.open') {
    chrome.storage.session.get('owned').then(({owned}) => { if (owned) chrome.tabs.update(owned.id, {active: true}); });
    reply({ok: true});
  }
});
