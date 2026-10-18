"""Interactive Ozon browser, accessible through a localhost-only SSH tunnel."""

import asyncio
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys

from playwright.async_api import Error, async_playwright


ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "data" / "ozon-browser"
PORTS = (5901, 6080)


def check_prerequisites():
    if os.geteuid() == 0:
        raise RuntimeError("Запустите от пользователя paintwatch, не от root")
    for command in ("xvfb-run", "xauth", "x11vnc", "websockify"):
        if not shutil.which(command):
            raise RuntimeError(
                f"Не установлена команда {command}: см. docs/ozon-manual.md"
            )
    if not Path("/usr/share/novnc/vnc.html").is_file():
        raise RuntimeError("Не установлен пакет novnc")
    for port in PORTS:
        with socket.socket() as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(f"Локальный порт {port} уже занят") from None


async def wait_ready(process, port):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(
                "Сервис рабочего стола завершился: см. data/ozon-browser/desktop.log"
            )
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            await writer.wait_closed()
            return
        except OSError:
            await asyncio.sleep(0.1)
    raise RuntimeError("Локальный рабочий стол не успел запуститься")


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


async def desktop():
    PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
    PROFILE.chmod(0o700)
    processes = []
    closed = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, closed.set)
    with (PROFILE / "desktop.log").open("a") as log:
        try:
            vnc = subprocess.Popen(
                [
                    "x11vnc",
                    "-display",
                    os.environ["DISPLAY"],
                    "-auth",
                    os.environ["XAUTHORITY"],
                    "-localhost",
                    "-listen",
                    "127.0.0.1",
                    "-no6",
                    "-nopw",
                    "-forever",
                    "-shared",
                    "-rfbport",
                    "5901",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            processes.append(vnc)
            await wait_ready(vnc, 5901)
            web = subprocess.Popen(
                [
                    "websockify",
                    "--web=/usr/share/novnc",
                    "127.0.0.1:6080",
                    "127.0.0.1:5901",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            processes.append(web)
            await wait_ready(web, 6080)
            async with async_playwright() as pw:
                options = {}
                proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                if proxy:
                    options["proxy"] = {"server": proxy}
                context = await pw.chromium.launch_persistent_context(
                    str(PROFILE),
                    headless=False,
                    chromium_sandbox=True,
                    locale="ru-RU",
                    viewport={"width": 1200, "height": 760},
                    **options,
                )
                try:
                    context.on("close", lambda _: closed.set())
                    print("Рабочий стол слушает только 127.0.0.1:6080.", flush=True)
                    print(
                        "Откройте http://127.0.0.1:6080/vnc.html через SSH-туннель.",
                        flush=True,
                    )
                    print(
                        "Пройдите CAPTCHA сами. Закройте окно Chromium для сохранения профиля и выхода.",
                        flush=True,
                    )
                    page = (
                        context.pages[0] if context.pages else await context.new_page()
                    )
                    try:
                        await page.goto(
                            "https://www.ozon.ru/product/196525150/",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                    except Error:
                        print(
                            "Страница не загрузилась полностью; проверьте окно браузера.",
                            flush=True,
                        )
                    while not closed.is_set():
                        if any(process.poll() is not None for process in processes):
                            raise RuntimeError("Сервис рабочего стола завершился")
                        try:
                            await asyncio.wait_for(closed.wait(), timeout=1)
                        except TimeoutError:
                            pass
                finally:
                    await context.close()
        finally:
            for process in reversed(processes):
                stop_process(process)


def main():
    os.umask(0o077)
    try:
        if "--display" not in sys.argv[1:]:
            check_prerequisites()
            raise SystemExit(
                subprocess.call(
                    [
                        "xvfb-run",
                        "-a",
                        "-s",
                        "-screen 0 1280x900x24 -nolisten tcp",
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--display",
                    ]
                )
            )
        asyncio.run(desktop())
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except Error:
        raise SystemExit(
            "Не удалось запустить Chromium. Проверьте установку браузера, системных библиотек и поддержку sandbox. Проверка TLS и sandbox не отключаются."
        ) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
