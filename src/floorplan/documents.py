"""Bound user-authored JSON before recursive model and Qt operations."""
import json
import math
from pathlib import Path


MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_DEPTH = 64
MAX_DOCUMENT_ITEMS = 250_000


def validate_document(value):
    """Keep opaque JSON fields, but reject excessive or non-finite input."""
    pending = [(value, 0)]
    items = text_size = 0
    while pending:
        current, depth = pending.pop()
        items += 1
        if depth > MAX_DOCUMENT_DEPTH or items > MAX_DOCUMENT_ITEMS:
            raise ValueError("Document is too complex; reduce its nesting or number of entries.")
        if isinstance(current, dict):
            if items + len(pending) + len(current) > MAX_DOCUMENT_ITEMS:
                raise ValueError("Document is too complex; reduce its number of entries.")
            if any(not isinstance(key, str) for key in current):
                raise ValueError("Document field names must be text.")
            text_size += sum(len(key) for key in current)
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, (list, tuple)):
            if items + len(pending) + len(current) > MAX_DOCUMENT_ITEMS:
                raise ValueError("Document is too complex; reduce its number of entries.")
            pending.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            text_size += len(current)
        elif isinstance(current, (int, float)) and not isinstance(current, bool):
            try:
                finite = math.isfinite(current)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError("Document numbers must be finite and within the supported numeric range.")
        elif current is not None and not isinstance(current, bool):
            raise ValueError("Document values must be JSON data.")
        if text_size > MAX_DOCUMENT_BYTES:
            raise ValueError("Document is too large; the supported limit is 16 MiB.")
    return value


def load_document(path):
    """Read a bounded UTF-8 JSON file and report parser limits as input errors."""
    with Path(path).expanduser().open("rb") as stream:
        content = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("Document is too large; the supported limit is 16 MiB.")
    try:
        state = json.loads(content.decode("utf-8"))
    except (RecursionError, OverflowError):
        raise ValueError("Document is too complex; reduce its nesting or numeric values.") from None
    return validate_document(state)
