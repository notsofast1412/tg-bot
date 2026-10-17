from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import manual_browser_desktop as desktop


def test_system_browser_preferred_to_old_managed_build(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.shutil, 'which', lambda name: '/usr/bin/google-chrome-stable'
                        if name == 'google-chrome-stable' else None)
    assert desktop.browser_path(tmp_path) == Path('/usr/bin/google-chrome-stable')


def test_explicit_browser_must_exist_and_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.shutil, 'which', lambda name: None)
    with pytest.raises(RuntimeError, match='Requested browser'):
        desktop.browser_path(tmp_path, '/missing/browser')


def test_existing_managed_browser_still_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.shutil, 'which', lambda name: None)
    browser = tmp_path / 'var/chromium/chromium-1187/chrome-linux/chrome'
    browser.parent.mkdir(parents=True)
    browser.touch(mode=0o700)
    assert desktop.browser_path(tmp_path) == browser


@pytest.mark.parametrize('url', ['http://www.ozon.ru/product/123/',
    'https://www.ozon.ru:443/product/123/', 'https://user:pass@www.ozon.ru/product/123/',
    'https://www.ozon.ru.evil.example/product/123/', 'https://www.ozon.ru/search/',
    'https://[broken/product/123/'])
def test_card_address_rejects_unrelated_or_unsafe_urls(url):
    with pytest.raises(desktop.argparse.ArgumentTypeError):
        desktop.card_url(url)


def test_browser_start_failure_stops_private_vnc(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop.os, 'umask', Mock())
    password = tmp_path / 'var/vnc.pass'
    password.parent.mkdir()
    password.touch(mode=0o600)
    monkeypatch.setattr(desktop, 'Path', lambda p: tmp_path if p == '/opt/paintbot' else Path(p))
    monkeypatch.setattr(desktop, 'browser_path', lambda *args: Path('/usr/bin/google-chrome-stable'))
    monkeypatch.setenv('DISPLAY', ':99')
    monkeypatch.setenv('XAUTHORITY', '/tmp/test-xauthority')
    monkeypatch.setattr(desktop.signal, 'signal', Mock())
    monkeypatch.setattr(desktop.threading, 'Event', lambda: SimpleNamespace(wait=lambda _: False))
    sock = Mock()
    class Socket:
        def __enter__(self):
            return sock
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(desktop.socket, 'socket', Socket)
    monkeypatch.setattr(desktop.subprocess, 'run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='Google Chrome test version\n'))
    vnc = Mock()
    vnc.poll.return_value = None
    calls = []
    def popen(args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return vnc
        raise OSError('Browser launch failed')
    monkeypatch.setattr(desktop.subprocess, 'Popen', popen)
    with pytest.raises(OSError, match='Browser launch failed'):
        desktop.main(browser='google-chrome-stable')
    sock.bind.assert_called_once_with(('127.0.0.1', 5901))
    assert '-localhost' in calls[0] and '-rfbauth' in calls[0]
    assert calls[0][calls[0].index('-listen') + 1] == '127.0.0.1'
    assert '--no-proxy-server' in calls[1]
    assert '--no-sandbox' not in calls[1]
    assert desktop.DEFAULT_URL == calls[1][-1]
    assert (tmp_path / 'var/manual-browser-stable-profile').is_dir()
    vnc.terminate.assert_called_once()
    vnc.wait.assert_called_once_with(timeout=5)
