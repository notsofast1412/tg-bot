import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from paintbot.marketplaces.links import MarketError
from paintbot.services.wb_native_navigation import NativeNavigator, NativeNavigationError


URL = 'https://www.wildberries.ru/catalog/518397236/detail.aspx?size=716573024'


async def test_only_owned_chrome_window_receives_canonical_url():
    navigator = NativeNavigator(gap=0)
    async def command(*args):
        if args[0] == 'search': return '101\n102'
        if args[:2] == ('getwindowname', '101'): return 'Краска — Google Chrome'
        if args[:2] == ('getwindowname', '102'): return 'DevTools - www.wildberries.ru'
        return ''
    navigator.command = AsyncMock(side_effect=command)
    await navigator.navigate(URL, 1234)
    calls = [c.args for c in navigator.command.call_args_list]
    assert calls[0] == ('search', '--all', '--onlyvisible', '--pid', '1234', '--class', '[Gg]oogle-[Cc]hrome')
    assert calls[-4:] == [('windowfocus', '--sync', '101'), ('key', '--clearmodifiers', 'alt+d'),
        ('type', '--clearmodifiers', '--delay', '1', '--', URL), ('key', '--clearmodifiers', 'Return')]


@pytest.mark.parametrize('case', ['multiple', 'empty', 'unsafe_url', 'unsafe_pid'])
async def test_ambiguous_windows_and_untrusted_input_are_not_typed(case):
    navigator = NativeNavigator(gap=0)
    navigator.command = AsyncMock(side_effect=lambda *args:
        ('1\n2' if case == 'multiple' else '') if args[0] == 'search' else 'Chrome')
    with pytest.raises((NativeNavigationError, MarketError)):
        await navigator.navigate('https://evil.test/' if case == 'unsafe_url' else URL,
                                 '1234' if case == 'unsafe_pid' else 1234)
    assert not any(c.args[0] in ('type', 'key') for c in navigator.command.call_args_list)


async def test_xdotool_is_reaped_if_navigation_is_cancelled(monkeypatch):
    started = asyncio.Event()
    async def communicate():
        started.set()
        await asyncio.Event().wait()
    process = Mock(returncode=None)
    process.communicate = communicate
    process.wait = AsyncMock()
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    task = asyncio.create_task(NativeNavigator().command('windowfocus', '--sync', '101'))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
