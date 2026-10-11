import logging
import logging.config
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

import structlog
import structlog.typing

from aegra_api.settings import settings


class SizeAndAgeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Size-based rotation with age-based cleanup of rotated files.

    stdlib rotates by size (`RotatingFileHandler`) or by time
    (`TimedRotatingFileHandler`) but never both, so cleanup by age runs here
    after each rollover. `LOG_FILE_MAX_MB` sets the rotation point;
    `LOG_FILE_RETENTION_DAYS` is the retention bound — backupCount is only a
    hard ceiling on the rename cascade (shortcut: fixed at _MAX_BACKUPS, raise
    it if a deployment needs more retained files than that).
    """

    _MAX_BACKUPS = 100

    def __init__(self, filename: str, *, retention_days: int = 7, **kwargs: Any) -> None:
        self.retention_days = retention_days
        super().__init__(filename, backupCount=self._MAX_BACKUPS, **kwargs)

    def doRollover(self) -> None:
        super().doRollover()
        cutoff = time.time() - self.retention_days * 86400
        for index in range(1, self._MAX_BACKUPS + 1):
            rotated = Path(f"{self.baseFilename}.{index}")
            if rotated.exists() and rotated.stat().st_mtime < cutoff:
                rotated.unlink(missing_ok=True)


def _log_file_path() -> Path | None:
    """Resolve LOG_FILE to an absolute path, or None when file logging is off or unusable.

    Returns None (stdout logging only) when the configured folder cannot be
    created, so a read-only working directory cannot stop the server booting.
    """
    if not settings.app.LOG_FILE or not settings.app.LOG_FILE.strip():
        return None
    path = Path(settings.app.LOG_FILE).expanduser().resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Cannot create log folder %s (%s) - file logging disabled", path.parent, exc
        )
        return None
    return path


def get_logging_config() -> dict[str, Any]:
    """Return a unified logging config dict for structlog + stdlib integration.

    Uses string references for streams (e.g., "ext://sys.stdout") to avoid
    the multiprocessing pickling error on Windows.
    """
    env_mode = settings.app.ENV_MODE
    log_level = settings.app.LOG_LEVEL

    # Determine mode-specific processors and renderer.
    #
    # Production: format_exc_info converts exc_info into a traceback string
    # because JSONRenderer cannot render exceptions on its own.
    #
    # Dev: ConsoleRenderer handles exc_info internally via its
    # exception_formatter, so format_exc_info must be excluded (it would
    # convert exceptions to plain strings before ConsoleRenderer sees them,
    # killing pretty traceback rendering).
    final_renderer: structlog.typing.Processor
    if env_mode in ("LOCAL", "DEVELOPMENT"):
        # RichTracebackFormatter uses Unicode box-drawing characters that
        # Windows cp1252 console encoding cannot render. Use plain_traceback
        # on Windows to avoid UnicodeEncodeError through colorama.
        if sys.platform == "win32":
            exception_formatter = structlog.dev.plain_traceback
        else:
            exception_formatter = structlog.dev.RichTracebackFormatter(
                show_locals=False,
                max_frames=10,
            )
        final_renderer = structlog.dev.ConsoleRenderer(
            colors=True,
            pad_level=True,
            exception_formatter=exception_formatter,
        )
        mode_processors: list[Any] = []
    else:
        final_renderer = structlog.processors.JSONRenderer()
        mode_processors = [structlog.processors.format_exc_info]

    # Shared processors used by BOTH structlog and stdlib (via foreign_pre_chain).
    #
    # Ordering constraints:
    #   - merge_contextvars must be FIRST so request-scoped context
    #     (request_id, run_id, etc.) is visible to all subsequent processors
    #   - format_exc_info (production only) must come after StackInfoRenderer
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        *mode_processors,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.UnicodeDecoder(),
    ]

    handlers: dict[str, Any] = {
        "default": {
            "level": log_level,
            "class": "logging.StreamHandler",
            "formatter": "default",
            # String reference defers sys.stdout lookup until the config
            # is loaded in the child process (avoids pickling error).
            "stream": "ext://sys.stdout",
        },
    }

    log_file = _log_file_path()
    if log_file is not None:
        handlers["file"] = {
            "()": SizeAndAgeRotatingFileHandler,
            "level": log_level,
            "formatter": "file",
            "filename": str(log_file),
            "maxBytes": settings.app.LOG_FILE_MAX_MB * 1024 * 1024,
            "retention_days": settings.app.LOG_FILE_RETENTION_DAYS,
            # The default locale encoding mangles non-ASCII on Windows consoles.
            "encoding": "utf-8",
        }

    return {
        "version": 1,
        # Keep library loggers (uvicorn, httpx, etc.) alive
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "structlog.stdlib.ProcessorFormatter",
                # remove_processors_meta strips internal keys before rendering
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    final_renderer,
                ],
                "foreign_pre_chain": shared_processors,
            },
            # Log files are always JSON: ANSI colour codes from the console
            # renderer would make them unreadable and unparseable, and
            # format_exc_info is needed because JSONRenderer cannot render
            # exceptions on its own (see the production branch above).
            "file": {
                "()": "structlog.stdlib.ProcessorFormatter",
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
                "foreign_pre_chain": shared_processors,
            },
        },
        "handlers": handlers,
        "loggers": {
            "": {
                "handlers": list(handlers),
                "level": log_level,
                "propagate": False,
            },
            "uvicorn.error": {
                "level": "INFO",
            },
            "uvicorn.access": {
                "level": "WARNING",
            },
        },
    }


def setup_logging() -> None:
    """Configure both standard logging and structlog. Call once at startup."""
    config = get_logging_config()

    logging.config.dictConfig(config)

    # Uvicorn installs its own handlers on startup; clear them so all logs
    # go through our structlog formatter instead.
    for name in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True

    # Silence overly chatty libraries
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    # Route structlog through the stdlib logging system we just configured.
    shared_processors = config["formatters"]["default"]["foreign_pre_chain"]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
