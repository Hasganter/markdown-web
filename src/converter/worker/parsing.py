import hashlib
import logging
import importlib
from html import escape
from pathlib import Path
from markdown2 import Markdown
from src.local import app_globals
from typing import Optional, Tuple, Dict, Any
from src.local.database import ContentDBManager
from multiprocessing.synchronize import Lock as LockType
from src.converter.utils.content import parse_source_with_yaml_header

DB_LOCK: Optional[LockType] = None

# The global db_manager will be instantiated per worker process.
db_manager: Optional[ContentDBManager] = None
log = logging.getLogger("content_converter")


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
    db_manager = ContentDBManager(app_globals.CONTENT_DB_PATH, DB_LOCK)


def _get_fallback_html(html_fragment: str, context: Dict[str, Any]) -> str:
    """A basic HTML wrapper for when no template is specified or found."""
    title = escape(context.get('title', 'Page'))
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title><meta charset="utf-8"></head>
<body>{html_fragment}</body></html>"""


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
        log.error("DB Manager not initialized in worker. Cannot process directory.")
        return str(dir_path), True

    content_file_path = db_manager.get_canonical_content_file(dir_path, subdomain)
    if not content_file_path:
        log.debug(f"No canonical content file for dir '{dir_path.name}'. Skipping.")
        return str(dir_path), True

    try:
        path_key = db_manager.get_path_key(dir_path, subdomain)
        source_content = content_file_path.read_text(encoding='utf-8')
        source_hash = hashlib.sha256(source_content.encode('utf-8')).hexdigest()

        if db_manager.get_page_hash(path_key) == source_hash:
            log.debug(f"Content for {path_key} is unchanged. Skipping.")
            return path_key, True

        log.info(f"Processing update for {path_key} from {content_file_path.name}...")
        body_content, cfg = parse_source_with_yaml_header(source_content)
        context = cfg.get("CONTEXT", {})
        template_cfg = cfg.get("TEMPLATE", {}) # e.g., {"NAME": "Default_Responsive", "CSS": "body{color:red}"}
        allowed_methods = cfg.get("ALLOWED_METHODS", ["GET"])
        
        # Determine the page title
        page_title = escape(context.get("title", path_key.split(":", 1)[1].replace('-', ' ').title() or "Home"))

        # Convert the main content from Markdown if necessary
        html_fragment = Markdown().convert(body_content) if content_file_path.suffix == '.md' else body_content

        final_html = ""
        template_name = template_cfg.get("NAME")

        if template_name:
            # Sanitize the template name to be a valid Python module name
            module_name = template_name.replace(' ', '_').replace('-', '_')
            try:
                # Dynamically import the template package
                template_module = importlib.import_module(f"templates.{module_name}")
                log.info(f"Using template '{template_name}' for page '{path_key}'.")

                # Call the 'format' function from the template's __init__.py
                final_html = template_module.format(
                    content=html_fragment,
                    context=context,
                    app_globals=app_globals,
                    custom_html=template_cfg.get("HTML", ""),
                    custom_css=template_cfg.get("CSS", ""),
                    custom_js=template_cfg.get("JS", "")
                )
            except Exception as e:
                log.error(f"Failed to load or use template '{template_name}' for {path_key}: {e}. Using basic fallback.")
                final_html = _get_fallback_html(html_fragment, context)
        else:
            # If no template is specified in YAML, use the basic fallback
            log.debug(f"No template specified for '{path_key}'. Using basic fallback.")
            final_html = _get_fallback_html(html_fragment, context)

        db_manager.update_page(path_key, source_hash, final_html, page_title, allowed_methods)
        return path_key, False

    except Exception as e:
        log.error(f"Unhandled exception processing {content_file_path}: {e}", exc_info=True)
        return f"error:{dir_path.name}", False


def scan_and_process_all_content() -> None:
    """
    Performs a full scan of all content directories and processes them in parallel.
    """
    from multiprocessing import Pool, cpu_count
    
    log.info("Starting full scan and conversion of all content...")
    content_dirs = db_manager.discover_content_directories() if db_manager else []
    if not content_dirs:
        log.info("No content directories found to process.")
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

    log.info(
        f"Full content scan complete. "
        f"Processed: {counts['processed']}, Skipped: {counts['skipped']}, Errors: {counts['errors']}."
    )
