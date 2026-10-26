import time

from .errors import HttpError


class RateLimiter:
    """Allow at most `max_hits` calls per IP in any rolling hour.

    Kept in memory, so it resets when the server restarts (and each serverless
    instance has its own). Good enough to protect an API key on a small app.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def check(self, ip: str, max_hits: int) -> None:
        now = time.time()
        recent = [t for t in self._hits.get(ip, []) if now - t < 3600]
        if len(recent) >= max_hits:
            self._hits[ip] = recent
            raise HttpError(429, "That's a lot of plans for one hour. Please try again a little later.")
        recent.append(now)
        self._hits[ip] = recent

    def reset(self) -> None:
        self._hits.clear()


rate_limiter = RateLimiter()  # one shared instance for the whole app
