import sys
import shutil
import logging
import subprocess
from pathlib import Path
from src.local import app_globals
from src.local.supervisor.process_utils import get_executable_path
from src.converter.utils.assets import get_media_type, check_ffmpeg_codec_support

log = logging.getLogger("content_converter")


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
        _copy_static_asset(source_path)
        return

    _convert_media_asset(source_path, media_type)


def _copy_static_asset(source_path: Path) -> None:
    """Copy a static asset (non-media file) to the output directory."""
    output_path = app_globals.ASSETS_OUTPUT_DIR / source_path.name
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Check if the destination is older than the source before copying.
    source_stat = source_path.stat()
    output_exists = output_path.exists()
    output_stat = output_path.stat() if output_exists else None
    if not output_exists or output_stat.st_mtime < source_stat.st_mtime:
        log.info(f"Copying static asset '{source_path.name}' to output directory.")
        shutil.copy2(source_path, output_path)  # copy2 preserves metadata
    else:
        log.debug(f"Static asset '{output_path.name}' is up-to-date. Skipping copy.")


def _convert_media_asset(source_path: Path, media_type: str) -> None:
    """Convert a media asset using FFmpeg."""
    output_map = {'image': '.avif', 'video': '.webm', 'audio': '.mp3'}
    output_filename = source_path.name + output_map[media_type]
    output_path = app_globals.ASSETS_OUTPUT_DIR / output_filename

    if output_path.exists() and output_path.stat().st_mtime > source_path.stat().st_mtime:
        log.debug(f"Skipping asset conversion, '{output_path.name}' is up-to-date.")
        return

    ffmpeg_exe = get_executable_path(app_globals.FFMPEG_PATH)
    if not ffmpeg_exe.exists():
        log.critical(f"FFmpeg not found at '{ffmpeg_exe}'. Asset conversion is disabled.")
        return

    log.info(f"Converting '{source_path.name}' -> '{output_path.name}'...")
    
    command = _get_ffmpeg_command(source_path, output_path, media_type)
    if not command:
        return

    _execute_ffmpeg_command(command, source_path)


def _get_ffmpeg_command(source_path: Path, output_path: Path, media_type: str) -> list:
    """Get the appropriate FFmpeg command for the media type."""
    ffmpeg_exe = get_executable_path(app_globals.FFMPEG_PATH)
    
    if media_type == 'image':
        return _get_image_command(source_path, output_path, ffmpeg_exe)
    
    command_map = {
        'video': ['-i', str(source_path), '-c:v', 'libvpx-vp9', '-crf', '35', '-b:v', '0', '-c:a', 'libopus', '-b:a', '96k', '-y', str(output_path)],
        'audio': ['-i', str(source_path), '-codec:a', 'libmp3lame', '-qscale:a', '2', '-y', str(output_path)],
    }
    
    return [str(ffmpeg_exe)] + command_map[media_type]


def _get_image_command(source_path: Path, output_path: Path, ffmpeg_exe: Path) -> list:
    """Get the FFmpeg command for image conversion with fallbacks."""
    if check_ffmpeg_codec_support('libaom-av1'):
        return [str(ffmpeg_exe), '-i', str(source_path), '-c:v', 'libaom-av1', '-crf', '30', '-b:v', '0', '-y', str(output_path)]
    
    if check_ffmpeg_codec_support('libwebp'):
        # Fallback to WebP
        webp_output = output_path.parent / (source_path.name + '.webp')
        log.warning(f"AV1 encoder not available, falling back to WebP for '{source_path.name}'")
        return [str(ffmpeg_exe), '-i', str(source_path), '-c:v', 'libwebp', '-quality', '80', '-y', str(webp_output)]
    
    # Last resort: just copy the file
    shutil.copy2(source_path, app_globals.ASSETS_OUTPUT_DIR / source_path.name)
    log.warning(f"No suitable encoder found, copying '{source_path.name}' as-is")
    return []


def _execute_ffmpeg_command(command: list, source_path: Path) -> None:
    """Execute the FFmpeg command and handle errors."""
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, creationflags=creationflags
        )
        log.debug(f"FFmpeg output for {source_path.name}:\n{result.stdout}")
        log.info(f"Successfully converted '{source_path.name}'.")
    except subprocess.CalledProcessError as e:
        log.error(f"Failed to convert '{source_path.name}'. FFmpeg error:\n{e.stderr}")
    except FileNotFoundError:
        ffmpeg_exe = get_executable_path(app_globals.FFMPEG_PATH)
        log.critical(f"FFmpeg executable not found at '{ffmpeg_exe}'. Cannot convert assets.")


def scan_and_process_all_assets() -> None:
    """Scans and processes all media files, cleaning up orphaned outputs."""
    log.info("Starting full scan and conversion of assets...")
    source_assets_dir = app_globals.ROOT_INDEX_DIR / ".assets"
    
    if not source_assets_dir.is_dir():
        log.warning(f"Source assets directory not found: '{source_assets_dir}'")
        return

    source_files = {p for p in source_assets_dir.iterdir() if p.is_file()}
    for file_path in source_files:
        process_asset_file(file_path)

    # Cleanup orphaned files in the output directory
    if not app_globals.ASSETS_OUTPUT_DIR.is_dir():
        return
        
    # Create a mapping of expected output names from source names
    source_names = {p.name for p in source_files}
    for output_file in app_globals.ASSETS_OUTPUT_DIR.iterdir():
        if output_file.stem not in source_names:
            log.info(f"Deleting orphaned asset '{output_file.name}' as source is missing.")
            output_file.unlink(missing_ok=True)
    log.info("Full asset scan complete.")
