import asyncio
import logging
import time

from paintbot.marketplaces.links import MarketError, parse_link

logger = logging.getLogger(__name__)


class Monitor:
    def __init__(self, store, adapters, interval=600):
        self.store, self.adapters, self.interval = store, adapters, interval
        self.lock = asyncio.Lock()
        self.last_tick = None
        self.last_error = None
        self.running = False
        for market, adapter in adapters.items():
            saved = store.setting("cooldown:" + market)
            if saved:
                adapter.blocked_until = int(saved)

    async def cycle(self):
        if self.lock.locked():
            return 0
        async with self.lock:
            self.last_tick = int(time.time())
            checked = 0
            for product in self.store.due(self.last_tick):
                self.store.begin_attempt(product.id, self.interval)
                try:
                    card = await self.adapters[product.marketplace].fetch(parse_link(product.url))
                    self.store.record(product.id, card, None, self.interval)
                except MarketError as exc:
                    interval = max(self.interval, exc.retry_after)
                    if exc.retry_after:
                        self.store.setting("cooldown:" + product.marketplace, int(time.time()) + exc.retry_after)
                    self.store.record(product.id, None, str(exc), interval)
                except Exception as exc:
                    # Do not log external exception strings: URLs may contain sensitive data.
                    logger.error("Adapter failure product=%s type=%s", product.id, type(exc).__name__)
                    self.store.record(product.id, None, f"Ошибка адаптера: {type(exc).__name__}", self.interval)
                checked += 1
            return checked

    async def run(self):
        self.running = True
        try:
            while True:
                try:
                    await self.cycle()
                    self.last_error = None
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    logger.error("Monitor cycle failed: %s", self.last_error)
                await asyncio.sleep(5)
        finally:
            self.running = False
