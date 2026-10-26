/* Pure parsing helpers shared by the content script and fixture tests. */
(() => {
  function ref(url) {
    const u = new URL(url);
    let m;
    if (u.protocol !== 'https:' || u.username || u.password || u.port) throw new Error('Неподдерживаемый адрес');
    if (u.hostname === 'www.ozon.ru' && (m = u.pathname.match(/^\/product\/(?:[^/]+-)?(\d+)\/?$/))) {
      return {market: 'ozon', article: m[1], variant: ''};
    }
    if (u.hostname === 'www.wildberries.ru' && (m = u.pathname.match(/^\/catalog\/(\d+)\/detail\.aspx\/?$/))) {
      const variant = u.searchParams.get('size') || '';
      if (!/^\d*$/.test(variant)) throw new Error('Неизвестный вариант товара');
      return {market: 'wb', article: m[1], variant};
    }
    throw new Error('Нужна карточка товара Ozon или Wildberries');
  }
  function money(text) {
    const value = String(text).replace(/\s/gu, '').replace(',', '.');
    if (!/^\d+(?:\.\d{1,2})?$/.test(value)) return null;
    const [whole, fraction = ''] = value.split('.');
    const cents = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
    return Number.isSafeInteger(cents) && cents > 0 && cents <= 1e10 ? cents : null;
  }
  function amounts(text) {
    return [...String(text).matchAll(/(?<![\d.,-])(\d[\d\s]*(?:[.,]\d{1,2})?)(?![\d.,])\s*(?:₽|руб\.?)/gu)]
      .map(m => money(m[1])).filter(p => p !== null);
  }
  function json(raw) {
    try { return typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { return null; }
  }
  function cleanOzon(data, article) {
    const widgets = data && data.widgetStates;
    if (!widgets || typeof widgets !== 'object') return null;
    const out = {};
    let identity = false;
    for (const [key, raw] of Object.entries(widgets)) {
      const value = json(raw);
      if (!value || typeof value !== 'object') continue;
      if (key.startsWith('webStickyProducts-') && String(value.sku ?? value.id) === article && typeof value.name === 'string') {
        out[key] = {sku: article, name: value.name.slice(0, 300), seller: {link: String(value.seller?.link || '').slice(0, 500)}};
        identity = true;
      } else if (key.startsWith('webPrice-')) {
        out[key] = {cardPrice: value.cardPrice, currency: value.currency || 'RUB', isAvailable: value.isAvailable};
      }
    }
    return identity && Object.keys(out).some(k => k.startsWith('webPrice-')) ? {widgetStates: out} : null;
  }
  function cleanWb(data, article) {
    const products = data?.products || data?.data?.products;
    if (!Array.isArray(products)) return null;
    const matched = products.filter(p => String(p.id ?? p.nmId) === article);
    if (matched.length !== 1) return null;
    const p = matched[0];
    return {products: [{id: p.id ?? p.nmId, name: p.name, supplierId: p.supplierId,
      sizes: (p.sizes || []).slice(0, 100).map(s => ({optionId: s.optionId,
        price: {product: s.price?.product, currency: s.price?.currency || 'RUB'},
        ...(Array.isArray(s.stocks) ? {stocks: s.stocks.slice(0, 100).map(s => ({qty: s.qty}))} : {})}))}]};
  }
  function labelledPrice(root, label) {
    if (!root) return null;
    const found = new Set();
    for (const leaf of [root, ...root.querySelectorAll('*')]) {
      if (leaf.children.length || !label.test(leaf.innerText || '')) continue;
      let node = leaf;
      for (let depth = 0; node && depth < 5 && root.contains(node); depth++, node = node.parentElement) {
        const prices = [...new Set(amounts(node.innerText || ''))];
        if (prices.length > 1) break;
        if (prices.length === 1) { found.add(prices[0]); break; }
      }
    }
    return found.size === 1 ? [...found][0] : null;
  }
  function visible(doc, current) {
    const title = doc.querySelector('h1')?.innerText?.trim();
    if (!title) return null;
    if (current.market === 'ozon') {
      const roots = doc.querySelectorAll('[data-widget="webPrice"]');
      if (roots.length !== 1) return null;
      const price = labelledPrice(roots[0], /(?:с\s*)?ozon\s*карт(?:ой|а|е)/iu);
      if (price !== null) {
        const sellerLink = doc.querySelector('[data-widget="webCurrentSeller"] a[href*="/seller/"]')?.href || '';
        const seller = sellerLink.match(/\/seller\/(?:[^/]*-)?(\d+)\/?/)?.[1] || '';
        return {kind: 'visible', name: title, price, currency: 'RUB', price_kind: 'ozon_card', seller};
      }
    } else {
      const roots = doc.querySelectorAll('.price-block, .product-page__price-block');
      // Only an explicit non-wallet label confirms this price; no "smallest price" heuristic.
      for (const root of roots) {
        const price = labelledPrice(root, /без\s*(?:wb\s*)?кошелька/iu);
        if (price !== null) return {kind: 'visible', name: title, price, currency: 'RUB', price_kind: 'wb_product', seller: ''};
      }
    }
    return null;
  }
  const api = {ref, money, amounts, cleanOzon, cleanWb, visible};
  globalThis.PaintWatchExtract = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
