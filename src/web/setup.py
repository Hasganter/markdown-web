import setproctitle
setproctitle.setproctitle("MDWeb - ASGI Server")

import asyncio
import sqlite3
import logging
from src.local import app_globals
from src.log import setup_logging
from starlette.routing import Route
from starlette.requests import Request
from starlette.websockets import WebSocket
from starlette.middleware import Middleware
from starlette.applications import Starlette
from typing import Any, Dict, Optional, Tuple
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, HTMLResponse, Response
from src.web.middleware import SecurityHeadersMiddleware, DDoSMiddleware

log = logging.getLogger("asgi_server")
setup_logging(console_level=logging.INFO)


# --- Database and Helper Functions ---
async def get_page_data_from_db(path_key: str) -> Optional[Dict[str, Any]]:
    """Asynchronously fetches page data from the database."""
    def db_read_sync() -> Optional[Tuple[str, str, str]]:
        try:
            db_uri = f"{app_globals.CONTENT_DB_PATH.as_uri()}?mode=ro"
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
            log.error(f"DB Read failed for key '{path_key}': {e}", exc_info=True)
            return None

    result_tuple = await asyncio.get_running_loop().run_in_executor(None, db_read_sync)
    if result_tuple:
        html, title, methods_str = result_tuple
        methods = [m.strip() for m in methods_str.split(',')] if methods_str else ["GET"]
        return {"html_content": html, "title": title, "allowed_methods": methods}
    return None


def get_subdomain_and_path(request: Request) -> Tuple[str, str]:
    """Parses the request to determine the subdomain and web path."""
    host = request.headers.get("host", app_globals.APP_PUBLIC_HOSTNAME).lower()
    host = host.removesuffix(app_globals.APP_PUBLIC_HOSTNAME.lower()).removesuffix(app_globals.APP_PUBLIC_HOSTNAME.lower().removesuffix(f":{app_globals.NGINX_PORT}"))
    subdomain = "main"

    if host.endswith("."):
        subdomain = host.removesuffix(".")

    web_path = request.url.path
    log.debug(f"returning subdomain: '{subdomain}', web path: '{web_path}'")
    return subdomain, web_path.rstrip("/") if len(web_path) > 1 else "/"


# Keep async because of concurrency
async def handle_asset_request(request: Request) -> FileResponse:
    """Handles proxied requests for original, unconverted assets."""
    if request.query_params.get("ori", "false").lower() != "true":
        log.warning(
            f"Asset request for '{request.url.path}' without 'ori=true' was proxied to the app. "
            "This indicates a potential Nginx misconfiguration."
        )
        raise HTTPException(status_code=404, detail="Asset not found")

    assets_dir = (app_globals.ROOT_INDEX_DIR / ".assets").resolve()
    # Security: Prevent directory traversal.
    requested_path = assets_dir.joinpath(request.url.path.lstrip('/')).resolve()
    if not str(requested_path).startswith(str(assets_dir)):
        log.warning(f"Directory traversal attempt blocked for asset: {request.url.path}")
        raise HTTPException(status_code=404)

    if requested_path.is_file():
        log.debug(f"Serving original asset '{requested_path.name}' due to 'ori=true'.")
        return FileResponse(requested_path)

    raise HTTPException(status_code=404, detail="Asset file does not exist")


async def handle_websocket_connection(websocket: WebSocket) -> None:
    """Manages an individual WebSocket connection."""
    await websocket.accept()
    log.info(f"WebSocket connection established from {websocket.client.host}")
    try:
        while True:
            data = await websocket.receive_text()
            if data == 'close':
                break
            await websocket.send_text(f"Echo from server: {data}")
    except Exception as e:
        log.warning(f"WebSocket Error: {e}")
    finally:
        await websocket.close()
        log.info(f"WebSocket connection from {websocket.client.host} closed.")


# --- Main Request Handler ---
async def main_handler(request: Request) -> Response:
    """The main request handler for all incoming HTTP requests."""
    log.debug(f"Received request: {request.method} {request.url.path} from {request.client.host}")
    domain_part, web_path = get_subdomain_and_path(request)

    if domain_part == app_globals.ASSETS_SUBDOMAIN_NAME:
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
    log.debug(f"Request: Host='{request.headers['host']}', Path='{request.url.path}' -> DBKey='{path_key}'")

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
        log.info(f"Received POST data for {path_key}: {dict(form_data)}")
        return HTMLResponse(f"<h1>Successfully handled POST for {path_key}.</h1>")

    return Response(status_code=204) # No Content for other methods like HEAD


# --- Application Instance Creation ---
routes = [
    Route("/{path:path}", endpoint=main_handler, methods=["GET", "POST", "HEAD"]),
]

middleware = [
    Middleware(SecurityHeadersMiddleware),
]
if app_globals.DDOS_PROTECTION_ENABLED:
    log.warning("Python-level DDoS protection (fallback) is ENABLED.")
    middleware.append(Middleware(DDoSMiddleware))
else:
    log.info("Python-level DDoS protection is DISABLED (recommended when using Nginx).")

# The main application object to be loaded by Hypercorn
app = Starlette(debug=False, routes=routes, middleware=middleware)

log.info("Starlette ASGI web server worker configured and ready.")
