from contextlib import asynccontextmanager
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from scripts import wb_browser_probe as worker


class BrowserTimeout(Exception):
    pass


@pytest.fixture
def browser_page(tmp_path, monkeypatch):
    url = worker.DEFAULT_URL
    payload = {'products': [{'id': 518397236, 'name': 'Краска', 'totalQuantity': 52,
                            'sizes': [{'optionId': 716573024, 'price': {'product': 26300},
                                       'stocks': [{'qty': 52}]}]}]}
    api = SimpleNamespace(url='https://www.wildberries.ru/__internal/u-card/cards/v4/detail?nm=518397236',
                          status=200, headers={}, json=AsyncMock(return_value=payload))
    doc = SimpleNamespace(status=200, headers={})
    page = SimpleNamespace(url=url, title=AsyncMock(return_value='Краска WB'),
                           wait_for_timeout=AsyncMock())
    page.locator = Mock(return_value=SimpleNamespace(wait_for=AsyncMock(), all_text_contents=AsyncMock(return_value=[])))
    callbacks = {}
    page.on = lambda event, callback: callbacks.update({event: callback})
    async def navigate(*args, **kwargs):
        if api is not None:
            callbacks['response'](api)
        return doc
    page.goto = AsyncMock(side_effect=navigate)
    context = SimpleNamespace(pages=[page], close=AsyncMock())
    native_calls = []
    @asynccontextmanager
    async def native(playwright, executable, profile, initial_url='about:blank'):
        native_calls.append((executable, profile, initial_url))
        try:
            yield context
        finally:
            await context.close()
    monkeypatch.setattr(worker, 'native_context', native)
    class Manager:
        async def __aenter__(self):
            return SimpleNamespace()
        async def __aexit__(self, *args):
            pass
    parent = ModuleType('playwright')
    parent.__path__ = []
    module = ModuleType('playwright.async_api')
    module.async_playwright = Manager
    module.TimeoutError = BrowserTimeout
    monkeypatch.setitem(sys.modules, 'playwright', parent)
    monkeypatch.setitem(sys.modules, 'playwright.async_api', module)
    monkeypatch.setattr(worker, 'project_root', lambda: tmp_path)
    monkeypatch.setattr(worker.shutil, 'which', lambda name: '/usr/bin/google-chrome-stable')
    monkeypatch.setattr(worker.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout='Chrome test'))
    return SimpleNamespace(api=api, doc=doc, page=page, context=context, native_calls=native_calls, callbacks=callbacks)


@pytest.mark.parametrize('status,error', [(200, None), (498, 'http_498'), (None, 'no_product'),
                                        (True, 'no_product')])
async def test_startup_url_observes_native_navigation_without_goto(browser_page, status, error):
    state = browser_page
    async def loaded(*args, **kwargs):
        state.callbacks['response'](state.api)
    state.page.wait_for_load_state = AsyncMock(side_effect=loaded)
    state.page.evaluate = AsyncMock(return_value=status)
    result = await worker.probe(worker.DEFAULT_URL, startup_url=True)
    assert result.get('error') == error
    assert result['launch_mode'] == 'native_cdp_startup_url'
    assert result['document_status_source'] == 'navigation_timing'
    assert state.native_calls[0][2] == worker.DEFAULT_URL
    state.page.goto.assert_not_awaited()
    state.context.close.assert_awaited_once()
    if status == 200 and type(status) is int:
        assert result['products'][0]['sizes'][0]['price']['product'] == 26300


async def test_startup_url_does_not_invent_api_data_when_response_preceded_attachment(browser_page):
    state = browser_page
    state.page.wait_for_load_state = AsyncMock()
    state.page.evaluate = AsyncMock(return_value=200)
    result = await worker.probe(worker.DEFAULT_URL, startup_url=True)
    assert result['error'] == 'no_product'
    assert result['products'] == []
    state.page.goto.assert_not_awaited()


async def test_collects_actual_page_response_and_closes_context(browser_page):
    result = await worker.probe(worker.DEFAULT_URL)
    assert result.get('error') is None
    assert result['same_card_and_size'] is True
    assert result['products'][0]['sizes'][0]['price']['product'] == 26300
    assert result['products'][0]['sizes'][0]['optionId'] == 716573024
    browser_page.context.close.assert_awaited_once()
    assert result['launch_mode'] == 'native_cdp'
    assert browser_page.native_calls[0][0] == '/usr/bin/google-chrome-stable'
    assert browser_page.native_calls[0][1].name == 'manual-browser-stable-profile'



@pytest.mark.parametrize('case,error', [('doc403', 'http_403'), ('doc498', 'http_498'), ('doc500', 'http_500'),
    ('api403', 'http_403'), ('api498', 'http_498'),
    ('rate_limit', 'http_429'), ('redirect', 'different_card'), ('captcha', 'challenge'),
    ('stale', 'no_product'), ('other_host', 'no_product'), ('timeout', 'timeout')])
async def test_failed_page_or_response_is_not_accepted(browser_page, case, error):
    state = browser_page
    if case.startswith('doc'):
        state.doc.status = int(case[3:])
    elif case.startswith('api'):
        state.api.status = int(case[3:])
    elif case == 'rate_limit':
        state.api.status = 429
        state.api.headers['retry-after'] = '7200'
    elif case == 'redirect':
        state.page.url = worker.DEFAULT_URL.replace('716573024', '999')
    elif case == 'captcha':
        state.page.title.return_value = 'Antibot Captcha'
    elif case == 'stale':
        state.api.headers['age'] = '121'
    elif case == 'other_host':
        state.api.url = 'https://www.wildberries.ru.evil.test/__internal/u-card/cards/v4/detail'
    else:
        state.page.goto.side_effect = BrowserTimeout()
    result = await worker.probe(worker.DEFAULT_URL)
    assert result['error'] == error
    state.context.close.assert_awaited_once()
    state.page.goto.assert_awaited_once()
    if case == 'rate_limit':
        assert result['retry_after'] == 7200
    if case.startswith('doc'):
        state.page.wait_for_timeout.assert_not_awaited()


async def test_changed_wb_route_is_reported_without_accepting_its_prices(browser_page):
    browser_page.api.url = 'https://www.wildberries.ru/__internal/u-card/cards/v5/detail?nm=518397236&token=PRIVATE'
    result = await worker.probe(worker.DEFAULT_URL)
    assert result['error'] == 'no_product'
    assert result['responses'][0]['source_allowed'] is False
    assert result['responses'][0]['skip_reason'] == 'different_source'
    assert result['products'] == []
    assert 'PRIVATE' not in repr(result)


async def test_cache_rejection_has_a_specific_reason(browser_page):
    browser_page.api.headers['age'] = '121'
    result = await worker.probe(worker.DEFAULT_URL)
    assert result['responses'][0]['skip_reason'] == 'stale_cache'
    assert result['responses'][0]['age_seconds'] == 121


async def test_unreadable_api_body_does_not_leak_its_exception(browser_page):
    browser_page.api.json.side_effect = ValueError('PRIVATE response body')
    result = await worker.probe(worker.DEFAULT_URL)
    assert result['error'] == 'no_product'
    assert result['responses'][0]['read_error'] == 'ValueError'
    assert 'PRIVATE' not in repr(result)


async def test_product_response_arriving_after_old_five_second_wait_is_collected(browser_page):
    import asyncio
    browser_page.page.goto.side_effect = None
    browser_page.page.goto.return_value = browser_page.doc
    elapsed = [0]
    async def wait(milliseconds):
        elapsed[0] += milliseconds
        if elapsed[0] == 6000:
            browser_page.callbacks['response'](browser_page.api)
        await asyncio.sleep(0)
    browser_page.page.wait_for_timeout.side_effect = wait
    result = await worker.probe(worker.DEFAULT_URL)
    assert not result.get('error')
    assert result['products'][0]['id'] == '518397236'
    assert elapsed[0] >= 6000
    browser_page.page.locator.return_value.wait_for.assert_not_awaited()
