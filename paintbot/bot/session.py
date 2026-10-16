"""Aiogram transport using HTTPX's documented environment proxy support.

Only JSON/text methods are needed by this application. No proxy credentials or
token-bearing URLs are copied into configuration or error messages.
"""
import httpx
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError


class ProxyAwareSession(BaseSession):
    def __init__(self, client=None, **kwargs):
        super().__init__(**kwargs)
        self.client = client or httpx.AsyncClient(trust_env=True, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def make_request(self, bot, method, timeout=None):
        files, data = {}, {}
        for key, value in method.model_dump(warnings=False).items():
            prepared = self.prepare_value(value, bot=bot, files=files)
            if prepared is not None:
                data[key] = prepared
        if files:
            raise ValueError("Приложение поддерживает только текстовые методы Telegram")
        try:
            response = await self.client.post(self.api.api_url(token=bot.token, method=method.__api_method__),
                data=data, timeout=self.timeout if timeout is None else timeout)
        except httpx.HTTPError as exc:
            raise TelegramNetworkError(method=method, message=type(exc).__name__) from None
        result = self.check_response(bot=bot, method=method, status_code=response.status_code, content=response.text)
        return result.result

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        async with self.client.stream("GET", url, headers=headers, timeout=timeout) as response:
            if raise_for_status:
                response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size):
                yield chunk
