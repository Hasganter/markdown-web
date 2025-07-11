import time
import psutil
import logging
from typing import Dict
from src.local import app_globals
from src.log.setup import setup_logging
from src.local.external import DependencyManager
from src.local.database import ContentDBManager, LogDBManager
from src.local.supervisor import background_tasks, persistence, process_utils, shutdown, startup

log = logging.getLogger(__name__)


class ProcessManager:
    """
    Manages the lifecycle of the application's subprocesses.

    This class centralizes state and orchestrates startup, shutdown, and
    supervision by calling logic from its helper modules.
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

        self.pids_on_disk: Dict[str, int] = {}
        self.running_procs: Dict[str, psutil.Process] = {}
        self.dependency_manager = DependencyManager()
        self.log_db_manager = LogDBManager(app_globals.LOG_DB_PATH)
        self.content_db_manager = ContentDBManager(app_globals.CONTENT_DB_PATH)
        self.restart_failures: Dict[str, int] = {}
        self.restart_cooldown_timers: Dict[str, float] = {}

        self.shutdown_signal_received = app_globals.stop_log_listener
        self.stop_tailing_event = app_globals.stop_log_listener
        self._initialized = True

    def _attempt_restart(self, process_name: str) -> bool:
        """
        Attempts to restart a single failed process with backoff logic.
        This remains a method of the main class as it directly manipulates its state.

        :param process_name: The logical name of the process to restart.
        :return: True if restart was successful, False otherwise.
        """
        if self.restart_cooldown_timers.get(process_name, 0) > time.time():
            log.debug(f"Process '{process_name}' is in cooldown. Skipping restart.")
            return False

        current_failures = self.restart_failures.get(process_name, 0)
        if current_failures >= app_globals.MAX_RESTART_ATTEMPTS:
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
            cooldown = app_globals.RESTART_COOLDOWN_PERIOD
            self.restart_cooldown_timers[process_name] = time.time() + cooldown
            log.error(
                f"Failed to restart '{process_name}'. Cooldown active for {cooldown}s."
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
        self.stop_tailing_event.set()
        self.shutdown_signal_received.set()
        app_globals.SHUTDOWN_SIGNAL_PATH.touch()

        all_procs_to_stop = shutdown.identify_processes_to_stop(self, is_cleanup_after_failure)

        if not all_procs_to_stop:
            log.info("No running application processes found to stop.")
            shutdown.cleanup_shutdown_files()
            return

        log.info(f"Initiating graceful shutdown for {len(all_procs_to_stop)} total processes...")
        shutdown.graceful_shutdown_sequence(all_procs_to_stop)

        self.running_procs.clear()
        log.info(f"Application stop sequence completed. Total app runtime: {time.strftime('%H:%M:%S', time.gmtime(time.time() - app_globals.start_time))}")

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

                time.sleep(app_globals.SUPERVISOR_SLEEP_INTERVAL)

            except KeyboardInterrupt:
                log.info("Supervisor loop interrupted by user.")
                break
            except Exception as e:
                log.critical(f"Critical error in supervisor loop: {e}", exc_info=True)
                self.stop_all()
                return
