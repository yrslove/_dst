from __future__ import annotations

import json
import logging
import re
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[int | None] = ContextVar("job_id", default=None)
SENSITIVE = re.compile(r"(password|secret|token|cookie|authorization)", re.IGNORECASE)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if SENSITIVE.search(str(key)) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", value)
        value = re.sub(
            r"(?i)((?:password|secret|token|cookie|authorization)[\w-]*\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(r"(://[^\s:/]+:)[^\s@]+@", r"\1[REDACTED]@", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "request_id": request_id_var.get(),
            "job_id": job_id_var.get(),
            "event": getattr(record, "event", None),
            "message": record.getMessage(),
        }
        for key in ("account_id", "runtime_id", "node_id"):
            payload[key] = getattr(record, key, None)
        if record.exc_info:
            # SQL/HTTP exceptions can contain parameters and credentials. Keep
            # the exception class and stack locations, never raw exception args.
            payload["exception"] = record.exc_info[0].__name__
            payload["stack"] = [
                f"{frame.filename}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(redact(payload), ensure_ascii=False, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level)
    root._dst_structured = True  # type: ignore[attr-defined]
