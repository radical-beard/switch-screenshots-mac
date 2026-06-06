#!/usr/bin/env python3
"""
get-switch-screenshots.py — Copy screenshots & video clips off a Nintendo Switch 2
(and the original Switch) over USB into ~/switch-screenshots/, with dedup so it is
always safe to re-run.

Requirements: macOS, Python 3.7+, and gphoto2. If gphoto2 is missing, the script
offers to `brew install` it for you. No Python packages to install (stdlib only).

How the Switch exposes screenshots
----------------------------------
On the console:  Settings -> Data Management -> Manage Screenshots and Videos
                 -> "Copy to PC over USB"
Connect the USB-C cable to the *bottom* port of the console directly (not the top
port, not through the dock). It then presents itself as an MTP device with a
Nintendo vendor extension.

macOS has no native MTP support, so we drive it with `gphoto2` (libgphoto2's
PTP/MTP driver), which is the only tool that reliably talks to it here.

macOS gotcha: the system service `ptpcamerad` grabs the USB interface the moment
a still-image/PTP device appears, which blocks gphoto2 from claiming it. We kill
it once right before each gphoto2 session (it is owned by the current user, so no
sudo needed). We do NOT kill it in a tight loop — that causes USB re-enumeration
churn that corrupts transfers.

Storage layout
--------------
  ~/switch-screenshots/                  <- the images & videos (flat, original names)
  ~/.local/share/switch-screenshots/     <- manifest.json (dedup state) + staging

Dedup
-----
Switch filenames are globally unique (timestamp-based), so we skip anything already
recorded in the manifest or already present in the destination. As a content-level
safety net we also sha256 every file and never store the same content twice, even
under a different name.

Usage
-----
  python3 get-switch-screenshots.py            # transfer everything new
  python3 get-switch-screenshots.py --list     # just list what's on the console
  python3 get-switch-screenshots.py --dry-run  # show what would transfer, download nothing
  python3 get-switch-screenshots.py --limit 5  # transfer at most 5 new files (testing)
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- configuration
DEST_DIR = Path.home() / "switch-screenshots"
DATA_DIR = Path.home() / ".local" / "share" / "switch-screenshots"
MANIFEST = DATA_DIR / "manifest.json"
STAGING = DATA_DIR / "staging"

_IS_MACOS = platform.system() == "Darwin"
_BREW_PREFIXES = ("/opt/homebrew", "/usr/local")  # Apple Silicon, Intel
GPHOTO2 = shutil.which("gphoto2")  # validated/resolved by ensure_gphoto2()
PTPCAMERAD = "/usr/libexec/ptpcamerad"

# Download a handful of files per gphoto2 session. Small batches keep each session
# short (more robust against interruptions) and make the whole run resumable: the
# manifest is flushed after every batch, so a re-run only fetches what's left.
BATCH_SIZE = 8
# Per-session wall-clock ceiling (seconds). Videos are ~35 MB; 8 of them is well
# under this.
SESSION_TIMEOUT = 600
# Retries per batch if a session fails (transient USB / ptpcamerad races).
BATCH_RETRIES = 3

FILE_LINE = re.compile(r"^#(\d+)\s+(\S+)\s+(.*)$")
FOLDER_LINE = re.compile(r"^There (?:is|are) .*? in folder '(.+)'\.\s*$")
TS_PREFIX = re.compile(r"^(\d{14})")  # YYYYMMDDhhmmss prefix on Switch filenames


# ----------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_dir(p: Path, label: str) -> None:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        sys.exit(f"Cannot create {label} at {p}: {e}\n"
                 f"Check that you own {p.parent} and that it is writable.")


def _resolve(name: str) -> str | None:
    """Find an executable on PATH, falling back to the Homebrew prefixes."""
    found = shutil.which(name)
    if found:
        return found
    for pref in _BREW_PREFIXES:
        cand = Path(pref) / "bin" / name
        if cand.exists():
            return str(cand)
    return None


def ensure_gphoto2() -> None:
    """Make sure gphoto2 is available, offering to install it via Homebrew."""
    global GPHOTO2
    found = _resolve("gphoto2")
    if found:
        GPHOTO2 = found
        return
    brew = _resolve("brew")
    if not brew:
        sys.exit(
            "gphoto2 is required but not installed, and Homebrew wasn't found.\n"
            "Install Homebrew first (https://brew.sh), then run:\n"
            "  brew install gphoto2\n"
            "and re-run this script.")
    log("gphoto2 isn't installed — it's needed to talk to the Switch over USB.")
    if sys.stdin.isatty():
        ans = input("Install it now with `brew install gphoto2`? [Y/n] ").strip().lower()
    else:
        ans = "n"  # non-interactive: don't hang on input()
    if ans not in ("", "y", "yes"):
        sys.exit("Can't continue without gphoto2. Install it with:  brew install gphoto2")
    log("Running `brew install gphoto2` (this can take a few minutes) ...")
    if subprocess.run([brew, "install", "gphoto2"]).returncode != 0:
        sys.exit("`brew install gphoto2` failed. Fix the error above and re-run.")
    found = _resolve("gphoto2")
    if not found:
        sys.exit("gphoto2 still not found after install; try `brew doctor`.")
    GPHOTO2 = found


def quiet_ptpcamerad() -> None:
    """Stop macOS's ptpcamerad so gphoto2 can claim the USB interface.

    Called once right before each gphoto2 invocation. ptpcamerad will respawn,
    but once gphoto2 holds interface 0 a respawn can't reclaim it. We deliberately
    do NOT hammer it in a loop (that re-enumerates the bus and corrupts transfers).
    No-op off macOS.
    """
    if not _IS_MACOS:
        return
    try:
        subprocess.run(["pkill", "-9", "-f", PTPCAMERAD],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass  # pkill absent (minimal environment) — nothing to suppress


def run_gphoto(args: list[str], *, cwd: Path | None = None,
               timeout: int = 120) -> subprocess.CompletedProcess:
    quiet_ptpcamerad()
    return subprocess.run(
        [GPHOTO2, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, timeout=timeout,
    )


def detect_device(retries: int = 6) -> bool:
    """Return True once gphoto2 can see the Switch on USB."""
    for _ in range(retries):
        try:
            out = run_gphoto(["--auto-detect"], timeout=30).stdout
        except (subprocess.TimeoutExpired, OSError):
            out = ""
        if re.search(r"nintendo|switch", out, re.I):
            return True
        time.sleep(1.0)
    return False


# ----------------------------------------------------------------- inventory
class Item:
    __slots__ = ("folder", "index", "name", "size_kb", "mtime")

    def __init__(self, folder, index, name, size_kb, mtime):
        self.folder = folder
        self.index = index
        self.name = name
        self.size_kb = size_kb
        self.mtime = mtime

    @property
    def game(self) -> str:
        return self.folder.rstrip("/").split("/")[-1]


def _parse_mtime(name: str, rest: str) -> int:
    """Capture time as a unix timestamp: prefer gphoto2's trailing value, else the
    YYYYMMDDhhmmss embedded in the Switch filename."""
    tm = re.search(r"(\d{6,})\s*$", rest)
    if tm:
        return int(tm.group(1))
    m = TS_PREFIX.match(name)
    if m:
        try:
            return int(datetime.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").timestamp())
        except ValueError:
            pass
    return 0


def list_inventory() -> list[Item]:
    """Parse `gphoto2 --list-files` into Items (folder, index, name, size, mtime)."""
    proc = run_gphoto(["--list-files"], timeout=120)
    if proc.returncode != 0 and "There" not in proc.stdout:
        raise RuntimeError(f"gphoto2 --list-files failed:\n{proc.stdout}\n{proc.stderr}")
    items: list[Item] = []
    folder = "/"
    folder_idx = 0  # per-folder 1-based counter
    for line in proc.stdout.splitlines():
        fm = FOLDER_LINE.match(line)
        if fm:
            folder = fm.group(1)
            folder_idx = 0
            continue
        m = FILE_LINE.match(line)
        if not m:
            continue
        # IMPORTANT: `gphoto2 --list-files` numbers files globally across the whole
        # tree (#1..#N), but `--folder F --get-file n` expects n to be 1-based WITHIN
        # folder F. So we renumber per folder by listing order and ignore the global #.
        folder_idx += 1
        name = m.group(2)
        rest = m.group(3)
        size_kb = 0
        sm = re.search(r"(\d+)\s*KB", rest)
        if sm:
            size_kb = int(sm.group(1))
        items.append(Item(folder, folder_idx, name, size_kb, _parse_mtime(name, rest)))
    return items


# ------------------------------------------------------------------- manifest
def load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text())
        except json.JSONDecodeError:
            log(f"warning: {MANIFEST} unreadable, starting fresh")
    return {"version": 1, "transferred": {}, "hashes": {}}


def save_manifest(man: dict) -> None:
    ensure_dir(DATA_DIR, "data folder")
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(man, indent=2, sort_keys=True))
    tmp.replace(MANIFEST)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unique_dest(name: str) -> Path:
    """Destination path for `name`, disambiguating only on a real name clash."""
    dest = DEST_DIR / name
    if not dest.exists():
        return dest
    stem, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = DEST_DIR / f"{stem}_{i}{ext}"
        if not cand.exists():
            return cand
        i += 1


# -------------------------------------------------------------------- download
def download_folder_batch(folder: str, items: list[Item], *, clean: bool = True) -> dict[str, Path]:
    """Download a batch of files from ONE folder in a single gphoto2 session.

    Returns {name: staged_path}. We deliberately do not chain multiple --folder
    switches in one invocation — gphoto2 resolves indices against the wrong folder
    when you do. One folder per session keeps index resolution correct. We also use
    repeated `--get-file N` flags (a comma list like `4,5` is rejected by gphoto2,
    and a range `4-5` can't express an arbitrary subset).

    `clean` wipes the staging dir first; the retry path passes clean=False so files
    already fetched on an earlier attempt are kept (not re-downloaded or orphaned).
    """
    STAGING.mkdir(parents=True, exist_ok=True)
    if clean:
        for f in STAGING.iterdir():
            f.unlink()

    args: list[str] = ["--force-overwrite", "--filename", "%f.%C", "--folder", folder]
    for it in sorted(items, key=lambda x: x.index):
        args += ["--get-file", str(it.index)]

    proc = run_gphoto(args, cwd=STAGING, timeout=SESSION_TIMEOUT)
    staged = {p.name: p for p in STAGING.iterdir() if p.is_file()}
    if not staged and proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-2:]
        raise RuntimeError("download failed: " + " | ".join(tail))
    return staged


def commit_staged(staged: dict[str, Path], batch: list[Item], man: dict) -> tuple[int, int]:
    """Move staged files into DEST with content-dedup. Returns (new, dup)."""
    new = dup = 0
    by_name = {it.name: it for it in batch}
    for name, path in staged.items():
        if not path.exists():  # defensive: a retry may have re-staged things
            continue
        it = by_name.get(name)
        if it is None:
            log(f"  note: staged '{name}' wasn't in the listing; saving without metadata")
        digest = sha256_file(path)
        if digest in man["hashes"]:
            dup += 1
            man["transferred"][name] = {
                "sha256": digest, "size_kb": it.size_kb if it else 0,
                "mtime": it.mtime if it else 0, "game": it.game if it else "",
                "dest": man["hashes"][digest], "duplicate_of": man["hashes"][digest],
            }
            path.unlink()
            continue
        dest = unique_dest(name)
        shutil.move(str(path), str(dest))
        if it and it.mtime:
            os.utime(dest, (it.mtime, it.mtime))
        man["hashes"][digest] = dest.name
        man["transferred"][name] = {
            "sha256": digest, "size_kb": it.size_kb if it else 0,
            "mtime": it.mtime if it else 0, "game": it.game if it else "",
            "dest": dest.name,
        }
        new += 1
    return new, dup


# ------------------------------------------------------------------------ main
SETUP_HELP = (
    "On the console:\n"
    "  Settings -> Data Management -> Manage Screenshots and Videos\n"
    "  -> 'Copy to PC over USB'   (leave that screen showing)\n"
    "Plug the cable into the *bottom* USB-C port of the console "
    "(not the top, not the dock)."
)


def main() -> int:
    if not _IS_MACOS:
        sys.exit("This script is macOS-only (it relies on gphoto2 + ptpcamerad handling).\n"
                 "On Linux, plain `gphoto2 --get-all-files` usually works without it.")
    if sys.version_info < (3, 7):
        sys.exit("This script needs Python 3.7+ (you have %d.%d)." % sys.version_info[:2])

    ap = argparse.ArgumentParser(
        description="Copy Nintendo Switch / Switch 2 screenshots and clips over USB "
                    "into ~/switch-screenshots/.",
        epilog=SETUP_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list files on the console and exit")
    ap.add_argument("--dry-run", action="store_true", help="show what would transfer; download nothing")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="transfer at most N new files (0 = all)")
    args = ap.parse_args()
    if args.limit < 0:
        ap.error("--limit must be >= 0")

    ensure_gphoto2()
    ensure_dir(DEST_DIR, "screenshots folder")
    ensure_dir(DATA_DIR, "data folder")

    log("Looking for the Switch over USB ...")
    if not detect_device():
        log("")
        log("Could not find the Switch. Please check:")
        log("  1. " + SETUP_HELP.replace("\n", "\n     "))
        log("  2. The cable supports data (not charge-only).")
        log("If it was working and stopped, unplug and replug the cable to reset it.")
        return 2

    log("Found it. Reading the album index ...")
    try:
        inventory = list_inventory()
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        log(f"Failed to list files: {e}")
        return 3

    if not inventory and not args.list:
        log("")
        log("The Switch is connected but no screenshots/videos are visible.")
        log("This usually means it isn't in transfer mode yet.")
        log(SETUP_HELP)
        log("Then re-run this script.")
        return 2

    log(f"Console has {len(inventory)} files across "
        f"{len({i.folder for i in inventory})} game folders.")

    if args.list:
        for it in inventory:
            log(f"  [{it.game}] {it.name}  ({it.size_kb} KB)")
        return 0

    man = load_manifest()
    # Skip anything we've already recorded OR that already sits in the destination.
    # The on-disk check keeps re-runs safe even if the manifest is ever lost.
    have = set(man["transferred"]) | {p.name for p in DEST_DIR.iterdir() if p.is_file()}
    already = sum(1 for it in inventory if it.name in have)
    todo = [it for it in inventory if it.name not in have]
    if args.limit:
        todo = todo[: args.limit]

    log(f"{already} already transferred, {len(todo)} new to fetch.")
    if args.dry_run:
        for it in todo:
            log(f"  would fetch [{it.game}] {it.name}  ({it.size_kb} KB)")
        return 0
    if not todo:
        log("Nothing new. ~/switch-screenshots/ is up to date.")
        return 0

    # Group by folder (each folder = its own gphoto2 session), then batch within it.
    by_folder: dict[str, list[Item]] = {}
    for it in todo:
        by_folder.setdefault(it.folder, []).append(it)

    total_new = total_dup = 0
    done_count = 0
    for folder, fitems in by_folder.items():
        game = fitems[0].game
        batches = [fitems[i:i + BATCH_SIZE] for i in range(0, len(fitems), BATCH_SIZE)]
        for batch in batches:
            done_count += len(batch)
            log(f"[{game}] fetching {len(batch)} ({done_count}/{len(todo)} overall) ...")
            staged: dict[str, Path] = {}
            for attempt in range(1, BATCH_RETRIES + 1):
                remaining = [it for it in batch if it.name not in staged]
                if not remaining:
                    break
                try:
                    # clean staging only on the first attempt so already-fetched
                    # files survive a partial-batch retry.
                    staged.update(download_folder_batch(folder, remaining, clean=(attempt == 1)))
                    if len(staged) >= len(batch):
                        break
                    log(f"  got {len(staged)}/{len(batch)}, retrying remainder (try {attempt})")
                    time.sleep(1.0)
                except (RuntimeError, subprocess.TimeoutExpired) as e:
                    log(f"  attempt {attempt} failed: {e}")
                    time.sleep(1.5)
            if not staged:
                log("  no files this batch; will retry on next run")
                continue
            new, dup = commit_staged(staged, batch, man)
            total_new += new
            total_dup += dup
            save_manifest(man)
            log(f"  +{new} new, {dup} duplicate(s); progress saved.")

    log("")
    log(f"Done. {total_new} new file(s) added, {total_dup} duplicate(s) skipped.")
    log(f"Screenshots are in {DEST_DIR}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\nInterrupted. Progress was saved; re-run to continue.")
        sys.exit(130)
