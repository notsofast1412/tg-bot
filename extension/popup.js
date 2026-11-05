const key = document.getElementById('key');
const status = document.getElementById('status');
const lightweight = document.getElementById('lightweight');
const autoCleanup = document.getElementById('autoCleanup');
chrome.storage.local.get(['key', 'status', 'lightweight', 'autoCleanup']).then(saved => { key.value = saved.key || ''; status.textContent = saved.status || 'Вставьте код подключения'; lightweight.checked = saved.lightweight !== false; autoCleanup.checked = saved.autoCleanup !== false; });
autoCleanup.onchange = async () => {
  try {
    await chrome.storage.local.set({autoCleanup: autoCleanup.checked});
    await chrome.storage.local.remove('cleanupNextAt');
    status.textContent = autoCleanup.checked ? 'Автоочистка включена: следующий запуск через 30 минут' : 'Автоочистка выключена';
    await chrome.runtime.sendMessage({type: 'paintwatch.tick'});
  } catch { status.textContent = 'Не удалось сохранить настройку автоочистки'; }
};
lightweight.onchange = async () => {
  try {
    await chrome.storage.local.set({lightweight: lightweight.checked});
    status.textContent = 'Настройка применяется к новым проверкам';
  } catch { status.textContent = 'Не удалось сохранить настройку загрузки фото и видео'; }
};
document.getElementById('connect').onclick = async () => {
  try {
    const result = await chrome.runtime.sendMessage({type: 'paintwatch.connect', key: key.value});
    status.textContent = result?.ok ? 'Подключено. Запустите мониторинг или разовую проверку в приложении' : result?.error || 'Расширение не ответило. Обновите его в chrome://extensions';
  } catch { status.textContent = 'Не удалось подключиться'; }
};
document.getElementById('check').onclick = () => chrome.runtime.sendMessage({type: 'paintwatch.tick'});
document.getElementById('open').onclick = async () => {
  const result = await chrome.runtime.sendMessage({type: 'paintwatch.open'});
  if (result?.error) status.textContent = result.error;
};
document.getElementById('cleanup').onclick = async () => {
  try {
    const result = await chrome.runtime.sendMessage({type: 'paintwatch.cleanup'});
    status.textContent = result?.ok ? 'Очистка запрошена. Новые проверки приостанавливаются' : result?.error || 'Расширение не ответило';
  } catch { status.textContent = 'Не удалось запустить очистку вкладок'; }
};
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
