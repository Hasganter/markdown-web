import time
import asyncio
import logging
from collections import deque
from typing import Dict, Deque
from src.local import app_globals
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

log = logging.getLogger(__name__)


class DDoSMiddleware(BaseHTTPMiddleware):
    """A Starlette-compatible middleware for basic rate-limiting."""

    def __init__(self, app):
        super().__init__(app)
        self._lock = asyncio.Lock()
        self._ip_requests: Dict[str, Deque[float]] = {}
        self._blocked_ips: Dict[str, float] = {}
        # Start a background task for periodic cleanup
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def _periodic_cleanup(self):
        """Periodically cleans up expired IPs to prevent memory leaks."""
        while True:
            await asyncio.sleep(60)
            current_time = time.monotonic()
            async with self._lock:
                self._blocked_ips = {
                    ip: unblock_ts
                    for ip, unblock_ts in self._blocked_ips.items()
                    if unblock_ts > current_time
                }
                # Also prune the request tracker for IPs not seen in a while
                expiry_threshold = current_time - (app_globals.REQUESTS_WINDOW_SECONDS * 5)
                self._ip_requests = {
                    ip: timestamps for ip, timestamps in self._ip_requests.items()
                    if timestamps and timestamps[-1] > expiry_threshold
                }

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not app_globals.DDOS_PROTECTION_ENABLED:
            return await call_next(request)

        # Use X-Forwarded-For if available, otherwise use client host.
        ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else None)
        if not ip:
            return await call_next(request)

        current_time = time.monotonic()

        async with self._lock:
            if ip in self._blocked_ips and self._blocked_ips[ip] > current_time:
                log.warning(f"Blocking request from already-blocked IP: {ip}")
                return PlainTextResponse("Too Many Requests", status_code=429)

            timestamps = self._ip_requests.setdefault(ip, deque())
            while timestamps and timestamps[0] < current_time - app_globals.REQUESTS_WINDOW_SECONDS:
                timestamps.popleft()

            timestamps.append(current_time)

            if len(timestamps) > app_globals.REQUESTS_LIMIT_PER_WINDOW:
                self._blocked_ips[ip] = current_time + app_globals.BLOCK_DURATION_SECONDS
                log.critical(
                    f"DDoS PROTECT: Rate limit exceeded for IP {ip}. "
                    f"Blocking for {app_globals.BLOCK_DURATION_SECONDS}s."
                )
                self._ip_requests.pop(ip, None)
                return PlainTextResponse("Too Many Requests", status_code=429)

        return await call_next(request)
