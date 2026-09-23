"""Legacy AMOS XML helpers backed by the pure :mod:`amos_data` package."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

from amos_data.parser import (
    AOC_BY_PREFIX,
    PROCESSED_DIR,
    REPO_ROOT,
    VARIANT_BY_TYPE,
    as_bool,
    as_date,
    as_int,
    as_timestamp,
    normalize_root,
    part_key,
    text,
)

XML_DIR = REPO_ROOT / "data" / "xml"


def iter_workorders(xml_dir: Path = XML_DIR) -> Iterator[tuple[Path, ET.Element]]:
    """Yield namespace-normalized roots, preserving the old skip-on-error API."""
    for path in sorted(xml_dir.glob("*.xml")):
        try:
            yield path, normalize_root(ET.parse(path).getroot())
        except (ET.ParseError, OSError):
            continue


def workorder_part_keys(root: ET.Element) -> set[str]:
    """Every part number referenced by a workorder, as normalized keys."""
    keys: set[str] = set()
    for cc in root.iter():
        if cc.tag.rsplit("}", 1)[-1] != "componentChange":
            continue
        for path in ("partOff/partNumber", "partOn/partNumber"):
            key = part_key(text(cc, path))
            if key:
                keys.add(key)
    for pr in root.iter():
        if pr.tag.rsplit("}", 1)[-1] == "partRequest":
            key = part_key(text(pr, "partNumber"))
            if key:
                keys.add(key)
    return keys


__all__ = [
    "AOC_BY_PREFIX",
    "PROCESSED_DIR",
    "REPO_ROOT",
    "VARIANT_BY_TYPE",
    "as_bool",
    "as_date",
    "as_int",
    "as_timestamp",
    "iter_workorders",
    "part_key",
    "text",
    "workorder_part_keys",
]
