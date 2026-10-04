# iGround

**Move everything from iCloud to your own SSD, so you can switch to a smaller (cheaper) iCloud plan.**

Your photos, iCloud Drive, app documents, messages and iPhone backups (with WhatsApp) go into one tidy, dated folder on your SSD:

```
MySSD/
└── iCloud Backup 2026-10-04/
    ├── Photos/
    │   ├── 2023/
    │   │   ├── 01 January/      IMG_0300.HEIC, IMG_0300.MOV, …
    │   │   └── 02 February/
    │   ├── 2024/ …
    │   ├── Albums/              Holiday/, Mum's 60th/, …
    │   └── Favourites/
    ├── iCloud Drive/            exactly as in iCloud Drive (incl. Desktop & Documents)
    ├── App Documents/           Pages/, Numbers/, Keynote/, …
    ├── Messages/
    ├── iPhone Backups/          full iPhone backups, including WhatsApp chats
    └── About this backup.txt    what's inside, and how to free up iCloud space
```

To see your photos, open **Photos**, then the year, then the month. Albums and Favourites are right there too. They don't take up extra space: each photo is stored once and appears in its albums like a shortcut. Every photo and video is the **full-quality original**, downloaded from iCloud if your Mac only had a preview.

Nothing is ever deleted from iCloud. iGround copies, checks every file, and tells you when it's safe to downgrade.

## How to use it

1. **Download iGround**: the green **Code** button on GitHub → *Download ZIP*, then open the ZIP.
2. **Plug in your SSD.**
3. **Double-click `iGround.command`.** The first time, macOS may say it's from an unidentified developer. If so, right-click the file → **Open** → **Open**.
4. Answer a few questions:

```
Step 1 of 4 · Choose your SSD
   Use “MySSD” (1.8 TB free)? [Y/n]

Step 2 of 4 · What's in your iCloud
   • Photos           48,310 photos & videos
   • iCloud Drive     12,408 files, 120.4 GB
   • Messages         3,112 files, 3.4 GB
   • App documents    1.2 GB
   Everything goes into a new folder: “iCloud Backup 2026-10-04”.
   Start now? [Y/n]

Step 3 of 4 · Copying to “iCloud Backup 2026-10-04”
  [1/5] Photos
      ███████████░░░░░░░░░░░░░  46%  22,410/48,310  about 3h 10m left

Step 4 of 4 · iPhone & WhatsApp
   Save your iPhone backups on this SSD from now on? [Y/n]

Result
   ✓ Photos: all 48,310 photos & videos copied
   ✓ iCloud Drive: 12,408 files copied
   ✗ iPhone backup (incl. WhatsApp): no iPhone backup on the SSD yet
       → Connect your iPhone, open Finder, select it and click 'Back Up Now'
```

5. **Back up your iPhone** to the SSD in Finder, as iGround describes. Then **double-click iGround again**. When every line shows ✓, open `About this backup.txt` for the steps to free up iCloud space and downgrade.

**Good to know**
- The first run can take hours, because every photo is downloaded in full quality. The Mac is kept awake while it works. You can close the window at any time; the next run picks up where it stopped.
- Run iGround whenever you like to bring the backup up to date. Only new and changed items are copied, and the folder is renamed to that day's date, so its name always shows when it was last updated.
- macOS asks for two permissions the first time: allowing Terminal to **control Photos** (click OK), and **Full Disk Access** for Terminal (iGround opens the right settings page). The second is needed for Messages and iPhone backups.
- If your Mac doesn't have Python yet, double-clicking iGround offers to install it (Apple's free *Command Line Developer Tools*). Then double-click again.

## Why WhatsApp works this way

WhatsApp's own iCloud backup is locked: nothing except WhatsApp on a phone can open it. Your chats and media are also included in a normal **iPhone backup**, and iGround makes Finder save those backups onto the SSD. That replaces iCloud Backup (often one of the biggest things in iCloud) and keeps your WhatsApp history safe.

## What isn't included

iCloud Mail, Notes, Contacts, Calendars, Reminders, passwords and Shared Albums stay in iCloud. They're usually small. Exported photos are the originals, so edits made in the Photos app (crops, filters) aren't applied to them.

Format the SSD as **APFS** (Disk Utility) if you can. On an exFAT drive, everything still works except the Albums and Favourites folders; the album list is then kept in `.iground/Photos/albums.json` inside the backup instead.

## For power users

```sh
iground                         # the guided flow (same as double-clicking)
iground backup /Volumes/MySSD   # non-interactive; --only photos,drive,apps,messages  --new  --dry-run  --evict-after
iground ready  /Volumes/MySSD   # safe to downgrade? exit code 0 = yes
iground iphone-backup /Volumes/MySSD   [--undo]
iground audit  [/Volumes/MySSD]
iground verify /Volumes/MySSD   # re-read every file and compare checksums
iground status /Volumes/MySSD [--failed]
```

Install as a command with `python3 -m pip install --user .`, or run it from the folder with `PYTHONPATH=src python3 -m iground`. `--evict-after` frees each iCloud Drive file from the Mac's own disk once it's safely on the SSD; use it when the Mac is short of space. `--new` starts a separate full backup instead of updating the latest one.

Each backup keeps its bookkeeping (a checksum for every file, progress, album list) in a hidden `.iground` folder, so the folders you browse contain only your own files.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Everything macOS-specific sits behind small classes: `ICloudClient` (`brctl`), `PhotosClient` (`osascript`), `Locations` (home-folder paths; override with `IGROUND_HOME`) and the wizard's injected runners. That's why the tests run on Linux.
