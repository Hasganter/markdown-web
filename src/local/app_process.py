import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.local.config import effective_settings as config

logger = logging.getLogger(__name__)


def get_popen_creation_flags() -> Dict[str, Any]:
    """
    Returns platform-specific creation flags for subprocess.Popen.

    On Windows, this uses flags to run the process detached and without a
    console window. On other platforms, it returns an empty dictionary,
    as session management is handled by the `start_new_session` Popen argument.

    :return dict: A dictionary of keyword arguments for Popen.
    """
    if sys.platform == "win32":
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
        }
    return {}


def get_executable_path(base_path: Path) -> Path:
    """
    Returns the platform-specific full path for an executable.

    It appends ".exe" on Windows systems.

    :param base_path: The base path of the executable (e.g., '.../nginx/nginx').
    :return pathlib.Path: The full, platform-aware Path object for the executable.
    """
    return base_path.with_suffix(".exe") if sys.platform == "win32" else base_path


def log_process_output(
    process: subprocess.Popen,
    process_name: str,
    line_handler: Optional[Callable[[str], None]] = None
) -> None:
    """
    Reads a process's stdout/stderr in threads and logs the output.

    This function spawns background daemon threads to consume the output pipes
    of a subprocess, preventing the pipes from filling up and blocking the child
    process. It logs each line using a logger named after the process.

    :param process: The `subprocess.Popen` object to monitor.
    :param process_name: The logical name of the process for logging context.
    :param line_handler: An optional callable to process stdout lines instead of logging them.
    """
    def reader_thread(pipe: Any, log_level: int, is_stdout: bool) -> None:
        """Target function for the thread that reads and logs a pipe."""
        # The logger is created inside the thread to be explicit about its context.
        proc_logger = logging.getLogger(f"proc.{process_name}")
        try:
            for line_bytes in iter(pipe.readline, b""):
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if is_stdout and line_handler:
                    try:
                        line_handler(line)
                    except Exception as e:
                        proc_logger.error(f"Error in custom line_handler: {e}", exc_info=True)
                else:
                    proc_logger.log(log_level, line)
        except Exception as e:
            # This can happen if the pipe is closed unexpectedly.
            proc_logger.debug(f"Pipe reader for {process_name} stream exited: {e}")
        finally:
            pipe.close()

    if process.stdout:
        threading.Thread(
            target=reader_thread, args=(process.stdout, logging.INFO, True), daemon=True
        ).start()
    if process.stderr:
        threading.Thread(
            target=reader_thread, args=(process.stderr, logging.ERROR, False), daemon=True
        ).start()


def get_process_args(process_name: str) -> Tuple[List[str], Path]:
    """
    Returns the command-line arguments and CWD for a specific process.

    This function centralizes the command definitions for all managed processes,
    ensuring consistency. It also determines the correct current working
    directory (CWD) for each process. External binaries run from `bin/`, while
    Python modules run from the project root.

    :param process_name: The logical name of the process (e.g., 'nginx').
    :return tuple: A tuple containing (list of command arguments, CWD path).
    :raises ValueError: If the process name is unknown.
    """
    bin_dir = config.BIN_DIR
    base_dir = config.BASE_DIR

    process_definitions = {
        "loki": (
            [
                str(get_executable_path(config.LOKI_PATH)),
                f"-config.file={str(config.LOKI_CONFIG_PATH.resolve())}"
            ],
            bin_dir
        ),
        "alloy": (
            [
                str(get_executable_path(config.ALLOY_PATH)),
                "run", str(config.ALLOY_CONFIG_PATH.resolve())
            ],
            bin_dir
        ),
        "content_converter": (
            [sys.executable, "-m", "src.web.process"],
            base_dir
        ),
        "asgi_server": (
            [
                "hypercorn",
                "-c", str(config.HYPERCORN_CONFIG_PATH.resolve()),
                "src.web.server:app"
            ],
            base_dir
        ),
        "nginx": (
            [
                str(get_executable_path(config.NGINX_EXECUTABLE_PATH)),
                "-p", str(bin_dir.resolve())
            ],
            bin_dir
        ),
        "supervisor": (
            [sys.executable, "-m", "src.local.supervisor_entry"],
            base_dir
        ),
        "ngrok": (
            [
                sys.executable, "-m", "ngrok",
                "http", str(config.NGINX_PORT),
                "--log", "stdout",
                "--authtoken", config.NGROK_AUTHTOKEN
            ],
            base_dir
        ),
    }

    if process_name in process_definitions:
        return process_definitions.get(process_name)

    raise ValueError(f"Unknown process name '{process_name}'. No arguments defined.")
