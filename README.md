# iGround

Move your **iCloud Drive onto an external SSD**, safely. You can stop it partway, re-run it, and check the copies afterwards.

macOS keeps many iCloud Drive files "in the cloud only": Finder shows them, but their contents are not on the Mac. If you drag them to a drive, the copy either fails or stalls. iGround handles this for you:

1. **Scan.** Finds every file in iCloud Drive and works out which ones are only in the cloud. It detects both the modern APFS "dataless" stubs and the older `.name.icloud` placeholders.
2. **Download.** Asks iCloud to fetch each cloud-only file using the built-in `brctl` tool and waits until it has arrived.
3. **Copy.** Writes each file to a hidden temporary file on the SSD, flushes it to disk, then renames it into place, so an interrupted copy never leaves a half-written file. File dates and permissions are kept.
4. **Verify.** Reads the copy back from the SSD and compares SHA-256 checksums.
5. **Record.** Logs every file in a manifest on the SSD (`.iground/manifest.sqlite`), so re-running skips files that are already done and retries the ones that failed.
6. **Free up space (optional).** With `--evict-after`, it removes the local download from the Mac once the copy is verified. The file stays in iCloud.

iGround **never deletes anything from iCloud**.

## Install

Needs macOS and Python 3.9 or newer (the system `python3` from the Xcode Command Line Tools works). There are no other dependencies.

```sh
git clone https://github.com/snesboola/iGround.git
cd iGround
python3 -m pip install --user .     # installs the `iground` command
# or run without installing:
PYTHONPATH=src python3 -m iground --help
```

The first run may trigger a macOS privacy prompt. If it can't read iCloud Drive, give your terminal **Full Disk Access** in System Settings → Privacy & Security.

## Usage

```sh
# 1. See what you have and how much has to be downloaded
iground scan

# 2. Preview the migration without changing anything
iground migrate "/Volumes/MySSD/iCloud Drive" --dry-run

# 3. Run it (Ctrl-C at any time; run the same command again to resume)
iground migrate "/Volumes/MySSD/iCloud Drive"

# 4. Check progress, or list any failures
iground status "/Volumes/MySSD/iCloud Drive" --failed

# 5. Later: re-check every file on the SSD and list anything not yet migrated
iground verify "/Volumes/MySSD/iCloud Drive"
```

### Options for `migrate`

| Flag | Meaning |
|---|---|
| `--source PATH` | Folder to migrate. Defaults to `~/Library/Mobile Documents/com~apple~CloudDocs` (iCloud Drive). Point it at another folder under `~/Library/Mobile Documents` to migrate a particular app's iCloud folder. |
| `--dry-run` | List what would be downloaded and copied, then stop. |
| `--workers N` | Number of files downloaded and copied at the same time (default 4). |
| `--evict-after` | After each verified copy, remove the local download from the Mac. The file stays in iCloud. |
| `--no-verify` | Skip the read-back checksum check. Faster, but not recommended. |
| `--exclude GLOB` | Skip matching files or folders. Repeat the flag for several patterns. `.DS_Store` and similar files are always skipped. |
| `--download-timeout S` | How long to wait for each file to download (default 1800 s). |
| `--force` | Continue even when the SSD looks too small. |
| `-v` | Print a line for every file. |

`migrate` exits with 0 when everything succeeded, 1 when some files failed (re-run to retry them), and 2 on errors such as a bad path, a too-small SSD or no manifest.

## Tips

- **Format the SSD as APFS** if you can. exFAT can't store symlinks or some metadata, and those files will show up as failures.
- **Keep the Mac awake and online.** Downloading many GB from iCloud takes a while. `caffeinate -i iground migrate …` stops the Mac from sleeping.
- **Low on space on the Mac?** Use `--evict-after`. Each file is downloaded, copied, checked and then removed locally before the next batch, so the Mac never needs room for all of iCloud Drive at once.
- **iCloud Photos is not covered.** Use Photos → Settings → *Download Originals*, then move the library in Finder while Photos is closed.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The iCloud-specific parts (`brctl` and the dataless flag) are behind `iground.icloud.ICloudClient`, so the tests run on Linux with a fake client.
