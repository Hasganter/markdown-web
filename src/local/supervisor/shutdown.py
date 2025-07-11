import psutil
import logging
import subprocess
from src.local import app_globals
from typing import TYPE_CHECKING, Set
from src.local.supervisor import persistence
from src.local.supervisor.process_utils import get_executable_path

if TYPE_CHECKING:
    from .supervisor import ProcessManager

log = logging.getLogger(__name__)


def identify_processes_to_stop(manager: "ProcessManager", is_cleanup_after_failure: bool) -> Set[psutil.Process]:
    """
    Identifies all parent and child processes that need to be stopped.
    
    :param manager: The ProcessManager instance.
    :param is_cleanup_after_failure: If True, uses internal state instead of PID file.
    :return: A set of psutil.Process objects to be stopped.
    """
    parent_procs: Set[psutil.Process] = set()
    if is_cleanup_after_failure:
        parent_procs = {p for p in manager.running_procs.values() if p.is_running()}
        log.warning("Cleaning up processes after a startup failure.")
    else:
        pid_info = persistence.get_pid_info(manager) or {}
        parent_procs = {psutil.Process(pid) for pid in pid_info.values() if psutil.pid_exists(pid)}

    hypercorn_pid_path = app_globals.BIN_DIR / "hypercorn.pid"
    if hypercorn_pid_path.exists():
        try:
            master_pid = int(hypercorn_pid_path.read_text().strip())
            if psutil.pid_exists(master_pid):
                parent_procs.add(psutil.Process(master_pid))
        except (ValueError, IOError, psutil.Error):
            pass

    all_procs_to_stop: Set[psutil.Process] = set(parent_procs)
    for proc in parent_procs:
        try:
            all_procs_to_stop.update(proc.children(recursive=True))
        except psutil.NoSuchProcess:
            log.warning(f"Process {proc.pid} no longer exists, skipping children retrieval.")
            continue
            
    return all_procs_to_stop


def cleanup_shutdown_files() -> None:
    """Removes PID files and the shutdown signal file."""
    app_globals.PID_FILE_PATH.unlink(missing_ok=True)
    app_globals.SHUTDOWN_SIGNAL_PATH.unlink(missing_ok=True)
    (app_globals.BIN_DIR / "hypercorn.pid").unlink(missing_ok=True)
    log.debug("Cleaned up PID and signal files.")


def _shutdown_nginx_gracefully() -> None:
    """Sends the 'quit' signal to Nginx for graceful shutdown."""
    try:
        nginx_exe = get_executable_path(app_globals.NGINX_EXECUTABLE_PATH)
        cmd = [str(nginx_exe.resolve()), '-s', 'quit', '-p', str(app_globals.BIN_DIR.resolve())]
        subprocess.run(cmd, timeout=10, check=False, capture_output=True)
        log.info("Nginx graceful quit signal sent.")
    except Exception as e:
        log.error(f"Failed to send graceful quit signal to Nginx: {e}")


def _terminate_processes(processes: Set[psutil.Process]) -> None:
    """Sends SIGTERM to all processes except Nginx."""
    for proc in processes:
        try:
            if 'nginx' in proc.name().lower(): continue
            log.debug(f"Sending SIGTERM to {proc.name()} (PID {proc.pid})")
            proc.terminate()
        except psutil.NoSuchProcess:
            log.warning(f"Process {proc.pid} no longer exists, skipping termination.")
            continue


def _forceful_kill(processes: list) -> None:
    """Forcefully kills processes that didn't terminate gracefully."""
    if not processes:
        return
        
    log.warning(f"{len(processes)} processes did not terminate gracefully. Forcing shutdown...")
    for proc in processes:
        try:
            log.warning(f"Killing stubborn process {proc.name()} (PID {proc.pid}).")
            proc.kill()
        except psutil.NoSuchProcess:
            log.warning(f"Process {proc.pid} no longer exists, skipping forceful kill.")
            continue


def graceful_shutdown_sequence(processes: Set[psutil.Process]) -> None:
    """
    Runs the full graceful shutdown sequence for the given processes.
    
    :param processes: A set of psutil.Process objects to shut down.
    """
    _shutdown_nginx_gracefully()
    _terminate_processes(processes)

    # Wait and verify
    procs_list = list(processes)
    try:
        _, alive = psutil.wait_procs(procs_list, timeout=app_globals.GRACEFUL_SHUTDOWN_TIMEOUT)
    except psutil.TimeoutExpired:
        alive = procs_list
    except psutil.NoSuchProcess:
        alive = []

    # If any processes are still alive after the timeout, forcefully kill them.
    _forceful_kill(alive)
    cleanup_shutdown_files()
