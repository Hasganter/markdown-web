import shutil
import logging
from src.local import app_globals
from .process_utils import get_executable_path

log = logging.getLogger(__name__)


def check_configuration() -> bool:
    """
    Validates that essential external binaries exist at their configured paths.

    :return: True if all required executables are found, otherwise False.
    """
    log.info("Performing configuration and path validation...")
    all_ok = True
    checks = {
        "FFmpeg": app_globals.FFMPEG_PATH,
        "Nginx": app_globals.NGINX_EXECUTABLE_PATH,
    }
    if app_globals.LOKI_ENABLED:
        checks.update({
            "Loki": app_globals.LOKI_PATH,
            "Alloy": app_globals.ALLOY_PATH
        })

    for name, path_base in checks.items():
        path_exe = get_executable_path(path_base)
        if not path_exe.exists():
            log.error(f"CONFIG CHECK FAILED: {name} not found at '{path_exe}'")
            all_ok = False
        else:
            log.info(f"Config Check OK: Found {name} at '{path_exe}'")
    return all_ok


def write_config_files() -> None:
    """
    Generates and writes all necessary runtime configuration files.
    """
    try:
        #* --- Hypercorn Config ---
        if app_globals.HYPERCORN_MODE == 'workers':
            workers, threads = app_globals.ASGI_WORKERS, 1
        else:
            workers, threads = 1, app_globals.ASGI_WORKERS

        hypercorn_conf = app_globals.HYPERCORN_CONFIG_TEMPLATE.format(
            bind_host=app_globals.WEB_SERVER_HOST,
            bind_port=app_globals.WEB_SERVER_PORT,
            pid_path=str(app_globals.BIN_DIR / "hypercorn.pid").replace("\\", "/"),
            mode=app_globals.HYPERCORN_MODE, workers=workers, threads=threads,
        )
        app_globals.HYPERCORN_CONFIG_PATH.write_text(hypercorn_conf)

        #* --- Nginx Config ---
        nginx_bin_conf = app_globals.BIN_DIR / "conf"
        if nginx_bin_conf.exists():
            shutil.rmtree(nginx_bin_conf)
        shutil.copytree(app_globals.NGINX_SOURCE_PATH / "conf", nginx_bin_conf)

        nginx_conf_target = nginx_bin_conf / "nginx.conf"
        nginx_conf = app_globals.NGINX_CONFIG_TEMPLATE.format(
            listen_port=app_globals.NGINX_PORT,
            assets_server_name=f"{app_globals.ASSETS_SUBDOMAIN_NAME}.{app_globals.APP_DOMAIN}",
            server_name=app_globals.APP_DOMAIN,
            assets_output_dir=str(app_globals.ASSETS_OUTPUT_DIR.resolve()).replace("\\", "/"),
            asgi_host=app_globals.WEB_SERVER_HOST,
            asgi_port=app_globals.WEB_SERVER_PORT,
            zone_size=app_globals.NGINX_RATELIMIT_ZONE_SIZE,
            rate=app_globals.NGINX_RATELIMIT_RATE,
            burst=app_globals.NGINX_RATELIMIT_BURST
        )
        nginx_conf_target.write_text(nginx_conf)

        (app_globals.BIN_DIR / "logs").mkdir(exist_ok=True)
        (app_globals.BIN_DIR / "temp").mkdir(exist_ok=True)
        log.info("Nginx and Hypercorn configs prepared in 'bin/'.")

        #* --- Loki/Alloy Configs ---
        if app_globals.LOKI_ENABLED:
            loki_data = (app_globals.BIN_DIR / "loki-data").resolve()
            loki_data.mkdir(exist_ok=True)
            loki_port = int(app_globals.LOKI_URL.split(":")[-1])
            loki_conf = app_globals.LOKI_CONFIG_TEMPLATE.format(loki_port=loki_port, loki_data_path=str(loki_data).replace("\\", "/"))
            app_globals.LOKI_CONFIG_PATH.write_text(loki_conf)

            headers = f'headers = {{ "X-Scope-OrgID" = "{app_globals.LOKI_ORG_ID}" }}' if app_globals.LOKI_ORG_ID else ""
            alloy_conf = app_globals.ALLOY_CONFIG_TEMPLATE.format(loki_push_url=app_globals.LOKI_URL, loki_headers=headers, nginx_log_path=str((app_globals.BIN_DIR / "logs" / "access.log").resolve()).replace("\\", "/"))
            app_globals.ALLOY_CONFIG_PATH.write_text(alloy_conf)
            log.info("Loki and Alloy configs written.")
    except Exception as e:
        log.critical(f"Failed to write one or more configuration files: {e}", exc_info=True)
        raise
