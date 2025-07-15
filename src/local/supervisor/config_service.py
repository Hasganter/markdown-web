import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.local.supervisor.supervisor import ProcessManager

log = logging.getLogger(__name__)

class ConfigServiceHandler(BaseHTTPRequestHandler):
    """
    A request handler for the internal configuration API.
    This runs in a thread within the Supervisor process.
    """
    # Make manager a class attribute to be set before instantiation
    manager: "ProcessManager" = None

    def _send_response(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/config":
            try:
                # The config dict might contain non-serializable types like Path objects.
                # We need to convert them to strings for JSON transport.
                serializable_config = {
                    key: str(value) if hasattr(value, 'resolve') else value
                    for key, value in self.manager.config.items()
                }
                body = json.dumps(serializable_config, indent=4).encode("utf-8")
                self._send_response(200, "application/json", body)
            except Exception as e:
                error_body = json.dumps({"error": "Failed to serialize config", "detail": str(e)}).encode("utf-8")
                self._send_response(500, "application/json", error_body)
        else:
            self._send_response(404, "text/plain", b"Not Found")

    def do_POST(self):
        if self.path == "/config":
            try:
                content_length = int(self.headers["Content-Length"])
                post_data = self.rfile.read(content_length)
                payload = json.loads(post_data)

                key = payload.get("key")
                value = payload.get("value")

                if not (key and value is not None):
                    body = json.dumps({"error": "Bad Request", "detail": "'key' and 'value' are required."}).encode("utf-8")
                    self._send_response(400, "application/json", body)
                    return

                # Delegate the update logic to the ProcessManager
                success, message = self.manager.update_setting(key, value)
                if success:
                    self._send_response(200, "application/json", json.dumps({"status": "success", "message": message}).encode("utf-8"))
                else:
                    self._send_response(400, "application/json", json.dumps({"error": "Update Failed", "detail": message}).encode("utf-8"))

            except json.JSONDecodeError:
                self._send_response(400, "application/json", b'{"error": "Bad Request", "detail": "Invalid JSON"}')
            except Exception as e:
                log.error(f"Error handling POST request in ConfigService: {e}", exc_info=True)
                body = json.dumps({"error": "Internal Server Error", "detail": str(e)}).encode("utf-8")
                self._send_response(500, "application/json", body)
        else:
            self._send_response(404, "text/plain", b"Not Found")

    def log_message(self, format_str: str, *args: any) -> None:
        """Override to direct HTTP server logs to our application's logger."""
        log.debug("ConfigService: " + (format_str % args))

def run_config_service(manager: "ProcessManager"):
    """
    Sets up and runs the configuration service in a dedicated thread.
    """
    host = manager.config.get("CONFIG_API_HOST")
    port = manager.config.get("CONFIG_API_PORT")

    # Pass the manager instance to the handler class
    ConfigServiceHandler.manager = manager
    
    server = HTTPServer((host, port), ConfigServiceHandler)
    log.info(f"Internal Config API service starting on http://{host}:{port}")

    # The server runs until the shutdown event is set
    while not manager.shutdown_signal_received.is_set():
        server.handle_request()

    log.info("Internal Config API service shutting down.")
    server.server_close()
