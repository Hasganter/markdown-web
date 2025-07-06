import json
import logging
import shutil
import socket
import subprocess
import sys
import os
import threading
import time
from typing import Dict, Optional, Set
from pathlib import Path
from multiprocessing import Lock
from src.web.process import scan_and_process_all_content, scan_and_process_all_assets, init_worker as init_content_worker
import psutil

from src.local.app_process import get_executable_path, get_popen_creation_flags, get_process_args, log_process_output
from src.local.config import effective_settings as config
from src.local.database import LogDBManager, ContentDBManager
from src.local.externals import DependencyManager
from src.log.setup import setup_logging

logger = logging.getLogger(__name__)

# --- Constants ---
SUPERVISOR_SLEEP_INTERVAL = config.SUPERVISOR_SLEEP_INTERVAL or 2
MAX_RESTART_ATTEMPTS = config.MAX_RESTART_ATTEMPTS or 3
RESTART_COOLDOWN_PERIOD = config.RESTART_COOLDOWN_PERIOD or 30
ASGI_HEALTH_CHECK_TIMEOUT = config.ASGI_HEALTH_CHECK_TIMEOUT or 15
GRACEFUL_SHUTDOWN_TIMEOUT = config.GRACEFUL_SHUTDOWN_TIMEOUT or 10
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
        self.dependency_manager = DependencyManager()
        self.log_db_manager = LogDBManager(config.LOG_DB_PATH)
        self.content_db_manager = ContentDBManager(config.CONTENT_DB_PATH)
        self.restart_failures: Dict[str, int] = {}
        self.restart_cooldown_timers: Dict[str, float] = {}
        self.shutdown_signal_received = threading.Event()
        self.stop_tailing_event = threading.Event()

    def _tail_nginx_log_file(self, log_path: Path):
        """
        Tails the Nginx access log file and processes new lines.
        This runs in a dedicated background thread until stop_tailing_event is set.
        """
        logger.info(f"Starting to tail Nginx access log at: {log_path}")
        
        # Wait for Nginx to start and create the log file
        for _ in range(5): # Wait up to 5 seconds
            if log_path.exists():
                break
            time.sleep(1)
        
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                # Go to the end of the file to only read new lines
                f.seek(0, os.SEEK_END)
                while not self.stop_tailing_event.is_set():
                    line = f.readline()
                    if not line:
                        time.sleep(0.2)  # Sleep briefly when there are no new lines
                        continue
                    # Pass the stripped line to the DB manager
                    self.log_db_manager.insert_nginx_log(line.strip())
        except FileNotFoundError:
            if not self.shutdown_signal_received.is_set():
                logger.error(f"Nginx log file not found at {log_path}. Tailing failed. Nginx might not have started correctly.")
        except Exception as e:
            if not self.shutdown_signal_received.is_set():
                logger.error(f"Error while tailing Nginx log file: {e}", exc_info=True)
        
        logger.info("Nginx log tailing thread has stopped.")

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
        pid_dict = {name: proc.pid for name, proc in self.running_procs.items() if psutil.pid_exists(proc.pid)}
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
            
            # The main nginx.conf file needs to be placed in the *copied* conf directory
            nginx_conf_target_path = nginx_bin_conf_dir / "nginx.conf"
            
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
            nginx_conf_target_path.write_text(nginx_conf_content)

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
            log_process_output(p, name, line_handler=None)

            # Store the psutil.Process object for supervision.
            self.running_procs[name] = psutil.Process(p.pid)
            logger.info(f"{name.capitalize()} started successfully with PID: {p.pid}")
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

    def start_all(self, verbose: bool = False) -> bool:
        """
        Starts all application processes as a cohesive background suite.
        
        :param verbose: If True, sets console logging to DEBUG level.
        :return bool: True on successful startup, False on failure.
        """
        if self.get_pid_info() and any(psutil.pid_exists(p) for p in self.get_pid_info().values()):
            logger.error("Application appears to be running. Use 'stop' or 'restart'.")
            return False

        # Set up logging with desired verbosity for the startup sequence
        console_level = logging.DEBUG if verbose else logging.INFO
        setup_logging(console_level)

        logger.info("=" * 20 + " Application Starting " + "=" * 20)
        self.running_procs.clear()

        # Dependencies check and installation
        is_first_run = not any(p.is_dir() for p in config.EXTERNAL_DIR.iterdir() if not p.name.startswith('.'))
        if is_first_run:
            logger.warning("External dependency directory is empty. Running initial installation...")
            if not self.dependency_manager.ensure_all_dependencies_installed():
                logger.critical("Dependency installation failed. Cannot start application.")
                return False
            logger.info("Initial dependency installation complete.")


        try:
            self.dependency_manager.apply_pending_installs()
            self.write_config_files()
            self.log_db_manager.initialize_database()

            logger.info("--- Performing initial blocking content and asset scan ---")
            db_lock = Lock()
            # The content converter process needs its own DB manager instance and a lock.
            init_content_worker(db_lock)
            self.content_db_manager.initialize_database()
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
                # Give server processes a moment to initialize before health checks.
                if name in ("asgi_server", "nginx", "loki"):
                    time.sleep(1)

                if name == "asgi_server":
                    if not self._wait_for_asgi_server():
                        raise RuntimeError("ASGI server health check failed.")

                # After Nginx starts, launch the thread to tail its access log file.
                if name == "nginx":
                    nginx_log_path = config.BIN_DIR / "logs" / "access.log"
                    self.stop_tailing_event.clear() # Reset event for this run
                    tail_thread = threading.Thread(
                        target=self._tail_nginx_log_file,
                        args=(nginx_log_path,),
                        daemon=True,
                        name="NginxLogTailerThread"
                    )
                    tail_thread.start()

            self._write_pid_file()
            logger.info("All application processes started successfully.")

            update_thread = threading.Thread(
                target=self.dependency_manager.check_for_updates_async,
                daemon=True,
                name="DepUpdateCheckThread"
            )
            update_thread.start()
            
            return True
        except Exception as e:
            logger.critical(f"Startup failed due to an error: {e}", exc_info=True)
            self.stop_all(is_cleanup_after_failure=True)
            return False

    def stop_all(self, is_cleanup_after_failure: bool = False) -> None:
        """
        Stops all managed application processes gracefully, including children.
    
        :param is_cleanup_after_failure: If True, uses internal state instead of PID file.
        """
        self.stop_tailing_event.set()
        self.shutdown_signal_received.set()
        config.SHUTDOWN_SIGNAL_PATH.touch()
    
        # --- Stage 1: Identify all processes to be stopped ---
        parent_procs: Set[psutil.Process] = set()
        if is_cleanup_after_failure:
            parent_procs = {p for p in self.running_procs.values() if p.is_running()}
            logger.warning("Cleaning up processes after a startup failure.")
        else:
            pid_info = self.get_pid_info() or {}
            parent_procs = {psutil.Process(pid) for pid in pid_info.values() if psutil.pid_exists(pid)}
    
        # Add Hypercorn master process from its specific PID file, if it exists
        hypercorn_pid_path = config.BIN_DIR / "hypercorn.pid"
        if hypercorn_pid_path.exists():
            try:
                master_pid = int(hypercorn_pid_path.read_text().strip())
                if psutil.pid_exists(master_pid):
                    logger.info(f"Found active Hypercorn master PID {master_pid} from file. Adding to shutdown.")
                    parent_procs.add(psutil.Process(master_pid))
            except (ValueError, IOError, psutil.Error) as e:
                logger.error(f"Could not read or use Hypercorn PID file: {e}")
    
        # Recursively find all children of all parent processes
        all_procs_to_stop: Set[psutil.Process] = set(parent_procs)
        for proc in parent_procs:
            try:
                children = proc.children(recursive=True)
                if children:
                    logger.debug(f"Found {len(children)} child process(es) for {proc.name()} (PID {proc.pid}).")
                    all_procs_to_stop.update(children)
            except psutil.NoSuchProcess:
                continue # Parent died before we could check for children
    
        if not all_procs_to_stop:
            logger.info("No running application processes found to stop.")
            self._cleanup_shutdown_files()
            return
    
        logger.info(f"Initiating graceful shutdown for {len(all_procs_to_stop)} total processes (parents and children)...")
    
        # --- Stage 2: Graceful Shutdown ---
        # Send Nginx 'quit' command for graceful shutdown
        try:
            nginx_exe = get_executable_path(config.NGINX_EXECUTABLE_PATH)
            cmd = [str(nginx_exe.resolve()), '-s', 'quit', '-p', str(config.BIN_DIR.resolve())]
            subprocess.run(cmd, timeout=10, check=False, capture_output=True)
            logger.info("Nginx graceful quit signal sent.")
        except Exception as e:
            logger.error(f"Failed to send graceful quit signal to Nginx: {e}")
    
        # Terminate all other processes (SIGTERM)
        for proc in all_procs_to_stop:
            try:
                if 'nginx' not in proc.name().lower():
                    logger.debug(f"Sending SIGTERM to {proc.name()} (PID {proc.pid})")
                    proc.terminate()
            except psutil.NoSuchProcess:
                continue
    
        # --- Stage 3: Wait and Verify ---
        procs_list = list(all_procs_to_stop)
        try:
            gone, alive = psutil.wait_procs(procs_list, timeout=GRACEFUL_SHUTDOWN_TIMEOUT)
            for proc in gone:
                logger.debug(f"Process {proc.name()} (PID {proc.pid}) terminated gracefully.")
        except psutil.TimeoutExpired:
            alive = procs_list
    
        # --- Stage 4: Forceful Shutdown (Kill) ---
        if alive:
            logger.warning(f"{len(alive)} processes did not terminate gracefully. Forcing shutdown...")
            for proc in alive:
                try:
                    logger.warning(f"Killing stubborn process {proc.name()} (PID {proc.pid}).")
                    proc.kill()
                except psutil.NoSuchProcess:
                    pass
    
        self._cleanup_shutdown_files()
        self.running_procs.clear()
        logger.info("Application stop sequence completed.")


    def _cleanup_shutdown_files(self) -> None:
        """Removes PID files and the shutdown signal file."""
        config.PID_FILE_PATH.unlink(missing_ok=True)
        config.SHUTDOWN_SIGNAL_PATH.unlink(missing_ok=True)
        (config.BIN_DIR / "hypercorn.pid").unlink(missing_ok=True)
        logger.debug("Cleaned up PID and signal files.")


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
        
    def _apply_pending_updates(self):
        """Checks for and applies updates from the .temp_update directory."""
        temp_update_dir = config.EXTERNAL_DIR / ".temp_update"
        if not temp_update_dir.is_dir():
            return

        logger.warning("Pending dependency updates found. Applying now...")
        for dep_key_dir in temp_update_dir.iterdir():
            if dep_key_dir.is_dir():
                dep_key = dep_key_dir.name
                if dep_key in config.EXTERNAL_DEPENDENCIES:
                    logger.info(f"Applying update for {dep_key}...")
                    # 1. Archive current version
                    self.dependency_manager._archive_current_version(dep_key)
                    # 2. Move in the new version from temp
                    new_version_path = temp_update_dir / dep_key / config.EXTERNAL_DEPENDENCIES[dep_key]['target_dir_name']
                    target_path = config.EXTERNAL_DIR / config.EXTERNAL_DEPENDENCIES[dep_key]['target_dir_name']
                    try:
                        shutil.move(str(new_version_path), str(target_path))
                        logger.info(f"Update for {dep_key} applied successfully.")
                    except OSError as e:
                        logger.error(f"Failed to apply update for {dep_key}: {e}")

        # Clean up the temp update directory
        shutil.rmtree(temp_update_dir)
        logger.warning("All pending updates applied.")

    def supervision_loop(self) -> None:
        """Main supervisor loop that monitors and restarts critical processes."""
        # Use setup_logging to ensure the supervisor has its own clean logging
        setup_logging()
        
        logger.info("Supervisor started. Monitoring application processes.")
        self.shutdown_signal_received.clear()

        # Initialize internal state from PID file on supervisor startup.
        pid_info = self.get_pid_info() or {}
        self.running_procs = {
            name: psutil.Process(pid)
            for name, pid in pid_info.items()
            if psutil.pid_exists(pid)
        }

        while not self.shutdown_signal_received.is_set():
            try:
                if config.SHUTDOWN_SIGNAL_PATH.exists():
                    logger.info("Shutdown signal file detected. Exiting supervisor loop.")
                    break

                # Create a copy of items to allow modification during iteration
                for name, proc in list(self.running_procs.items()):
                    try:
                        is_running = proc.is_running()
                        status_ok = proc.status() != psutil.STATUS_ZOMBIE
                    except psutil.NoSuchProcess:
                        is_running = False
                        status_ok = False

                    if not (is_running and status_ok):
                        status = "stopped"
                        try:
                           if proc.status() == psutil.STATUS_ZOMBIE:
                               status = "zombie"
                        except psutil.Error:
                            pass
                        
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
                                    # Call stop_all directly instead of sys.exit to ensure cleanup
                                    self.stop_all()
                                    return
                                    
                time.sleep(SUPERVISOR_SLEEP_INTERVAL)

            except KeyboardInterrupt:
                logger.info("Supervisor loop interrupted by user.")
                break
            except Exception as e:
                logger.critical(f"Critical error in supervisor loop: {e}", exc_info=True)
                self.stop_all()
                return
