import hashlib
import importlib
import logging
import re
import shutil
import signal
import subprocess
import sys
import time
import setproctitle
from html import escape
from multiprocessing.synchronize import Event as EventType
from multiprocessing.synchronize import Lock as LockType
from multiprocessing.pool import Pool as PoolType
from multiprocessing import Pool, cpu_count, Event, Lock
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml
from markdown2 import Markdown
from watchdog.events import (DirDeletedEvent, FileDeletedEvent,
                             FileSystemEventHandler)
from watchdog.observers import Observer

from src.local.app_process import get_executable_path
from src.local.config import effective_settings as config
from src.local.database import ContentDBManager
from src.log.setup import setup_logging
from src.templates.default import DefaultTemplate

# This logger will be configured by the setup_logging call.
logger = logging.getLogger("content_converter")
DB_LOCK: Optional[LockType] = None
# The global db_manager will be instantiated per worker process.
db_manager: Optional[ContentDBManager] = None


def init_worker(lock: LockType) -> None:
    """
    Initializer for worker processes in the multiprocessing pool.

    This function sets up global state (lock and DB manager) for each worker
    process, ensuring they can safely interact with the shared database.

    :param lock: The multiprocessing lock to be shared among workers.
    """
    global DB_LOCK, db_manager
    DB_LOCK = lock
    # Each worker gets its own DB manager instance but shares the lock for writes.
    db_manager = ContentDBManager(config.CONTENT_DB_PATH, DB_LOCK)


def parse_source_with_yaml_header(source_content: str) -> Tuple[str, Dict[str, Any]]:
    """
    Parses a source file's content, separating YAML front matter from the body.

    The YAML block, enclosed in '~~~', is safely parsed to extract context
    variables, template settings, and allowed HTTP methods.

    :param source_content: The full string content of the source file.
    :return tuple: A tuple containing (body_content, parsed_config_data).
    """
    config_data: Dict[str, Any] = {
        "CONTEXT": {},
        "TEMPLATE": {},
        "ALLOWED_METHODS": ["GET"]
    }
    body_content = source_content

    # Regex to find a YAML front matter block.
    match = re.match(r"^\s*~~~\s*\n(.*?)\n~~~\s*\n(.*)", source_content, re.DOTALL)
    if not match:
        return body_content, config_data

    yaml_header, body_content = match.group(1), match.group(2)
    try:
        # Use safe_load to prevent arbitrary code execution from malicious YAML.
        parsed_yaml = yaml.safe_load(yaml_header)

        if isinstance(parsed_yaml, dict):
            config_data["CONTEXT"] = parsed_yaml.get("CONTEXT", {})
            config_data["TEMPLATE"] = parsed_yaml.get("TEMPLATE", {})
            methods = parsed_yaml.get("ALLOWED_METHODS", ["GET"])
            # Ensure methods are uppercase strings.
            config_data["ALLOWED_METHODS"] = [str(m).upper().strip() for m in methods] if isinstance(methods, list) else ["GET"]
        else:
            logger.warning("YAML front matter did not parse into a dictionary. Ignoring.")
    except yaml.YAMLError as e:
        logger.error(f"Error parsing YAML front matter: {e}", exc_info=True)

    return body_content, config_data


def get_media_type(file_path: Path) -> Optional[str]:
    """
    Determines if a file is a video, image, or audio based on its extension.

    :param file_path: The path to the file to check.
    :return str or None: 'image', 'video', 'audio', or None if not recognized.
    """
    ext = file_path.suffix.lower()
    if ext in {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.avif'}:
        return 'image'
    if ext in {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'}:
        return 'video'
    if ext in {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.opus'}:
        return 'audio'
    return None


def process_asset_file(source_path: Path) -> None:
    """
    Processes a single asset file. It converts recognized media types to a
    web-optimized format using FFmpeg and copies all other file types directly.
    This function is idempotent, skipping operations if the output is up-to-date.

    :param source_path: The path to the source asset file.
    """
    if not source_path.is_file():
        return

    media_type = get_media_type(source_path)
    if not media_type:
        # This is a non-media file (like CSS, JS) that should be copied directly.
        output_path = config.ASSETS_OUTPUT_DIR / source_path.name
        # Check if the destination is older than the source before copying.
        if not output_path.exists() or output_path.stat().st_mtime < source_path.stat().st_mtime:
            logger.info(f"Copying static asset '{source_path.name}' to output directory.")
            shutil.copy2(source_path, output_path)  # copy2 preserves metadata
        else:
            logger.debug(f"Static asset '{output_path.name}' is up-to-date. Skipping copy.")
        return

    # --- Media Conversion Logic ---
    output_map = {'image': '.avif', 'video': '.webm', 'audio': '.mp3'}
    output_filename = source_path.name + output_map[media_type]
    output_path = config.ASSETS_OUTPUT_DIR / output_filename

    if output_path.exists() and output_path.stat().st_mtime > source_path.stat().st_mtime:
        logger.debug(f"Skipping asset conversion, '{output_path.name}' is up-to-date.")
        return

    ffmpeg_exe = get_executable_path(config.FFMPEG_PATH)
    if not ffmpeg_exe.exists():
        logger.critical(f"FFmpeg not found at '{ffmpeg_exe}'. Asset conversion is disabled.")
        return

    logger.info(f"Converting '{source_path.name}' -> '{output_path.name}'...")
    command_map = {
        'image': ['-i', str(source_path), '-c:v', 'libaom-av1', '-crf', '30', '-b:v', '0', '-y', str(output_path)],
        'video': ['-i', str(source_path), '-c:v', 'libvpx-vp9', '-crf', '35', '-b:v', '0', '-c:a', 'libopus', '-b:a', '96k', '-y', str(output_path)],
        'audio': ['-i', str(source_path), '-codec:a', 'libmp3lame', '-qscale:a', '2', '-y', str(output_path)],
    }
    command = [str(ffmpeg_exe)] + command_map[media_type]

    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, creationflags=creationflags
        )
        logger.debug(f"FFmpeg output for {source_path.name}:\n{result.stdout}")
        logger.info(f"Successfully converted '{source_path.name}'.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to convert '{source_path.name}'. FFmpeg error:\n{e.stderr}")
    except FileNotFoundError:
        logger.critical(f"FFmpeg executable not found at '{ffmpeg_exe}'. Cannot convert assets.")


def process_content_directory(dir_path: Path, subdomain: Optional[str]) -> Tuple[str, bool]:
    """
    Processes a content directory to convert its source file to HTML.

    This function identifies the canonical source file (.md or .html), checks
    if an update is needed, converts the content, and stores it in the DB.

    :param dir_path: The path to the content directory to process.
    :param subdomain: The name of the subdomain this path belongs to.
    :return tuple: A tuple containing the processed path_key and a boolean (True if skipped).
    """
    if not db_manager:
        logger.error("DB Manager not initialized in worker. Cannot process directory.")
        return str(dir_path), True

    content_file_path = db_manager.get_canonical_content_file(dir_path, subdomain)
    if not content_file_path:
        logger.debug(f"No canonical content file for dir '{dir_path.name}'. Skipping.")
        return str(dir_path), True

    try:
        path_key = db_manager.get_path_key(dir_path, subdomain)
        source_content = content_file_path.read_text(encoding='utf-8')
        source_hash = hashlib.sha256(source_content.encode('utf-8')).hexdigest()

        if db_manager.get_page_hash(path_key) == source_hash:
            logger.debug(f"Content for {path_key} is unchanged. Skipping.")
            return path_key, True

        logger.info(f"Processing update for {path_key} from {content_file_path.name}...")
        body_content, cfg = parse_source_with_yaml_header(source_content)
        context = cfg.get("CONTEXT", {})
        template_cfg = cfg.get("TEMPLATE", {})
        allowed_methods = cfg.get("ALLOWED_METHODS", ["GET"])
        page_title = escape(context.get("title", path_key.split(":", 1)[1].replace('-', ' ').title() or "Home"))

        # Template handling
        template_instance = DefaultTemplate()
        if template_cfg.get("module") and template_cfg.get("class"):
            try:
                mod = importlib.import_module(f"src.templates.{template_cfg['module']}")
                TemplateClass = getattr(mod, template_cfg['class'])
                template_instance = TemplateClass()
            except (ImportError, AttributeError) as e:
                logger.error(f"Failed to load custom template for {path_key}: {e}. Using default.")

        html_fragment = Markdown().convert(body_content) if content_file_path.suffix == '.md' else body_content
        final_html = template_instance.convert(html_fragment, context)

        db_manager.update_page(path_key, source_hash, final_html, page_title, allowed_methods)
        return path_key, False

    except Exception as e:
        logger.error(f"Unhandled exception processing {content_file_path}: {e}", exc_info=True)
        return f"error:{dir_path.name}", False


def scan_and_process_all_content() -> None:
    """
    Performs a full scan of all content directories and processes them in parallel.
    """
    logger.info("Starting full scan and conversion of all content...")
    content_dirs = db_manager.discover_content_directories() if db_manager else []
    if not content_dirs:
        logger.info("No content directories found to process.")
        return

    num_processes = min(cpu_count(), len(content_dirs))
    if num_processes == 0:
        return
    
    with Pool(processes=num_processes, initializer=init_worker, initargs=(DB_LOCK,)) as pool:
        results = pool.starmap(process_content_directory, content_dirs)

    counts = {"processed": 0, "skipped": 0, "errors": 0}
    for key, skipped in results:
        if key.startswith("error:"): counts["errors"] += 1
        elif skipped: counts["skipped"] += 1
        else: counts["processed"] += 1

    logger.info(
        f"Full content scan complete. "
        f"Processed: {counts['processed']}, Skipped: {counts['skipped']}, Errors: {counts['errors']}."
    )


def scan_and_process_all_assets() -> None:
    """Scans and processes all media files, cleaning up orphaned outputs."""
    logger.info("Starting full scan and conversion of assets...")
    source_assets_dir = config.ROOT_INDEX_DIR / ".assets"
    
    if not source_assets_dir.is_dir():
        logger.warning(f"Source assets directory not found: '{source_assets_dir}'")
        return

    source_files = {p for p in source_assets_dir.iterdir() if p.is_file()}
    for file_path in source_files:
        process_asset_file(file_path)

    # Cleanup orphaned files in the output directory
    if not config.ASSETS_OUTPUT_DIR.is_dir():
        return
        
    # Create a mapping of expected output names from source names
    source_names = {p.name for p in source_files}
    for output_file in config.ASSETS_OUTPUT_DIR.iterdir():
        if output_file.stem not in source_names:
            logger.info(f"Deleting orphaned asset '{output_file.name}' as source is missing.")
            output_file.unlink(missing_ok=True)
    logger.info("Full asset scan complete.")


class ContentChangeHandler(FileSystemEventHandler):
    """A watchdog event handler that responds to changes in content source files."""

    def __init__(self, pool: PoolType):
        super().__init__()
        self.pool = pool
        self.debounce_cache: Dict[Path, float] = {}
        self.debounce_interval = 1.0  # seconds

    def _get_relevant_paths(self, event_path_str: str) -> Tuple[Optional[Path], Optional[str]]:
        """
        Determines the relevant directory and subdomain for a file system event.
        
        :param event_path_str: The path string from the watchdog event.
        :return tuple: A tuple of (directory_to_process, subdomain_name) or (None, None).
        """
        event_path = Path(event_path_str).resolve()
        
        # Ignore events in output directories
        if str(config.BIN_DIR.resolve()) in str(event_path):
            return None, None

        if not str(event_path).startswith(str(config.ROOT_INDEX_DIR.resolve())):
            return None, None
            
        relative_path = event_path.relative_to(config.ROOT_INDEX_DIR)
        
        # Asset change
        if relative_path.parts and relative_path.parts[0] == '.assets':
            return event_path, 'asset'

        # Content change
        dir_to_check = event_path.parent if event_path.is_file() else event_path
        subdomain = db_manager.get_subdomain_from_path(dir_to_check) if db_manager else None
        
        return dir_to_check, subdomain

    def on_any_event(self, event) -> None:
        """The main event handler method for watchdog, called on any file change."""
        if event.is_directory and event.src_path == str(config.ROOT_INDEX_DIR):
            return

        now = time.time()
        # Debounce to avoid handling rapid-fire save events.
        if self.debounce_cache.get(event.src_path, 0) > now - self.debounce_interval:
            return
        self.debounce_cache[event.src_path] = now
        
        path_to_process, type_id = self._get_relevant_paths(event.src_path)
        if not path_to_process:
            return

        logger.debug(f"Watchdog event: {event.event_type} on {event.src_path}")
        
        if type_id == 'asset':
            if event.event_type in (FileDeletedEvent.event_type, DirDeletedEvent.event_type):
                 # Handle asset deletion by removing its converted counterpart
                 media_type = get_media_type(Path(event.src_path))
                 if media_type:
                     output_map = {'image': '.avif', 'video': '.webm', 'audio': '.mp3'}
                     output_filename = Path(event.src_path).stem + output_map[media_type]
                     output_path = config.ASSETS_OUTPUT_DIR / output_filename
                     if output_path.exists():
                         logger.info(f"Source asset deleted. Removing converted file: {output_path.name}")
                         output_path.unlink(missing_ok=True)
            else: # File created or modified
                process_asset_file(path_to_process)
            return
        
        subdomain = type_id
        content_dir = path_to_process
        
        if event.event_type in (FileDeletedEvent.event_type, DirDeletedEvent.event_type):
            if db_manager:
                path_key_to_delete = db_manager.get_path_key(Path(event.src_path), subdomain)
                logger.info(f"Source deleted. Removing page from DB: {path_key_to_delete}")
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
    db_manager.initialize_database()

    logger.info("Content processor process started.")

    # Perform initial full scan on startup
    scan_and_process_all_content()
    scan_and_process_all_assets()
    
    # Setup watchdog observer
    observer = Observer()
    with Pool(processes=min(cpu_count(), 4), initializer=init_worker, initargs=(lock,)) as pool:
        event_handler = ContentChangeHandler(pool)
        observer.schedule(event_handler, str(config.ROOT_INDEX_DIR), recursive=True)
        observer.start()
        logger.info(f"Watching '{config.ROOT_INDEX_DIR}' for changes...")
        
        try:
            # Main loop: wait for stop event or periodic rescan interval
            while not stop_event.wait(timeout=config.MARKDOWN_SCAN_INTERVAL_SECONDS):
                logger.debug("Periodic rescan initiated by timer...")
                scan_and_process_all_content()
                scan_and_process_all_assets()
        except Exception as e:
            logger.error(f"Unhandled exception in content processor loop: {e}", exc_info=True)
        finally:
            observer.stop()
            observer.join()
            logger.info("Content processor observer stopped.")

    logger.info("Content processor process shut down.")

if __name__ == "__main__":
    setproctitle.setproctitle("MDWeb - ContentConverter")
    db_lock = Lock()
    stop_event = Event()

    def handle_shutdown_signal(signum, frame):
        logger.info(f"Signal {signum} received, shutting down content processor.")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    content_converter_process_loop(stop_event, db_lock)
