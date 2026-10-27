const key = document.getElementById('key');
const status = document.getElementById('status');
chrome.storage.local.get(['key', 'status']).then(saved => { key.value = saved.key || ''; status.textContent = saved.status || 'Вставьте код подключения'; });
document.getElementById('connect').onclick = async () => {
  try {
    const result = await chrome.runtime.sendMessage({type: 'paintwatch.connect', key: key.value});
    status.textContent = result?.ok ? 'Подключено. Запустите мониторинг или разовую проверку в приложении' : result?.error || 'Расширение не ответило. Обновите его в chrome://extensions';
  } catch { status.textContent = 'Не удалось подключиться'; }
};
document.getElementById('check').onclick = () => chrome.runtime.sendMessage({type: 'paintwatch.tick'});
document.getElementById('open').onclick = () => chrome.runtime.sendMessage({type: 'paintwatch.open'});
chrome.storage.onChanged.addListener(changes => { if (changes.status) status.textContent = changes.status.newValue; });

document.getElementById('diagnose').onclick = async () => {
  const report = document.getElementById('report');
  report.hidden = false;
  try {
    report.value = 'Читаем блок цены…';
    const data = await chrome.runtime.sendMessage({type: 'paintwatch.diagnoseActive'});
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('No report');
    report.value = JSON.stringify(data, null, 2);
    report.focus(); report.select();
  } catch { report.value = 'Расширение не вернуло отчёт. Нажмите ↻ для PaintWatch в chrome://extensions и повторите. Приложение для диагностики не требуется'; }
};
