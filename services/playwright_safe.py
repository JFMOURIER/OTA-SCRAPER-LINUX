from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from services.scraper_errors import is_browser_closed_error


LogFn = Callable[[str], None] | None


def safe_page_url(page: Any, log: LogFn = None) -> str | None:
    try:
        return page.url
    except Exception as exc:
        if log:
            log(f"Warning: could not read page URL: {exc}")
        return None


def safe_page_title(page: Any, log: LogFn = None) -> str | None:
    try:
        return page.title()
    except Exception as exc:
        if log:
            log(f"Warning: could not read page title: {exc}")
        return None


def safe_screenshot(page: Any, path: str | Path, label: str = "screenshot", log: LogFn = None, *, full_page: bool = True) -> bool:
    try:
        if page is None or (hasattr(page, "is_closed") and page.is_closed()):
            if log:
                log(f"Warning: skipped {label}; page is already closed.")
            return False
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=full_page)
        return True
    except Exception as exc:
        prefix = "browser closed" if is_browser_closed_error(exc) else "failed"
        if log:
            log(f"Warning: {label} screenshot {prefix}: {exc}")
        return False


def safe_close_browser(browser: Any = None, context: Any = None, page: Any = None, log: LogFn = None) -> None:
    for label, target in (("page", page), ("context", context), ("browser", browser)):
        if target is None:
            continue
        try:
            if label == "page" and hasattr(target, "is_closed") and target.is_closed():
                continue
            target.close()
        except Exception as exc:
            if log:
                log(f"Warning: ignored {label} close failure: {exc}")
