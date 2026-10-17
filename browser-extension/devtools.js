"use strict";
// Reads the responses of the inspected tab. No injected page scripts or replayed WB requests.
const endpoint = "http://127.0.0.1:8765";
const source = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail";
const session = crypto.randomUUID();
const tabId = chrome.devtools.inspectedWindow.tabId;
let active = null;
let polling = false;
let sending = false;
let timer = null;

async function local(path, data) {
  const response = await fetch(endpoint + path, {
    method: data === undefined ? "GET" : "POST",
    headers: {Authorization: "Bearer " + PAINTBOT_KEY, "Content-Type": "application/json"},
    body: data === undefined ? undefined : JSON.stringify(data),
    redirect: "error", cache: "no-store", signal: AbortSignal.timeout(5000)
  });
  if (!response.ok) throw new Error("Local bridge unavailable");
  return response.json();
}

function publicProduct(payload, nm) {
  const products = (payload?.data ?? payload)?.products;
  if (!Array.isArray(products)) return null;
  const matches = products.filter(p => p && String(p.id) === nm);
  if (matches.length !== 1) return null;
  const p = matches[0];
  if (typeof p.name !== "string" || !p.name.trim() || !Array.isArray(p.sizes) || p.sizes.length > 100) return null;
  const out = {id: p.id, name: p.name.slice(0, 500), sizes: []};
  if (Number.isSafeInteger(p.totalQuantity) && p.totalQuantity >= 0) out.totalQuantity = p.totalQuantity;
  for (const s of p.sizes) {
    if (!s || typeof s !== "object") return null;
    if (["optionId", "chrtId"].some(k => s[k] != null && (!Number.isSafeInteger(s[k]) || s[k] <= 0))) return null;
    const v = {price: {}};
    for (const k of ["optionId", "chrtId"]) if (Number.isSafeInteger(s[k]) && s[k] > 0) v[k] = s[k];
    if (Number.isSafeInteger(s.price?.product) && s.price.product > 0 && s.price.product < 1e12) v.price.product = s.price.product;
    if (Object.hasOwn(s, "stocks")) {
      if (!Array.isArray(s.stocks) || s.stocks.some(x => !Number.isSafeInteger(x?.qty) || x.qty < 0)) return null;
      v.stocks = [{qty: s.stocks.reduce((sum, x) => sum + x.qty, 0)}];
    }
    out.sizes.push(v);
  }
  return out;
}

async function send(job, extra) {
  if (active !== job || sending) return;
  sending = true;
  clearTimeout(timer);
  try {
    const tab = await chrome.tabs.get(tabId);
    const page = new URL(tab.url);
    const expected = new URL(job.url);
    if (page.origin !== expected.origin || page.pathname !== expected.pathname ||
        page.searchParams.get("size") !== expected.searchParams.get("size")) return;
    await local("/result", {job_id: job.id, session, page_url: job.url, ...extra});
    if (active === job) active = null;
  } catch (_) {
    // No request headers, cookies, exception strings or API bodies are logged.
  } finally { sending = false; }
}

chrome.devtools.network.onRequestFinished.addListener(entry => {
  const job = active;
  if (!job || sending || Date.now() / 1000 - job.issued_at > 75) return;
  const started = Date.parse(entry.startedDateTime) / 1000;
  if (!Number.isFinite(started) || started < job.issued_at) return;
  let url;
  try { url = new URL(entry.request.url); } catch (_) { return; }
  const nm = new URL(job.url).pathname.split("/")[2];
  const status = entry.response.status;
  const base = {started_at: started, status};
  if (url.origin + url.pathname === new URL(job.url).origin + new URL(job.url).pathname && status >= 400) {
    void send(job, {...base, kind: "document"});
    return;
  }
  if (url.origin + url.pathname !== source || !(url.searchParams.get("nm") ?? "").split(";").includes(nm)) return;
  if (status !== 200) { void send(job, {...base, source}); return; }
  const rawAge = entry.response.headers.find(h => h.name.toLowerCase() === "age")?.value ?? "0";
  const age = Number(rawAge);
  if (!rawAge.trim() || !Number.isFinite(age) || age < 0 || age > 120) return;
  entry.getContent((text, encoding) => {
    if (active !== job || !text || text.length > 2e6) return;
    let product;
    try {
      const json = encoding === "base64" ? new TextDecoder().decode(Uint8Array.from(atob(text), c => c.charCodeAt(0))) : text;
      product = publicProduct(JSON.parse(json), nm);
    } catch (_) { return; }
    if (!product) return;
    const serialized = JSON.stringify(product);
    if (!job.products.some(p => JSON.stringify(p) === serialized)) job.products.push(product);
    if (job.products.length > 10) { active = null; return; }
    clearTimeout(timer);
    timer = setTimeout(() => void send(job, {...base, source, age_seconds: age,
      products: job.products, dest: url.searchParams.get("dest")}), 1000);
  });
});

async function poll() {
  if (polling) return;
  polling = true;
  try {
    if (active && Date.now() / 1000 - active.issued_at > 75) active = null;
    const {job} = await local("/job?session=" + encodeURIComponent(session));
    if (job) {
      const url = new URL(job.url);
      if (url.origin !== "https://www.wildberries.ru" || !/^\/catalog\/\d+\/detail\.aspx$/.test(url.pathname)) return;
      clearTimeout(timer);
      active = {...job, products: []};
      await chrome.tabs.update(tabId, {url: job.url});
    }
  } catch (_) { /* Retry local connection; no WB requests are retried here. */ }
  finally { polling = false; }
}
setInterval(() => void poll(), 1500);
void poll();
