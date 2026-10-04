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

The iGround window opens:

```
 iGround                                              ⚙
 ▭ Samsung T7 · 1.8 TB free

 Ready to copy your iCloud
 48,310 photos and 140 GB of files will be copied to Samsung T7.

 ○  Photos                         48,310 photos & videos  ›
 ○  iCloud Drive                   12,408 files · 120 GB   ›
 ○  App Documents                  37 files · 1.2 GB       ›
 ○  Messages                       3,112 files · 3.4 GB    ›
 ○  iPhone & WhatsApp              Not set up              ›

 [                 Copy to Samsung T7                 ]
       Nothing is deleted from iCloud. You can stop at any time.
```

- **One button.** Click **Copy to …**. Each row shows its own progress, and the title shows how far along you are and roughly how long is left. You can **Stop** at any time and continue later.
- **Every row tells you where it stands**: ✓ when it's safely on the SSD, or what's still missing. Click a row for more detail, to show it in Finder, or to see anything that couldn't be copied.
- **iPhone & WhatsApp**: click the row → **Set up**, then back up your iPhone in Finder as the row describes, then click **Check again**.
- When every row shows ✓, the title changes to **"Everything is on your SSD"**. Click **How?** for the steps to free up iCloud and downgrade.
- **Update backup** copies only what's new or changed since last time. **Check files** re-reads every file on the SSD to make sure nothing is damaged.
- **⚙ Settings**: choose what to back up, free up space on the Mac as files are copied, start a separate backup, or move iPhone backups back to the Mac.

The Terminal window that opens alongside keeps iGround running. Close it when you're done. The app follows your Mac's light or dark mode. If Google Chrome is installed, iGround opens in its own clean window; otherwise it opens in your default browser.

**Good to know**
- The first run can take hours, because every photo is downloaded in full quality. The Mac is kept awake while it works.
- Each update renames the backup folder to that day's date, so its name always shows when it was last updated.
- macOS asks for two permissions: allowing Terminal to **control Photos** (click OK), and **Full Disk Access** for Terminal, which is needed for Messages and iPhone backups. If either is missing, its row says so and has a button that opens the right settings page.
- If your Mac doesn't have Python yet, double-clicking iGround offers to install it (Apple's free *Command Line Developer Tools*). Then double-click again.

## Why WhatsApp works this way

WhatsApp's own iCloud backup is locked: nothing except WhatsApp on a phone can open it. Your chats and media are also included in a normal **iPhone backup**, and iGround makes Finder save those backups onto the SSD. That replaces iCloud Backup (often one of the biggest things in iCloud) and keeps your WhatsApp history safe.

## What isn't included

iCloud Mail, Notes, Contacts, Calendars, Reminders, passwords and Shared Albums stay in iCloud. They're usually small. Exported photos are the originals, so edits made in the Photos app (crops, filters) aren't applied to them.

Format the SSD as **APFS** (Disk Utility) if you can. On an exFAT drive, everything still works except the Albums and Favourites folders; the album list is then kept in `.iground/Photos/albums.json` inside the backup instead.

## For power users

```sh
iground                         # open the app (same as double-clicking)
iground guided                  # the same journey as questions in the terminal
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

The app is a small local web page (`src/iground/app/`): `service.py` holds all state and actions, and `server.py` serves it on 127.0.0.1 behind a random per-launch token. There are no extra dependencies.

Everything macOS-specific sits behind small classes: `ICloudClient` (`brctl`), `PhotosClient` (`osascript`), `Locations` (home-folder paths; override with `IGROUND_HOME`) and the wizard's injected runners. That's why the tests run on Linux.
