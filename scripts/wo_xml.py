"""Shared helpers for reading AMOS transferWorkorder XML exports.

The XML lives in ``data/xml/`` as one ``transferWorkorder`` document per file.
Both ``xml_to_ndjson.py`` and ``build_faa_sdr_wo_parts.py`` read it through here
so the two stay consistent about typing and part-number normalisation.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
XML_DIR = REPO_ROOT / "data" / "xml"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

# Registration prefixes are preserved in the anonymised export, so the AOC is
# still identifiable even though the operator element reads OPERATOR_ANON.
AOC_BY_PREFIX = {
    "EI": "Ryanair DAC (Ireland)",
    "9H": "Malta Air / Lauda Europe (Malta)",
    "SP": "Buzz (Poland)",
    "G": "Ryanair UK (UK)",
    "OE": "Lauda (Austria)",
}

# AMOS type codes are not ICAO codes: B737-8 is the 737-800, not the MAX 8.
# Inferred from MSN ranges and in-service dates; see docs before trusting.
VARIANT_BY_TYPE = {
    "B737-8": "737-800",
    "M73-82": "737-8200",
    "B737-7": "737-700",
    "32S": "A320",
}


def text(node: ET.Element | None, path: str | None = None) -> str | None:
    """Return stripped element text, or None for missing/empty elements."""
    if node is None:
        return None
    el = node if path is None else node.find(path)
    if el is None or el.text is None:
        return None
    value = el.text.strip()
    return value or None


def as_bool(value: str | None) -> bool | None:
    """AMOS writes Y/N flags; anything else is left unknown rather than guessed."""
    if value is None:
        return None
    return {"Y": True, "N": False}.get(value.upper())


def as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def as_date(value: str | None) -> str | None:
    """``2010-06-19Z`` and ``2010-06-19T04:00:00.000Z`` both yield ``2010-06-19``."""
    if value is None:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", value)
    return match.group(1) if match else None


def as_timestamp(value: str | None) -> str | None:
    """Normalise to a form BigQuery accepts as TIMESTAMP (``Z`` -> ``+00:00``)."""
    if value is None:
        return None
    value = value.strip()
    if not re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        return None
    return value[:-1] + "+00:00" if value.endswith("Z") else value


def part_key(value: str | None) -> str | None:
    """Join key for part numbers.

    WO writes ``15800-029-3`` where FAA SDR writes ``158000293`` for the same
    part, so both sides are reduced to uppercase alphanumerics before matching.
    """
    if value is None:
        return None
    key = re.sub(r"[^A-Z0-9]", "", value.upper())
    return key or None


def iter_workorders(xml_dir: Path = XML_DIR) -> Iterator[tuple[Path, ET.Element]]:
    """Yield (path, root) for each parsable XML file, in filename order."""
    for path in sorted(xml_dir.glob("*.xml")):
        try:
            yield path, ET.parse(path).getroot()
        except ET.ParseError:
            continue


def workorder_part_keys(root: ET.Element) -> set[str]:
    """Every part number referenced by a workorder, as normalised keys."""
    keys: set[str] = set()
    for cc in root.findall(".//componentChange"):
        for path in ("partOff/partNumber", "partOn/partNumber"):
            key = part_key(text(cc, path))
            if key:
                keys.add(key)
    for pr in root.findall(".//partRequest"):
        key = part_key(text(pr, "partNumber"))
        if key:
            keys.add(key)
    return keys
