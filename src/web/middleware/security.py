from src.local import app_globals
from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security-related HTTP headers to every response."""
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        asset_host = f"{app_globals.ASSETS_SUBDOMAIN_NAME}.{app_globals.APP_PUBLIC_HOSTNAME}"
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
