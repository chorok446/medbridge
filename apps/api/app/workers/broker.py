import dramatiq
from dramatiq.brokers.redis import RedisBroker

from app.core.config import get_settings

_broker: RedisBroker | None = None


def setup_broker() -> RedisBroker:
    global _broker
    if _broker is None:
        _broker = RedisBroker(url=get_settings().redis_url)
        dramatiq.set_broker(_broker)
    return _broker
