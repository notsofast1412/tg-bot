"""Bounded bridge to the separately installed Playwright browser worker."""
import asyncio
import json
import os
from pathlib import Path
import re
import signal

from .links import MarketError

ROOT = Path(__file__).resolve().parents[2]
WORKER_TIMEOUT = 100

ERRORS = {
    'busy': 'Браузер WB занят. Остановите ручной VNC-браузер или дождитесь текущей проверки',
    'browser_missing': 'Для WB требуется Google Chrome и отдельное окружение .browser-probe с Playwright',
    'browser_failed': 'Не удалось запустить браузер WB. Проверьте профиль, Chrome и Playwright; остановите VNC-браузер',
    'timeout': 'Браузер WB не завершил загрузку карточки за отведённое время',
    'different_card': 'WB открыл другую карточку или вариант; цена не используется',
    'challenge': 'WB запросил CAPTCHA или ограничил доступ; цена не получена',
    'no_product': 'Страница WB не подтвердила данные выбранного товара',
}


def check_report(report):
    if not isinstance(report, dict) or report.get('schema') != 1:
        raise MarketError('Некорректный ответ браузера WB')
    error = report.get('error')
    if error:
        if not isinstance(error, str):
            raise MarketError('Некорректный статус браузера WB')
        if re.fullmatch(r'http_[1-5][0-9]{2}', error):
            value = report.get('retry_after', 600)
            retry = max(600, min(value, 86400)) if error == 'http_429' and type(value) is int else (600 if error == 'http_429' else 0)
            raise MarketError(f"HTTP {error[5:]}: браузер WB получил отказ; получение данных остановлено", retry_after=retry)
        raise MarketError(ERRORS.get(error, 'Браузер WB не подтвердил цену'))
    if report.get('page_http') != 200 or report.get('same_card_and_size') is not True:
        raise MarketError('WB не подтвердил загрузку выбранной карточки и варианта')
    if not isinstance(report.get('products'), list) or not report['products']:
        raise MarketError(ERRORS['no_product'])
    return report


async def fetch_wb_page(url):
    root = ROOT
    python = root / '.browser-probe/bin/python'
    worker = root / 'scripts/wb_browser_probe.py'
    if not python.is_file():
        raise MarketError(ERRORS['browser_missing'])
    environment = dict(os.environ)
    # Preserve the normal application user's HOME, as in the successful manual run.
    # systemd permits writing only the browser's private home directories and var.
    environment['HOME'] = str(root)
    for directory in (root / '.cache', root / '.config', root / '.pki'):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for key in ('XDG_CACHE_HOME', 'XDG_CONFIG_HOME'):
        environment.pop(key, None)
    for key in ('BOT_TOKEN', 'OWNER_ID', 'GROUP_ID'):
        environment.pop(key, None)
    environment['PLAYWRIGHT_BROWSERS_PATH'] = str(root / 'var/chromium')
    process = None
    try:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            'xvfb-run', '-a', str(python), str(worker), '--machine', url,
            cwd=str(root), env=environment, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            raise
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=WORKER_TIMEOUT)
        if len(stdout) > 1_000_000:
            raise MarketError('Слишком большой ответ браузера WB')
        try:
            report = json.loads(stdout)
        except (ValueError, UnicodeError):
            raise MarketError(ERRORS['browser_failed']) from None
        result = check_report(report)
        if process.returncode != 0:
            raise MarketError(ERRORS['browser_failed'])
        return result
    except asyncio.TimeoutError:
        raise MarketError(ERRORS['timeout']) from None
    except OSError:
        raise MarketError(ERRORS['browser_missing']) from None
    finally:
        if process is not None and process.returncode is None:
            # Reap the full Xvfb/Playwright/Chrome group on cancellation or timeout.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
