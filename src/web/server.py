import asyncio
import logging
import sqlite3
import time
import setproctitle
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

# Starlette imports for a native ASGI application
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, PlainTextResponse, Response)
from starlette.routing import Route
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.websockets import WebSocket

from src.local.config import effective_settings as config
from src.log.setup import setup_logging

# --- Setup Logging for this Module ---
setup_logging(console_level=logging.INFO)
logger = logging.getLogger("asgi_server")

# --- Middleware rewritten for Starlette ---

class DDoSMiddleware(BaseHTTPMiddleware):
    """A Starlette-compatible middleware for basic rate-limiting."""

    def __init__(self, app):
        super().__init__(app)
        self._lock = asyncio.Lock()
        self._ip_requests: Dict[str, Deque[float]] = {}
        self._blocked_ips: Dict[str, float] = {}
        # Start a background task for periodic cleanup
        asyncio.create_task(self._periodic_cleanup())

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
                expiry_threshold = current_time - (config.REQUESTS_WINDOW_SECONDS * 5)
                self._ip_requests = {
                    ip: timestamps for ip, timestamps in self._ip_requests.items()
                    if timestamps and timestamps[-1] > expiry_threshold
                }

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not config.DDOS_PROTECTION_ENABLED:
            return await call_next(request)

        # Use X-Forwarded-For if available, otherwise use client host.
        ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else None)
        if not ip:
            return await call_next(request)

        current_time = time.monotonic()

        async with self._lock:
            if ip in self._blocked_ips and self._blocked_ips[ip] > current_time:
                logger.warning(f"Blocking request from already-blocked IP: {ip}")
                return PlainTextResponse("Too Many Requests", status_code=429)

            timestamps = self._ip_requests.setdefault(ip, deque())
            while timestamps and timestamps[0] < current_time - config.REQUESTS_WINDOW_SECONDS:
                timestamps.popleft()

            timestamps.append(current_time)

            if len(timestamps) > config.REQUESTS_LIMIT_PER_WINDOW:
                self._blocked_ips[ip] = current_time + config.BLOCK_DURATION_SECONDS
                logger.critical(
                    f"DDoS PROTECT: Rate limit exceeded for IP {ip}. "
                    f"Blocking for {config.BLOCK_DURATION_SECONDS}s."
                )
                self._ip_requests.pop(ip, None)
                return PlainTextResponse("Too Many Requests", status_code=429)

        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security-related HTTP headers to every response."""
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        asset_host = f"{config.ASSETS_SUBDOMAIN_NAME}.{config.APP_DOMAIN}"
        csp = (
            "default-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' http://{asset_host} https://{asset_host}; "
            f"img-src 'self' data: http://{asset_host} https://{asset_host}; "
            f"media-src 'self' http://{asset_host} https://{asset_host};"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


# --- Database and Helper Functions ---
async def get_page_data_from_db(path_key: str) -> Optional[Dict[str, Any]]:
    """Asynchronously fetches page data from the database."""
    def db_read_sync() -> Optional[Tuple[str, str, str]]:
        try:
            db_uri = f"{config.CONTENT_DB_PATH.as_uri()}?mode=ro"
            with sqlite3.connect(db_uri, uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT html_content, title, allowed_methods FROM pages WHERE path_key = ?",
                    (path_key,)
                )
                row = cursor.fetchone()
                return (row["html_content"], row["title"], row["allowed_methods"]) if row else None
        except sqlite3.OperationalError as e:
            logger.error(f"DB Read failed for key '{path_key}': {e}", exc_info=True)
            return None

    result_tuple = await asyncio.get_running_loop().run_in_executor(None, db_read_sync)
    if result_tuple:
        html, title, methods_str = result_tuple
        methods = [m.strip() for m in methods_str.split(',')] if methods_str else ["GET"]
        return {"html_content": html, "title": title, "allowed_methods": methods}
    return None


def get_subdomain_and_path(request: Request) -> Tuple[str, str]:
    """Parses the request to determine the subdomain and web path."""
    host = request.headers.get("host", config.APP_DOMAIN).lower()
    host = host.removesuffix(config.APP_DOMAIN.lower()).removesuffix(config.APP_DOMAIN.lower().removesuffix(f":{config.NGINX_PORT}"))
    subdomain = "main"

    logger.debug(f"Host after removing app domain: {host}")
    if host.endswith("."):
        subdomain = host.removesuffix(".")

    web_path = request.url.path
    logger.debug(f"returning subdomain: '{subdomain}', web path: '{web_path}'")
    return subdomain, web_path.rstrip("/") if len(web_path) > 1 else "/"


async def handle_asset_request(request: Request) -> FileResponse:
    """Handles proxied requests for original, unconverted assets."""
    if request.query_params.get("ori", "false").lower() != "true":
        logger.warning(
            f"Asset request for '{request.url.path}' without 'ori=true' was proxied to the app. "
            "This indicates a potential Nginx misconfiguration."
        )
        raise HTTPException(status_code=404, detail="Asset not found")

    assets_dir = (config.ROOT_INDEX_DIR / ".assets").resolve()
    # Security: Prevent directory traversal.
    requested_path = assets_dir.joinpath(request.url.path.lstrip('/')).resolve()
    if not str(requested_path).startswith(str(assets_dir)):
        logger.warning(f"Directory traversal attempt blocked for asset: {request.url.path}")
        raise HTTPException(status_code=404)

    if requested_path.is_file():
        logger.debug(f"Serving original asset '{requested_path.name}' due to 'ori=true'.")
        return FileResponse(requested_path)

    raise HTTPException(status_code=404, detail="Asset file does not exist")


async def handle_websocket_connection(websocket: WebSocket) -> None:
    """Manages an individual WebSocket connection."""
    await websocket.accept()
    logger.info(f"WebSocket connection established from {websocket.client.host}")
    try:
        while True:
            data = await websocket.receive_text()
            if data == 'close':
                break
            await websocket.send_text(f"Echo from server: {data}")
    except Exception as e:
        logger.warning(f"WebSocket Error: {e}")
    finally:
        await websocket.close()
        logger.info(f"WebSocket connection from {websocket.client.host} closed.")


# --- Main Request Handler ---
async def main_handler(request: Request) -> Response:
    """The main request handler for all incoming HTTP requests."""
    logger.debug(f"Received request: {request.method} {request.url.path} from {request.client.host}")
    domain_part, web_path = get_subdomain_and_path(request)

    if domain_part == config.ASSETS_SUBDOMAIN_NAME:
        return await handle_asset_request(request)

    # Handle WebSocket upgrade requests specifically
    #TODO: Websockets soon
    if "websocket" in request.headers.get("upgrade", "").lower():
        websocket: WebSocket = await request.websocket()
        await handle_websocket_connection(websocket)
        # This part of the function will only be reached after the websocket closes.
        # Starlette requires a Response, but it won't be sent.
        return Response(status_code=101) # Switching Protocols

    path_key = f"{domain_part}:{web_path}"
    logger.debug(f"Request: Host='{request.headers['host']}', Path='{request.url.path}' -> DBKey='{path_key}'")

    page_data = await get_page_data_from_db(path_key)
    if not page_data:
        return HTMLResponse("<h1>404 - Page Not Found</h1>", status_code=404)

    allowed_methods = page_data.get("allowed_methods", ["GET"])
    if request.method not in allowed_methods:
        raise HTTPException(status_code=405, headers={"Allow": ", ".join(allowed_methods)})

    if request.method == "GET":
        return HTMLResponse(content=page_data["html_content"])

    if request.method == "POST":
        form_data = await request.form()
        logger.info(f"Received POST data for {path_key}: {dict(form_data)}")
        return HTMLResponse(f"<h1>Successfully handled POST for {path_key}.</h1>")

    return Response(status_code=204) # No Content for other methods like HEAD


# --- Application Instance Creation ---
routes = [
    Route("/{path:path}", endpoint=main_handler, methods=["GET", "POST", "HEAD"]),
]

middleware = [
    Middleware(SecurityHeadersMiddleware),
]
if config.DDOS_PROTECTION_ENABLED:
    logger.warning("Python-level DDoS protection (fallback) is ENABLED.")
    middleware.append(Middleware(DDoSMiddleware))
else:
    logger.info("Python-level DDoS protection is DISABLED (recommended when using Nginx).")

# The main application object to be loaded by Hypercorn
app = Starlette(debug=False, routes=routes, middleware=middleware)

# Set the process title when the module is loaded by Hypercorn
setproctitle.setproctitle("MDWeb - ASGI Server")

logger.info("Starlette ASGI web server worker configured and ready.")
