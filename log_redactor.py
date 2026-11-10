"""Narrow log redaction for one-time Xingxy binding inputs and URLs."""

import logging
import re


_CONFIRM_COMMAND_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?P<command>/?xy_bind_confirm(?:@[A-Za-z0-9_]+)?(?:\s+@[A-Za-z0-9_]+)?)"
    r"(?P<separator>\s+)"
    r"(?P<code>[A-Za-z0-9_-]{16})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_OAUTH_AUTHORIZE_URL_RE = re.compile(
    r"https?://[^\s<>\"']*?/wp-json/zibll-oauth/v1/authorize\?[^\s<>\"']*",
    re.IGNORECASE,
)
_SITE_BALANCE_RE = re.compile(r"(站点积分余额[：:]\s*)[0-9]+")


def redact_sensitive_text(message: str) -> str:
    """Replace confirmation arguments and provider authorization URLs."""
    message = _CONFIRM_COMMAND_RE.sub(
        lambda match: "{} [REDACTED]".format(match.group("command")),
        message,
    )
    message = _OAUTH_AUTHORIZE_URL_RE.sub("[REDACTED OAuth authorization URL]", message)
    return _SITE_BALANCE_RE.sub(r"\1[REDACTED]", message)


class XingxyBindingLogFilter(logging.Filter):
    """Redact sensitive values from LogRecord.msg/args before handler fan-out."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # A malformed third-party format may still carry a secret in args.
            # Fall back to their string representation and neutralize the
            # original fields if a sensitive value is found.
            try:
                message = "{} {}".format(record.msg, record.args)
            except Exception:
                return True
        redacted = redact_sensitive_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_CORE_LOG_FILTER = XingxyBindingLogFilter()


def install_core_log_filter() -> XingxyBindingLogFilter:
    """Install one filter on AstrBot's core logger before its handlers run."""
    core_logger = logging.getLogger("astrbot")
    if _CORE_LOG_FILTER not in core_logger.filters:
        core_logger.addFilter(_CORE_LOG_FILTER)
    return _CORE_LOG_FILTER


def remove_core_log_filter(log_filter: XingxyBindingLogFilter) -> None:
    """Remove only this plugin's filter from the core logger."""
    logging.getLogger("astrbot").removeFilter(log_filter)
