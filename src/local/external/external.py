import re
import sys
import time
import json
import shutil
import zipfile
import logging
import requests
from pathlib import Path
from typing import Dict, Any
from src.local import app_globals

log = logging.getLogger(__name__)


class DependencyManager:
    """Manages downloading, updating, and recovering external dependencies."""

    def __init__(self):
        self.dependencies: Dict[str, Dict[str, Any]] = app_globals.EXTERNAL_DEPENDENCIES
        self.external_dir = app_globals.EXTERNAL_DIR
        self.old_dir = self.external_dir / ".old"
        # Consolidated temporary directory for all downloads
        self.temp_dir = self.external_dir / ".temp"
        self.old_dir.mkdir(exist_ok=True)
        # Ensure the temp dir is clean on startup
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)
        self.temp_dir.mkdir(exist_ok=True)


    def _get_latest_version(self, dep_key: str) -> str | None:
        """Fetches the latest version string for a dependency from its version_url."""
        dep_info = self.dependencies[dep_key]
        try:
            headers = {"User-Agent": "MyWebApp/1.0"}
            res = requests.get(dep_info["version_url"], timeout=10, headers=headers)
            res.raise_for_status()

            match = re.search(dep_info["version_regex"], res.text)
            if match:
                version = match.group(1)
                log.debug(f"Latest version for {dep_info['name']} is {version}.")
                return version
            log.warning(f"Could not parse version from {dep_info['version_url']}")
        except (requests.RequestException, IndexError) as e:
            log.error(f"Failed to fetch latest version for {dep_info['name']}: {e}")
        return None

    def get_current_versions_for_dir(self, target_dir_name: str) -> Dict[str, str]:
        """Reads currently installed versions from the .version or .versions.json file."""
        target_dir = self.external_dir / target_dir_name
        version_file = target_dir / ".version"
        versions_file_json = target_dir / ".versions.json"

        if versions_file_json.exists():
            try:
                return json.loads(versions_file_json.read_text())
            except (json.JSONDecodeError, IOError):
                return {}
        elif version_file.exists():
            dep_key = next((k for k, v in self.dependencies.items() if v['target_dir_name'] == target_dir_name), target_dir_name)
            return {dep_key: version_file.read_text().strip()}

        return {}

    def _download_file(self, url: str, dest_path: Path):
        """Downloads a file with a simple progress bar."""
        log.info(f"Downloading from {url}...")
        try:
            headers = {"User-Agent": "MyWebApp/1.0"}
            with requests.get(url, stream=True, timeout=30, headers=headers) as r:
                r.raise_for_status()
                total_size = int(r.headers.get("content-length", 0))
                with open(dest_path, "wb") as f:
                    downloaded = 0
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        done = int(50 * downloaded / total_size) if total_size else 0
                        sys.stdout.write(f"\r[{'=' * done}{' ' * (50-done)}] {downloaded/1024/1024:.2f} MB")
                        sys.stdout.flush()
            sys.stdout.write("\n")
            log.info(f"Successfully downloaded to '{dest_path}'.")
            return True
        except requests.RequestException as e:
            log.error(f"Download failed: {e}")
            if dest_path.exists():
                dest_path.unlink()
            return False

    def _unzip_archive(self, archive_path: Path, dep_info: dict, version: str):
        """Unzips an archive into a temporary extraction directory inside .temp"""
        # The final extracted content will be in a directory named after the dependency's target dir.
        # This makes moving it into place later very easy.
        final_extract_path = self.temp_dir / dep_info["target_dir_name"]
        
        # Use a nested temp directory for the raw extraction to handle different zip structures cleanly
        raw_extract_dir = self.temp_dir / f"_extract_{dep_info['target_dir_name']}"

        log.info(f"Extracting '{archive_path.name}'...")
        try:
            with zipfile.ZipFile(archive_path, "r") as zip_ref:
                zip_ref.extractall(raw_extract_dir)

            source_path_in_zip = dep_info.get("archive_path_in_zip")
            if source_path_in_zip:
                source_path_in_zip = source_path_in_zip.format(version=version)
                source_dir = raw_extract_dir / source_path_in_zip
                # Move the contents of the nested folder to our final extract path
                shutil.move(str(source_dir), str(final_extract_path))
            else:
                # Move the individual files into the final extract path
                final_extract_path.mkdir(exist_ok=True)
                for item in raw_extract_dir.iterdir():
                    shutil.move(str(item), str(final_extract_path / item.name))

            log.info(f"Extracted content for {dep_info['name']} is ready in temp directory.")
            return True
        except (zipfile.BadZipFile, OSError) as e:
            log.error(f"Extraction failed: {e}")
            return False
        finally:
            if raw_extract_dir.exists():
                shutil.rmtree(raw_extract_dir)

    def _update_version_file(self, dep_key: str, version: str, directory: Path):
        """Writes the version to the appropriate version file inside the specified directory."""
        dep_info = self.dependencies[dep_key]
        target_dir_name = dep_info['target_dir_name']
        is_shared = sum(1 for d in self.dependencies.values() if d['target_dir_name'] == target_dir_name) > 1

        if is_shared:
            versions_file_json = directory / ".versions.json"
            current_versions = {}
            if versions_file_json.exists():
                try:
                    current_versions = json.loads(versions_file_json.read_text())
                except json.JSONDecodeError: pass
            current_versions[dep_key] = version
            versions_file_json.write_text(json.dumps(current_versions, indent=4))
        else:
            (directory / ".version").write_text(version)

    def _install_dependency(self, dep_key: str, version: str):
        """Downloads and extracts a dependency into the .temp directory."""
        dep_info = self.dependencies[dep_key]
        url = dep_info["url_template"].format(version=version)
        archive_name = url.split("/")[-1]
        archive_path = self.temp_dir / archive_name

        if not self._download_file(url, archive_path): return False
        if not self._unzip_archive(archive_path, dep_info, version): return False

        archive_path.unlink()
        
        # Write the version file inside the temporary extracted directory
        temp_target_dir = self.temp_dir / dep_info["target_dir_name"]
        self._update_version_file(dep_key, version, temp_target_dir)
        return True

    def ensure_all_dependencies_installed(self):
        """BLOCKING: Ensures all dependencies are installed, fetching latest versions."""
        log.info("--- Ensuring all external dependencies are installed ---")
        deps_to_install = []
        for key, info in self.dependencies.items():
            current_versions = self.get_current_versions_for_dir(info["target_dir_name"])
            if key not in current_versions:
                deps_to_install.append(key)
        
        if not deps_to_install:
            log.info("All dependencies are already installed.")
            return True

        for key in deps_to_install:
            info = self.dependencies[key]
            log.info(f"Installing {info['name']} for the first time...")
            latest_version = self._get_latest_version(key)
            if not latest_version:
                log.critical(f"Could not get latest version for {info['name']}. Cannot proceed.")
                return False
            if not self._install_dependency(key, latest_version):
                log.critical(f"Installation failed for {info['name']}.")
                return False
        
        # After all downloads are complete, apply them from the temp directory
        log.info("Applying initial installations...")
        self.apply_pending_installs()
        return True

    def check_for_updates_async(self):
        """NON-BLOCKING: Checks for updates and downloads them to the .temp folder for next restart."""
        log.info("Starting background check for dependency updates...")
        
        updates_found = False
        for key, info in self.dependencies.items():
            target_dir_name = info['target_dir_name']
            current_versions = self.get_current_versions_for_dir(target_dir_name)
            current_v = current_versions.get(key)
            latest_v = self._get_latest_version(key)

            if not current_v or not latest_v or current_v == latest_v:
                continue

            updates_found = True
            log.info(f"UPDATE FOUND for {info['name']}: {current_v} -> {latest_v}. Downloading...")
            if not self._install_dependency(key, latest_v):
                log.error(f"Failed to download update for {info['name']}.")
        
        if updates_found:
            log.warning("Updates have been downloaded. Restart the application to apply them.")
        log.info("Background update check finished.")

    def apply_pending_installs(self):
        """Checks for and applies updates/installs from the .temp directory."""
        if not self.temp_dir.is_dir() or not any(self.temp_dir.iterdir()):
            return

        log.warning("Pending dependency installations found. Applying now...")
        for item in self.temp_dir.iterdir():
            if not item.is_dir(): continue
            
            target_dir_name = item.name
            log.info(f"Applying changes for '{target_dir_name}'...")
            
            # For shared directories, we need to merge, not replace.
            is_shared = sum(1 for d in self.dependencies.values() if d['target_dir_name'] == target_dir_name) > 1

            if is_shared:
                # Merge the contents of the temp dir into the final dir
                final_dir = self.external_dir / target_dir_name
                final_dir.mkdir(exist_ok=True)
                shutil.copytree(str(item), str(final_dir), dirs_exist_ok=True)
            else:
                # For non-shared dirs, we can do a full archive and replace
                self._archive_current_version(target_dir_name)
                final_dir = self.external_dir / target_dir_name
                shutil.move(str(item), str(final_dir))
            
            log.info(f"Changes for '{target_dir_name}' applied successfully.")

        # Clean up the temp directory
        shutil.rmtree(self.temp_dir)
        self.temp_dir.mkdir(exist_ok=True) # Recreate for next run
        log.warning("All pending installations applied.")

    def _archive_current_version(self, target_dir_name: str):
        """Moves the current version of a dependency directory to the .old directory."""
        target_dir = self.external_dir / target_dir_name
        if not target_dir.is_dir():
            return True

        current_versions = self.get_current_versions_for_dir(target_dir_name)
        if not current_versions:
            archive_name = f"{target_dir_name}_unknown_{int(time.time())}"
        else:
            version_str = "_".join(f"{k}-{v}" for k, v in sorted(current_versions.items()))
            archive_name = f"{target_dir_name}_{version_str}"

        archive_path = self.old_dir / archive_name
        if archive_path.exists():
            shutil.rmtree(archive_path)

        log.info(f"Archiving current '{target_dir_name}' to '{archive_path}'.")
        try:
            shutil.move(str(target_dir), str(archive_path))
        except OSError as e:
            log.error(f"Failed to archive current version: {e}")
            return False
        return True

    def interactive_recover(self, dep_key: str):
        """Handles the CLI recovery process for a dependency."""
        if dep_key not in self.dependencies:
            print(f"Error: Unknown dependency '{dep_key}'. Available: {', '.join(self.dependencies.keys())}")
            return

        target_dir_name = self.dependencies[dep_key]['target_dir_name']
        available_archives = sorted([
            p.name for p in self.old_dir.iterdir() if p.name.startswith(f"{target_dir_name}_")
        ], reverse=True)

        if not available_archives:
            print(f"No archived versions found for '{target_dir_name}'."); return

        print(f"\nAvailable archived versions for '{target_dir_name}':")
        for i, version_name in enumerate(available_archives):
            print(f"  [{i+1}] {version_name}")

        try:
            choice = int(input("\nEnter the number of the version to recover (0 to cancel): "))
            if choice == 0: print("Recovery cancelled."); return
            if not (1 <= choice <= len(available_archives)): raise ValueError
        except ValueError:
            print("Invalid choice."); return

        selected_archive = available_archives[choice - 1]
        print(f"\nYou have selected to recover '{selected_archive}'.")
        
        confirm1 = input("This will replace the currently installed version. Are you sure? (y/N): ").lower()
        if confirm1 != 'y': print("Recovery cancelled."); return

        confirm2 = input("Please type 'recover' to confirm this irreversible action: ")
        if confirm2 != 'recover': print("Confirmation failed. Recovery cancelled."); return

        print("\nProceeding with recovery...")
        if not self._archive_current_version(target_dir_name):
             print("ERROR: Failed to archive the current version. Aborting recovery."); return

        archive_path = self.old_dir / selected_archive
        target_path = self.external_dir / target_dir_name
        try:
            shutil.move(str(archive_path), str(target_path))
            print(f"Successfully recovered '{selected_archive}' as the active version for '{target_dir_name}'.")
        except OSError as e:
            print(f"ERROR: Failed to move recovered version into place: {e}")
