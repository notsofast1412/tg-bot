import fcntl
import logging
import re
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler


@contextmanager
def instance_lock(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (data_dir / "paintwatch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Другой экземпляр PaintWatch уже работает с этим DATA_DIR"
            ) from None
        yield


class Redactor(logging.Formatter):
    def __init__(self, token):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record):
        text = super().format(record)
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[REDACTED]", text)
        return re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[REDACTED]@", text)


def configure_logging(config):
    config.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    formatter = Redactor(config.bot_token.get_secret_value())
    file = RotatingFileHandler(
        config.log_dir / "paintwatch.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    console = logging.StreamHandler()
    for handler in (file, console):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file, console], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
