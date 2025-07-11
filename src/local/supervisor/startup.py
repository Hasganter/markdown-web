import time
import socket
import logging
from multiprocessing import Lock
from typing import TYPE_CHECKING
from src.local import app_globals
from src.log.setup import setup_logging
from src.local.supervisor import background_tasks, config_utils, persistence, process_utils

if TYPE_CHECKING:
    from .supervisor import ProcessManager

log = logging.getLogger(__name__)


def check_if_already_running(manager: "ProcessManager") -> bool:
    """
    Checks if the application is already running based on the PID file.
    
    :param manager: The ProcessManager instance.
    :return: True if already running, False otherwise.
    """
    pid_info = persistence.get_pid_info(manager)
    if pid_info and any(process_utils.pid_exists(p) for p in pid_info.values()):
        log.error("Application appears to be running. Use 'stop' or 'restart'.")
        return True
    return False


def setup_initial_environment(manager: "ProcessManager") -> None:
    """
    Sets up the initial environment, including dependencies and config files.
    
    :param manager: The ProcessManager instance.
    """
    # Dependencies check and installation
    is_first_run = not any(p.is_dir() for p in app_globals.EXTERNAL_DIR.iterdir() if not p.name.startswith('.'))
    if is_first_run:
        log.warning("External dependency directory is empty. Running initial installation...")
        if not manager.dependency_manager.ensure_all_dependencies_installed():
            raise RuntimeError("Dependency installation failed. Cannot start application.")
        log.info("Initial dependency installation complete.")

    manager.dependency_manager.apply_pending_installs()
    config_utils.write_config_files()
    manager.log_db_manager.initialize_database()


def perform_initial_content_processing(manager: "ProcessManager") -> None:
    """
    Performs the initial content and asset scan.
    
    :param manager: The ProcessManager instance.
    """
    from src import converter
    log.info("--- Performing initial content and asset scan ---")
    db_lock = Lock()
    # The content converter process needs its own DB manager instance and a lock.
    converter.init_worker(db_lock)
    manager.content_db_manager.initialize_database()
    converter.scan_and_process_all_content()
    converter.scan_and_process_all_assets()
    log.info("--- Initial scan complete. Starting background processes. ---")


def wait_for_asgi_server() -> bool:
    """
    Waits for the ASGI server to become responsive on its port.

    :return: True if the server is up, False if it times out.
    """
    host, port = app_globals.WEB_SERVER_HOST, app_globals.WEB_SERVER_PORT
    timeout = app_globals.ASGI_HEALTH_CHECK_TIMEOUT

    log.info(f"Waiting for ASGI server at {host}:{port}...")
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                log.info("ASGI server is up and listening.")
                return True
        except (socket.timeout, ConnectionRefusedError):
            time.sleep(0.5)
    log.critical(f"ASGI server did not become available after {timeout} seconds.")
    return False


def start_all_processes(manager: "ProcessManager") -> None:
    """
    Starts all application processes in the correct order.
    
    :param manager: The ProcessManager instance.
    """
    process_launch_order = [
        ("loki", app_globals.LOKI_ENABLED),
        ("alloy", app_globals.LOKI_ENABLED),
        ("content_converter", True),
        ("asgi_server", True),
        ("ngrok", app_globals.NGROK_ENABLED),
        ("nginx", True),
        ("supervisor", True),
    ]

    for name, is_enabled in process_launch_order:
        if not is_enabled:
            continue

        process_utils.launch_process(manager, name)

        if name in ("asgi_server", "nginx", "loki"):
            time.sleep(1)

        if name == "asgi_server" and not wait_for_asgi_server():
            raise RuntimeError("ASGI server health check failed.")

        if name == "nginx":
            background_tasks.start_nginx_log_tailing(manager)


def initialize_supervision(manager: "ProcessManager") -> None:
    """
    Initialize logging and state for the supervision loop.
    
    :param manager: The ProcessManager instance.
    """
    setup_logging()
    log.info("Supervisor started. Monitoring application processes.")
    manager.shutdown_signal_received.clear()

    # Initialize internal state from PID file on supervisor startup.
    pid_info = persistence.get_pid_info(manager) or {}
    manager.running_procs = {
        name: process_utils.get_process_from_pid(pid)
        for name, pid in pid_info.items()
        if process_utils.pid_exists(pid)
    }
