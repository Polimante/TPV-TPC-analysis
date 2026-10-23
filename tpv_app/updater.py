"""
Self-update via GitHub Releases.

Each release on GitHub (tag "vX.Y.Z") carries the built TPV-TPC.exe as an
asset. The app compares the latest release tag with version.__version__ and,
when running as the packaged .exe, downloads the new exe and swaps it in:

    TPV-TPC.exe      → TPV-TPC.exe.old   (Windows allows renaming a running exe)
    TPV-TPC.exe.new  → TPV-TPC.exe
    launch the new exe, quit; the .old file is deleted on the next start.

When running from source (python main.py) updates are only reported, with a
link to the release page — use "git pull" there instead.
"""
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

from version import __version__, GITHUB_REPO

logger = logging.getLogger(__name__)

API_LATEST = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
RELEASES_PAGE = f'https://github.com/{GITHUB_REPO}/releases/latest'
_TIMEOUT_S = 10


@dataclass
class Release:
    version: str        # e.g. '1.2.0'
    notes: str
    page_url: str
    exe_url: str | None  # None if the release has no .exe asset
    exe_size: int


def is_frozen() -> bool:
    """True when running as the PyInstaller-built exe."""
    return bool(getattr(sys, 'frozen', False))


def _parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r'\d+', text)
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def fetch_latest() -> Release:
    """Query GitHub for the latest release. Raises on network/API errors."""
    req = urllib.request.Request(API_LATEST, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': f'TPV-TPC/{__version__}',
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        data = json.load(resp)

    exe = next((a for a in data.get('assets', [])
                if a.get('name', '').lower().endswith('.exe')), None)
    return Release(
        version=data.get('tag_name', '').lstrip('vV'),
        notes=data.get('body') or '',
        page_url=data.get('html_url') or RELEASES_PAGE,
        exe_url=exe['browser_download_url'] if exe else None,
        exe_size=exe.get('size', 0) if exe else 0,
    )


def is_newer(release: Release) -> bool:
    return _parse_version(release.version) > _parse_version(__version__)


def download_exe(release: Release, progress_cb=None) -> str:
    """
    Download the release exe next to the running exe (as <exe>.new) so the
    final swap is a same-volume rename. progress_cb(done_bytes, total_bytes)
    is called during the download. Returns the path of the downloaded file.
    """
    if not release.exe_url:
        raise RuntimeError("This release has no .exe file attached.")

    target = sys.executable + '.new'
    req = urllib.request.Request(release.exe_url,
                                 headers={'User-Agent': f'TPV-TPC/{__version__}'})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp, open(target, 'wb') as f:
        total = int(resp.headers.get('Content-Length') or release.exe_size or 0)
        done = 0
        while chunk := resp.read(256 * 1024):
            f.write(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)

    if total and os.path.getsize(target) != total:
        os.remove(target)
        raise RuntimeError("Download incomplete — please try again.")
    logger.info("Update downloaded: %s (%d bytes)", target, done)
    return target


def install_and_restart(new_exe: str) -> None:
    """Swap the running exe for new_exe and launch it. Caller must then quit."""
    current = sys.executable
    old = current + '.old'
    if os.path.exists(old):
        os.remove(old)
    os.replace(current, old)
    try:
        os.replace(new_exe, current)
    except Exception:
        os.replace(old, current)   # roll back so the app still starts next time
        raise

    # Without this the new onefile process would reuse (and lose) our
    # temporary extraction folder when this process exits.
    env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT='1')
    subprocess.Popen([current], env=env, cwd=os.path.dirname(current),
                     close_fds=True)
    logger.info("Update installed — relaunching %s", current)


def cleanup_previous_update() -> None:
    """Remove the exe left behind by the previous update, if any."""
    if not is_frozen():
        return
    for leftover in (sys.executable + '.old', sys.executable + '.new'):
        try:
            if os.path.exists(leftover):
                os.remove(leftover)
        except OSError:
            pass   # still locked — try again next start
