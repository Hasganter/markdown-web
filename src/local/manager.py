import json
import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional
from multiprocessing import Lock
from src.web.process import scan_and_process_all_content, scan_and_process_all_assets, init_worker as init_content_worker
import psutil

from src.local.app_process import (get_executable_path, get_popen_creation_flags,
                                 get_process_args, log_process_output)
from src.local.config import effective_settings as config
from src.local.database import LogDBManager, ContentDBManager
from src.log.setup import setup_logging

logger = logging.getLogger(__name__)

# --- Constants ---
SUPERVISOR_SLEEP_INTERVAL = 2
MAX_RESTART_ATTEMPTS = 3
RESTART_COOLDOWN_PERIOD = 30  # seconds
ASGI_HEALTH_CHECK_TIMEOUT = 15 # seconds
CRITICAL_PROCESSES = {"nginx", "asgi_server", "content_converter"}


class ProcessManager:
    """
    Manages the lifecycle of the application's subprocesses.

    This class centralizes logic for starting, stopping, supervising, and
    checking the status of all components like Nginx, Hypercorn, and logging
    services. It acts as the master controller for the application suite.
    """

    def __init__(self) -> None:
        """Initializes the ProcessManager state."""
        self.pids_on_disk: Dict[str, int] = {}
        self.running_procs: Dict[str, psutil.Process] = {}
        self.log_db_manager = LogDBManager(config.LOG_DB_PATH)
        self.content_db_manager = ContentDBManager(config.CONTENT_DB_PATH)
        self.restart_failures: Dict[str, int] = {}
        self.restart_cooldown_timers: Dict[str, float] = {}
        self.shutdown_signal_received = threading.Event()

    def _handle_nginx_log_line(self, line: str) -> None:
        """
        Parses a JSON log line from Nginx and inserts it into the database.

        :param line: The raw JSON string from Nginx's stdout.
        """
        self.log_db_manager.insert_nginx_log(line)

    def check_configuration(self) -> bool:
        """
        Validates that essential external binaries exist at their configured paths.

        :return bool: True if all required executables are found, otherwise False.
        """
        logger.info("Performing configuration and path validation...")
        all_ok = True
        checks = {
            "FFmpeg": config.FFMPEG_PATH,
            "Nginx": config.NGINX_EXECUTABLE_PATH,
        }
        if config.LOKI_ENABLED:
            checks.update({
                "Loki": config.LOKI_PATH,
                "Alloy": config.ALLOY_PATH
            })

        for name, path_base in checks.items():
            path_exe = get_executable_path(path_base)
            if not path_exe.exists():
                logger.error(f"CONFIG CHECK FAILED: {name} not found at '{path_exe}'")
                all_ok = False
            else:
                logger.info(f"Config Check OK: Found {name} at '{path_exe}'")
        return all_ok

    def get_pid_info(self) -> Optional[Dict[str, int]]:
        """
        Reads the PID file from disk and returns its contents.

        :return dict or None: The dictionary of PIDs if the file exists and is valid, else None.
        """
        if not config.PID_FILE_PATH.exists():
            return None
        try:
            with config.PID_FILE_PATH.open("r") as f:
                pids = json.load(f)
            if not isinstance(pids, dict):
                logger.error(f"PID file '{config.PID_FILE_PATH}' is malformed. Deleting.")
                config.PID_FILE_PATH.unlink()
                return None
            self.pids_on_disk = pids
            return pids
        except (json.JSONDecodeError, IOError):
            logger.warning("Could not read PID file, assuming stale.")
            if config.PID_FILE_PATH.exists():
                config.PID_FILE_PATH.unlink(missing_ok=True)
            return None

    def _write_pid_file(self) -> None:
        """Atomically writes the current running process PIDs to the PID file."""
        pid_dict = {name: proc.pid for name, proc in self.running_procs.items() if proc.is_running()}
        temp_pid_path = config.PID_FILE_PATH.with_suffix(".tmp")
        try:
            with temp_pid_path.open("w") as f:
                json.dump(pid_dict, f, indent=4)
            # Atomic move/replace
            temp_pid_path.replace(config.PID_FILE_PATH)
        except (IOError, OSError) as e:
            logger.error(f"Failed to write PID file: {e}", exc_info=True)
        finally:
            temp_pid_path.unlink(missing_ok=True)

    def write_config_files(self) -> None:
        """
        Generates and writes all necessary runtime configuration files.
        """
        try:
            # --- Hypercorn Config ---
            hypercorn_workers = 0
            hypercorn_threads = 0
            if config.HYPERCORN_MODE == 'workers':
                hypercorn_workers = config.ASGI_WORKERS
                hypercorn_threads = 1 # Recommended to be 1 in worker mode
            else: # 'threads' mode
                hypercorn_workers = 1
                hypercorn_threads = config.ASGI_WORKERS

            hypercorn_conf_content = config.HYPERCORN_CONFIG_TEMPLATE.format(
                bind_host=config.WEB_SERVER_HOST,
                bind_port=config.WEB_SERVER_PORT,
                pid_path=str(config.BIN_DIR / "hypercorn.pid").replace("\\", "/"),
                mode=config.HYPERCORN_MODE,
                workers=hypercorn_workers,
                threads=hypercorn_threads,
            )
            config.HYPERCORN_CONFIG_PATH.write_text(hypercorn_conf_content)
            logger.info(f"Hypercorn config written to '{config.HYPERCORN_CONFIG_PATH}' for '{config.HYPERCORN_MODE}' mode.")

            # --- Nginx Config ---
            # Ensure a clean slate for nginx config in `bin`
            nginx_bin_conf_dir = config.BIN_DIR / "conf"
            if nginx_bin_conf_dir.exists():
                shutil.rmtree(nginx_bin_conf_dir)
            shutil.copytree(config.NGINX_SOURCE_PATH / "conf", nginx_bin_conf_dir)

            nginx_conf_content = config.NGINX_CONFIG_TEMPLATE.format(
                listen_port=config.NGINX_PORT,
                assets_server_name=f"{config.ASSETS_SUBDOMAIN_NAME}.{config.APP_DOMAIN}",
                server_name=config.APP_DOMAIN,
                assets_output_dir=str(config.ASSETS_OUTPUT_DIR.resolve()).replace("\\", "/"),
                asgi_host=config.WEB_SERVER_HOST,
                asgi_port=config.WEB_SERVER_PORT,
                zone_size=config.NGINX_RATELIMIT_ZONE_SIZE,
                rate=config.NGINX_RATELIMIT_RATE,
                burst=config.NGINX_RATELIMIT_BURST
            )
            # Nginx's -p prefix points to bin/, so conf file should be in bin/conf/
            (nginx_bin_conf_dir / "nginx.conf").write_text(nginx_conf_content)

            (config.BIN_DIR / "logs").mkdir(exist_ok=True)
            (config.BIN_DIR / "temp").mkdir(exist_ok=True)
            logger.info("Nginx configs and directories prepared in 'bin/'.")

            # --- Loki/Alloy Configs ---
            if config.LOKI_ENABLED:
                loki_data_path = (config.BIN_DIR / "loki-data").resolve()
                loki_data_path.mkdir(exist_ok=True)
                loki_port = int(config.LOKI_URL.split(":")[-1])

                loki_conf = config.LOKI_CONFIG_TEMPLATE.format(
                    loki_port=loki_port,
                    loki_data_path=str(loki_data_path).replace("\\", "/")
                )
                config.LOKI_CONFIG_PATH.write_text(loki_conf)

                loki_headers = f'headers = {{ "X-Scope-OrgID" = "{config.LOKI_ORG_ID}" }}' if config.LOKI_ORG_ID else ""
                alloy_conf = config.ALLOY_CONFIG_TEMPLATE.format(
                    loki_push_url=config.LOKI_URL.replace("\\", "/"),
                    loki_headers=loki_headers,
                    nginx_log_path=str((config.BIN_DIR / "logs" / "access.log").resolve()).replace("\\", "/")
                )
                config.ALLOY_CONFIG_PATH.write_text(alloy_conf)
                logger.info("Loki and Alloy configs written.")
        except Exception as e:
            logger.critical(f"Failed to write one or more configuration files: {e}", exc_info=True)
            raise  # Re-raise to be caught by start_all

    def _launch_process(self, name: str) -> None:
        """
        Launches a single process, adding it to the internal tracking dictionary.

        :param name: The logical name of the process to launch.
        :raises Exception: Propagates exceptions from subprocess creation or argument fetching.
        """
        logger.info(f"Starting process: {name}...")
        try:
            args, cwd = get_process_args(name)
            popen_kwargs = get_popen_creation_flags()

            # For Unix-like systems, Popen needs start_new_session=True for proper detachment.
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True

            p = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                cwd=str(cwd.resolve()),
                **popen_kwargs
            )

            line_handler = self._handle_nginx_log_line if name == "nginx" else None
            log_process_output(p, name, line_handler)

            # Store the psutil.Process object for supervision.
            self.running_procs[name] = psutil.Process(p.pid)
            logger.info(f"{name} started successfully with PID: {p.pid}")
        except (FileNotFoundError, ValueError, Exception) as e:
            logger.critical(f"Failed to start process '{name}': {e}", exc_info=True)
            raise

    def _wait_for_asgi_server(self) -> bool:
        """
        Waits for the ASGI server to become responsive on its port.

        :return bool: True if the server is up, False if it times out.
        """
        logger.info(f"Waiting for ASGI server at {config.WEB_SERVER_HOST}:{config.WEB_SERVER_PORT}...")
        start_time = time.monotonic()
        while time.monotonic() - start_time < ASGI_HEALTH_CHECK_TIMEOUT:
            try:
                with socket.create_connection((config.WEB_SERVER_HOST, config.WEB_SERVER_PORT), timeout=1):
                    logger.info("ASGI server is up and listening.")
                    return True
            except (socket.timeout, ConnectionRefusedError):
                time.sleep(0.5)
        logger.critical(f"ASGI server did not become available after {ASGI_HEALTH_CHECK_TIMEOUT} seconds.")
        return False

    def start_all(self) -> bool:
        """
        Starts all application processes as a cohesive background suite.

        :return bool: True on successful startup, False on failure.
        """
        if self.get_pid_info() and any(psutil.pid_exists(p) for p in self.get_pid_info().values()):
            logger.error("Application appears to be running. Use 'stop' or 'restart'.")
            return False

        setup_logging()
        logger.info("=" * 20 + " Application Starting " + "=" * 20)
        self.running_procs.clear()

        try:
            self.write_config_files()
            self.log_db_manager.initialize_database()

            logger.info("--- Performing initial blocking content and asset scan ---")
            db_lock = Lock()
            # The content converter process needs its own DB manager instance and a lock.
            init_content_worker(db_lock)
            self.content_db_manager.initialize_database() # Ensure table exists
            scan_and_process_all_content()
            scan_and_process_all_assets()
            logger.info("--- Initial blocking scan complete. Starting background processes. ---")

            process_launch_order = [
                ("loki", config.LOKI_ENABLED),
                ("alloy", config.LOKI_ENABLED),
                ("content_converter", True),
                ("asgi_server", True),
                ("ngrok", config.NGROK_ENABLED),
                ("nginx", True),
                ("supervisor", True),
            ]
            for name, is_enabled in process_launch_order:
                if not is_enabled:
                    continue
                
                self._launch_process(name)
                sleep_time = 2 if name == "asgi_server" else 1
                time.sleep(sleep_time) # Give process a moment to initialize.

                if name == "asgi_server":
                    if not self._wait_for_asgi_server():
                        raise RuntimeError("ASGI server health check failed.")

            self._write_pid_file()
            logger.info("All application processes started successfully.")
            return True
        except Exception as e:
            logger.critical(f"Startup failed due to an error: {e}", exc_info=True)
            self.stop_all(is_cleanup_after_failure=True)
            return False

    def stop_all(self, is_cleanup_after_failure: bool = False) -> None:
        """
        Stops all managed application processes gracefully.

        :param is_cleanup_after_failure: If True, uses internal state instead of PID file.
        """
        self.shutdown_signal_received.set()
        # Create signal file for supervisor process to see
        config.SHUTDOWN_SIGNAL_PATH.touch()

        # Determine which processes to stop.
        if is_cleanup_after_failure:
            procs_to_stop = self.running_procs
        else:
            procs_to_stop = {
                name: psutil.Process(pid)
                for name, pid in (self.get_pid_info() or {}).items()
                if psutil.pid_exists(pid)
            }

        if not procs_to_stop:
            logger.warning("Application not running or no active PIDs found.")
            self._cleanup_shutdown_files()
            return

        logger.info("Stopping application daemon...")

        # Stop Nginx gracefully first.
        if 'nginx' in procs_to_stop and procs_to_stop['nginx'].is_running():
            try:
                nginx_exe = get_executable_path(config.NGINX_EXECUTABLE_PATH)
                cmd = [str(nginx_exe.resolve()), '-s', 'stop', '-p', str(config.BIN_DIR.resolve())]
                subprocess.run(cmd, timeout=10, check=False, capture_output=True)
                logger.info("Nginx stop signal sent.")
            except Exception as e:
                logger.error(f"Failed to send stop signal to Nginx: {e}")

        time.sleep(1) # Allow Nginx to release ports.

        # Terminate other processes.
        terminated_procs = []
        # Stop in reverse order of criticality (supervisor last before nginx)
        for name, proc in reversed(list(procs_to_stop.items())):
            if name == 'nginx':
                continue
            try:
                if proc.is_running():
                    logger.debug(f"Terminating {name} (PID {proc.pid})")
                    proc.terminate()
                    terminated_procs.append(proc)
            except psutil.NoSuchProcess:
                pass # Already gone.
            except psutil.Error as e:
                logger.warning(f"Error terminating {name} (PID {proc.pid}): {e}")

        # Wait for processes to terminate and kill any that remain.
        _, alive = psutil.wait_procs(terminated_procs, timeout=5)
        for proc in alive:
            try:
                logger.warning(f"Process {proc.name()} (PID {proc.pid}) did not terminate, killing.")
                proc.kill()
            except psutil.Error as e:
                logger.error(f"Error killing process PID {proc.pid}: {e}")

        self._cleanup_shutdown_files()
        self.running_procs.clear()
        logger.info("Application stop sequence completed.")

    def _cleanup_shutdown_files(self) -> None:
        """Removes PID file and shutdown signal file."""
        config.PID_FILE_PATH.unlink(missing_ok=True)
        config.SHUTDOWN_SIGNAL_PATH.unlink(missing_ok=True)

    def _attempt_restart(self, process_name: str) -> bool:
        """
        Attempts to restart a single failed process with backoff logic.

        :param process_name: The logical name of the process to restart.
        :return bool: True if restart was successful, False otherwise.
        """
        if self.restart_cooldown_timers.get(process_name, 0) > time.time():
            logger.debug(f"Process '{process_name}' is in cooldown. Skipping restart.")
            return False

        current_failures = self.restart_failures.get(process_name, 0)
        if current_failures >= MAX_RESTART_ATTEMPTS:
            logger.critical(
                f"Process '{process_name}' has failed {current_failures} times. "
                "Halting restart attempts."
            )
            return False

        logger.warning(f"Process '{process_name}' is down. Restart attempt #{current_failures + 1}...")
        try:
            self._launch_process(process_name)
            logger.info(f"Process '{process_name}' restarted successfully.")
            # On success, reset failure count and cooldown.
            self.restart_failures.pop(process_name, None)
            self.restart_cooldown_timers.pop(process_name, None)
            self._write_pid_file() # Update PID file with new PID.
            return True
        except Exception:
            self.restart_failures[process_name] = current_failures + 1
            self.restart_cooldown_timers[process_name] = time.time() + RESTART_COOLDOWN_PERIOD
            logger.error(
                f"Failed to restart '{process_name}'. Cooldown active for {RESTART_COOLDOWN_PERIOD}s."
            )
            return False

    def supervision_loop(self) -> None:
        """Main supervisor loop that monitors and restarts critical processes."""
        setup_logging()
        logger.info(f"Supervisor started. Monitoring application processes.")
        self.shutdown_signal_received.clear()

        # Initialize internal state from PID file on supervisor startup.
        self.running_procs = {
            name: psutil.Process(pid)
            for name, pid in (self.get_pid_info() or {}).items()
            if psutil.pid_exists(pid)
        }

        while not self.shutdown_signal_received.is_set():
            try:
                if config.SHUTDOWN_SIGNAL_PATH.exists():
                    logger.info("Shutdown signal file detected. Exiting supervisor loop.")
                    break

                for name, proc in list(self.running_procs.items()):
                    if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                        status = "zombie" if proc.status() == psutil.STATUS_ZOMBIE else "stopped"
                        logger.warning(f"Detected {status} process: {name} (PID: {proc.pid})")
                        self.running_procs.pop(name, None)

                        if name in CRITICAL_PROCESSES:
                            if not self._attempt_restart(name):
                                # If restart fails and we've hit max attempts, panic.
                                if self.restart_failures.get(name, 0) >= MAX_RESTART_ATTEMPTS:
                                    logger.critical(
                                        f"PANIC: Unrecoverable failure for critical process '{name}'. "
                                        "Initiating full application shutdown."
                                    )
                                    self.stop_all()
                                    sys.exit(1)

                time.sleep(SUPERVISOR_SLEEP_INTERVAL)
            except KeyboardInterrupt:
                logger.info("Supervisor loop interrupted by user.")
                break
            except Exception as e:
                logger.critical(f"Critical error in supervisor loop: {e}", exc_info=True)
                self.stop_all()
                sys.exit(1)
