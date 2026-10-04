# iGround

Move **everything** off iCloud onto an external SSD (iCloud Drive, the apps' iCloud folders, your Photos library, Messages, and your iPhone backups, including WhatsApp) so you can **downgrade your iCloud storage plan** without losing anything.

iGround downloads every file that is only in the cloud, copies it, checks each copy with a checksum, and keeps track of what's done, so you can stop and resume at any point. When it's finished, `iground ready` checks everything and tells you whether it's safe to downgrade. **iGround never deletes anything from iCloud.** You decide when to do that, after the report says READY.

## What gets moved, and how

| Your data | On the SSD | How |
|---|---|---|
| iCloud Drive (incl. Desktop & Documents) | `iCloud Drive/` | Downloads cloud-only files with macOS's `brctl` tool, then copies and verifies them |
| App iCloud folders (Pages, Numbers, Keynote, Preview, third-party apps…) | `iCloud App Folders/<app>/` | Same as iCloud Drive |
| iCloud Photos | `Photos/YYYY/MM/…` + `albums.json` | Photos.app exports the **full-resolution original** of every photo and video. It downloads originals from iCloud when the Mac only has optimised previews. Live Photos keep both their photo and video files. Album membership and favourites are saved to `albums.json`. |
| Messages | `Messages/` | Copies the Mac's message database and attachments |
| iPhone/iPad backups, **including WhatsApp** | `iPhone Backups/` | Points Finder's backup folder at the SSD (see below) |

### Why WhatsApp works differently

WhatsApp's iCloud backup is sealed: no Mac, website or third-party tool can download it, and only WhatsApp on a phone can restore it. iCloud device backups work the same way. What *can* be kept off iCloud is a **local backup of your iPhone**, which includes your WhatsApp chats and media. `iground iphone-backup` makes Finder save those backups straight to the SSD. That one step replaces iCloud Backup (often one of the largest items in iCloud) and keeps WhatsApp safe.

## Install

Needs macOS and Python 3.9 or newer (the system `python3` from the Xcode Command Line Tools works). There are no other dependencies.

```sh
git clone https://github.com/snesboola/iGround.git
cd iGround
python3 -m pip install --user .     # installs the `iground` command
# or run without installing:  PYTHONPATH=src python3 -m iground --help
```

**Permissions.** Give your terminal app **Full Disk Access** (System Settings → Privacy & Security), which it needs to read Messages and iPhone backups. The first Photos export asks to let your terminal **control Photos**; click Allow.

## The full move, step by step

Use one folder on the SSD for everything, e.g. `/Volumes/MySSD/iCloud`.

```sh
# 0. See what you have, how much must be downloaded, and whether the SSD is big enough
iground audit /Volumes/MySSD

# 1. Preview, then copy everything. This can take hours or days for large libraries:
#    Ctrl-C at any time and run the same command again to resume.
iground migrate /Volumes/MySSD/iCloud --dry-run
caffeinate -i iground migrate /Volumes/MySSD/iCloud

# 2. Make Finder back up your iPhone (incl. WhatsApp) to the SSD, then back it up in Finder
iground iphone-backup /Volumes/MySSD/iCloud

# 3. Check that everything is safely on the SSD
iground ready /Volumes/MySSD/iCloud
```

`ready` prints a checklist and ends with **READY** or **NOT READY**:

```
[OK]   iCloud Drive: 12,408 files on the SSD
[OK]   App folder com.apple.Pages: 37 files on the SSD
[TODO] Photos: 212 items of 48,310 not exported yet
         → iground migrate "/Volumes/MySSD/iCloud" --only photos
[OK]   iPhone / iPad backup (incl. WhatsApp): Sam's iPhone — 2026-10-03 (encrypted)
[NOTE] WhatsApp: ...
NOT READY: finish the [TODO] items before deleting anything from iCloud.
```

### 4. After READY: actually free the iCloud space

These steps are done by hand on purpose. Deleting from iCloud deletes on **all** your devices.

1. **Keep a second copy** of the SSD if you can (another drive or Time Machine). An SSD on its own is a single point of failure.
2. **iCloud Backup.** On the iPhone: Settings → [your name] → iCloud → iCloud Backup → off. Then delete the old backup under *Manage Account Storage → Backups*.
3. **Photos.** Delete the photos from iCloud Photos (or turn off iCloud Photos on every device and delete the iCloud copy under *Manage Account Storage → Photos*). Then empty *Recently Deleted*.
4. **iCloud Drive.** Delete what you no longer need in iCloud, and empty *Recently Deleted* in Finder or on iCloud.com.
5. **WhatsApp.** Turn off "Include Videos" in Chat Backup to keep its iCloud backup small, or turn the iCloud backup off and rely on the iPhone backup on the SSD.
6. Check *Manage Account Storage*, then downgrade the plan.

Mail, Notes, Contacts, Calendars, Reminders, Keychain and Shared Albums stay in iCloud. They're usually small.

## Command reference

| Command | Purpose |
|---|---|
| `iground audit [SSD]` | Sizes per source, how much is cloud-only, number of photos, existing iPhone backups, SSD free space |
| `iground migrate SSD` | Copy everything. Options: `--only drive,apps,photos,messages`, `--dry-run`, `--workers N`, `--evict-after`, `--exclude GLOB`, `--download-timeout S`, `--photos-batch N`, `--no-verify`, `--force`, `-v` |
| `iground iphone-backup SSD` | Move existing iPhone backups to the SSD and redirect Finder there. `--undo` reverts this. |
| `iground ready SSD` | Readiness checklist. Exits 0 only when everything is on the SSD and there's a backup from the last 14 days. |
| `iground verify SSD` | Re-reads every file on the SSD and checks its checksum, and lists anything not yet migrated |
| `iground status SSD [--failed]` | Progress and failure reasons |

`--evict-after` removes each iCloud Drive file's local download from the Mac once it has been copied and verified. Use it when the Mac's own disk is too small to hold everything during the move.

## Notes and limits

- **Format the SSD as APFS.** exFAT can't store symlinks and some metadata.
- **Photos exports originals.** Edits made in Photos (crops, filters) are not applied to the exported files. People, Memories and similar Photos-only data are not exported; albums and favourites are listed in `albums.json`.
- **Messages:** quit Messages before migrating for a consistent copy of the database. If Messages in iCloud is set to optimise storage, very old attachments that only exist in iCloud can't be fetched by any tool.
- Re-running `migrate` only copies what is new or changed, so you can run it again right before you downgrade.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Everything macOS-specific is behind small classes: `ICloudClient` (`brctl`), `PhotosClient` (`osascript`), and `Locations` (home-folder paths, which can be overridden with `IGROUND_HOME`). That's why the tests run on Linux with fakes.
