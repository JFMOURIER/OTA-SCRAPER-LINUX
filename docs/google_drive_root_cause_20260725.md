# Google Drive upload diagnosis — 2026-07-25

The application was not treating a Drive web URL as a local filesystem path.
Its delivery backend was already designed around `rclone`, folder IDs, raw-file
uploads, immutable destination names, and remote size/checksum checks.

The machine-side failure had two concrete causes:

1. `rclone` was not installed and
   `/home/jf/.config/rclone/rclone.conf` did not exist. A user-local rclone
   executable is now installed at `/home/jf/.local/bin/rclone`; OAuth
   configuration of the `gdrive` remote is still required.
2. The connected Google Drive integration can read and enumerate the supplied
   folders, but every write probe returned Google error
   `ACCESS_TOKEN_SCOPE_INSUFFICIENT`. No probe or production file was created
   by those rejected requests.

One legacy full-year CSV was found in the top-level `OTA - SCRAPED PRICES`
folder rather than an instance-specific destination. It was left untouched.

The authoritative target IDs are:

- scraper 1: `18kkxujFodzEfoOmhfpBuZI8xDzaoUk8b`
- scraper 2: `1ATZztmP0pS9c0oprzF3LzQKYcpsXtT7S`
- scraper 3: `19TNxbw6-ymHmUE2dkrSLyvKwYtjP4H5c`

Local exports remain successful independently of Drive. Missing authentication
now leaves each verified CSV in an atomic per-instance `pending` queue. The
minute dispatcher retries both current and legacy isolated queue directories
after process or machine restart.

One-time setup:

```bash
scripts/ota-drive configure
scripts/verify-google-drive-sync --instance near_30_days
scripts/verify-google-drive-sync --instance medium_31_120_days
scripts/verify-google-drive-sync --instance long_121_365_days
```

The verification command creates a uniquely named tiny probe, verifies it by
name and size/checksum, and deletes only that probe.
