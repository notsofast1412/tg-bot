"""Navigate the owned Chrome window through its address bar, without browser APIs."""
import asyncio
import time

from paintbot.marketplaces.links import parse_link


class NativeNavigationError(Exception):
    pass


class NativeNavigator:
    def __init__(self, environment=None, gap=5):
        self.environment = environment
        self.gap = gap
        self.next_navigation = 0
        self.lock = asyncio.Lock()

    async def command(self, *args):
        process = await asyncio.create_subprocess_exec(
            'xdotool', *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self.environment)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 3)
            if process.returncode != 0:
                raise NativeNavigationError()
            return output.decode('utf-8', errors='replace').strip()
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()

    async def navigate(self, url, chrome_pid):
        ref = parse_link(url)
        if ref.marketplace != 'wb' or ref.url != url or type(chrome_pid) is not int or chrome_pid <= 0:
            raise NativeNavigationError()
        async with self.lock:
            await asyncio.sleep(max(0, self.next_navigation - time.monotonic()))
            self.next_navigation = time.monotonic() + self.gap
            async with asyncio.timeout(15):
                matches = await self.command('search', '--all', '--onlyvisible', '--pid', str(chrome_pid),
                                             '--class', '[Gg]oogle-[Cc]hrome')
                windows = []
                for window in matches.splitlines()[:10]:
                    if not window.isdecimal():
                        raise NativeNavigationError()
                    title = (await self.command('getwindowname', window)).lower()
                    if 'devtools' not in title and 'инструменты разработчика' not in title:
                        windows.append(window)
                # Refuse ambiguous desktops instead of typing into another window.
                if len(windows) != 1:
                    raise NativeNavigationError()
                # XSetInputFocus works under bare Xvfb; windowactivate requires a WM.
                await self.command('windowfocus', '--sync', windows[0])
                await self.command('key', '--clearmodifiers', 'alt+d')
                await self.command('type', '--clearmodifiers', '--delay', '1', '--', url)
                await self.command('key', '--clearmodifiers', 'Return')
