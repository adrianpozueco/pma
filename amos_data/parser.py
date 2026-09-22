"""Safe, deterministic parsing of AMOS transfer-workorder XML.

This module deliberately has no dependency on :mod:`pm_agent`: it is used by
the offline loaders as well as by the upload path.  The field mapping itself is
kept in ``scripts.xml_to_ndjson`` for backwards compatibility with the batch
schema; this module validates and dispatches to that mapping for each workorder.
"""

from __future__ import annotations

import copy
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

MAX_XML_BYTES = 25 * 1024 * 1024
MAX_WORKORDERS = 10_000
MAX_XML_DEPTH = 100

# These mappings are part of the stable AMOS batch field contract and are kept
# here so the pure record builder does not import the application or scripts.
REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
AOC_BY_PREFIX = {
    "EI": "Ryanair DAC (Ireland)",
    "9H": "Malta Air / Lauda Europe (Malta)",
    "SP": "Buzz (Poland)",
    "G": "Ryanair UK (UK)",
    "OE": "Lauda (Austria)",
}
VARIANT_BY_TYPE = {
    "B737-8": "737-800",
    "M73-82": "737-8200",
    "B737-7": "737-700",
    "32S": "A320",
}


def text(node: ET.Element | None, path: str | None = None) -> str | None:
    if node is None:
        return None
    element = node if path is None else node.find(path)
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def as_bool(value: str | None) -> bool | None:
    return None if value is None else {"Y": True, "N": False}.get(value.upper())


def as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def part_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = re.sub(r"[^A-Z0-9]", "", value.upper())
    return key or None


def as_date(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:Z)?", value)
    if match:
        try:
            date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.match(r"^(\d{4}-\d{2}-\d{2})T", value)
    return as_date(match.group(1)) if match else None


def as_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:?\d{2})?",
        value,
    )
    if not match:
        return None
    try:
        date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        hour, minute, second = (int(match.group(i) or 0) for i in (4, 5, 6))
        if hour > 23 or minute > 59 or second > 59:
            return None
    except ValueError:
        return None
    return value[:-1] + "+00:00" if value.endswith("Z") else value


class ParseError(ValueError):
    """An XML upload cannot safely or correctly be parsed.

    ``code`` is stable for callers that need to present a useful input error;
    ``details`` contains only JSON-serializable values.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_xml",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(slots=True)
class ParseResult:
    workorders: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    upload_hash: str


class _SafeTreeBuilder(ET.TreeBuilder):
    """Reject declarations at parser level, including BOM-less UTF-16 input."""

    def doctype(self, name: str, pubid: str, system: str) -> None:
        raise ParseError("DTD declarations are not supported", code="unsafe_xml")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def normalize_root(root: ET.Element) -> ET.Element:
    """Return a namespace-free copy while retaining UUIDs and attributes."""
    attrs = {_local_name(k): v for k, v in root.attrib.items()}
    result = ET.Element(_local_name(root.tag), attrs)
    result.text = root.text
    result.tail = root.tail
    stack: list[tuple[ET.Element, ET.Element]] = [(root, result)]
    while stack:
        source, destination = stack.pop()
        children = list(source)
        for child in children:
            child_attrs = {_local_name(k): v for k, v in child.attrib.items()}
            copy_node = ET.Element(_local_name(child.tag), child_attrs)
            copy_node.text = child.text
            copy_node.tail = child.tail
            destination.append(copy_node)
            stack.append((child, copy_node))
    return result


def _parse_root(xml_bytes: bytes) -> ET.Element:
    if not isinstance(xml_bytes, (bytes, bytearray, memoryview)):
        raise ParseError("XML input must be bytes", code="invalid_input")
    payload = bytes(xml_bytes)
    if len(payload) > MAX_XML_BYTES:
        raise ParseError(
            f"XML input exceeds the {MAX_XML_BYTES} byte limit",
            code="oversized_input",
            details={"max_bytes": MAX_XML_BYTES, "actual_bytes": len(payload)},
        )
    # ElementTree does not fetch external entities, but it accepts declarations
    # and entity syntax.  Reject both before parsing so this contract remains
    # true if the XML implementation changes underneath us.
    try:
        if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            encoding = "utf-32"
        elif payload.startswith((b"\xff\xfe", b"\xfe\xff")):
            encoding = "utf-16"
        elif payload.startswith(b"<\x00"):
            encoding = "utf-16le"
        elif payload.startswith(b"\x00<"):
            encoding = "utf-16be"
        else:
            encoding = "utf-8"
        text_payload = payload.decode(encoding, errors="ignore")
    except UnicodeError:
        text_payload = ""
    declaration = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
    unknown_entity = re.compile(r"&(?!amp;|lt;|gt;|apos;|quot;)[A-Za-z_][\w.-]*;", re.I)
    if declaration.search(text_payload) or unknown_entity.search(text_payload):
        raise ParseError(
            "DTD and entity declarations are not supported", code="unsafe_xml"
        )
    try:
        parsed_root = ET.fromstring(
            payload, parser=ET.XMLParser(target=_SafeTreeBuilder())
        )
    except ParseError:
        raise
    except ET.ParseError as exc:
        raise ParseError(
            "Malformed XML", code="malformed_xml", details={"error": str(exc)}
        ) from exc
    # Check depth before copying so a deeply nested hostile tree cannot trigger
    # recursive cloning or consume unbounded memory.
    depth = 0
    stack: list[tuple[ET.Element, int]] = [(parsed_root, 1)]
    while stack:
        node, level = stack.pop()
        depth = max(depth, level)
        if level > MAX_XML_DEPTH:
            raise ParseError(
                "XML nesting exceeds parser limit",
                code="oversized_input",
                details={"max_depth": MAX_XML_DEPTH},
            )
        stack.extend((child, level + 1) for child in node)
    root = normalize_root(parsed_root)
    if _local_name(root.tag) not in {"amosTransportEnvelope", "transferWorkorder"}:
        raise ParseError(
            "Unsupported XML envelope",
            code="unsupported_envelope",
            details={"root": root.tag},
        )
    return root


def _validate_temporals(
    root: ET.Element,
) -> tuple[list[dict[str, str | None]], list[dict[str, Any]]]:
    """Validate AMOS date/timestamp fields and return raw timestamp sources."""
    raw_timestamps: list[dict[str, str | None]] = []
    diagnostics: list[dict[str, Any]] = []
    stack: list[tuple[ET.Element, str, str | None]] = [
        (root, _local_name(root.tag), None)
    ]
    while stack:
        element, path, owner_uuid = stack.pop()
        name = _local_name(element.tag)
        if name == "workorder":
            owner_uuid = element.attrib.get("uuid")
        value = (element.text or "").strip()
        if value and re.search(r"(?:date|time|timestamp)$", name, re.I):
            lowered = name.lower()
            is_timestamp = (
                "datetime" in lowered
                or "timestamp" in lowered
                or ("time" in lowered and not lowered.endswith("date"))
                or "T" in value
            )
            try:
                if is_timestamp:
                    _validate_timestamp(value)
                else:
                    _validate_date(value)
            except ValueError as exc:
                # Date/counter fields are optional in AMOS exports. Keep the
                # workorder available for symptom retrieval while surfacing the
                # invalid value so timing consumers can abstain explicitly.
                diagnostics.append(
                    {
                        "code": "invalid_date",
                        "severity": "error",
                        "field": name,
                        "value": value,
                        "error": str(exc),
                    }
                )
                if is_timestamp:
                    raw_timestamps.append(
                        {"field": path, "value": value, "workorder_uuid": owner_uuid}
                    )
                continue
            if is_timestamp:
                raw_timestamps.append(
                    {"field": path, "value": value, "workorder_uuid": owner_uuid}
                )
        children = list(element)
        for index, child in reversed(list(enumerate(children))):
            stack.append(
                (child, f"{path}/{_local_name(child.tag)}[{index}]", owner_uuid)
            )
    return raw_timestamps, diagnostics


def _validate_date(value: str) -> None:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:Z)?", value)
    if not match:
        raise ValueError("expected YYYY-MM-DD")
    date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _validate_timestamp(value: str) -> None:
    match = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:?\d{2})?",
        value,
    )
    if not match:
        raise ValueError("expected ISO timestamp")
    date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    hour, minute, second = (int(match.group(i) or 0) for i in (4, 5, 6))
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("time is out of range")
    offset = match.group(8)
    if offset and offset != "Z":
        signless = offset[1:].replace(":", "")
        if int(signless[:2]) > 23 or int(signless[2:]) > 59:
            raise ValueError("timezone offset is out of range")


def _workorder_nodes(root: ET.Element) -> list[ET.Element]:
    nodes = [node for node in root.iter() if _local_name(node.tag) == "workorder"]
    if not nodes:
        raise ParseError("Envelope contains no workorder", code="missing_workorder")
    if len(nodes) > MAX_WORKORDERS:
        raise ParseError(
            "Envelope contains too many workorders",
            code="oversized_input",
            details={"max_workorders": MAX_WORKORDERS},
        )
    return nodes


def _identity_diagnostics(nodes: list[ET.Element]) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], tuple[str, int]] = {}
    for wo_index, workorder in enumerate(nodes):
        for node in workorder.iter():
            uuid = node.attrib.get("uuid")
            kind = _local_name(node.tag)
            if not uuid or kind not in {
                "workorder",
                "workStep",
                "action",
                "componentChange",
                "partRequest",
            }:
                continue
            fingerprint = ET.tostring(node, encoding="unicode")
            key = (kind, uuid)
            previous = seen.get(key)
            if previous is None:
                seen[key] = (fingerprint, wo_index)
                continue
            diagnostics.append(
                {
                    "code": "conflicting_identity"
                    if previous[0] != fingerprint
                    else "duplicate_identity",
                    "severity": "warning",
                    "identity_type": kind,
                    "uuid": uuid,
                    "workorder_indexes": [previous[1], wo_index],
                }
            )
    return diagnostics


def parse_workorders(
    xml_bytes: bytes,
    *,
    source_name: str | None = None,
    artifact_version: str | None = None,
) -> ParseResult:
    """Parse every workorder in an AMOS envelope into the batch record shape."""
    payload = (
        bytes(xml_bytes)
        if isinstance(xml_bytes, (bytes, bytearray, memoryview))
        else xml_bytes
    )
    upload_hash = (
        hashlib.sha256(payload).hexdigest() if isinstance(payload, bytes) else ""
    )
    root = _parse_root(xml_bytes)
    payload_node = next(
        (n for n in root.iter() if _local_name(n.tag) == "payload"), None
    )
    if (
        payload_node is not None
        and (payload_node.attrib.get("type") or "transferWorkorder")
        != "transferWorkorder"
    ):
        raise ParseError(
            "Unsupported payload type",
            code="unsupported_payload",
            details={"type": payload_node.attrib.get("type")},
        )
    raw_timestamps, temporal_diagnostics = _validate_temporals(root)
    nodes = _workorder_nodes(root)
    diagnostics = temporal_diagnostics + _identity_diagnostics(nodes)
    from .records import build_record

    records: list[dict[str, Any]] = []
    for selected in nodes:
        record = build_record(
            Path(source_name or "upload.xml"), root, selected_workorder=selected
        )
        if record is None:
            diagnostics.append(
                {"code": "missing_workorder_header", "severity": "error"}
            )
            continue
        record = copy.deepcopy(record)
        selected_uuid = selected.attrib.get("uuid")
        record["provenance"] = {
            "source_name": source_name,
            "artifact_version": artifact_version,
            "upload_hash": upload_hash,
            "raw_timestamps": [
                source
                for source in raw_timestamps
                if source["workorder_uuid"] == selected_uuid
            ],
        }
        records.append(record)
    return ParseResult(records, diagnostics, upload_hash)


def parse_file(path: str | Path, **kwargs: Any) -> ParseResult:
    """Compatibility client for callers that historically passed a filename."""
    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ParseError(
            "Unable to read XML file",
            code="invalid_input",
            details={"path": str(source)},
        ) from exc
    return parse_workorders(
        payload, source_name=kwargs.pop("source_name", source.name), **kwargs
    )


__all__ = [
    "MAX_WORKORDERS",
    "MAX_XML_BYTES",
    "ParseError",
    "ParseResult",
    "normalize_root",
    "parse_file",
    "parse_workorders",
]
