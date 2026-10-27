importScripts('extractor.js');
const BASE = 'http://127.0.0.1:18765';
const active = new Map();
let pumping = false, wakeTimer;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function bounded(promise, ms) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('Время проверки истекло')), Math.max(1, ms));
    })]);
  } finally { clearTimeout(timer); }
}
async function api(path, body) {
  const {key} = await chrome.storage.local.get('key');
  if (!key) throw new Error('Вставьте код подключения из PaintWatch');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(BASE + path, {method: body === undefined ? 'GET' : 'POST',
      headers: {Authorization: `Bearer ${key}`, 'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}), signal: controller.signal});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Данные задания не приняты приложением');
    return data;
  } finally { clearTimeout(timer); }
}
async function showStatus(status) {
  await chrome.storage.local.set({status, updated: Date.now()});
}
async function ready(tab) {
  try { return await bounded(chrome.tabs.sendMessage(tab, {type: 'paintwatch.ready'}), 300); }
  catch { return null; }
}
async function openJob(job, slot, deadline) {
  const key = `tab_${job.market}_${slot}`;
  const owned = (await chrome.storage.session.get(key))[key];
  let tab, previous;
  if (owned) {
    try {
      const existing = await chrome.tabs.get(owned.id);
      const old = PaintWatchExtract.ref(existing.url), expected = PaintWatchExtract.ref(owned.url);
      if (old.market === expected.market && old.article === expected.article && old.variant === expected.variant) tab = existing;
    } catch { /* The user closed or repurposed the tab. */ }
  }
  if (Date.now() >= deadline) throw new Error('Время проверки истекло');
  if (tab) {
    previous = (await ready(tab.id))?.documentId;
    if (Date.now() >= deadline) throw new Error('Время проверки истекло');
    const old = PaintWatchExtract.ref(tab.url);
    if (old.market === job.market && old.article === job.article && old.variant === job.variant) await chrome.tabs.reload(tab.id, {bypassCache: true});
    else await chrome.tabs.update(tab.id, {url: job.url});
  } else tab = await chrome.tabs.create({url: job.url, active: false});
  await chrome.storage.session.set({[key]: {id: tab.id, url: job.url}, owned: {id: tab.id, url: job.url}});
  const navigationDeadline = Math.min(deadline, Date.now() + Math.min(12000, job.timeout_ms * 0.6));
  while (Date.now() < navigationDeadline) {
    const state = await ready(tab.id);
    if (state?.documentId && state.documentId !== previous) {
      try {
        const ref = PaintWatchExtract.ref(state.url);
        if (ref.market === job.market && ref.article === job.article && ref.variant === job.variant) return {tab: tab.id, documentId: state.documentId};
      } catch { /* Still navigating. */ }
    }
    await pause(100);
  }
  throw new Error('Карточка не стала доступна за отведённое время');
}
async function runSlot(slot, client) {
  let job;
  try {
    ({job} = await api('/v1/next', {protocol: 2, client, slot}));
    if (!job) return;
    const deadline = Date.now() + job.timeout_ms;
    await showStatus(`Проверяется ${job.market}: ${job.article} · активных слотов: ${active.size}`);
    const {tab, documentId} = await bounded(openJob(job, slot, deadline), deadline - Date.now());
    const result = await bounded(chrome.tabs.sendMessage(tab, {type: 'paintwatch.collect', job: {...job, deadline, documentId}}), deadline - Date.now());
    if (!result || typeof result !== 'object') throw new Error('Расширение не смогло прочитать страницу');
    await api('/v1/result', {...result, id: job.id, lease: job.lease});
    await showStatus(result.error || `Данные ${job.article} переданы. Результат смотрите в PaintWatch`);
    job = null;
    // Refill this slot immediately. A card never waits for the rest of its batch.
    return true;
  } catch (e) {
    if (job) {
      try { await api('/v1/result', {id: job.id, lease: job.lease, url: job.url, error: e.message || 'Не удалось прочитать карточку'}); }
      catch { /* The persisted lease recovers a lost result after expiry. */ }
    }
    await showStatus(e.message || 'Нет связи с PaintWatch. Проверьте, что приложение запущено');
  }
}
function schedule(delay = 1000) {
  clearTimeout(wakeTimer);
  wakeTimer = setTimeout(() => tick(), delay);
}
async function tick() {
  if (pumping) return;
  pumping = true;
  try {
    const config = await chrome.storage.local.get(['enabled', 'client']);
    if (!config.enabled) return;
    const client = config.client || crypto.randomUUID();
    if (!config.client) await chrome.storage.local.set({client});
    const status = await api('/v1/status');
    if (status.protocol !== 2) throw new Error('Обновите приложение PaintWatch до версии 0.3');
    if (!status.monitoring) { await showStatus('Связь есть. Включите мониторинг в PaintWatch'); return; }
    for (let slot = 0; slot < Math.min(32, status.parallelism); slot++) {
      if (active.has(slot)) continue;
      const task = runSlot(slot, client);
      active.set(slot, task);
      task.then(refill => { active.delete(slot); if (refill) schedule(0); }, () => { active.delete(slot); });
    }
  } catch (e) { await showStatus(e.message || 'Нет связи с PaintWatch'); }
  finally { pumping = false; schedule(); }
}
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'paintwatch') tick(); });
chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create('paintwatch', {periodInMinutes: 0.5}); tick(); });
chrome.runtime.onStartup.addListener(() => { chrome.alarms.create('paintwatch', {periodInMinutes: 0.5}); tick(); });
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message.type === 'paintwatch.connect') {
    chrome.storage.local.set({key: message.key.trim(), enabled: true}).then(async () => {
      chrome.alarms.create('paintwatch', {periodInMinutes: 0.5});
      try { const status = await api('/v1/status'); if (status.protocol !== 2) throw new Error(); await showStatus('Подключено'); reply({ok: true}); tick(); }
      catch { reply({ok: false, error: 'Проверьте код и запустите PaintWatch 0.3'}); }
    });
    return true;
  }
  if (message.type === 'paintwatch.tick') { tick(); reply({ok: true}); }
  if (message.type === 'paintwatch.open') {
    chrome.storage.session.get('owned').then(({owned}) => { if (owned) chrome.tabs.update(owned.id, {active: true}); });
    reply({ok: true});
  }
});
