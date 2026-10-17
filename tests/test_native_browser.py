from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest

from scripts import wb_browser_probe as worker


@pytest.fixture
def native(tmp_path, monkeypatch):
    chrome = Mock()
    chrome.poll.return_value = None
    chrome.returncode = None
    launch = Mock(return_value=chrome)
    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    connection = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    ready = AsyncMock(return_value=(None, connection))
    monkeypatch.setattr(worker.asyncio, 'open_connection', ready)
    context = object()
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    connect = AsyncMock(return_value=browser)
    playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
    return SimpleNamespace(chrome=chrome, launch=launch, connect=connect, playwright=playwright,
                           context=context, browser=browser, ready=ready, profile=tmp_path / 'profile')


async def test_native_chrome_uses_local_debugging_and_closes_owned_process(native):
    async with worker.native_context(native.playwright, '/usr/bin/google-chrome-stable', native.profile) as context:
        assert context is native.context
        args = native.launch.call_args.args[0]
        assert args[0] == '/usr/bin/google-chrome-stable'
        assert '--remote-debugging-address=127.0.0.1' in args
        endpoint = urlsplit(native.connect.call_args.args[0])
        assert endpoint.hostname == '127.0.0.1' and endpoint.port > 0
        assert '--remote-debugging-port=' + str(endpoint.port) in args
        assert '--user-data-dir=' + str(native.profile) in args
        assert '--no-proxy-server' in args
        assert '--no-sandbox' not in args and '--enable-automation' not in args
        assert '--disable-blink-features=AutomationControlled' not in args
        assert not any('ignore-certificate' in flag for flag in args)
        assert args[-1] == 'about:blank'
    native.browser.close.assert_awaited_once()
    native.chrome.terminate.assert_called_once()
    native.chrome.wait.assert_called_once_with(timeout=5)


async def test_failed_protocol_connection_still_stops_chrome(native):
    native.connect.side_effect = RuntimeError('connection failed')
    with pytest.raises(RuntimeError, match='connection failed'):
        async with worker.native_context(native.playwright, '/browser', native.profile):
            pytest.fail('Must not yield a context')
    native.chrome.terminate.assert_called_once()
    native.chrome.wait.assert_called_once()


async def test_reader_failure_still_disconnects_and_stops_chrome(native):
    with pytest.raises(ValueError, match='reader failed'):
        async with worker.native_context(native.playwright, '/browser', native.profile):
            raise ValueError('reader failed')
    native.browser.close.assert_awaited_once()
    native.chrome.terminate.assert_called_once()


async def test_an_existing_profile_process_is_not_mistaken_for_a_new_browser(native):
    native.chrome.poll.return_value = 0
    native.chrome.returncode = 0
    with pytest.raises(worker.BrowserError, match='busy'):
        async with worker.native_context(native.playwright, '/browser', native.profile):
            pytest.fail('Must not connect to an unowned browser')
    native.connect.assert_not_awaited()
    native.chrome.terminate.assert_not_called()


async def test_unresponsive_chrome_is_killed_after_graceful_shutdown_fails(native):
    import subprocess
    native.chrome.wait.side_effect = [subprocess.TimeoutExpired('chrome', 5), 0]
    async with worker.native_context(native.playwright, '/browser', native.profile):
        pass
    native.chrome.kill.assert_called_once()
    assert native.chrome.wait.call_count == 2


async def test_protocol_close_failure_does_not_leave_chrome_running(native):
    native.browser.close.side_effect = RuntimeError('disconnect failed')
    with pytest.raises(RuntimeError):
        async with worker.native_context(native.playwright, '/browser', native.profile):
            pass
    native.chrome.terminate.assert_called_once()
    native.chrome.wait.assert_called_once()


async def test_readiness_timeout_stops_the_browser(native, monkeypatch):
    native.ready.side_effect = OSError('not listening yet')
    monkeypatch.setattr(worker.asyncio, 'sleep', AsyncMock())
    with pytest.raises(worker.BrowserError, match='timeout'):
        async with worker.native_context(native.playwright, '/browser', native.profile):
            pytest.fail('Must not yield a disconnected browser')
    native.connect.assert_not_awaited()
    native.chrome.terminate.assert_called_once()
    native.chrome.wait.assert_called_once()


async def test_native_startup_uses_validated_card_url(native):
    async with worker.native_context(native.playwright, '/browser', native.profile,
                                     initial_url=worker.DEFAULT_URL):
        assert native.launch.call_args.args[0][-1] == worker.DEFAULT_URL


async def test_native_startup_rejects_unrelated_url_before_launch(native):
    with pytest.raises(ValueError):
        async with worker.native_context(native.playwright, '/browser', native.profile,
                                         initial_url='https://example.com/'):
            pytest.fail('Must reject an unrelated startup URL')
    native.launch.assert_not_called()
