(() => {
  const parser = globalThis.PaintWatchExtract;
  const error = (message, code = 'unavailable', retry_after = 0) => ({error: message, code, retry_after});
  function blocked() {
    const text = document.body?.innerText || '';
    if (/fab_chlg_|подтвердите.{0,30}человек|проверка браузера|доступ ограничен|access denied/iu.test(text.slice(0, 10000))) {
      return error('Площадка требует ручную проверку в обычном браузере', 'access');
    }
    return null;
  }
  async function collect(job) {
    const current = parser.ref(location.href);
    if (current.market !== job.market || current.article !== job.article || current.variant !== job.variant) return error('Открылась другая карточка');
    const navigationStatus = performance.getEntriesByType('navigation')[0]?.responseStatus;
    if (navigationStatus === 429) return error('Страница просит снизить частоту (HTTP 429)', 'rate_limit', 600);
    if ([401, 403, 498].includes(navigationStatus)) return error(`Страница ограничила доступ (HTTP ${navigationStatus})`, 'access');
    for (let attempt = 0; attempt < 8; attempt++) {
      const visible = parser.visible(document, current);
      if (visible && (!job.seller || visible.seller) && !(current.market === 'wb' && current.variant)) return visible;
      const block = blocked();
      if (block) return block;
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    // One regular same-origin request in the user's ordinary browser session.
    // No cookies or private page state are exported to the desktop application.
    const path = current.market === 'ozon'
      ? '/api/composer-api.bx/page/json/v2?' + new URLSearchParams({url: `/product/${current.article}/`})
      : '/__internal/u-card/cards/v4/detail?' + new URLSearchParams({appType: '1', curr: 'rub', dest: job.wb_dest, spp: '30', nm: current.article});
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (response.status === 429) {
        const header = response.headers.get('retry-after') || '';
        const delay = /^\d+$/.test(header) ? Number(header) : Math.ceil((Date.parse(header) - Date.now()) / 1000);
        return error('Площадка просит снизить частоту (HTTP 429)', 'rate_limit', Math.max(600, Number.isFinite(delay) ? delay : 600));
      }
      if ([401, 403, 498].includes(response.status)) return error(`Резервный API ${current.market.toUpperCase()} ограничил доступ (HTTP ${response.status}). Если карточка и цена видны в Chrome, нужна проверка распознавания цены, а не CAPTCHA`, 'access');
      if (!response.ok) return error(`Источник вернул HTTP ${response.status}`);
      if (Number(response.headers.get('age') || 0) > 0) return error('Источник вернул кэшированный ответ');
      const target = new URL(response.url);
      if (target.origin !== location.origin) return error('API перенаправил на другой сайт');
      const text = await response.text();
      if (text.length > 10000000) return error('Ответ слишком большой');
      let raw;
      try { raw = JSON.parse(text); } catch {
        if (/\/__wbaas\/challenges\/antibot\/|fab_chlg_|captcha-container|id=["']captcha["']|<title>\s*(?:access denied|доступ ограничен|robot or human|проверка браузера)/iu.test(text)) {
          return error('Резервный API вернул страницу проверки доступа вместо данных карточки', 'access');
        }
        return error(`Резервный API ${current.market.toUpperCase()} вернул ответ без данных JSON (HTTP ${response.status}); цена не подтверждена`);
      }
      const data = current.market === 'ozon' ? parser.cleanOzon(raw, current.article) : parser.cleanWb(raw, current.article);
      return data ? {kind: 'json', data} : error('SKU и тип цены в ответе не подтверждены');
    } catch { return error('Не удалось прочитать цену в браузере'); }
    finally { clearTimeout(timer); }
  }
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (message.type !== 'paintwatch.collect') return;
    collect(message.job).then(result => reply({...result, url: location.href})).catch(() => reply({...error('Структура карточки изменилась'), url: location.href}));
    return true;
  });
})();
