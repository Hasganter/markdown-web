import sys
import subprocess
from pathlib import Path
from typing import Optional
from src.local import app_globals
from src.local.supervisor.process_utils import get_executable_path


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


def check_ffmpeg_codec_support(codec_name: str) -> bool:
    """
    Check if FFmpeg supports a specific codec.
    
    :param codec_name: The codec name to check (e.g., 'libaom-av1')
    :return bool: True if codec is available, False otherwise
    """
    ffmpeg_exe = get_executable_path(app_globals.FFMPEG_PATH)
    if not ffmpeg_exe.exists():
        return False
    
    try:
        result = subprocess.run(
            [str(ffmpeg_exe), '-encoders'], 
            capture_output=True, 
            text=True, 
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        return codec_name in result.stdout
    except subprocess.CalledProcessError:
        return False
