"""Interactive local configuration: token is never a command-line argument."""
import getpass
import os
import warnings
from pathlib import Path

from aiogram.utils.token import TokenValidationError, validate_token
from dotenv import dotenv_values, set_key


def save_settings(path, token, owner_id, group_id=None):
    validate_token(token)
    if not isinstance(owner_id, int) or isinstance(owner_id, bool) or owner_id <= 0:
        raise ValueError("OWNER_ID должен быть положительным числом")
    if group_id is not None and group_id >= 0:
        raise ValueError("GROUP_ID должен быть отрицательным числом")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Конфигурация не должна быть символической ссылкой")
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    set_key(path, "BOT_TOKEN", token)
    set_key(path, "OWNER_ID", str(owner_id))
    if group_id is not None:
        set_key(path, "GROUP_ID", str(group_id))
    os.chmod(path, 0o600)


def main():
    os.umask(0o077)
    path = Path(".env")
    existing = dotenv_values(path) if path.is_file() else {}
    try:
        # Refuse an echoing fallback if a protected interactive terminal is absent.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass("Токен BotFather (скрытый ввод; Enter — оставить существующий): ")
        token = token or existing.get("BOT_TOKEN") or ""
        owner_text = input("Ваш числовой Telegram OWNER_ID: ").strip()
        group_text = input("GROUP_ID (необязательно; Enter — настроить позднее в боте): ").strip()
        save_settings(path, token, int(owner_text), int(group_text) if group_text else None)
    except (ValueError, TokenValidationError, getpass.GetPassWarning):
        raise SystemExit("Настройки не сохранены: проверьте токен, числовые ID и защищённый интерактивный терминал") from None
    print("Конфигурация сохранена с правами 600; токен не выводится. Дальше: python -m scripts.verify_server")


if __name__ == "__main__":
    main()
