import json
import psutil
import logging
from src.local import app_globals
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from .supervisor import ProcessManager

log = logging.getLogger(__name__)


def get_pid_info(manager: "ProcessManager") -> Optional[Dict[str, int]]:
    """
    Reads the PID file from disk and returns its contents.
    
    :param manager: The ProcessManager instance.
    :return: A dictionary of PIDs if the file exists and is valid, else None.
    """
    if not app_globals.PID_FILE_PATH.exists():
        return None
    try:
        with app_globals.PID_FILE_PATH.open("r") as f:
            pids = json.load(f)
        if not isinstance(pids, dict):
            app_globals.PID_FILE_PATH.unlink()
            return None
        manager.pids_on_disk = pids
        return pids
    except (json.JSONDecodeError, IOError):
        app_globals.PID_FILE_PATH.unlink(missing_ok=True)
        return None

def write_pid_file(manager: "ProcessManager") -> None:
    """
    Atomically writes the current running process PIDs to the PID file.
    
    :param manager: The ProcessManager instance.
    """
    pid_dict = {name: proc.pid for name, proc in manager.running_procs.items() if psutil.pid_exists(proc.pid)}
    temp_pid_path = app_globals.PID_FILE_PATH.with_suffix(".tmp")
    try:
        with temp_pid_path.open("w") as f:
            json.dump(pid_dict, f, indent=4)
        temp_pid_path.replace(app_globals.PID_FILE_PATH)
    except (IOError, OSError) as e:
        log.error(f"Failed to write PID file: {e}", exc_info=True)
    finally:
        temp_pid_path.unlink(missing_ok=True)

def check_for_shutdown_signal() -> bool:
    """Checks if the shutdown signal file exists."""
    if app_globals.SHUTDOWN_SIGNAL_PATH.exists():
        log.info("Shutdown signal file detected. Exiting supervisor loop.")
        return True
    return False
