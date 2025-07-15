import time
import logging
from pathlib import Path
from src.local import app_globals
from watchdog.observers import Observer
from src.log.setup import setup_logging
from typing import Dict, Optional, Tuple
from multiprocessing import Pool, cpu_count
from src.local.database import ContentDBManager
from multiprocessing.pool import Pool as PoolType
from src.converter.utils.assets import get_media_type
from multiprocessing.synchronize import Lock as LockType
from multiprocessing.synchronize import Event as EventType
from watchdog.events import FileSystemEventHandler, FileDeletedEvent, DirDeletedEvent
from src.converter.worker.media import process_asset_file, scan_and_process_all_assets
from src.converter.worker.parsing import init_worker, process_content_directory, scan_and_process_all_content

DB_LOCK: Optional[LockType] = None

# The global db_manager will be instantiated per worker process.
db_manager: Optional[ContentDBManager] = None

# This logger will be configured by the setup_logging call.
log = logging.getLogger("content_converter")


class ContentChangeHandler(FileSystemEventHandler):
    """A watchdog event handler that responds to changes in content source files."""

    def __init__(self, pool: PoolType):
        super().__init__()
        self.pool = pool
        self.debounce_cache: Dict[Path, float] = {}
        self.debounce_interval = app_globals.WATCHDOG_DEBOUNCE_SECONDS

    def _get_relevant_paths(self, event_path_str: str) -> Tuple[Optional[Path], Optional[str]]:
        """
        Determines the relevant directory and subdomain for a file system event.
        
        :param event_path_str: The path string from the watchdog event.
        :return tuple: A tuple of (directory_to_process, subdomain_name) or (None, None).
        """
        event_path = Path(event_path_str).resolve()
        
        # Ignore events in output directories
        if str(app_globals.BIN_DIR.resolve()) in str(event_path):
            return None, None

        if not str(event_path).startswith(str(app_globals.ROOT_INDEX_DIR.resolve())):
            return None, None
            
        relative_path = event_path.relative_to(app_globals.ROOT_INDEX_DIR)
        
        # Asset change
        if relative_path.parts and relative_path.parts[0] == '.assets':
            return event_path, 'asset'

        # Content change
        dir_to_check = event_path.parent if event_path.is_file() else event_path
        subdomain = db_manager.get_subdomain_from_path(dir_to_check) if db_manager else None
        
        return dir_to_check, subdomain

    def on_any_event(self, event) -> None:
        """The main event handler method for watchdog, called on any file change."""
        if event.is_directory and event.src_path == str(app_globals.ROOT_INDEX_DIR):
            return

        if not self._should_process_event(event.src_path):
            return
        
        path_to_process, type_id = self._get_relevant_paths(event.src_path)
        if not path_to_process:
            return

        log.debug(f"Watchdog event: {event.event_type} on {event.src_path}")
        
        if type_id == 'asset':
            self._handle_asset_event(event, path_to_process)
        else:
            self._handle_content_event(event, path_to_process, type_id)
    
    def _should_process_event(self, path_str: str) -> bool:
        """Check if the event should be processed or skipped due to debouncing."""
        now = time.time()
        if self.debounce_cache.get(path_str, 0) > now - self.debounce_interval:
            return False
        self.debounce_cache[path_str] = now
        return True
        
    def _handle_asset_event(self, event, path_to_process: Path) -> None:
        """Handle events for asset files."""
        is_deletion = event.event_type in (FileDeletedEvent.event_type, DirDeletedEvent.event_type)
        
        if is_deletion:
            media_type = get_media_type(Path(event.src_path))
            if media_type:
                output_map = {'image': '.avif', 'video': '.webm', 'audio': '.mp3'}
                output_filename = Path(event.src_path).name + output_map[media_type]
                output_path = app_globals.ASSETS_OUTPUT_DIR / output_filename
                if output_path.exists():
                    log.info(f"Source asset deleted. Removing converted file: {output_path.name}")
                    output_path.unlink(missing_ok=True)
        else:
            process_asset_file(path_to_process)
            
    def _handle_content_event(self, event, content_dir: Path, subdomain: str) -> None:
        """Handle events for content files/directories."""
        is_deletion = event.event_type in (FileDeletedEvent.event_type, DirDeletedEvent.event_type)
        
        if is_deletion:
            if db_manager:
                path_key_to_delete = db_manager.get_path_key(Path(event.src_path), subdomain)
                log.info(f"Source deleted. Removing page from DB: {path_key_to_delete}")
                db_manager.delete_page(path_key_to_delete)
        elif content_dir.is_dir():
            # Use the pool to process the change asynchronously
            self.pool.apply_async(process_content_directory, args=(content_dir, subdomain))


def content_converter_process_loop(stop_event: EventType, lock: LockType) -> None:
    """
    The main loop for the content processor process.

    It performs an initial full scan, then watches for file changes.

    :param stop_event: A multiprocessing.Event to signal termination.
    :param lock: A multiprocessing.Lock for safe database access.
    """
    # Setup logging and DB access for this specific process
    setup_logging(console_level=logging.INFO)
    init_worker(lock)

    log.info("Content processor process started.")
    
    # Initialize the global db_manager for this process
    global db_manager
    db_manager = ContentDBManager(app_globals.CONTENT_DB_PATH)

    # Setup watchdog observer
    observer = Observer()
    # Determine optimal pool size based on content directories or config
    content_dirs = db_manager.discover_content_directories() if db_manager else []
    num_processes = min(cpu_count(), len(content_dirs)) if content_dirs else 1
    with Pool(processes=num_processes, initializer=init_worker, initargs=(lock,)) as pool:
        log.info(f"Starting content processor observer with {num_processes} worker processes.")
        event_handler = ContentChangeHandler(pool)
        observer.schedule(event_handler, str(app_globals.ROOT_INDEX_DIR), recursive=True)
        observer.start()
        try:
            # Main loop: wait for stop event or periodic rescan interval
            while not stop_event.is_set():
                # Wait for the scan interval or until stop_event is set
                stop_event.wait(timeout=app_globals.MARKDOWN_SCAN_INTERVAL_SECONDS)
                log.debug("Periodic rescan initiated by timer...")
                scan_and_process_all_content()
                scan_and_process_all_assets()
                # Check observer health
                if not observer.is_alive():
                    log.error("Watchdog observer thread has stopped unexpectedly. Restarting observer.")
                    observer.stop()
                    observer.join(timeout=5)
                    observer = Observer()
                    event_handler = ContentChangeHandler(pool)
                    observer.schedule(event_handler, str(app_globals.ROOT_INDEX_DIR), recursive=True)
                    observer.start()
                    log.info("Observer restarted.")
        except Exception as e:
            log.error(f"Unhandled exception in content processor loop: {e}", exc_info=True)
            shutdown_reason = f"due to exception: {e}"
        else:
            shutdown_reason = "because stop event was received"
        finally:
            observer.stop()
            observer.join()
            log.info("Content processor observer stopped.")

    log.info(f"Content processor process shut down ({shutdown_reason}).")
