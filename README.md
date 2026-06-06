# switch-screenshots-mac

Copy your **Nintendo Switch 2** (and original Switch) screenshots and video clips to your Mac over a USB cable — one command, safe to re-run.

macOS has no native MTP support, so there's no Finder mount and no Image Capture for the Switch. This is a single, dependency-free Python script that pulls everything off the console anyway and drops it into `~/switch-screenshots/`.

## What it does

Pulls every screenshot and video clip off your Switch into `~/switch-screenshots/` as a flat folder of files with their original names. It remembers what it has already copied, so you can run it again any time and it only fetches what's new.

- Single Python file, **standard library only** — no `pip install`, no virtualenv.
- Original filenames, flat layout, capture timestamps preserved.
- Built-in dedup: re-running is always safe.
- Resumable: progress is saved after every small batch.

## Requirements

- **macOS** (Apple Silicon or Intel).
- **`gphoto2`** — if it's missing, the script offers to `brew install` it for you. Or install it yourself:
  ```bash
  brew install gphoto2
  ```
  (needs [Homebrew](https://brew.sh))
- **Python 3.7+** — the `python3` that ships with macOS is fine. No packages to install.
- A **USB-C data cable** (charge-only cables won't work).

## Quick start

```bash
# 1. Grab the script
curl -fLO https://raw.githubusercontent.com/radical-beard/switch-screenshots-mac/main/get-switch-screenshots.py

# 2. On the console, open the "Copy to PC over USB" screen (see below),
#    plug the cable into the BOTTOM USB-C port, then run:
python3 get-switch-screenshots.py
```

That's it. If `gphoto2` isn't installed yet, the script asks to install it for you. Your screenshots and clips land in `~/switch-screenshots/`.

## On the console (do this first)

1. Open **Settings → Data Management → Manage Screenshots and Videos → Copy to PC over USB**.
2. Leave that screen showing — the console only exposes its album while it's open.
3. Plug the USB-C cable into the **bottom port of the console** (the one you charge from in handheld mode).
   - **Not** the top port.
   - **Not** through the dock.

## Usage

```bash
python3 get-switch-screenshots.py            # transfer everything new
python3 get-switch-screenshots.py --list     # just list what's on the console, transfer nothing
python3 get-switch-screenshots.py --dry-run  # show what would transfer, download nothing
python3 get-switch-screenshots.py --limit 5  # transfer at most 5 new files (handy for a first test)
```

| Flag | Effect |
|------|--------|
| `--list` | Print every file on the console (grouped by game) and exit. |
| `--dry-run` | Show exactly what *would* be fetched without downloading anything. |
| `--limit N` | Stop after `N` new files. Great for a quick sanity check. |

## How dedup works (safe to re-run)

Switch filenames are timestamp-based and globally unique, so the script skips anything it has seen before. It checks three things, so a re-run never duplicates work:

1. A **manifest** at `~/.local/share/switch-screenshots/manifest.json` recording everything transferred.
2. An **on-disk name check** against `~/switch-screenshots/`, so re-runs stay correct even if the manifest is ever deleted.
3. A **SHA-256 content hash** of every file, so identical content is never stored twice — even under a different name.

The manifest is flushed after each small batch, so an interrupted run (or a yanked cable) just picks up where it left off next time.

## Where files go

| Path | Contents |
|------|----------|
| `~/switch-screenshots/` | Your screenshots and video clips (flat, original names). |
| `~/.local/share/switch-screenshots/manifest.json` | Dedup state. |
| `~/.local/share/switch-screenshots/staging/` | Temporary download staging (auto-managed). |

## Troubleshooting

**"Could not find the Switch."**
- Make sure the **Copy to PC over USB** screen is open on the console.
- Use the **bottom** USB-C port, directly — not the top port, not the dock.
- Confirm your cable carries **data**, not just power. Swap cables if unsure.
- Sanity check: `gphoto2 --auto-detect` should list a Nintendo/Switch device while the copy screen is open.

**It worked, then got stuck mid-transfer.**
- **Unplug and replug the cable** to reset the connection, then re-run. Thanks to dedup it resumes cleanly and won't re-copy finished files.

**"The Switch is connected but no screenshots/videos are visible."**
- The console is plugged in but not in transfer mode — open the **Copy to PC over USB** screen and re-run.

## How it works under the hood (and why it's not trivial)

The Switch presents itself as an **MTP device** (PTP with a Nintendo vendor extension). On macOS that's surprisingly hard to deal with:

- **macOS has no native MTP support.** There's no Finder mount and no Image Capture support — Apple's `ImageCaptureCore` framework sees **zero cameras** when the Switch is plugged in.
- So the script drives **`gphoto2`** (Homebrew's CLI front-end to `libgphoto2`'s PTP/MTP driver), the one tool that reliably talks to the console here.
- macOS's **`ptpcamerad`** service grabs the USB interface the moment a still-image device appears, blocking `gphoto2` from claiming it. The script stops `ptpcamerad` **once** right before each session (it's user-owned, so no `sudo`). It deliberately does **not** kill it in a loop — that re-enumerates the USB bus and corrupts transfers.
- It runs **one `gphoto2` session per folder**, using repeated `--get-file N` flags with **per-folder, 1-based indices**. (`gphoto2 --list-files` numbers files globally across the whole tree, but `--folder F --get-file n` expects `n` relative to that folder — a subtle mismatch the script handles for you.)

The result is a transfer that "just works" from a single command, hiding a fair amount of macOS USB plumbing.

## License

MIT — see [LICENSE](LICENSE).
