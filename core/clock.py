"""站内自然日统一使用北京时间，不依赖服务器的本地时区。"""
import time
from datetime import datetime, timedelta, timezone

SITE_TZ = timezone(timedelta(hours=8))


def day_start(now=None):
    now = time.time() if now is None else now
    return int(datetime.fromtimestamp(now, SITE_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
