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

async function local(path, data, timeout = 5000) {
  const response = await fetch(endpoint + path, {
    method: data === undefined ? "GET" : "POST",
    headers: {Authorization: "Bearer " + PAINTBOT_KEY, "Content-Type": "application/json"},
    body: data === undefined ? undefined : JSON.stringify(data),
    redirect: "error", cache: "no-store", signal: AbortSignal.timeout(timeout)
  });
  if (!response.ok) throw new Error("Local bridge unavailable");
  return response.json();
}

async function progress(job, event, fields = {}) {
  if (active !== job) return;
  try { await local("/progress", {job_id: job.id, session, event, ...fields}); }
  catch (_) { /* Diagnostics must not prevent price collection. */ }
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
        page.searchParams.get("size") !== expected.searchParams.get("size")) {
      await progress(job, "page_mismatch");
      return;
    }
    await local("/result", {job_id: job.id, session, page_url: job.url, ...extra});
    if (active === job) active = null;
  } catch (_) {
    await progress(job, "result_failed");
  } finally { sending = false; }
}

chrome.devtools.network.onRequestFinished.addListener(entry => {
  const job = active;
  if (!job || sending || Date.now() / 1000 - job.issued_at > 75) return;
  const started = Date.parse(entry.startedDateTime) / 1000;
  let url;
  try { url = new URL(entry.request.url); } catch (_) { return; }
  const nm = new URL(job.url).pathname.split("/")[2];
  const status = entry.response.status;
  const base = {started_at: started, status};
  const document = url.origin + url.pathname === new URL(job.url).origin + new URL(job.url).pathname;
  const api = url.origin + url.pathname === source;
  if (!document && !api) {
    if (["www.wildberries.ru", "wildberries.ru", "card.wb.ru"].includes(url.hostname) &&
        url.pathname.includes("/cards/") && url.pathname.endsWith("/detail")) void progress(job, "other_cards_endpoint", {status});
    return;
  }
  if (!Number.isFinite(started) || started < job.issued_at) {
    void progress(job, "stale_response", {status});
    return;
  }
  if (document) {
    void progress(job, "document_response", {status});
    if (status >= 400) void send(job, {...base, kind: "document"});
    return;
  }
  if (!(url.searchParams.get("nm") ?? "").split(";").includes(nm)) return;
  void progress(job, "api_response", {status});
  if (status !== 200) { void send(job, {...base, source}); return; }
  const rawAge = entry.response.headers.find(h => h.name.toLowerCase() === "age")?.value ?? "0";
  const age = Number(rawAge);
  if (!rawAge.trim() || !Number.isFinite(age) || age < 0 || age > 120) {
    void progress(job, "cache_rejected"); return;
  }
  entry.getContent((text, encoding) => {
    if (active !== job) return;
    if (!text || text.length > 2e6) { void progress(job, "body_unavailable"); return; }
    let product;
    try {
      const json = encoding === "base64" ? new TextDecoder().decode(Uint8Array.from(atob(text), c => c.charCodeAt(0))) : text;
      product = publicProduct(JSON.parse(json), nm);
    } catch (_) { void progress(job, "invalid_json"); return; }
    if (!product) { void progress(job, "product_not_matched"); return; }
    void progress(job, "products_received");
    const serialized = JSON.stringify(product);
    if (!job.products.some(p => JSON.stringify(p) === serialized)) job.products.push(product);
    if (job.products.length > 10) { void progress(job, "too_many_products"); active = null; return; }
    clearTimeout(timer);
    timer = setTimeout(() => void send(job, {...base, source, age_seconds: age,
      products: job.products, dest: url.searchParams.get("dest")}), 1000);
  });
});

chrome.devtools.network.onNavigated.addListener(() => {
  if (active) void progress(active, "navigated");
});

async function navigate(current) {
  try {
    if (active !== current) return;
    await chrome.tabs.update(tabId, {active: true});
    if (active !== current) return;
    await progress(current, "navigation_requested");
    await local("/navigate", {job_id: current.id, session}, 25000);
  } catch (_) { await progress(current, "navigation_failed"); }
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    if (active && Date.now() / 1000 - active.issued_at > 75) active = null;
    const {job} = await local("/job?session=" + encodeURIComponent(session) + "&v=3");
    if (job) {
      const url = new URL(job.url);
      if (url.origin !== "https://www.wildberries.ru" || !/^\/catalog\/\d+\/detail\.aspx$/.test(url.pathname)) return;
      clearTimeout(timer);
      const current = active = {...job, products: []};
      // Keep the heartbeat polling while native UI navigation is in progress.
      void navigate(current);
    }
  } catch (_) { /* Retry local connection; no WB requests are retried here. */ }
  finally { polling = false; }
}
setInterval(() => void poll(), 1500);
void poll();
