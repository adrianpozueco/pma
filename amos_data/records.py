"""Convert AMOS transferWorkorder XML into newline-delimited JSON for BigQuery.

BigQuery cannot read XML, so this flattens each document into one JSON record
per workorder, keeping the nested structure as RECORD/REPEATED fields. Values
are typed on the way through (Y/N -> BOOL, dates -> DATE, timestamps ->
TIMESTAMP, counters -> INT64) because BigQuery autodetect cannot infer them
from JSON strings.

Outputs:
    data/processed/wo_workorders.ndjson.gz
    deployment/terraform/shared/wo_workorders_schema.json

Usage:
    uv run python scripts/xml_to_ndjson.py
"""

from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from .parser import (
    AOC_BY_PREFIX,
    PROCESSED_DIR,
    REPO_ROOT,
    VARIANT_BY_TYPE,
    as_bool,
    as_date,
    as_int,
    as_timestamp,
    part_key,
    text,
)

NDJSON_PATH = PROCESSED_DIR / "wo_workorders.ndjson.gz"
SCHEMA_PATH = (
    REPO_ROOT / "deployment" / "terraform" / "shared" / "wo_workorders_schema.json"
)


def _s(name: str, mode: str = "NULLABLE") -> dict:
    return {"name": name, "type": "STRING", "mode": mode}


def _f(name: str, type_: str) -> dict:
    return {"name": name, "type": type_, "mode": "NULLABLE"}


def _rec(name: str, fields: list[dict], mode: str = "NULLABLE") -> dict:
    return {"name": name, "type": "RECORD", "mode": mode, "fields": fields}


REFERENCE_FIELDS = [
    _s("uuid"),
    _s("category"),
    _s("description"),
    _s("reference"),
    _s("remarks"),
    _s("type"),
    _s("type_description"),
]

INSPECTION_FIELDS = [
    _s("headline"),
    _s("inspection_text"),
    _f("performed_ts", "TIMESTAMP"),
    _s("performed_sign"),
]

SCHEMA: list[dict] = [
    _s("source_file"),
    _rec(
        "envelope",
        [
            _s("sender_id"),
            _s("receiver_id"),
            _s("conversation_id"),
            _s("amos_version"),
            _s("instance_type"),
            _f("envelope_ts", "TIMESTAMP"),
            _s("date_timezone"),
            _s("envelope_version"),
            _s("payload_type"),
            _s("payload_version"),
            _s("transfer_workorder_version"),
        ],
    ),
    _s("workorder_uuid"),
    _s("workorder_number"),
    _s("workorder_type"),
    _s("workorder_state"),
    _s("barcode"),
    _s("ata_chapter"),
    _s("ata_code"),
    _s("ata_major"),
    _s("remarks"),
    _s("external_workorder_number"),
    _s("workorder_origin_type"),
    _s("origin_workorder_of_finding_uuid"),
    _rec(
        "position_info",
        [
            _s("position"),
            _s("special"),
            _s("zone"),
            _s("area"),
            _s("panel"),
        ],
    ),
    _rec("component", [_s("part_number"), _s("serial_number")]),
    _rec(
        "event",
        [
            _s("event_id"),
            _s("event_type"),
            _s("event_sub_type"),
            _s("description_title"),
            _s("issuer_cage_code"),
            _s("issuer_company"),
            _s("revision_number"),
        ],
    ),
    _rec(
        "aircraft",
        [
            _s("type_raw"),
            _s("variant"),
            _s("full_registration"),
            _s("registration"),
            _s("reg_country_prefix"),
            _s("aoc"),
            _s("msn"),
            _s("operator"),
            _rec(
                "routine_checks", [_s("check_code"), _s("performed")], mode="REPEATED"
            ),
        ],
    ),
    _rec(
        "issue",
        [
            _f("date", "DATE"),
            _f("ts", "TIMESTAMP"),
            _s("sign"),
            _s("station"),
            _s("leg"),
            _s("flt_from"),
            _s("flt_to"),
            _f("tah_hours", "INT64"),
            _f("tac", "INT64"),
        ],
    ),
    _rec(
        "closing",
        [
            _f("date", "DATE"),
            _f("ts", "TIMESTAMP"),
            _s("leg"),
            _s("sign"),
            _s("station"),
            _f("total_aircraft_cycles", "INT64"),
            _f("total_aircraft_hours_minutes", "INT64"),
        ],
    ),
    _rec("certification", [_s("number"), _f("ts", "TIMESTAMP"), _f("date", "DATE")]),
    _rec(
        "release_to_service", [_s("number"), _f("ts", "TIMESTAMP"), _s("release_sign")]
    ),
    _rec(
        "classifications",
        [
            _f("administrative_service_check", "BOOL"),
            _f("aog_risk", "BOOL"),
            _f("cdccl", "BOOL"),
            _f("cdcp", "BOOL"),
            _s("defect_classification"),
            _s("maintenance_type"),
            _f("occurence_report", "BOOL"),
            _f("ops_consequence", "BOOL"),
            _f("structural_damage", "BOOL"),
            _f("tal", "BOOL"),
            _f("watch_item", "BOOL"),
        ],
    ),
    _rec(
        "statistics",
        [
            _f("accident", "BOOL"),
            _f("incident", "BOOL"),
            _f("not_contracted", "BOOL"),
        ],
    ),
    _rec(
        "requests",
        [
            _f("engine_change", "BOOL"),
            _s("hangar_costs"),
            _s("towing_costs"),
            _s("other"),
            _f("ramp_activity", "BOOL"),
            _s("special_equipment"),
            _s("special_equipment2"),
            _f("srm_repair", "BOOL"),
            _f("taxi", "BOOL"),
            _f("weight_and_balance", "BOOL"),
        ],
    ),
    _rec("references", REFERENCE_FIELDS, mode="REPEATED"),
    _rec(
        "work_steps",
        [
            _s("uuid"),
            _s("sequence_number"),
            _s("work_phase"),
            _s("description"),
            _s("description_sign"),
            _s("headline"),
            _f("ts", "TIMESTAMP"),
            _s("state"),
            _f("double_inspection_required", "BOOL"),
            _rec("references", REFERENCE_FIELDS, mode="REPEATED"),
            _rec(
                "required_parts",
                [
                    _s("uuid"),
                    _s("part_number"),
                    _s("part_key"),
                    _s("serial_number"),
                    _s("description"),
                    _s("needed_quantity"),
                    _s("needed_quantity_uom"),
                    _s("probability_of_usage"),
                    _s("tool"),
                    _s("ipc_reference"),
                    _s("location_on_aircraft"),
                ],
                mode="REPEATED",
            ),
            _rec(
                "resource_requirements",
                [
                    _s("uuid"),
                    _s("classification"),
                    _s("duration"),
                    _s("quantity"),
                    _s("resource_type"),
                    _s("total_hours"),
                    _rec(
                        "constraints",
                        [
                            _s("uuid"),
                            _s("property_type"),
                            _s("value_string"),
                        ],
                        mode="REPEATED",
                    ),
                ],
                mode="REPEATED",
            ),
            _rec(
                "actions",
                [
                    _s("uuid"),
                    _s("action_type"),
                    _s("action_text"),
                    _s("closing_action"),
                    _f("performed_ts", "TIMESTAMP"),
                    _s("performed_sign"),
                    _rec("inspection", INSPECTION_FIELDS),
                    _rec("double_inspection", INSPECTION_FIELDS),
                    _rec("references", REFERENCE_FIELDS, mode="REPEATED"),
                    _rec(
                        "component_changes",
                        [
                            _s("uuid"),
                            _s("position"),
                            _s("label_number"),
                            _s("certificate_number"),
                            _s("part_off_number"),
                            _s("part_off_serial"),
                            _s("part_on_number"),
                            _s("part_on_serial"),
                            _s("part_key"),
                            _f("like_for_like", "BOOL"),
                        ],
                        mode="REPEATED",
                    ),
                    _rec(
                        "staff_time_bookings",
                        [
                            _s("uuid"),
                            _s("booking_status"),
                            _s("duration"),
                            _f("start_ts", "TIMESTAMP"),
                            _f("end_ts", "TIMESTAMP"),
                            _s("entity_code"),
                            _s("type_of_work"),
                            _s("skill"),
                            _s("scope"),
                            _s("user_sign"),
                            _s("remarks"),
                        ],
                        mode="REPEATED",
                    ),
                    # Fluid uplifts recorded against the action. Oil and hydraulic
                    # arrive in separate containers that share a shape, so they are
                    # merged into one array discriminated by `system`.
                    _rec(
                        "uplifts",
                        [
                            _s("system"),
                            _s("code"),
                            _s("measure"),
                            _s("sign"),
                        ],
                        mode="REPEATED",
                    ),
                ],
                mode="REPEATED",
            ),
        ],
        mode="REPEATED",
    ),
    _f("part_swap_count", "INT64"),
    _s("part_keys", mode="REPEATED"),
]


def _reference(node: ET.Element) -> dict:
    return {
        "uuid": node.get("uuid"),
        "category": text(node, "category"),
        "description": text(node, "description"),
        "reference": text(node, "reference"),
        "remarks": text(node, "remarks"),
        "type": text(node, "type"),
        "type_description": text(node, "typeDescription"),
    }


def _references(parent: ET.Element | None) -> list[dict]:
    if parent is None:
        return []
    container = parent.find("references")
    if container is None:
        return []
    return [_reference(r) for r in container.findall("reference")]


def _inspection(node: ET.Element | None) -> dict | None:
    if node is None:
        return None
    return {
        "headline": text(node, "headline"),
        "inspection_text": text(node, "inspectionText"),
        "performed_ts": as_timestamp(text(node, "performedData/performedDateTime")),
        "performed_sign": text(node, "performedData/performedSign"),
    }


def _component_change(node: ET.Element) -> dict:
    off = text(node, "partOff/partNumber")
    on = text(node, "partOn/partNumber")
    return {
        "uuid": node.get("uuid"),
        "position": text(node, "position"),
        "label_number": text(node, "labelNumber"),
        "certificate_number": text(node, "certificateNumber"),
        "part_off_number": off,
        "part_off_serial": text(node, "partOff/serialNumber"),
        "part_on_number": on,
        "part_on_serial": text(node, "partOn/serialNumber"),
        # One key per swap: the removed part where known, else the installed one.
        "part_key": part_key(off or on),
        "like_for_like": (off == on) if (off and on) else None,
    }


def _uplifts(node: ET.Element | None) -> list[dict]:
    """Flatten oilUplifts / hydraulicUplifts into one array.

    The sign is recorded once per container rather than per uplift, so it is
    copied onto each row of that container.
    """
    if node is None:
        return []
    rows: list[dict] = []
    for container, system in (("oilUplifts", "OIL"), ("hydraulicUplifts", "HYDRAULIC")):
        holder = node.find(container)
        if holder is None:
            continue
        sign = text(holder, "sign")
        for uplift in holder.findall("uplift"):
            rows.append(
                {
                    "system": system,
                    "code": text(uplift, "code"),
                    "measure": text(uplift, "measure"),
                    "sign": sign,
                }
            )
    return rows


def _action(node: ET.Element) -> dict:
    changes = node.find("componentChanges")
    bookings = node.find("staffTimeBookings")
    return {
        "uuid": node.get("uuid"),
        "action_type": text(node, "actionType"),
        "action_text": text(node, "actionText"),
        "closing_action": text(node, "closingAction"),
        "performed_ts": as_timestamp(text(node, "performedData/performedDateTime")),
        "performed_sign": text(node, "performedData/performedSign"),
        "inspection": _inspection(node.find("inspection")),
        "double_inspection": _inspection(node.find("doubleInspection")),
        "references": _references(node),
        "component_changes": [
            _component_change(c)
            for c in (changes.findall("componentChange") if changes is not None else [])
        ],
        "staff_time_bookings": [
            {
                "uuid": b.get("uuid"),
                "booking_status": text(b, "bookingStatus"),
                "duration": text(b, "duration"),
                "start_ts": as_timestamp(text(b, "startDateTime")),
                "end_ts": as_timestamp(text(b, "endDateTime")),
                "entity_code": text(b, "entityCode"),
                "type_of_work": text(b, "typeOfWork"),
                "skill": text(b, "skill"),
                "scope": text(b, "scope"),
                "user_sign": text(b, "userSign"),
                "remarks": text(b, "remarks"),
            }
            for b in (
                bookings.findall("staffTimeBooking") if bookings is not None else []
            )
        ],
        "uplifts": _uplifts(node.find("uplifts")),
    }


def _work_step(node: ET.Element) -> dict:
    required = node.find("requiredParts")
    resources = node.find("resourceRequest/resourceRequirements")
    actions = node.find("actions")
    return {
        "uuid": node.get("uuid"),
        "sequence_number": text(node, "workStepSequenceNumber"),
        "work_phase": text(node, "workPhase"),
        "description": text(node, "description"),
        "description_sign": text(node, "descriptionSign"),
        "headline": text(node, "headline"),
        "ts": as_timestamp(text(node, "workStepDateTime")),
        "state": text(node, "workstepState"),
        "double_inspection_required": as_bool(text(node, "doubleInspectionRequired")),
        "references": _references(node),
        "required_parts": [
            {
                "uuid": p.get("uuid"),
                "part_number": text(p, "partNumber"),
                "part_key": part_key(text(p, "partNumber")),
                "serial_number": text(p, "serialNumber"),
                "description": text(p, "description"),
                "needed_quantity": text(p, "neededQuantity"),
                "needed_quantity_uom": (
                    p.find("neededQuantity").get("UOM")
                    if p.find("neededQuantity") is not None
                    else None
                ),
                "probability_of_usage": text(p, "propabilityOfUsage"),
                "tool": text(p, "tool"),
                "ipc_reference": text(p, "ipcReference"),
                "location_on_aircraft": text(p, "locationOnAircraft"),
            }
            for p in (required.findall("partRequest") if required is not None else [])
        ],
        "resource_requirements": [
            {
                "uuid": r.get("uuid"),
                "classification": text(r, "classification"),
                "duration": text(r, "duration"),
                "quantity": text(r, "quantity"),
                "resource_type": text(r, "resourceType"),
                "total_hours": text(r, "totalHours"),
                "constraints": [
                    {
                        "uuid": c.get("uuid"),
                        "property_type": text(c, "propertyType"),
                        "value_string": text(c, "valueString"),
                    }
                    for c in (
                        r.find("constraints").findall("constraint")
                        if r.find("constraints") is not None
                        else []
                    )
                ],
            }
            for r in (
                resources.findall("resourceRequirement")
                if resources is not None
                else []
            )
        ],
        "actions": [
            _action(a)
            for a in (actions.findall("action") if actions is not None else [])
        ],
    }


def build_record(
    path: Path,
    root: ET.Element,
    selected_workorder: ET.Element | None = None,
) -> dict | None:
    """Return one BigQuery row for a workorder document, or None if unusable."""
    workorder = (
        selected_workorder
        if selected_workorder is not None
        else root.find(".//workorder")
    )
    if workorder is None:
        return None
    header = workorder.find("workorderHeader")
    if header is None:
        return None

    aircraft = header.find("aircraft")
    registration = (
        text(aircraft, "aircraftFullRegistration") if aircraft is not None else None
    )
    prefix = (
        registration.split("-")[0] if registration and "-" in registration else None
    )
    type_raw = text(aircraft, "aircraftType") if aircraft is not None else None
    checks = aircraft.find("routineChecks") if aircraft is not None else None

    payload = root.find("payload")
    transfer = root.find("payload/transferWorkorder")
    header_date = root.find("header/date")
    origin_ref = workorder.find("originWorkorderOfFinding/uniqueWorkorderReference")
    position = header.find("positionInfo")
    component = header.find("component")
    event = header.find("event")

    ata_chapter = text(header, "ataChapter")
    steps_container = workorder.find("workSteps")
    work_steps = [
        _work_step(s)
        for s in (
            steps_container.findall("workStep") if steps_container is not None else []
        )
    ]

    changes = [
        c
        for step in work_steps
        for a in step["actions"]
        for c in a["component_changes"]
    ]
    # Both sides of a swap count: on the ~19% of changes that are not
    # like-for-like, the installed part would otherwise never appear here.
    swap_keys = {
        part_key(c[field])
        for c in changes
        for field in ("part_off_number", "part_on_number")
        if c[field]
    }
    part_keys = sorted(
        {k for k in swap_keys if k}
        | {
            p["part_key"]
            for step in work_steps
            for p in step["required_parts"]
            if p["part_key"]
        }
    )

    return {
        "source_file": path.name,
        "envelope": {
            "sender_id": text(root, "header/senderID"),
            "receiver_id": text(root, "header/receiverID"),
            "conversation_id": text(root, "header/conversationID"),
            "amos_version": text(root, "header/amosVersion"),
            "instance_type": text(root, "header/instanceType"),
            "envelope_ts": as_timestamp(text(root, "header/date")),
            "date_timezone": header_date.get("timezone")
            if header_date is not None
            else None,
            "envelope_version": root.get("version"),
            "payload_type": payload.get("type") if payload is not None else None,
            "payload_version": payload.get("version") if payload is not None else None,
            "transfer_workorder_version": transfer.get("version")
            if transfer is not None
            else None,
        },
        "workorder_uuid": workorder.get("uuid"),
        "workorder_number": text(workorder, "workorderNumber"),
        "workorder_type": text(header, "workorderType"),
        "workorder_state": text(header, "workorderState"),
        "barcode": text(header, "barcode"),
        "ata_chapter": ata_chapter,
        "ata_code": ata_chapter.replace("-", "") if ata_chapter else None,
        "ata_major": ata_chapter[:2] if ata_chapter else None,
        "remarks": text(workorder, "remarks"),
        "external_workorder_number": text(header, "externalWorkorderNumber"),
        "workorder_origin_type": text(header, "workorderOriginType"),
        "origin_workorder_of_finding_uuid": origin_ref.get("uuid")
        if origin_ref is not None
        else None,
        "position_info": {
            "position": text(position, "position") if position is not None else None,
            "special": text(position, "special") if position is not None else None,
            "zone": text(position, "zone") if position is not None else None,
            "area": text(position, "area") if position is not None else None,
            "panel": text(position, "panel") if position is not None else None,
        },
        "component": {
            "part_number": text(component, "partNumber")
            if component is not None
            else None,
            "serial_number": text(component, "serialNumber")
            if component is not None
            else None,
        },
        "event": {
            "event_id": text(event, "eventID") if event is not None else None,
            "event_type": text(event, "eventType") if event is not None else None,
            "event_sub_type": text(event, "eventSubType")
            if event is not None
            else None,
            "description_title": text(event, "eventDescriptionTitle")
            if event is not None
            else None,
            "issuer_cage_code": text(event, "issuer/cageCode")
            if event is not None
            else None,
            "issuer_company": text(event, "issuer/company")
            if event is not None
            else None,
            "revision_number": text(event, "revision/revisionNumber")
            if event is not None
            else None,
        },
        "aircraft": {
            "type_raw": type_raw,
            "variant": VARIANT_BY_TYPE.get(type_raw) if type_raw else None,
            "full_registration": registration,
            "registration": text(aircraft, "aircraftRegistration")
            if aircraft is not None
            else None,
            "reg_country_prefix": prefix,
            "aoc": AOC_BY_PREFIX.get(prefix) if prefix else None,
            "msn": text(aircraft, "aircraftMsn") if aircraft is not None else None,
            "operator": text(aircraft, "operator") if aircraft is not None else None,
            "routine_checks": [
                {"check_code": text(c, "checkCode"), "performed": text(c, "performed")}
                for c in (checks.findall("routineCheck") if checks is not None else [])
            ],
        },
        "issue": {
            # Documents carry either issueDate or issueDateTime, not both.
            "date": as_date(
                text(header, "issueData/issueDate")
                or text(header, "issueData/issueDateTime")
            ),
            "ts": as_timestamp(text(header, "issueData/issueDateTime")),
            "sign": text(header, "issueData/issueSign"),
            "station": text(header, "issueData/issueStation"),
            "leg": text(header, "issueData/issueLeg"),
            "flt_from": text(header, "issueData/issueFltFrom"),
            "flt_to": text(header, "issueData/issueFltTo"),
            "tah_hours": as_int(text(header, "issueData/issueTahHours")),
            "tac": as_int(text(header, "issueData/issueTac")),
        },
        "closing": {
            "date": as_date(
                text(header, "closingData/closingDate")
                or text(header, "closingData/closingDateTime")
            ),
            "ts": as_timestamp(text(header, "closingData/closingDateTime")),
            "leg": text(header, "closingData/closingLeg"),
            "sign": text(header, "closingData/closingSign"),
            "station": text(header, "closingData/closingStation"),
            "total_aircraft_cycles": as_int(text(header, "closingData/closingTac")),
            # Left in minutes as AMOS reports it; some values look implausible as
            # airframe hours, so no derived hours column is offered here.
            "total_aircraft_hours_minutes": as_int(
                text(header, "closingData/closingTahMinutes")
            ),
        },
        "certification": {
            "number": text(header, "certification/number"),
            "ts": as_timestamp(text(header, "certification/dateTime")),
            "date": as_date(
                text(header, "certification/date")
                or text(header, "certification/dateTime")
            ),
        },
        "release_to_service": {
            "number": text(header, "releaseToService/number"),
            "ts": as_timestamp(text(header, "releaseToService/dateTime")),
            "release_sign": text(header, "releaseToService/releaseSign"),
        },
        "classifications": {
            "administrative_service_check": as_bool(
                text(header, "classifications/administrativeServiceCheck")
            ),
            "aog_risk": as_bool(text(header, "classifications/aogRisk")),
            "cdccl": as_bool(text(header, "classifications/cdccl")),
            "cdcp": as_bool(text(header, "classifications/cdcp")),
            "defect_classification": text(
                header, "classifications/defectClassification"
            ),
            "maintenance_type": text(header, "classifications/maintenanceType"),
            "occurence_report": as_bool(
                text(header, "classifications/occurenceReport")
            ),
            "ops_consequence": as_bool(text(header, "classifications/opsConsequence")),
            "structural_damage": as_bool(
                text(header, "classifications/structuralDamage")
            ),
            "tal": as_bool(text(header, "classifications/tal")),
            "watch_item": as_bool(text(header, "classifications/watchItem")),
        },
        "statistics": {
            "accident": as_bool(text(header, "statistics/accident")),
            "incident": as_bool(text(header, "statistics/incident")),
            "not_contracted": as_bool(text(header, "statistics/notContracted")),
        },
        "requests": {
            "engine_change": as_bool(text(header, "requests/engineChange")),
            "hangar_costs": text(header, "requests/hangarCosts"),
            "towing_costs": text(header, "requests/towingCosts"),
            "other": text(header, "requests/other"),
            "ramp_activity": as_bool(text(header, "requests/rampActivity")),
            "special_equipment": text(header, "requests/specialEquipement"),
            "special_equipment2": text(header, "requests/specialEquipement2"),
            "srm_repair": as_bool(text(header, "requests/srmRepair")),
            "taxi": as_bool(text(header, "requests/taxi")),
            "weight_and_balance": as_bool(text(header, "requests/weightAndBalance")),
        },
        "references": _references(workorder),
        "work_steps": work_steps,
        "part_swap_count": len(changes),
        "part_keys": part_keys,
    }


def build_records(path: Path, root: ET.Element) -> list[dict]:
    """Build one record per workorder while retaining ``build_record`` legacy API."""
    workorders = [
        node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "workorder"
    ]
    return [
        record
        for node in workorders
        if (record := build_record(path, root, selected_workorder=node)) is not None
    ]


def check_against_schema(record: dict, schema: list[dict], path: str = "") -> list[str]:
    """Report keys the schema does not declare, and declared fields never built.

    The schema and the record builder are hand-written side by side, so this
    guards against the two drifting apart as fields are added.
    """
    problems: list[str] = []
    declared = {f["name"]: f for f in schema}
    for key, value in record.items():
        where = f"{path}{key}"
        field = declared.get(key)
        if field is None:
            problems.append(f"record key not in schema: {where}")
            continue
        if field["type"] != "RECORD":
            continue
        children = value if isinstance(value, list) else [value]
        for child in children:
            if isinstance(child, dict):
                problems.extend(
                    check_against_schema(child, field["fields"], f"{where}.")
                )
    for name in declared:
        if name not in record:
            problems.append(f"schema field never produced: {path}{name}")
    return problems


def main() -> int:
    from scripts.wo_xml import iter_workorders

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)

    written = skipped = swaps = 0
    problems: list[str] = []
    seen_files = 0

    with gzip.open(NDJSON_PATH, "wt", encoding="utf-8") as out:
        for path, root in iter_workorders():
            seen_files += 1
            records = build_records(path, root)
            if not records:
                skipped += 1
                continue
            for record in records:
                if written == 0:
                    problems = check_against_schema(record, SCHEMA)
                swaps += record["part_swap_count"]
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1

    SCHEMA_PATH.write_text(json.dumps(SCHEMA, indent=2) + "\n")

    print(f"xml files parsed : {seen_files}")
    print(f"rows written     : {written}")
    print(f"skipped (no header): {skipped}")
    print(f"component changes: {swaps}")
    print(
        f"ndjson           : {NDJSON_PATH.relative_to(REPO_ROOT)} ({NDJSON_PATH.stat().st_size / 1e6:.1f} MB gz)"
    )
    print(f"schema           : {SCHEMA_PATH.relative_to(REPO_ROOT)}")
    if problems:
        print("\nSCHEMA DRIFT:")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
