import multiprocessing
import os
import pathlib

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)

# --- Core Paths ---
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent  # Project Root
BIN_DIR = BASE_DIR / "bin"
SRC_DIR = BASE_DIR / "src"
EXTERNAL_DIR = BASE_DIR / "external"
LOGS_DIR = BASE_DIR / "logs"

# --- Domain & App Settings ---
APP_DOMAIN = os.getenv("MYAPP_DOMAIN", "localhost:8080")
ASSETS_SUBDOMAIN_NAME = "assets"

# --- Application File Paths ---
ROOT_INDEX_DIR = BASE_DIR / "_ROOT-INDEX_"
TEMPLATES_DIR = SRC_DIR / "templates"
CONTENT_DB_PATH = BIN_DIR / "content.db"
ASSETS_OUTPUT_DIR = BIN_DIR / "assets"
LOG_DB_PATH = LOGS_DIR / "app_logs.db"
PID_FILE_PATH = BIN_DIR / "app.pid"
OVERRIDES_JSON_PATH = BIN_DIR / "overrides.json"
SHUTDOWN_SIGNAL_PATH = BIN_DIR / "shutdown.signal"

# --- External Executable Paths ---
FFMPEG_PATH = EXTERNAL_DIR / "ffmpeg" / "bin" / "ffmpeg"
NGINX_EXECUTABLE_PATH = EXTERNAL_DIR / "nginx" / "nginx"
LOKI_PATH = EXTERNAL_DIR / "grafana" / "loki-windows-amd64"
ALLOY_PATH = EXTERNAL_DIR / "grafana" / "alloy-windows-amd64"

# --- Web Server Settings ---
# ASGI Server (Hypercorn) - internal application server
WEB_SERVER_HOST = "127.0.0.1"
WEB_SERVER_PORT = int(os.getenv("ASGI_PORT", "8000"))
# Concurrency mode: 'workers' for multiprocessing (production), 'threads' for multithreading (development)
HYPERCORN_MODE = os.getenv("HYPERCORN_MODE", "workers").lower()
# Auto-calculate workers if set to 0, otherwise use the specified value
ASGI_WORKERS = int(os.getenv("ASGI_WORKERS", "0")) or (multiprocessing.cpu_count() * 2) + 1
HYPERCORN_CONFIG_PATH = BIN_DIR / "hypercorn_config.py"

# Nginx (Reverse Proxy) - public-facing server
NGINX_HOST = os.getenv("NGINX_HOST", "0.0.0.0")
NGINX_PORT = int(os.getenv("NGINX_PORT", "8080"))
NGINX_SOURCE_PATH = EXTERNAL_DIR / "nginx"
NGINX_RATELIMIT_ZONE_SIZE = "10m" # Shared memory zone size for rate limiting
NGINX_RATELIMIT_RATE = "5r/s"    # Rate limit (e.g., 5 requests per second)
NGINX_RATELIMIT_BURST = "20"     # How many requests to allow in a burst

# --- Optional Services ---
# Ngrok (for development)
NGROK_ENABLED = os.getenv("NGROK_ENABLED", "False").lower() in ('true', '1', 't')
NGROK_AUTHTOKEN = os.getenv("NGROK_AUTHTOKEN", "")

# Grafana Loki (for observability)
LOKI_ENABLED = os.getenv("LOKI_ENABLED", "False").lower() in ('true', '1', 't')
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
LOKI_ORG_ID = os.getenv("LOKI_ORG_ID", "fake")
ALLOY_CONFIG_PATH = BIN_DIR / "alloy.river"
LOKI_CONFIG_PATH = BIN_DIR / "loki-config.yaml"

# --- MODIFIABLE SETTINGS (Changeable at runtime via 'config' command) ---
MODIFIABLE_SETTINGS = {
    # Markdown Converter
    "MARKDOWN_SCAN_INTERVAL_SECONDS",
    # Logging
    "LOG_BUFFER_SIZE", "LOG_BUFFER_FLUSH_INTERVAL",
    "MAX_LOG_DB_SIZE_MB", "LOG_DB_SIZE_CHECK_INTERVAL_SECONDS", "LOG_HISTORY_COUNT",
    # DDoS Protection (Python fallback)
    "DDOS_PROTECTION_ENABLED", "REQUESTS_LIMIT_PER_WINDOW",
    "REQUESTS_WINDOW_SECONDS", "BLOCK_DURATION_SECONDS"
}

# --- Default Values for Modifiable Settings ---
MARKDOWN_SCAN_INTERVAL_SECONDS = 12 * 3600  # 12 hours
LOG_BUFFER_SIZE = 100
LOG_BUFFER_FLUSH_INTERVAL = 10
MAX_LOG_DB_SIZE_MB = 100
LOG_DB_SIZE_CHECK_INTERVAL_SECONDS = 12 * 3600 # 12 hours
LOG_HISTORY_COUNT = 50
DDOS_PROTECTION_ENABLED = os.getenv("DDOS_PROTECTION_ENABLED", "True").lower() in ('true', '1', 't')
REQUESTS_LIMIT_PER_WINDOW = 20
REQUESTS_WINDOW_SECONDS = 5
BLOCK_DURATION_SECONDS = 300

# --- External Dependency Configuration ---
EXTERNAL_DEPENDENCIES = {
    "nginx": {
        "name": "Nginx",
        "version_url": "https://api.github.com/repos/nginx/nginx/releases/latest",
        "version_regex": r'"tag_name":\s*"release-([\d\.]+)"',
        "url_template": "https://nginx.org/download/nginx-{version}.zip",
        "target_dir_name": "nginx",
        "archive_path_in_zip": "nginx-{version}/"
    },
    "ffmpeg": {
        "name": "FFmpeg",
        "version_url": "https://www.gyan.dev/ffmpeg/builds/release-version",
        "version_regex": r"([\d\.]+)",
        "url_template": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "target_dir_name": "ffmpeg",
        "archive_path_in_zip": "ffmpeg-{version}-essentials_build/"
    },
    "Loki": {
        "name": "Grafana Loki",
        "version_url": "https://api.github.com/repos/grafana/loki/releases/latest",
        "version_regex": r'"tag_name":\s*"v([\d\.]+)"',
        "url_template": "https://github.com/grafana/loki/releases/download/v{version}/loki-windows-amd64.exe.zip",
        "target_dir_name": "grafana",
        "archive_path_in_zip": None
    },
    "Alloy": {
        "name": "Grafana Alloy",
        "version_url": "https://api.github.com/repos/grafana/alloy/releases/latest",
        "version_regex": r'"tag_name":\s*"v([\d\.]+)"',
        "url_template": "https://github.com/grafana/alloy/releases/download/v{version}/alloy-windows-amd64.exe.zip",
        "target_dir_name": "grafana",
        "archive_path_in_zip": None
    }
}

# --- Configuration Templates ---
HYPERCORN_CONFIG_TEMPLATE = """
# This file is auto-generated by ProcessManager. Do not edit directly.

bind = "{bind_host}:{bind_port}"
pid_path = "{pid_path}"

# -- Concurrency --
# Mode is '{mode}'.
workers = {workers}
threads = {threads}

# -- Logging --
# Directs Hypercorn's own logs to stdout/stderr so the ProcessManager can capture them.
accesslog = "-"
errorlog = "-"
loglevel = "info"

# By omitting 'worker_class', we allow Hypercorn to choose the best
# default for the current operating system (e.g., 'uvloop' if available, otherwise 'asyncio').
"""

NGINX_CONFIG_TEMPLATE = """
# This file is auto-generated by ProcessManager. Do not edit directly.
worker_processes auto;
pid logs/nginx.pid;

events {{
    worker_connections 1024;
}}

http {{
    include       mime.types;
    default_type  application/octet-stream;
    sendfile      on;
    tcp_nopush    on;
    keepalive_timeout 65;

    # Shared rate limiting zone for all servers.
    limit_req_zone $binary_remote_addr zone=global_limit:{zone_size} rate={rate};

    # Custom log format for structured (JSON) logging, consumable by Alloy/Loki.
    log_format loki_json escape=json '{{'
        '"time": "$time_iso8601", '
        '"remote_addr": "$remote_addr", '
        '"request_method": "$request_method", '
        '"request_uri": "$request_uri", '
        '"status": $status, '
        '"body_bytes_sent": $body_bytes_sent, '
        '"http_referer": "$http_referer", '
        '"http_user_agent": "$http_user_agent", '
        '"http_x_forwarded_for": "$http_x_forwarded_for"'
    '}}';

    # Send access and error logs to stdout/stderr to be captured by the ProcessManager.
    access_log stdout loki_json;
    error_log stderr error;

    # SERVER 1: Assets Subdomain (assets.domain.com)
    # Serves pre-optimized media directly from bin/assets.
    server {{
        listen {listen_port};
        server_name {assets_server_name};

        # Apply rate limiting. Burst allows short spikes. Nodelay serves burst requests instantly.
        limit_req zone=global_limit burst={burst} nodelay;

        # The root for this server is the output directory for converted assets.
        root "{assets_output_dir}";

        location / {{
            # Tries to serve the requested file with various web-optimized extensions first.
            # $uri is the request path, e.g., /background.jpg
            # Nginx will check for existence in this order:
            # 1. /background.jpg (for non-media assets like CSS that are copied directly)
            # 2. /background.jpg.avif (for converted images)
            # 3. /background.jpg.webm (for converted videos)
            # 4. /background.jpg.mp3 (for converted audio)
            # If none are found, it falls back to the Python app.
            try_files $uri $uri.avif $uri.webm $uri.mp3 @asset_fallback;
        }}

        # This named location is the fallback for assets not found in the root.
        # It proxies the request to the Python app with a special query parameter,
        # asking for the *original* unconverted file.
        location @asset_fallback {{
            proxy_pass http://{asgi_host}:{asgi_port}$uri?ori=true;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }}
    }}

    # SERVER 2: Main Application (domain.com)
    # Proxies all other traffic to the backend ASGI server.
    server {{
        listen {listen_port};
        server_name {server_name};

        limit_req zone=global_limit burst={burst} nodelay;

        location / {{
            proxy_pass http://{asgi_host}:{asgi_port};
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;

            # Required headers for WebSocket support.
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }}
    }}
}}
"""

ALLOY_CONFIG_TEMPLATE = """
loki.write "default" {{
    endpoint {{
        url = "{loki_push_url}/loki/api/v1/push"
        {loki_headers}
    }}
}}

local.file_match "nginx_logs" {{
    path_targets = [{{
        __meta__ = {{ "job" = "nginx" }},
        __path__ = "{nginx_log_path}",
    }}]
}}

loki.source.file "nginx_source" {{
    targets = local.file_match.nginx_logs.targets
    forward_to = [loki.write.default.receiver]
}}
"""

LOKI_CONFIG_TEMPLATE = """
server:
  http_listen_port: {loki_port}
  grpc_listen_port: 0 # Disable gRPC unless needed

auth_enabled: false # Simplifies setup, assumes trusted network environment

common:
  instance_addr: 127.0.0.1
  path_prefix: {loki_data_path}
  storage:
    filesystem:
      chunks_directory: {loki_data_path}/chunks
      rules_directory: {loki_data_path}/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: 2022-01-01
      store: boltdb-shipper
      object_store: filesystem
      schema: v12
      index:
        prefix: index_
        period: 24h

# Limits can be tuned to prevent out-of-memory issues.
limits_config:
  # Do not reject batches of logs from a single IP address.
  reject_old_samples: false
  reject_old_samples_max_age: 168h
  # Maximum length of a log line.
  max_line_size: 1024000 # 1MB
  allow_structured_metadata: false
"""
