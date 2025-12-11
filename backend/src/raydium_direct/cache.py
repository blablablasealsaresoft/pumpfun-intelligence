import time
from typing import Any, Dict, Optional, Tuple


class PoolCache:
    def __init__(self, ttl_ms_hot: int = 5000, ttl_ms_cold: int = 30000, max_size: int = 256):
        self.ttl_ms_hot = ttl_ms_hot
        self.ttl_ms_cold = ttl_ms_cold
        self.max_size = max_size
        self._cache: Dict[str, Tuple[float, Any, int]] = {}  # key -> (expires_at, value, ttl_ms)

    def get(self, key: str) -> Optional[Any]:
        now = time.time() * 1000
        entry = self._cache.get(key)
        if not entry:
            return None
        expires_at, value, _ttl = entry
        if now > expires_at:
            self._cache.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, hot: bool = True):
        ttl = self.ttl_ms_hot if hot else self.ttl_ms_cold
        expires_at = time.time() * 1000 + ttl
        if len(self._cache) >= self.max_size:
            # naive eviction: remove oldest
            oldest_key = min(self._cache.items(), key=lambda kv: kv[1][0])[0]
            self._cache.pop(oldest_key, None)
        self._cache[key] = (expires_at, value, ttl)

    def invalidate(self, key: str):
        self._cache.pop(key, None)


