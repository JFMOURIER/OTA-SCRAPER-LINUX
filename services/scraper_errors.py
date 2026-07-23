from __future__ import annotations


class ScraperError(Exception):
    reason = "scraper_error"


class RecoverableScraperError(ScraperError):
    reason = "recoverable"


class BrowserClosedError(RecoverableScraperError):
    reason = "browser_closed"


class PageTimeoutError(RecoverableScraperError):
    reason = "timeout"


class AccessRestrictionError(ScraperError):
    reason = "access_restricted"


class FatalScraperConfigError(ScraperError):
    reason = "fatal_config"


class ResourcePressureError(ScraperError):
    reason = "resource_pressure"

    def __init__(self, message: str, *, level: str = "soft") -> None:
        super().__init__(message)
        self.level = level


class PaginationUnsupportedError(ScraperError):
    reason = "pagination_unsupported"


BROWSER_CLOSED_TOKENS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "context has been closed",
    "targetclosederror",
    "playwright._impl._errors.targetclosederror",
)

TIMEOUT_TOKENS = (
    "timeout",
    "timed out",
)

ACCESS_RESTRICTION_TOKENS = (
    "blocked_or_access_restricted",
    "captcha",
    "verify you are human",
    "access denied",
    "unusual traffic",
)


def error_text(error: BaseException | str) -> str:
    return str(error).lower()


def is_browser_closed_error(error: BaseException | str) -> bool:
    text = error_text(error)
    return any(token in text for token in BROWSER_CLOSED_TOKENS)


def is_timeout_error(error: BaseException | str) -> bool:
    text = error_text(error)
    return any(token in text for token in TIMEOUT_TOKENS)


def is_access_restriction_error(error: BaseException | str) -> bool:
    text = error_text(error)
    return any(token in text for token in ACCESS_RESTRICTION_TOKENS)


def classify_scraper_error(error: BaseException) -> ScraperError:
    if isinstance(error, ScraperError):
        return error
    message = str(error)
    if is_browser_closed_error(error):
        return BrowserClosedError(message)
    if is_access_restriction_error(error):
        return AccessRestrictionError(message)
    if is_timeout_error(error):
        return PageTimeoutError(message)
    return RecoverableScraperError(message)
