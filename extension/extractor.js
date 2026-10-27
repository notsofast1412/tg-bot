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
  function rendered(node) {
    if (!node?.getClientRects().length) return false;
    const style = node.ownerDocument.defaultView.getComputedStyle(node);
    return style.visibility !== 'hidden' && style.visibility !== 'collapse';
  }
  function struck(node, root) {
    for (; node && root.contains(node); node = node.parentElement) {
      if (node.matches('del,s,strike') || node.ownerDocument.defaultView.getComputedStyle(node).textDecorationLine.includes('line-through')) return true;
    }
    return false;
  }
  const WB_CARD = '.mo-typography_colors_danger, .mo-typography_color_danger';
  function cardAmount(text) {
    const normalized = String(text).replace(/[\u200B-\u200D\uFEFF]/g, '').trim();
    const match = normalized.match(/^(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?)$/u);
    return match ? money(match[1]) : null;
  }
  function walletNodes(doc) {
    return [...doc.querySelectorAll(WB_CARD)];
  }
  function wbPriceValues(doc) {
    const found = new Set();
    for (const node of walletNodes(doc)) {
      if (!rendered(node) || struck(node, doc.documentElement)) continue;
      const price = cardAmount(node.innerText || '');
      if (price !== null) found.add(price);
    }
    return found;
  }
  function wbWalletPrice(doc) {
    const found = wbPriceValues(doc);
    return found.size === 1 ? [...found][0] : null;
  }
  function labelledPrice(root, label) {
    if (!root) return null;
    const found = new Set();
    for (const leaf of [root, ...root.querySelectorAll('*')]) {
      if (!rendered(leaf) || struck(leaf, root)) continue;
      // Payment labels can contain nested spans/icons. Read their rendered text,
      // not just childless nodes, and never use display:none prices.
      const labelText = ((leaf.innerText || '') + ' ' + (leaf.getAttribute('aria-label') || '')).replace(/[\u200B-\u200D\uFEFF]/g, '');
      if (!label.test(labelText)) continue;
      let node = leaf;
      for (let depth = 0; node && depth < 5 && root.contains(node); depth++, node = node.parentElement) {
        if (struck(node, root)) break;
        const prices = [...new Set(amounts(node.innerText || ''))];
        if (prices.length > 1) break;
        if (prices.length === 1) { found.add(prices[0]); break; }
      }
    }
    return found.size === 1 ? [...found][0] : null;
  }
  function wbSeller(doc) {
    const sellers = new Set();
    for (const node of doc.querySelectorAll('a[href*="/seller/"]')) {
      try {
        const link = new URL(node.href);
        const id = link.pathname.match(/^\/seller\/(\d+)\/?$/)?.[1];
        if (id && link.protocol === 'https:' && ['www.wildberries.ru', 'wildberries.ru'].includes(link.hostname)) sellers.add(id);
      } catch { /* Not a public seller link. */ }
    }
    return sellers.size === 1 ? [...sellers][0] : '';
  }
  function visible(doc, current) {
    const title = doc.querySelector('h1')?.innerText?.trim();
    if (current.market === 'ozon') {
      if (!title) return null;
      const roots = doc.querySelectorAll('[data-widget="webPrice"]');
      if (roots.length !== 1) return null;
      const price = labelledPrice(roots[0], /(?:с\s*)?ozon\s*карт(?:ой|а|е)/iu);
      if (price !== null) {
        const sellerLink = doc.querySelector('[data-widget="webCurrentSeller"] a[href*="/seller/"]')?.href || '';
        const seller = sellerLink.match(/\/seller\/(?:[^/]*-)?(\d+)\/?/)?.[1] || '';
        return {kind: 'visible', name: title, price, currency: 'RUB', price_kind: 'ozon_card', seller};
      }
    } else {
      const price = wbWalletPrice(doc);
      const name = title || (/^\d+$/.test(current.article || '') ? current.article : '');
      if (price !== null && name) return {kind: 'visible', name, name_source: title ? 'heading' : 'article', price, currency: 'RUB', price_kind: 'wb_wallet', seller: wbSeller(doc)};
    }
    return null;
  }
  function diagnose(doc, current) {
    const matched = visible(doc, current);
    const titleFound = Boolean(doc.querySelector('h1')?.innerText?.trim());
    const cardPrices = current.market === 'wb' ? [...wbPriceValues(doc)] : [];
    const reason = matched ? null : current.market === 'wb'
      ? cardPrices.length > 1 ? 'На странице разные подтверждённые цены по WB Карте' : 'Цена по WB Карте не подтверждена'
      : !titleFound ? 'Не найден заголовок карточки Ozon' : 'Цена по Ozon Карте не подтверждена';
    const reasonCode = matched ? null : current.market === 'wb'
      ? cardPrices.length > 1 ? 'ambiguous_card_price' : 'card_price_missing'
      : !titleFound ? 'title_missing' : 'card_price_missing';
    const selector = current.market === 'wb' ? WB_CARD : '[data-widget="webPrice"]';
    return {report_version: 1, market: current.market, article: current.article, variant: current.variant,
      price_kind: current.market === 'wb' ? 'wb_wallet' : 'ozon_card',
      walletNodes: current.market === 'wb' ? walletNodes(doc).slice(0, 20).map(node => ({
        tag: node.tagName, className: String(node.className).slice(0, 300), prices: amounts(node.innerText || ''),
        visible: rendered(node), struck: struck(node, doc.documentElement), parsedPrice: cardAmount(node.innerText || '')
      })) : [],
      titleFound, nameSource: matched?.name_source || (titleFound ? 'heading' : null), cardPrices, reasonCode, reason, matched,
      // No HTML, links, cookies, customer names or delivery addresses leave the page.
      roots: [...doc.querySelectorAll(selector)].slice(0, 5).map(root => ({
        className: String(root.className).slice(0, 200),
        prices: amounts(root.innerText || '').slice(0, 20),
        hasNonWalletLabel: /без\s*(?:(?:wb|вб)\s*)?кошелька/iu.test(root.innerText || ''),
        nodes: [...root.querySelectorAll('*')].filter(node => amounts(node.innerText || '').length === 1).slice(0, 40).map(node => ({
          tag: node.tagName, className: String(node.className).slice(0, 200),
          prices: amounts(node.innerText || ''), visible: rendered(node), struck: struck(node, root)
        }))
      }))};
  }
  const api = {ref, money, amounts, cardAmount, cleanOzon, cleanWb, visible, diagnose};
  globalThis.PaintWatchExtract = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
