from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path


FORBIDDEN_PATTERNS = [
    ".env",
    "*.env",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    "*.xlsx",
    "*.xls",
    "*.csv",
    "*.log",
    "data/*",
    "data/instances/*",
    "venv/*",
    ".venv/*",
    "browser_profile/*",
    "logs/*",
    "screenshots/*",
    "debug/*",
    "checkpoints/*",
    "partial/*",
    "exports/*",
    ".streamlit/secrets.toml",
]


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], text=True, capture_output=True, check=False)
    if result.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def is_forbidden(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for pattern in FORBIDDEN_PATTERNS:
        pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatch(normalized, pattern):
            return True
        if pattern.endswith("/*") and normalized.startswith(pattern[:-1]):
            return True
    return False


def main() -> int:
    repo_root = Path.cwd()
    if not (repo_root / ".git").exists():
        print("GitHub readiness check failed: this folder is not a git repository.")
        return 1

    tracked = set(git_lines("ls-files"))
    staged = set(git_lines("diff", "--cached", "--name-only"))
    unsafe = sorted(path for path in tracked | staged if is_forbidden(path))

    if unsafe:
        print("GitHub readiness check failed. Unsafe tracked or staged files:")
        for path in unsafe:
            print(f"- {path}")
        return 1

    print("GitHub readiness check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
