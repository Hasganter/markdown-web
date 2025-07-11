import sys
import psutil
import logging
import threading
import subprocess
from pathlib import Path
from src.local import app_globals
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .supervisor import ProcessManager

log = logging.getLogger(__name__)


#* --- Process Status & Monitoring ---
def pid_exists(pid: int) -> bool:
    """A wrapper for psutil.pid_exists for easy testing/mocking if needed."""
    return psutil.pid_exists(pid)

def get_process_from_pid(pid: int) -> psutil.Process:
    """A wrapper for psutil.Process for easy testing/mocking if needed."""
    return psutil.Process(pid)

def _get_proc_status_string(proc: psutil.Process) -> str:
    """Gets a string representation of a process status."""
    try:
        if proc.status() == psutil.STATUS_ZOMBIE:
            return "zombie"
        return "running"
    except psutil.NoSuchProcess:
        return "stopped"
    except psutil.Error:
        return "unknown"

def _handle_failed_process(manager: "ProcessManager", name: str, proc: psutil.Process) -> bool:
    """
    Handles a failed process, logs its status, and decides if a shutdown is needed.
    Returns True if a critical failure requires application shutdown.
    """
    status = _get_proc_status_string(proc)
    log.warning(f"Detected {status} process: {name} (PID: {proc.pid})")
    manager.running_procs.pop(name, None)

    if name in app_globals.CRITICAL_PROCESSES:
        if not manager._attempt_restart(name):
            # Restart failed and it's a critical process. Check if we've hit the limit.
            if manager.restart_failures.get(name, 0) >= app_globals.MAX_RESTART_ATTEMPTS:
                log.critical(
                    f"PANIC: Unrecoverable failure for critical process '{name}'. "
                    "Initiating full application shutdown."
                )
                manager.stop_all()
                return True
    return False

def monitor_processes(manager: "ProcessManager") -> bool:
    """
    Monitors all running processes and handles failures.
    Returns True if a critical shutdown is initiated.
    """
    # Create a copy to allow modification during iteration
    for name, proc in manager.running_procs.items():
        try:
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                if _handle_failed_process(manager, name, proc):
                    return True # Critical failure, stop monitoring.
        except psutil.NoSuchProcess:
            if _handle_failed_process(manager, name, proc):
                return True # Critical failure
    return False

#* --- Process Creation ---
def get_executable_path(base_path: Path) -> Path:
    """Returns the platform-specific full path for an executable."""
    return base_path.with_suffix(".exe") if sys.platform == "win32" else base_path

def _get_popen_creation_flags() -> Dict[str, Any]:
    """Returns platform-specific creation flags for subprocess.Popen."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW}
    return {}

def get_process_args(process_name: str) -> Tuple[List[str], Path]:
    """Returns the command-line arguments and CWD for a specific process."""
    bin_dir = app_globals.BIN_DIR
    base_dir = app_globals.BASE_DIR

    process_definitions = {
        "loki": (
            [
                str(get_executable_path(app_globals.LOKI_PATH)),
                f"-app_globals.file={str(app_globals.LOKI_CONFIG_PATH.resolve())}"
            ],
            bin_dir
        ),
        "alloy": (
            [
                str(get_executable_path(app_globals.ALLOY_PATH)),
                "run", str(app_globals.ALLOY_CONFIG_PATH.resolve())
            ],
            bin_dir
        ),
        "content_converter": (
            [app_globals.PYTHON_EXECUTABLE, "-m", "src.local.script_entry.converter"],
            base_dir
        ),
        
        # Note: The colon (:) in "src.web.setup:app" specifies the ASGI app object for Hypercorn.
        # This is intentional and required by Hypercorn's CLI syntax.
        "asgi_server": (
            [
                app_globals.PYTHON_EXECUTABLE, "-m",
                "hypercorn",
                "-c", str(app_globals.HYPERCORN_CONFIG_PATH.resolve()),
                "src.web.setup:app"
            ],
            base_dir
        ),
        "nginx": (
            [
                str(get_executable_path(app_globals.NGINX_EXECUTABLE_PATH)),
                "-p", str(bin_dir.resolve())
            ],
            bin_dir
        ),
        "supervisor": (
            [app_globals.PYTHON_EXECUTABLE, "-m", "src.local.script_entry.supervisor"],
            base_dir
        ),
        "ngrok": (
            [
                app_globals.PYTHON_EXECUTABLE, "-m", "ngrok",
                "http", str(app_globals.NGINX_PORT),
                "--log", "stdout",
                "--authtoken", app_globals.NGROK_AUTHTOKEN
            ],
            base_dir
        ),
    }

    if process_name in process_definitions:
        return process_definitions[process_name]
    raise ValueError(f"Unknown process name '{process_name}'. No arguments defined.")

def _read_pipe(pipe, process_name: str, level: int, line_handler: Optional[Callable] = None):
    """Target function for reader threads. Reads and logs lines from a subprocess pipe."""
    proc_logger = logging.getLogger(f"proc.{process_name}")
    try:
        for line_bytes in iter(pipe.readline, b""):
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line_handler:
                line_handler(line)
            else:
                proc_logger.log(level, line)
    except Exception as e:
        proc_logger.debug(f"Pipe reader for {process_name} stream exited: {e}")
    finally:
        pipe.close()

def log_process_output(process: subprocess.Popen, name: str, line_handler: Optional[Callable] = None):
    """Starts background threads to consume and log a process's stdout/stderr."""
    if process.stdout:
        threading.Thread(target=_read_pipe, args=(process.stdout, name, logging.INFO, line_handler), daemon=True).start()
    if process.stderr:
        threading.Thread(target=_read_pipe, args=(process.stderr, name, logging.ERROR), daemon=True).start()

def launch_process(manager: "ProcessManager", name: str) -> None:
    """Launches a single process and adds it to the manager's tracking dictionary."""
    log.info(f"Starting process: {name}...")
    try:
        args, cwd = get_process_args(name)
        popen_kwargs = _get_popen_creation_flags()
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True

        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, cwd=str(cwd.resolve()), **popen_kwargs)
        
        log_process_output(p, name)
        manager.running_procs[name] = psutil.Process(p.pid)
        log.info(f"{name.capitalize()} started successfully with PID: {p.pid}")
    except Exception as e:
        log.critical(f"Failed to start process '{name}': {e}", exc_info=True)
        raise
