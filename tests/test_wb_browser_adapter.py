import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import sys
from unittest.mock import AsyncMock

import pytest

from paintbot.marketplaces import WildberriesAdapter
from paintbot.marketplaces.browser import check_report
from paintbot.marketplaces.links import MarketError, parse_link
from paintbot.scheduler.monitor import Monitor
from scripts.wb_browser_probe import browser_lock, BrowserError, retry_seconds

URL = 'https://www.wildberries.ru/catalog/518397236/detail.aspx?size=716573024'


@pytest.fixture
def report():
    # Public fields supplied by the user's successful on-VM browser diagnostic.
    return {'schema': 1, 'browser': 'Google Chrome 155.0.8059.84',
            'card_id': '518397236', 'size': '716573024', 'page_http': 200,
            'same_card_and_size': True,
            'products': [{'id': '518397236',
                'name': 'Краска автомобильная шоколадно-коричневый 8017, 400 мл',
                'totalQuantity': 52,
                'sizes': [{'chrtId': None, 'optionId': 716573024,
                           'price': {'basic': 51600, 'product': 26300, 'logistics': 0, 'return': 0},
                           'stock_qty': 52}]}]}


async def test_user_card_add_and_monitor_preserve_exact_price(store, report):
    fetch = AsyncMock(return_value=report)
    adapter = WildberriesAdapter(gap=0, browser_fetch=fetch)
    try:
        card = await adapter.fetch(parse_link(URL))
        assert card.price == 26300
        assert card.card_id == '518397236|size:716573024'
        assert '/__internal/u-card/cards/v4/detail' in card.source
        product_id = store.add_batch([card], 'Моя краска', store.tags()[0].id)[0][0]
        assert store.product(product_id).last_price is None
        assert not store.queue()
        assert await Monitor(store, {'wb': adapter}).cycle() == 1
        assert store.product(product_id).last_price == 26300
        assert [entry.price for entry in store.history(product_id)[0]] == [26300]
        assert len(store.queue()) == 1
        assert fetch.await_count == 2
    finally:
        await adapter.client.aclose()


@pytest.mark.parametrize('case', ['card', 'variant', 'redirect', 'product'])
async def test_different_card_or_size_never_produces_a_price(report, case):
    if case == 'card':
        report['card_id'] = '999'
    elif case == 'variant':
        report['size'] = '999'
    elif case == 'redirect':
        report['same_card_and_size'] = False
    else:
        report['products'][0]['id'] = '999'
    adapter = WildberriesAdapter(gap=0, browser_fetch=AsyncMock(return_value=report))
    try:
        with pytest.raises(MarketError):
            await adapter.fetch(parse_link(URL))
    finally:
        await adapter.client.aclose()


@pytest.mark.parametrize('case', ['unavailable', 'conflicting_prices', 'wrong_option'])
async def test_ambiguous_or_unavailable_browser_prices_remain_unknown(report, case):
    if case == 'unavailable':
        report['products'][0]['sizes'][0]['stock_qty'] = 0
    elif case == 'wrong_option':
        report['products'][0]['sizes'][0]['optionId'] = 999
    else:
        alternate = deepcopy(report['products'][0])
        alternate['sizes'][0]['price']['product'] = 25500
        report['products'].append(alternate)
    adapter = WildberriesAdapter(gap=0, browser_fetch=AsyncMock(return_value=report))
    try:
        card = await adapter.fetch(parse_link(URL))
        assert card.price is None and card.warning
    finally:
        await adapter.client.aclose()


@pytest.mark.parametrize('error', ['http_401', 'http_403', 'http_429', 'challenge', 'busy', 'timeout', 'no_product'])
def test_worker_failure_is_not_a_card(error):
    with pytest.raises(MarketError):
        check_report({'schema': 1, 'error': error})


def test_browser_profile_lock_is_released_after_failure(tmp_path):
    with pytest.raises(RuntimeError):
        with browser_lock(tmp_path):
            with pytest.raises(BrowserError, match='busy'):
                with browser_lock(tmp_path):
                    pytest.fail('Concurrent browser access must be rejected')
            raise RuntimeError('test failure')
    with browser_lock(tmp_path):
        pass


@pytest.mark.parametrize('header,expected', [('7200', 7200), ('1', 600), ('999999', 86400),
    (None, 600), ('NaN', 600), ('inf', 600)])
def test_browser_retry_after_is_bounded(header, expected):
    assert retry_seconds(header) == expected


@pytest.fixture
def worker(tmp_path, monkeypatch):
    from paintbot.marketplaces import browser
    monkeypatch.setattr(browser, 'ROOT', tmp_path)
    python = tmp_path / '.browser-probe/bin/python'
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    script = tmp_path / 'scripts/wb_browser_probe.py'
    script.parent.mkdir()
    real_exec = asyncio.create_subprocess_exec
    processes = []
    async def execute(*args, **kwargs):
        assert args[:2] == ('xvfb-run', '-a')
        assert args[4] == '--machine'
        process = await real_exec(*args[2:], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(browser.asyncio, 'create_subprocess_exec', execute)
    return browser, script, processes


async def test_real_worker_roundtrip_keeps_normal_home_and_secrets_out(worker, monkeypatch, report):
    browser, script, processes = worker
    monkeypatch.setenv('BOT_TOKEN', 'PRIVATE_TOKEN')
    script.write_text('import os, json\nfrom pathlib import Path\n'
        'assert "BOT_TOKEN" not in os.environ\n'
        'assert Path(os.environ["HOME"]) == Path.cwd()\n'
        'assert "XDG_CACHE_HOME" not in os.environ\n'
        + 'print(' + repr(json.dumps(report)) + ')\n')
    result = await browser.fetch_wb_page(URL)
    assert result == report
    assert processes[0].returncode == 0


@pytest.mark.parametrize('behavior', ['timeout', 'cancel'])
async def test_worker_process_is_reaped_on_timeout_or_cancellation(worker, monkeypatch, behavior):
    browser, script, processes = worker
    script.write_text('import time\ntime.sleep(60)\n')
    monkeypatch.setattr(browser, 'WORKER_TIMEOUT', 0.05 if behavior == 'timeout' else 100)
    killed = []
    real_kill = os.killpg
    def kill(group, sig):
        killed.append((group, sig))
        real_kill(group, sig)
    monkeypatch.setattr(browser.os, 'killpg', kill)
    task = asyncio.create_task(browser.fetch_wb_page(URL))
    if behavior == 'cancel':
        await asyncio.sleep(0.05)
        task.cancel()
    with pytest.raises(MarketError if behavior == 'timeout' else asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None
    assert killed == [(processes[0].pid, signal.SIGKILL)]


@pytest.mark.parametrize('mode', ['nonzero', 'invalid_json', 'oversized'])
async def test_invalid_worker_output_never_becomes_a_price(worker, report, mode):
    browser, script, _ = worker
    if mode == 'nonzero':
        script.write_text('print(' + repr(json.dumps(report)) + ')\nraise SystemExit(2)\n')
    elif mode == 'invalid_json':
        script.write_text('print("PRIVATE_TOKEN: not json")\n')
    else:
        script.write_text('print("X" * 1000001)\n')
    with pytest.raises(MarketError) as exc:
        await browser.fetch_wb_page(URL)
    assert 'PRIVATE_TOKEN' not in str(exc.value)


async def test_cancel_during_process_creation_still_reaps_worker(worker, monkeypatch):
    browser, script, processes = worker
    script.write_text('import time\ntime.sleep(60)\n')
    execute = browser.asyncio.create_subprocess_exec
    started = asyncio.Event()
    async def delayed(*args, **kwargs):
        process = await execute(*args, **kwargs)
        started.set()
        await asyncio.sleep(0.05)
        return process
    monkeypatch.setattr(browser.asyncio, 'create_subprocess_exec', delayed)
    task = asyncio.create_task(browser.fetch_wb_page(URL))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode == -signal.SIGKILL


@pytest.mark.parametrize('status', [498, 500])
def test_nonstandard_http_error_is_reported_as_http_error(status):
    with pytest.raises(MarketError, match=f'HTTP {status}'):
        check_report({'schema': 1, 'error': f'http_{status}'})
