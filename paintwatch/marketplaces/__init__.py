from .ozon import OzonAdapter
from .wb import WildberriesAdapter


def adapters(web, config):
    return {"wb": WildberriesAdapter(web, config.wb_dest), "ozon": OzonAdapter(web)}
