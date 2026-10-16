import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values

from paintbot.config import Config
from scripts.configure import save_settings


def test_interactive_settings_preserve_unrelated_configuration_and_secret_permissions(tmp_path):
    path = tmp_path / ".env"
    path.write_text("CHECK_INTERVAL_SECONDS=900\nGROUP_ID=-100123\n")
    save_settings(path, "999:TEST_TOKEN", 42)
    values = dotenv_values(path)
    assert values["BOT_TOKEN"] == "999:TEST_TOKEN"
    assert values["OWNER_ID"] == "42"
    assert values["GROUP_ID"] == "-100123"
    assert values["CHECK_INTERVAL_SECONDS"] == "900"
    assert path.stat().st_mode & 0o777 == 0o600


def test_invalid_configuration_cannot_partially_overwrite_existing_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("OWNER_ID=7\n")
    with pytest.raises(ValueError):
        save_settings(path, "999:TEST_TOKEN", -1)
    assert path.read_text() == "OWNER_ID=7\n"


def test_configuration_rejects_symlinks(tmp_path):
    target = tmp_path / "other"
    target.write_text("unchanged")
    path = tmp_path / ".env"
    path.symlink_to(target)
    with pytest.raises(ValueError):
        save_settings(path, "999:TEST_TOKEN", 1)
    assert target.read_text() == "unchanged"


async def test_server_verification_does_not_send_unless_explicitly_requested(store, monkeypatch):
    from scripts import verify_server
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(id=999)
    bot.get_webhook_info.return_value = SimpleNamespace(url="")
    monkeypatch.setattr(verify_server, "Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(verify_server, "ProxyAwareSession", lambda: None)
    monkeypatch.setattr(verify_server, "probe", AsyncMock(return_value=0))
    monkeypatch.setattr(verify_server, "group_access", AsyncMock(return_value=(True, "member")))
    config = Config(_env_file=None, bot_token="999:TEST_TOKEN", owner_id=1,
                    group_id=-100123, database_path=store.path)
    assert await verify_server.verify(config) == 0
    bot.send_message.assert_not_awaited()
    assert await verify_server.verify(config, send_test=True) == 0
    bot.send_message.assert_awaited_once()
    assert store.statistics()["last_attempt"] is None


async def test_failed_marketplace_probe_is_not_reported_as_success(store, monkeypatch):
    from scripts import verify_server
    bot = AsyncMock()
    bot.get_me.return_value = SimpleNamespace(id=999)
    bot.get_webhook_info.return_value = SimpleNamespace(url="")
    monkeypatch.setattr(verify_server, "Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(verify_server, "ProxyAwareSession", lambda: None)
    monkeypatch.setattr(verify_server, "probe", AsyncMock(return_value=1))
    config = Config(_env_file=None, bot_token="999:TEST_TOKEN", owner_id=1, database_path=store.path)
    assert await verify_server.verify(config) == 1
    bot.send_message.assert_not_awaited()
