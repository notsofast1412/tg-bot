/* Pure parsing helpers shared by the content script and fixture tests. */
(() => {
  function ref(url) {
    const u = new URL(url);
    let m;
    if (u.protocol !== 'https:' || u.username || u.password || u.port) throw new Error('Неподдерживаемый адрес');
    if (u.hostname === 'www.ozon.ru' && (m = u.pathname.match(/^\/product\/(?:[^/]+-)?(\d+)\/?$/))) {
      return {market: 'ozon', article: m[1], variant: ''};
    }
    if (u.hostname === 'www.ozon.ru' && u.pathname === '/search/') {
      const ids = u.searchParams.getAll('product_id');
      if (ids.length === 1 && /^\d+$/.test(ids[0])) return {market: 'ozon', article: ids[0], variant: '', view: 'sold_out_search'};
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
  const WB_CARD = '.mo-typography_colors_danger, .mo-typography_color_danger, .mo-typography_colors_accent';
  const WB_WALLET = '[class*="priceBlockWalletPrice"], .price-block__wallet-price';
  const walletLabel = text => /(?<![\p{L}\p{N}])(?:с\s*|при\s+оплате\s+(?:с\s+)?)(?:(?:wb|вб)\s*)?кошельком(?=\s|$|[.,:]|\d)/iu.test(String(text).replace(/\s+/gu, ' ').trim());
  const withoutWallet = text => /без\s*(?:(?:wb|вб)\s*)?кошелька/iu.test(text);
  function cardAmount(text) {
    const normalized = String(text).replace(/[\u200B-\u200D\uFEFF]/g, '').trim();
    const match = normalized.match(/^(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?)$/u);
    return match ? money(match[1]) : null;
  }
  function walletAmount(text) {
    const value = String(text).replace(/[\u200B-\u200D\uFEFF]/g, '').replace(/\s+/gu, ' ').trim();
    const labels = [...value.matchAll(/(?<![\p{L}\p{N}])(?:с\s*|при\s+оплате\s+(?:с\s+)?)(?:(?:wb|вб)\s*)?кошельком(?=\s|$|[.,:]|\d)/giu)];
    if (labels.length !== 1) return null;
    const label = labels[0];
    return cardAmount((value.slice(0, label.index) + value.slice(label.index + label[0].length)).replace(/^\s*:\s*|\s*:\s*$/gu, '').trim());
  }
  function walletNodes(doc) {
    return [...doc.querySelectorAll(WB_CARD)];
  }
  function otherWbProduct(node, current) {
    if (node.closest('.product-card, .j-card-item')) return true;
    const identified = node.closest('[data-nm-id], [data-product-id]');
    const article = identified?.getAttribute('data-nm-id') || identified?.getAttribute('data-product-id');
    if (/^\d+$/.test(article || '') && current.article && article !== current.article) return true;
    for (let parent = node; parent; parent = parent.parentElement) {
      if (parent.matches('a[href]') && /\/catalog\/\d+\/detail\.aspx(?:[/?#]|$)/i.test(parent.getAttribute('href') || '')) return true;
    }
    return false;
  }
  function nonWalletPrice(node, root) {
    for (let depth = 0; node && depth < 3 && root.contains(node); depth++, node = node.parentElement) {
      if (node.matches('body, html, main')) break;
      const text = node.innerText || '';
      if (text.length > 500) break;
      if (/скидк|к[еэ]шб[еэ]к|верн[её]м|балл|рассрочк|частями|в\s+месяц/iu.test(text)) return true;
      if (walletLabel(text)) break;
      if (withoutWallet(text)) return true;
    }
    return false;
  }
  function walletAmounts(root, current) {
    const found = new Set();
    const accept = (node, price) => {
      if (price !== null && !otherWbProduct(node, current) && rendered(node) && !struck(node, root) && !hasStruckDigits(node, root) && !nonWalletPrice(node, root)) found.add(price);
    };
    for (const node of [root, ...root.querySelectorAll('*')]) {
      const value = node.innerText || '';
      accept(node, cardAmount(value) ?? walletAmount(value));
    }
    // A price can be a bare text node next to a nested payment label.
    const walker = root.ownerDocument.createTreeWalker(root, 4);
    let text;
    while ((text = walker.nextNode())) accept(text.parentElement, cardAmount(text.textContent || '') ?? walletAmount(text.textContent || ''));
    return found;
  }
  function hasStruckDigits(node, root) {
    return [...node.querySelectorAll('*')].some(child => /\d/.test(child.innerText || '') && struck(child, root));
  }
  function wbPriceRead(doc, current) {
    const containers = [...doc.querySelectorAll(WB_WALLET)].filter(node =>
      !otherWbProduct(node, current) && (node.classList.contains('price-block__wallet-price') ||
        [...node.classList].some(name => /^priceBlockWalletPrice(?:-|$)/.test(name))));
    if (containers.length) {
      const prices = new Set(containers.flatMap(node => [...walletAmounts(node, current)]));
      return {source: 'wallet_container', prices, roots: containers.length};
    }
    const labels = new Set();
    const walker = doc.createTreeWalker(doc.body || doc.documentElement, 4);
    let text;
    while ((text = walker.nextNode())) {
      if (!/кошель/iu.test(text.textContent || '')) continue;
      let node = text.parentElement;
      if (!node || node.closest('header, nav, footer, script, style') || otherWbProduct(node, current)) continue;
      for (let depth = 0; node && depth < 5 && !node.matches('body, html, main'); depth++, node = node.parentElement) {
        const value = node.innerText || '';
        if (value.length > 500) break;
        if (withoutWallet(value) && !walletLabel(value)) break;
        if (!walletLabel(value) || !rendered(node) || struck(node, doc.documentElement)) continue;
        labels.add(node);
        if (walletAmounts(node, current).size) break;
      }
    }
    if (labels.size) {
      const prices = new Set([...labels].flatMap(node => [...walletAmounts(node, current)]));
      return {source: 'wallet_label', prices, roots: labels.size};
    }
    const prices = new Set();
    for (const node of walletNodes(doc)) {
      if (node.closest('header, nav, footer')) continue;
      const price = cardAmount(node.innerText || '');
      if (price !== null && !otherWbProduct(node, current) && rendered(node) && !struck(node, doc.documentElement) && !hasStruckDigits(node, doc.documentElement) && !nonWalletPrice(node, doc.documentElement)) prices.add(price);
    }
    return {source: 'legacy_color', prices, roots: 0};
  }
  function labelledPrice(root, label) {
    if (!root) return null;
    const found = new Set();
    for (const leaf of [root, ...root.querySelectorAll('*')]) {
      // Payment labels can contain nested spans/icons. Read their rendered text,
      // not just childless nodes, and never use display:none prices.
      const labelText = ((leaf.innerText || '') + ' ' + (leaf.getAttribute('aria-label') || '')).replace(/[\u200B-\u200D\uFEFF]/g, '');
      if (!label.test(labelText)) continue;
      if (!rendered(leaf) || struck(leaf, root)) continue;
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
  function wbSeller(doc, current) {
    const sellers = new Set();
    for (const node of doc.querySelectorAll('a[href*="/seller/"]')) {
      if (otherWbProduct(node, current)) continue;
      try {
        const link = new URL(node.href);
        const id = link.pathname.match(/^\/seller\/(\d+)\/?$/)?.[1];
        if (id && link.protocol === 'https:' && ['www.wildberries.ru', 'wildberries.ru'].includes(link.hostname)) sellers.add(id);
      } catch { /* Not a public seller link. */ }
    }
    return sellers.size === 1 ? [...sellers][0] : '';
  }
  function soldOut(doc, current) {
    const controls = 'a, [role="link"], button, [role="button"]';
    const isLabel = node => node && rendered(node) && !node.closest(controls) && !node.querySelector(controls) &&
      /^(?:товар закончился|этот товар закончился|нет в наличии)$/iu.test((node.innerText || '').replace(/[\u200B-\u200D\uFEFF]/g, '').replace(/\s+/gu, ' ').trim());
    if (current.view === 'sold_out_search' && current.market === 'ozon') {
      // Only the verified product_id redirect may use this label outside h1.
      // Search cards and their prices are never used as the monitored product.
      const walker = doc.createTreeWalker(doc.body || doc.documentElement, 4);
      let text;
      while ((text = walker.nextNode())) {
        if (!/товар|наличии/iu.test(text.textContent || '')) continue;
        let node = text.parentElement;
        for (let depth = 0; node && depth < 3; depth++, node = node.parentElement) {
          if (isLabel(node)) return {kind: 'sold_out', availability: 'out_of_stock', evidence: 'ozon_sold_out_search'};
        }
      }
    } else if (isLabel(doc.querySelector('h1'))) {
      return {kind: 'sold_out', availability: 'out_of_stock', evidence: 'product_sold_out_heading'};
    }
    return null;
  }
  function visible(doc, current) {
    if (current.view === 'sold_out_search' || soldOut(doc, current)) return null;
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
      const wallet = wbPriceRead(doc, current);
      const price = wallet.prices.size === 1 ? [...wallet.prices][0] : null;
      const name = title || (/^\d+$/.test(current.article || '') ? current.article : '');
      if (price !== null && name) return {kind: 'visible', name, name_source: title ? 'heading' : 'article', price, currency: 'RUB', price_kind: 'wb_wallet', seller: wbSeller(doc, current)};
    }
    return null;
  }
  function diagnose(doc, current) {
    const matched = soldOut(doc, current) || visible(doc, current);
    const titleFound = Boolean(doc.querySelector('h1')?.innerText?.trim());
    const wallet = current.market === 'wb' ? wbPriceRead(doc, current) : null;
    const cardPrices = wallet ? [...wallet.prices] : [];
    const reason = matched ? null : current.market === 'wb'
      ? cardPrices.length > 1 ? 'На странице разные подтверждённые цены по WB Карте' : 'Цена по WB Карте не подтверждена'
      : !titleFound ? 'Не найден заголовок карточки Ozon' : 'Цена по Ozon Карте не подтверждена';
    const reasonCode = matched ? null : current.market === 'wb'
      ? cardPrices.length > 1 ? 'ambiguous_card_price' : 'card_price_missing'
      : !titleFound ? 'title_missing' : 'card_price_missing';
    const selector = current.market === 'wb' ? WB_CARD : '[data-widget="webPrice"]';
    return {report_version: 1, market: current.market, article: current.article, variant: current.variant,
      price_kind: current.market === 'wb' ? 'wb_wallet' : 'ozon_card',
      ...(wallet ? {walletEvidence: {source: wallet.source, roots: wallet.roots, prices: cardPrices}} : {}),
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
  const api = {ref, money, amounts, cardAmount, walletAmount, cleanOzon, cleanWb, visible, soldOut, diagnose};
  globalThis.PaintWatchExtract = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
