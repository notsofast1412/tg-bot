const key = document.getElementById('key');
const status = document.getElementById('status');
chrome.storage.local.get(['key', 'status']).then(saved => { key.value = saved.key || ''; status.textContent = saved.status || 'Вставьте код подключения'; });
document.getElementById('connect').onclick = async () => {
  try {
    const result = await chrome.runtime.sendMessage({type: 'paintwatch.connect', key: key.value});
    status.textContent = result.ok ? 'Подключено. Включите мониторинг в приложении' : result.error;
  } catch { status.textContent = 'Не удалось подключиться'; }
};
document.getElementById('check').onclick = () => chrome.runtime.sendMessage({type: 'paintwatch.tick'});
document.getElementById('open').onclick = () => chrome.runtime.sendMessage({type: 'paintwatch.open'});
chrome.storage.onChanged.addListener(changes => { if (changes.status) status.textContent = changes.status.newValue; });

document.getElementById('diagnose').onclick = async () => {
  const report = document.getElementById('report');
  report.hidden = false;
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const data = await chrome.tabs.sendMessage(tab.id, {type: 'paintwatch.diagnose'});
    report.value = JSON.stringify(data, null, 2);
    report.focus(); report.select();
  } catch { report.value = 'Откройте карточку товара WB/Ozon, обновите страницу и повторите диагностику'; }
};
