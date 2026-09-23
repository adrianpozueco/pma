"""Pure AMOS data contracts and parser."""

from .parser import (
    MAX_WORKORDERS,
    MAX_XML_BYTES,
    ParseError,
    ParseResult,
    parse_file,
    parse_workorders,
)

__all__ = [
    "MAX_WORKORDERS",
    "MAX_XML_BYTES",
    "ParseError",
    "ParseResult",
    "parse_file",
    "parse_workorders",
]
