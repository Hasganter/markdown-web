import json
import time
import psutil
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Tuple
from src.local import app_globals # This now works contextually
from src.log.setup import setup_logging
from src.local.external import DependencyManager
from src.local.database import ContentDBManager, LogDBManager
from src.local.supervisor import background_tasks, persistence, process_utils, shutdown, startup
from src.local.supervisor.config_service import run_config_service

log = logging.getLogger(__name__)


class ProcessManager:
    """
    Manages the lifecycle of the application's subprocesses and acts as the
    central authority for application configuration.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ProcessManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Initializes the ProcessManager state."""
        if getattr(self, '_initialized', False):
            return

        # The supervisor holds the master copy of the configuration.
        self.config: Dict[str, Any] = app_globals.get_all_settings()
        self.config_lock = threading.Lock()

        self.pids_on_disk: Dict[str, int] = {}
        self.running_procs: Dict[str, psutil.Process] = {}
        self.dependency_manager = DependencyManager()
        self.log_db_manager = LogDBManager(self.config["LOG_DB_PATH"])
        self.content_db_manager = ContentDBManager(self.config["CONTENT_DB_PATH"])
        self.restart_failures: Dict[str, int] = {}
        self.restart_cooldown_timers: Dict[str, float] = {}

        self.shutdown_signal_received = threading.Event()
        self._initialized = True

    def update_setting(self, key: str, value: Any) -> Tuple[bool, str]:
        """
        Thread-safe method to update a configuration setting.
        This is called by the config service.
        """
        with self.config_lock:
            if key not in self.config.get("MODIFIABLE_SETTINGS", set()):
                message = f"Setting '{key}' is not modifiable."
                log.warning(f"Rejected config update: {message}")
                return False, message
            
            # Coerce the new value to the type of the old value
            try:
                original_value = self.config.get(key)
                if isinstance(original_value, bool):
                    new_value = str(value).lower() in ('true', '1', 't', 'yes', 'y')
                elif original_value is not None:
                    new_value = type(original_value)(value)
                else:
                    new_value = value # Cannot determine type, accept as is
                
                self.config[key] = new_value
                self._save_overrides_to_disk()
                # Also update the singleton's attribute for internal supervisor use
                setattr(app_globals, key, new_value)
                
                message = f"Setting '{key}' updated to '{new_value}'. Restart required for all services to apply."
                log.info(message)
                return True, message

            except (ValueError, TypeError) as e:
                message = f"Could not convert value '{value}' for key '{key}'. Error: {e}"
                log.error(f"Config update failed: {message}")
                return False, message

    def _save_overrides_to_disk(self) -> None:
        """Persists the modifiable parts of the config to overrides.json."""
        overrides_path = Path(self.config["OVERRIDES_JSON_PATH"])
        modifiable_keys = self.config["MODIFIABLE_SETTINGS"]
        
        current_overrides = {}
        if overrides_path.exists():
            try:
                current_overrides = json.loads(overrides_path.read_text())
            except json.JSONDecodeError:
                pass # Ignore malformed file

        # Update the overrides with all current modifiable values
        for key in modifiable_keys:
            if key in self.config:
                current_overrides[key] = self.config[key]

        try:
            overrides_path.write_text(json.dumps(current_overrides, indent=4))
        except IOError as e:
            log.error(f"Failed to write overrides to '{overrides_path}': {e}")

    def _attempt_restart(self, process_name: str) -> bool:
        """
        Attempts to restart a single failed process with backoff logic.
        This remains a method of the main class as it directly manipulates its state.

        :param process_name: The logical name of the process to restart.
        :return: True if restart was successful, False otherwise.
        """
        cooldown_period = self.config.get("RESTART_COOLDOWN_PERIOD", 30)
        max_attempts = self.config.get("MAX_RESTART_ATTEMPTS", 3)

        if self.restart_cooldown_timers.get(process_name, 0) > time.time():
            log.debug(f"Process '{process_name}' is in cooldown. Skipping restart.")
            return False

        current_failures = self.restart_failures.get(process_name, 0)
        if current_failures >= max_attempts:
            log.critical(
                f"Process '{process_name}' has failed {current_failures} times. "
                "Halting restart attempts."
            )
            return False

        log.warning(f"Process '{process_name}' is down. Restart attempt #{current_failures + 1}...")
        try:
            process_utils.launch_process(self, process_name)
            log.info(f"Process '{process_name}' restarted successfully.")
            self.restart_failures.pop(process_name, None)
            self.restart_cooldown_timers.pop(process_name, None)
            persistence.write_pid_file(self)  # Update PID file with new PID.
            return True
        except Exception:
            self.restart_failures[process_name] = current_failures + 1
            self.restart_cooldown_timers[process_name] = time.time() + cooldown_period
            log.error(
                f"Failed to restart '{process_name}'. Cooldown active for {cooldown_period}s."
            )
            return False

    def start_all(self, verbose: bool = False) -> bool:
        """
        Starts all application processes as a cohesive background suite.

        :param verbose: If True, sets console logging to DEBUG level.
        :return: True on successful startup, False on failure.
        """
        if startup.check_if_already_running(self):
            return False

        console_level = logging.DEBUG if verbose else logging.INFO
        setup_logging(console_level)

        log.info("=" * 20 + " Application Starting " + "=" * 20)
        self.running_procs.clear()
        start_time = time.time()

        # Start the config service thread before launching any child processes
        config_thread = threading.Thread(target=run_config_service, args=(self,), daemon=True, name="ConfigServiceThread")
        config_thread.start()

        try:
            startup.setup_initial_environment(self)
            startup.perform_initial_content_processing(self)
            startup.start_all_processes(self)

            persistence.write_pid_file(self)
            background_tasks.start_update_checker(self)

            log.info(f"All application processes started successfully in {time.time() - start_time:.2f} seconds.")
            return True
        except Exception as e:
            log.critical(f"Startup failed due to an error: {e}", exc_info=True)
            self.stop_all(is_cleanup_after_failure=True)
            return False

    def stop_all(self, is_cleanup_after_failure: bool = False) -> None:
        """
        Stops all managed application processes gracefully.

        :param is_cleanup_after_failure: If True, uses internal state instead of PID file.
        """
        self.shutdown_signal_received.set()
        if not is_cleanup_after_failure:
            self.config["SHUTDOWN_SIGNAL_PATH"].touch()

        all_procs_to_stop = shutdown.identify_processes_to_stop(self, is_cleanup_after_failure)

        if not all_procs_to_stop:
            log.info("No running application processes found to stop.")
            shutdown.cleanup_shutdown_files()
            return

        log.info(f"Initiating graceful shutdown for {len(all_procs_to_stop)} total processes...")
        shutdown.graceful_shutdown_sequence(all_procs_to_stop)

        self.running_procs.clear()
        if app_globals.start_time:
            log.info(f"Application stop sequence completed. Total app runtime: {time.strftime('%H:%M:%S', time.gmtime(time.time() - app_globals.start_time))}")
        else:
            log.info("Application stop sequence completed.")

    def get_pid_info(self) -> Dict[str, int]:
        """
        Retrieves the current process IDs from the PID file.
        If the PID file does not exist or is invalid, it returns an empty dictionary.
        
        :return: A dictionary of process names and their PIDs.
        """
        return persistence.get_pid_info(self)

    def supervision_loop(self) -> None:
        """Main supervisor loop that monitors and restarts critical processes."""
        startup.initialize_supervision(self)

        while not self.shutdown_signal_received.is_set():
            try:
                if persistence.check_for_shutdown_signal():
                    break

                if process_utils.monitor_processes(self):
                    # A critical, unrecoverable error occurred.
                    return

                time.sleep(self.config.get("SUPERVISOR_SLEEP_INTERVAL", 2))

            except KeyboardInterrupt:
                log.info("Supervisor loop interrupted by user.")
                break
            except Exception as e:
                log.critical(f"Critical error in supervisor loop: {e}", exc_info=True)
                self.stop_all()
                return
