from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys


def data_directory():
    if override := os.getenv("PAINTWATCH_DATA_DIR"):
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "PaintWatch"
    return Path.home() / ".local" / "share" / "paintwatch-desktop"


class SecretStore:
    """DPAPI per Windows user. On other platforms secrets stay in RAM only."""

    def __init__(self, directory, filename="telegram.dpapi"):
        self.path = Path(directory) / filename
        self.memory = ""

    def read(self):
        if sys.platform != "win32":
            return self.memory
        if not self.path.exists():
            return ""
        return self._crypt(self.path.read_bytes(), False).decode("utf-8")

    def save(self, value):
        if sys.platform != "win32":
            self.memory = value
            return
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(self._crypt(value.encode("utf-8"), True))
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)
        self.memory = ""

    @staticmethod
    def _crypt(value, protect):
        class Blob(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("data", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        buffer = ctypes.create_string_buffer(value)
        source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if protect:
            ok = crypt.CryptProtectData(
                ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
            )
        else:
            ok = crypt.CryptUnprotectData(
                ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)
            )
        if not ok:
            raise RuntimeError(
                "Не удалось открыть защищённый токен этого пользователя Windows"
            )
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            kernel.LocalFree(target.data)


@contextmanager
def instance_lock(directory):
    path = Path(directory) / "app.lock"
    file = path.open("a+b")
    if file.tell() == 0:
        file.write(b"0")
        file.flush()
    file.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError(
                    "PaintWatch уже запущен. Проверьте значок в трее"
                ) from None
        else:
            import fcntl

            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("PaintWatch уже запущен") from None
        yield
    finally:
        file.close()


def autostart(enabled):
    if sys.platform != "win32":
        raise ValueError("Автозапуск доступен в Windows")
    import subprocess
    import winreg

    if getattr(sys, "frozen", False):
        command = subprocess.list2cmdline([sys.executable, "--minimized"])
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command = subprocess.list2cmdline(
            [
                str(pythonw),
                str(Path(__file__).resolve().parent.parent / "run_app.py"),
                "--minimized",
            ]
        )
    with winreg.CreateKey(
        winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
    ) as key:
        if enabled:
            winreg.SetValueEx(key, "PaintWatch", 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, "PaintWatch")
            except FileNotFoundError:
                pass


def open_normal_browser(url, browser="chrome"):
    import subprocess
    import webbrowser

    if sys.platform == "win32":
        suffix = (
            Path("Google/Chrome/Application/chrome.exe")
            if browser == "chrome"
            else Path("Microsoft/Edge/Application/msedge.exe")
        )
        for base in (
            os.getenv("PROGRAMFILES"),
            os.getenv("PROGRAMFILES(X86)"),
            os.getenv("LOCALAPPDATA"),
        ):
            if base and (path := Path(base) / suffix).is_file():
                subprocess.Popen([str(path), url])
                return
    webbrowser.open(url)


def extension_directory():
    base = (
        Path(sys.executable).parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent
    )
    return base / "extension"
