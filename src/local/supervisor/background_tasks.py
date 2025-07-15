import os
import time
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from src.local import app_globals

if TYPE_CHECKING:
    from .supervisor import ProcessManager

log = logging.getLogger(__name__)


def _tail_nginx_log_file(manager: "ProcessManager", log_path: Path):
    """
    Tails the Nginx access log file and sends new lines to the DB manager.
    Runs in a dedicated background thread.
    
    :param manager: The ProcessManager instance.
    :param log_path: The path to the Nginx access log.
    """
    log.info(f"Starting to tail Nginx access log at: {log_path}")

    for _ in range(5):  # Wait up to 5 seconds for Nginx to create the log file
        if log_path.exists():
            break
        time.sleep(1)

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            f.seek(0, os.SEEK_END)
            while not manager.shutdown_signal_received.is_set():
                line = f.readline()
                if not line:
                    time.sleep(0.2)
                    continue
                manager.log_db_manager.insert_nginx_log(line.strip())
    except FileNotFoundError:
        if not manager.shutdown_signal_received.is_set():
            log.error(f"Nginx log file not found at {log_path}. Tailing failed.")
    except Exception as e:
        if not manager.shutdown_signal_received.is_set():
            log.error(f"Error while tailing Nginx log file: {e}", exc_info=True)
    
    log.info("Nginx log tailing thread has stopped.")


def start_nginx_log_tailing(manager: "ProcessManager") -> None:
    """
    Starts a thread to tail the Nginx access log file.
    
    :param manager: The ProcessManager instance.
    """
    nginx_log_path = app_globals.BIN_DIR / "logs" / "access.log"
    manager.shutdown_signal_received.clear()
    tail_thread = threading.Thread(
        target=_tail_nginx_log_file,
        args=(manager, nginx_log_path,),
        daemon=True,
        name="NginxLogTailerThread"
    )
    tail_thread.start()


def start_update_checker(manager: "ProcessManager") -> None:
    """
    Starts a thread to check for dependency updates.
    
    :param manager: The ProcessManager instance.
    """
    app_globals.start_time = time.time() # Record the start time for status checks
    update_thread = threading.Thread(
        target=manager.dependency_manager.check_for_updates_async,
        daemon=True,
        name="DepUpdateCheckThread"
    )
    update_thread.start()
