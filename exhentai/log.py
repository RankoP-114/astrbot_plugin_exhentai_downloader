import logging

try:
    from astrbot.api import logger as astrbot_logger
except Exception:
    astrbot_logger = logging.getLogger("astrbot_plugin_exhentai_downloader")

logger = astrbot_logger
