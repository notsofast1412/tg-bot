from typing import Protocol

from paintwatch.domain import CardRef, Snapshot


class Adapter(Protocol):
    async def fetch(self, ref: CardRef) -> Snapshot: ...
