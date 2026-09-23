"""Conservative, reproducible cohort construction for the AMOS snapshot.

This module deliberately constructs *candidates*, not component-failure labels.
The source export has component changes but no independently reviewed outcome table
or component-level observation denominator.  Callers must supply those artifacts
before a candidate can become a training row.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

TARGET_PNS = ("2085M31G03", "62197-301-001", "8201-11-0000-01")
PLACEHOLDER_SERIALS = frozenset(
    {
        "NA",
        "N/A",
        "N.A.",
        "NIL",
        "NONE",
        "NULL",
        "UNKNOWN",
        "UNK",
        "NOTKNOWN",
        "NOTAPPLICABLE",
        "TBD",
        "NOSERIAL",
        "NOSERIALNUMBER",
        "NOSN",
        "NOTSERIALIZED",
        "UNSERIALIZED",
        "NONSERIALIZED",
    }
)
REVIEWED_OUTCOMES = frozenset(
    {"confirmed_failure", "scheduled_or_serviceable", "other", "unknown"}
)


def normalized_identifier(value: object) -> str:
    """Return the punctuation-insensitive matching key used for part numbers."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalized_serial(value: object) -> str:
    """Preserve serial punctuation; only trim and uppercase it for linkage."""
    return str(value or "").strip().upper()


def is_placeholder_serial(value: object) -> bool:
    serial = normalized_serial(value)
    lexical = normalized_identifier(serial)
    return (
        not serial
        or lexical in PLACEHOLDER_SERIALS
        or bool(re.fullmatch(r"(?:SN|SERIAL|SERIALNO|SERIALNUMBER)?0+", lexical))
    )


def _parse_date(value: object) -> dt.date | None:
    if not value:
        return None
    text = str(value)
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None


def _parse_timestamp(value: object) -> dt.datetime | None:
    if not value or "T" not in str(value):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def _safe_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _text_hash(row: Mapping[str, Any]) -> str | None:
    fragments: list[str] = []
    for step in row.get("work_steps") or []:
        if not isinstance(step, Mapping):
            continue
        fragments.extend(
            str(step.get(key) or "") for key in ("description", "headline")
        )
    text = " ".join(fragments)
    text = re.sub(r"\s+", " ", text).strip().upper()
    if not text:
        return None
    return hashlib.sha256(text.encode()).hexdigest()


def _aircraft_key(row: Mapping[str, Any]) -> str | None:
    aircraft = row.get("aircraft") or {}
    if not isinstance(aircraft, Mapping):
        return None
    for key in ("msn", "full_registration", "registration"):
        value = normalized_identifier(aircraft.get(key))
        if value:
            return f"{key}:{value}"
    return None


@dataclasses.dataclass(frozen=True)
class ComponentEvent:
    event_id: str
    workorder_id: str
    pn: str
    serial: str
    side: str
    aircraft: str | None
    aircraft_family: str | None
    position: str
    date: dt.date | None
    closing_ts: dt.datetime | None
    tac: int | None
    narrative_hash: str | None


@dataclasses.dataclass(frozen=True)
class InstallationCandidate:
    candidate_id: str
    pn: str
    serial: str
    aircraft: str
    aircraft_family: str | None
    install_event_id: str
    removal_event_id: str
    install_workorder_id: str
    removal_workorder_id: str
    prediction_date: dt.date
    removal_date: dt.date
    install_tac: int
    removal_tac: int
    position: str
    continuity_reasons: tuple[str, ...]
    intervening_workorders: int
    narrative_hash: str | None
    is_target_pn: bool

    @property
    def continuity_ok(self) -> bool:
        return not self.continuity_reasons


@dataclasses.dataclass(frozen=True)
class LabelAssessment:
    status: str
    reason: str


def assess_horizon_label(
    reviewed_outcome: str | None,
    reliable_event_free_exposure: bool,
    horizon_approved: bool,
    *,
    valid_origin: bool = False,
    event_delta_cycles: int | None = None,
    horizon_cycles: int | None = None,
) -> LabelAssessment:
    """Classify only reviewed, horizon-matured evidence.

    A non-failure removal alone is not a negative.  A negative requires a reviewed
    non-failure outcome and independently reliable event-free observation through
    the approved horizon.
    """
    if not horizon_approved or horizon_cycles is None or horizon_cycles <= 0:
        return LabelAssessment("unknown", "horizon_not_approved")
    if not valid_origin:
        return LabelAssessment("unknown", "valid_component_origin_unavailable")
    if reviewed_outcome not in REVIEWED_OUTCOMES - {"unknown"}:
        return LabelAssessment("unknown", "outcome_not_independently_reviewed")
    if reviewed_outcome == "confirmed_failure":
        if event_delta_cycles is None or event_delta_cycles < 0:
            return LabelAssessment("unknown", "failure_timing_unavailable")
        if event_delta_cycles <= horizon_cycles:
            return LabelAssessment(
                "positive", "reviewed_confirmed_failure_within_horizon"
            )
        if reliable_event_free_exposure:
            return LabelAssessment(
                "negative", "reviewed_failure_after_observed_horizon"
            )
        return LabelAssessment("unknown", "no_reliable_event_free_horizon")
    if reliable_event_free_exposure:
        return LabelAssessment(
            "negative", "reviewed_nonfailure_with_observed_event_free_horizon"
        )
    return LabelAssessment("unknown", "no_reliable_event_free_horizon")


def extract_component_events(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[ComponentEvent], dict[str, int]]:
    """Extract normalized on/off events while retaining missing-origin diagnostics."""
    events: list[ComponentEvent] = []
    diagnostics: collections.Counter[str] = collections.Counter()
    for row_index, row in enumerate(rows):
        closing = row.get("closing") or {}
        date = _parse_date(closing.get("date") or closing.get("ts"))
        closing_ts = _parse_timestamp(closing.get("ts"))
        tac = _safe_int(closing.get("total_aircraft_cycles"))
        aircraft = _aircraft_key(row)
        aircraft_data = row.get("aircraft") or {}
        aircraft_family = (
            str(aircraft_data.get("type_raw") or "").strip() or None
            if isinstance(aircraft_data, Mapping)
            else None
        )
        workorder_id = str(
            row.get("workorder_uuid") or row.get("workorder_number") or row_index
        )
        narrative_hash = _text_hash(row)
        for step_index, step in enumerate(row.get("work_steps") or []):
            if not isinstance(step, Mapping):
                continue
            for action_index, action in enumerate(step.get("actions") or []):
                if not isinstance(action, Mapping):
                    continue
                for change_index, change in enumerate(
                    action.get("component_changes") or []
                ):
                    if not isinstance(change, Mapping):
                        continue
                    position = normalized_identifier(change.get("position"))
                    for side, pn_key, serial_key in (
                        ("on", "part_on_number", "part_on_serial"),
                        ("off", "part_off_number", "part_off_serial"),
                    ):
                        pn = normalized_identifier(change.get(pn_key))
                        serial = normalized_serial(change.get(serial_key))
                        if not pn:
                            diagnostics["missing_part_number"] += 1
                            continue
                        if is_placeholder_serial(serial):
                            diagnostics["missing_or_placeholder_serial"] += 1
                            continue
                        if aircraft is None:
                            diagnostics["missing_aircraft"] += 1
                        if date is None:
                            diagnostics["missing_closing_date"] += 1
                        if tac is None:
                            diagnostics["missing_closing_tac"] += 1
                        events.append(
                            ComponentEvent(
                                event_id=f"{workorder_id}:{step_index}:{action_index}:{change_index}:{side}",
                                workorder_id=workorder_id,
                                pn=pn,
                                serial=serial,
                                side=side,
                                aircraft=aircraft,
                                aircraft_family=aircraft_family,
                                position=position,
                                date=date,
                                closing_ts=closing_ts,
                                tac=tac,
                                narrative_hash=narrative_hash,
                            )
                        )
    return events, dict(sorted(diagnostics.items()))


def _between(event: ComponentEvent, start: ComponentEvent, end: ComponentEvent) -> bool:
    """Calendar/timestamp containment, deliberately independent of TAC quality."""
    if not event.date or not start.date or not end.date:
        return False
    if start.date < event.date < end.date:
        return True
    if start.date == end.date == event.date:
        return bool(
            start.closing_ts
            and event.closing_ts
            and end.closing_ts
            and start.closing_ts < event.closing_ts < end.closing_ts
        )
    if event.date == start.date:
        return bool(
            start.closing_ts
            and event.closing_ts
            and start.closing_ts < event.closing_ts
        )
    if event.date == end.date:
        return bool(
            event.closing_ts and end.closing_ts and event.closing_ts < end.closing_ts
        )
    return False


def _tac_outside(
    event: ComponentEvent, start: ComponentEvent, end: ComponentEvent
) -> bool:
    return bool(
        event.tac is not None
        and start.tac is not None
        and end.tac is not None
        and not start.tac < event.tac < end.tac
    )


def _row_between(
    date: dt.date | None,
    closing_ts: dt.datetime | None,
    start: ComponentEvent,
    end: ComponentEvent,
) -> bool:
    probe = ComponentEvent(
        "", "", "", "", "", None, None, "", date, closing_ts, None, None
    )
    return _between(probe, start, end)


def _candidate_id(install: ComponentEvent, removal: ComponentEvent) -> str:
    value = "|".join((install.event_id, removal.event_id, install.pn, install.serial))
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def build_installation_candidates(
    rows: Sequence[Mapping[str, Any]], target_pns: Sequence[str] = TARGET_PNS
) -> tuple[list[InstallationCandidate], dict[str, int]]:
    """Pair adjacent observed on/off events without claiming uninterrupted service."""
    events, diagnostics = extract_component_events(rows)
    groups: dict[tuple[str, str, str], list[ComponentEvent]] = collections.defaultdict(
        list
    )
    serial_locations: dict[tuple[str, str], list[ComponentEvent]] = (
        collections.defaultdict(list)
    )
    rows_by_aircraft: dict[
        str, list[tuple[dt.date | None, dt.datetime | None, int | None, str]]
    ] = collections.defaultdict(list)
    target_keys = {normalized_identifier(pn) for pn in target_pns}
    for event in events:
        serial_locations[(event.pn, event.serial)].append(event)
        if event.aircraft:
            groups[(event.pn, event.serial, event.aircraft)].append(event)
    for row_index, row in enumerate(rows):
        aircraft = _aircraft_key(row)
        if not aircraft:
            continue
        closing = row.get("closing") or {}
        rows_by_aircraft[aircraft].append(
            (
                _parse_date(closing.get("date") or closing.get("ts")),
                _parse_timestamp(closing.get("ts")),
                _safe_int(closing.get("total_aircraft_cycles")),
                str(
                    row.get("workorder_uuid")
                    or row.get("workorder_number")
                    or row_index
                ),
            )
        )

    candidates: list[InstallationCandidate] = []
    rejected = collections.Counter[str]()
    for (pn, serial, aircraft), stream in groups.items():
        stream = sorted(
            stream,
            key=lambda e: (
                e.date or dt.date.min,
                e.closing_ts or dt.datetime.min.replace(tzinfo=dt.UTC),
                -1 if e.tac is None else e.tac,
                e.event_id,
            ),
        )
        ambiguous_endpoints = collections.Counter(
            (event.workorder_id, event.date, event.closing_ts, event.tac)
            for event in stream
        )
        for install, removal in pairwise(stream):
            if install.side != "on" or removal.side != "off":
                continue
            if install.date is None or removal.date is None:
                rejected["missing_ordering_date"] += 1
                continue
            if install.tac is None or removal.tac is None:
                rejected["missing_ordering_tac"] += 1
                continue
            if install.workorder_id == removal.workorder_id:
                rejected["same_workorder_endpoint"] += 1
                continue
            if (
                ambiguous_endpoints[
                    (
                        install.workorder_id,
                        install.date,
                        install.closing_ts,
                        install.tac,
                    )
                ]
                > 1
                or ambiguous_endpoints[
                    (
                        removal.workorder_id,
                        removal.date,
                        removal.closing_ts,
                        removal.tac,
                    )
                ]
                > 1
            ):
                rejected["ambiguous_repeated_endpoint"] += 1
                continue
            if install.date > removal.date or (
                install.date == removal.date
                and (
                    install.closing_ts is None
                    or removal.closing_ts is None
                    or install.closing_ts >= removal.closing_ts
                )
            ):
                rejected["non_increasing_or_ambiguous_date"] += 1
                continue
            if install.tac >= removal.tac:
                rejected["non_increasing_tac"] += 1
                continue
            reasons: list[str] = []
            if not install.position or not removal.position:
                reasons.append("missing_position")
            elif install.position != removal.position:
                reasons.append("position_conflict")
            for event in serial_locations[(pn, serial)]:
                if (
                    event.aircraft
                    and event.aircraft != aircraft
                    and event.date
                    and install.date <= event.date <= removal.date
                ):
                    reasons.append("cross_aircraft_serial_conflict")
                    break
            for event in events:
                if (
                    event.aircraft == aircraft
                    and event.pn == pn
                    and event.event_id not in {install.event_id, removal.event_id}
                    and _between(event, install, removal)
                    and install.position
                    and event.position == install.position
                ):
                    reasons.append("intervening_same_position_movement")
                    break
            for event in events:
                if (
                    event.aircraft == aircraft
                    and event.event_id not in {install.event_id, removal.event_id}
                    and _between(event, install, removal)
                    and _tac_outside(event, install, removal)
                ):
                    reasons.append("intervening_tac_outside_endpoint_range")
                    break
            for date, closing_ts, tac, _workorder_id in rows_by_aircraft[aircraft]:
                if (
                    _row_between(date, closing_ts, install, removal)
                    and tac is not None
                    and not install.tac < tac < removal.tac
                ):
                    reasons.append("intervening_workorder_tac_outside_endpoint_range")
                    break
            intervening_workorders = len(
                {
                    workorder_id
                    for date, closing_ts, _tac, workorder_id in rows_by_aircraft[
                        aircraft
                    ]
                    if _row_between(date, closing_ts, install, removal)
                    and workorder_id not in {install.workorder_id, removal.workorder_id}
                }
            )
            for reason in set(reasons):
                rejected[reason] += 1
            candidates.append(
                InstallationCandidate(
                    candidate_id=_candidate_id(install, removal),
                    pn=pn,
                    serial=serial,
                    aircraft=aircraft,
                    aircraft_family=install.aircraft_family,
                    install_event_id=install.event_id,
                    removal_event_id=removal.event_id,
                    install_workorder_id=install.workorder_id,
                    removal_workorder_id=removal.workorder_id,
                    prediction_date=install.date,
                    removal_date=removal.date,
                    install_tac=install.tac,
                    removal_tac=removal.tac,
                    position=install.position,
                    continuity_reasons=tuple(sorted(set(reasons))),
                    intervening_workorders=intervening_workorders,
                    narrative_hash=install.narrative_hash or removal.narrative_hash,
                    is_target_pn=pn in target_keys,
                )
            )
    return candidates, {
        **diagnostics,
        **{f"candidate_{k}": v for k, v in sorted(rejected.items())},
    }


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


def group_related_episodes(
    candidates: Sequence[InstallationCandidate],
) -> dict[str, str]:
    """Keep shared endpoint WOs and duplicated narratives in one split group."""
    union_find = _UnionFind(candidate.candidate_id for candidate in candidates)
    seen: dict[tuple[str, str], str] = {}
    for candidate in candidates:
        keys = [
            ("workorder", candidate.install_workorder_id),
            ("workorder", candidate.removal_workorder_id),
        ]
        if candidate.narrative_hash:
            keys.append(("narrative", candidate.narrative_hash))
        for key in keys:
            if key in seen:
                union_find.union(candidate.candidate_id, seen[key])
            else:
                seen[key] = candidate.candidate_id
    members: dict[str, list[str]] = collections.defaultdict(list)
    for candidate in candidates:
        members[union_find.find(candidate.candidate_id)].append(candidate.candidate_id)
    group_ids = {
        root: hashlib.sha256("|".join(sorted(values)).encode()).hexdigest()[:24]
        for root, values in members.items()
    }
    return {
        candidate.candidate_id: group_ids[union_find.find(candidate.candidate_id)]
        for candidate in candidates
    }


def build_split_manifest(
    candidates: Sequence[InstallationCandidate],
    source_sha256: str,
    source_name: str,
    source_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a deterministic split separate from failure-label eligibility."""
    usable = [candidate for candidate in candidates if candidate.continuity_ok]
    groups = group_related_episodes(usable)
    grouped: dict[str, list[InstallationCandidate]] = collections.defaultdict(list)
    for candidate in usable:
        grouped[groups[candidate.candidate_id]].append(candidate)
    dates = sorted({candidate.prediction_date for candidate in usable})
    if dates:
        train_end = dates[(len(dates) * 70 - 1) // 100]
        dev_end = dates[(len(dates) * 85 - 1) // 100]
    else:
        train_end = dev_end = None
    assignments: list[dict[str, Any]] = []
    group_split: dict[str, str] = {}
    for group_id, members in sorted(grouped.items()):
        group_date = max(member.prediction_date for member in members)
        has_target = any(member.is_target_pn for member in members)
        if train_end is None or dev_end is None:
            split = "purged"
        elif group_date <= train_end:
            split = "train"
        elif group_date <= dev_end:
            split = "development"
        elif has_target:
            split = "final_test"
        else:
            split = "purged"
        group_split[group_id] = split
        assignments.append(
            {
                "episode_group_id": group_id,
                "prediction_date": group_date.isoformat(),
                "split": split,
                "candidate_count": len(members),
                "target_pns": sorted(
                    {member.pn for member in members if member.is_target_pn}
                ),
                "label_maturity": "unavailable_horizon_and_review_not_approved",
                "failure_training_eligible": False,
            }
        )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source": {"name": source_name, "sha256": source_sha256},
        "split_method": {
            "name": "calendar_70_15_newer_target_final",
            "train_end": train_end.isoformat() if train_end else None,
            "development_end": dev_end.isoformat() if dev_end else None,
            "final_test": "Only episode groups with a target PN after development_end.",
            "purge": "Shared endpoint workorders and duplicate symptom-only narrative hashes are assigned as one episode group; newer non-target groups are purged from predictive evaluation.",
            "label_maturity": "No row is trainable until the approved horizon has elapsed and its reviewed outcome/exposure is present.",
        },
        "manifest_entry_contract": {
            "key": "source_workorder_id",
            "split_values": ["train", "development", "final_test", "purged"],
            "historical_case": "Closed-snapshot text is snapshot-only and visible only when the envelope export timestamp is strictly before request_as_of.",
            "prediction_time_symptom": "Closed-snapshot symptom text is not verified as available at request_as_of and is excluded from prediction features.",
            "retrieval": "Retrieval eligibility is independent of failure_training_eligible.",
        },
        "immutable": {
            "policy": "write_once",
            "rule": "A changed source hash, split method, or assignment requires a new manifest path/version; existing frozen manifests are never overwritten.",
        },
        "assignments": assignments,
        "workorder_split_index": {
            "format": "ndjson, one source_workorder_id mapping per line",
            "source_workorder_count": len(source_rows or ()),
            "derivation": "Deterministic from this manifest's source hash, cutoffs, episode grouping and source rows.",
        },
    }
    frozen_material = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    manifest["freeze_id"] = hashlib.sha256(frozen_material).hexdigest()
    return manifest


def build_workorder_split_index(
    candidates: Sequence[InstallationCandidate],
    source_rows: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return the separate full-WO index consumed by retrieval preparation."""
    usable = [candidate for candidate in candidates if candidate.continuity_ok]
    groups = group_related_episodes(usable)
    endpoint_group = {
        workorder_id: groups[candidate.candidate_id]
        for candidate in usable
        for workorder_id in (
            candidate.install_workorder_id,
            candidate.removal_workorder_id,
        )
    }
    group_split = {
        str(assignment["episode_group_id"]): str(assignment["split"])
        for assignment in manifest.get("assignments", [])
    }
    method = manifest.get("split_method", {})
    train_end = _parse_date(method.get("train_end"))
    dev_end = _parse_date(method.get("development_end"))
    return _source_workorder_assignments(
        source_rows, endpoint_group, group_split, train_end, dev_end
    )


def _source_workorder_assignments(
    rows: Sequence[Mapping[str, Any]],
    endpoint_group: Mapping[str, str],
    group_split: Mapping[str, str],
    train_end: dt.date | None,
    dev_end: dt.date | None,
) -> dict[str, dict[str, Any]]:
    """Publish per-WO split and as-of provenance for the retrieval pipeline."""
    target_keys = {normalized_identifier(pn) for pn in TARGET_PNS}
    records: list[dict[str, Any]] = []
    narrative_members: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for index, row in enumerate(rows):
        workorder_id = str(
            row.get("workorder_uuid") or row.get("workorder_number") or index
        )
        closing = row.get("closing") or {}
        closing_date = _parse_date(closing.get("date") or closing.get("ts"))
        envelope = row.get("envelope") or {}
        export_timestamp = _parse_timestamp(
            envelope.get("envelope_ts") if isinstance(envelope, Mapping) else None
        )
        has_target = False
        for step in row.get("work_steps") or []:
            if not isinstance(step, Mapping):
                continue
            for action in step.get("actions") or []:
                if not isinstance(action, Mapping):
                    continue
                for change in action.get("component_changes") or []:
                    if isinstance(change, Mapping) and (
                        normalized_identifier(change.get("part_on_number"))
                        in target_keys
                        or normalized_identifier(change.get("part_off_number"))
                        in target_keys
                    ):
                        has_target = True
        narrative_hash = _text_hash(row)
        record = {
            "workorder_id": workorder_id,
            "closing_date": closing_date,
            "export_timestamp": export_timestamp,
            "has_target": has_target,
            "narrative_hash": narrative_hash,
        }
        records.append(record)
        if narrative_hash:
            narrative_members[narrative_hash].append(record)
    duplicate_split: dict[str, str] = {}
    duplicate_group_id: dict[str, str] = {}
    for narrative_hash, members in narrative_members.items():
        group_date = max(
            (member["closing_date"] for member in members if member["closing_date"]),
            default=None,
        )
        has_target = any(member["has_target"] for member in members)
        if group_date is None or train_end is None or dev_end is None:
            split = "purged"
        elif group_date <= train_end:
            split = "train"
        elif group_date <= dev_end:
            split = "development"
        elif has_target:
            split = "final_test"
        else:
            split = "purged"
        duplicate_split[narrative_hash] = split
        duplicate_group_id[narrative_hash] = hashlib.sha256(
            "|".join(sorted(member["workorder_id"] for member in members)).encode()
        ).hexdigest()[:24]
    assignments: dict[str, dict[str, Any]] = {}
    for record in records:
        workorder_id = record["workorder_id"]
        group_id = endpoint_group.get(workorder_id)
        closing_date = record["closing_date"]
        narrative_hash = record["narrative_hash"]
        if narrative_hash and len(narrative_members[narrative_hash]) > 1:
            split, purge_reason = (
                duplicate_split[narrative_hash],
                "duplicate_narrative_grouped",
            )
        elif group_id:
            split, purge_reason = group_split[group_id], None
        elif closing_date is None or train_end is None or dev_end is None:
            split, purge_reason = "purged", "missing_calendar_date_or_cutoff"
        elif closing_date <= train_end:
            split, purge_reason = "train", None
        elif closing_date <= dev_end:
            split, purge_reason = "development", None
        elif record["has_target"]:
            split, purge_reason = "final_test", None
        else:
            split, purge_reason = (
                "purged",
                "newer_non_target_not_in_final_predictive_evaluation",
            )
        duplicate = bool(narrative_hash and len(narrative_members[narrative_hash]) > 1)
        assignments[workorder_id] = {
            "split": split,
            "episode_group_id": group_id
            or (duplicate_group_id.get(narrative_hash) if narrative_hash else None),
            "purge_reason": purge_reason,
            "failure_training_eligible": False,
            "label_maturity": "unavailable_horizon_and_review_not_approved",
            "historical_case": {
                "available_from": record["export_timestamp"].isoformat()
                if record["export_timestamp"]
                else None,
                "requires_request_as_of_strictly_after": True,
                "availability_status": "snapshot_only"
                if record["export_timestamp"]
                else "unknown",
                "available": record["export_timestamp"] is not None,
            },
            "prediction_time_symptom": {
                "available_from": None,
                "available": False,
                "availability_status": "unverified_in_closed_snapshot",
                "excludes": "all closed-snapshot symptom text, closing actions, removal text, and outcomes",
            },
            "retrieval_index_eligible": bool(
                record["export_timestamp"]
                and narrative_hash
                and not duplicate
                and split not in {"final_test", "purged"}
            ),
            "retrieval_evaluation_eligible": bool(
                record["export_timestamp"]
                and narrative_hash
                and not duplicate
                and split not in {"final_test", "purged"}
            ),
            "duplicate_symptom_narrative_grouped": duplicate,
        }
    return dict(sorted(assignments.items()))


def summarize_feasibility(
    rows: Sequence[Mapping[str, Any]], source_sha256: str, source_name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates, diagnostics = build_installation_candidates(rows)
    episode_groups = group_related_episodes(candidates)
    per_pn: dict[str, dict[str, Any]] = {}
    target_keys = {normalized_identifier(pn) for pn in TARGET_PNS}
    all_pns = sorted({candidate.pn for candidate in candidates} | target_keys)
    for pn in all_pns:
        selected = [candidate for candidate in candidates if candidate.pn == pn]
        clean = [candidate for candidate in selected if candidate.continuity_ok]
        independent = {episode_groups[candidate.candidate_id] for candidate in clean}
        per_pn[pn] = {
            "candidate_installation_to_removal_pairs": len(selected),
            "continuity_unflagged_candidates": len(clean),
            "independent_episode_groups": len(independent),
            "observed_on_event_origins": len(clean),
            "validated_complete_component_origins": 0,
            "aircraft_family_support": dict(
                sorted(
                    collections.Counter(
                        candidate.aircraft_family or "UNKNOWN" for candidate in clean
                    ).items()
                )
            ),
            "intervening_workorders_inside_candidates": sum(
                candidate.intervening_workorders for candidate in clean
            ),
            "position_or_serial_conflicts": dict(
                sorted(
                    collections.Counter(
                        reason
                        for candidate in selected
                        for reason in candidate.continuity_reasons
                    ).items()
                )
            ),
            "reviewed_failure_labels": 0,
            "reliable_event_free_horizons": 0,
            "risk_labels_positive": 0,
            "risk_labels_negative": 0,
            "risk_labels_unknown": len(clean),
            "training_eligible": 0,
        }
    clean_all = [candidate for candidate in candidates if candidate.continuity_ok]
    gate_reasons = [
        "cycle_horizon_warning_lead_and_replacement_policy_not_approved",
        "no_independently_reviewed_failure_or_removal_labels_in_fixed_corpus",
        "no_reliable_component_level_event_free_exposure_through_a_horizon",
        "no_verified_complete_component_installation_origin_or_service_history",
        "candidate_endpoints_do_not_establish_continuous_installation_or_healthy_follow_up",
    ]
    audit = {
        "schema_version": 1,
        "scope": "Fixed local AMOS snapshot only; FAA reports are not joined into aircraft timelines.",
        "source": {
            "name": source_name,
            "sha256": source_sha256,
            "workorders": len(rows),
        },
        "definitions": {
            "linkage": "Uppercase alphanumeric PN plus exact trimmed-uppercase serial, exact same aircraft identity, strictly ordered closing timestamp where same-day ordering is needed, and strictly increasing aircraft TAC.",
            "candidate": "Adjacent observed on-to-off serial occurrence. It is an audit candidate, not proof of uninterrupted installation, failure, or a prediction example.",
            "negative": "Only a reviewed non-failure with independently reliable component-level event-free observation through the approved horizon.",
            "unknown": "Missing review, horizon, component-level follow-up, or origin is unknown; it is never converted to a negative.",
        },
        "all_pn_support": {
            "candidate_installation_to_removal_pairs": len(candidates),
            "continuity_unflagged_candidates": len(clean_all),
            "independent_episode_groups": len(
                {episode_groups[candidate.candidate_id] for candidate in clean_all}
            ),
            "observed_on_event_origins": len(clean_all),
            "validated_complete_component_origins": 0,
            "reviewed_failure_labels": 0,
            "reliable_event_free_horizons": 0,
            "risk_labels_positive": 0,
            "risk_labels_negative": 0,
            "risk_labels_unknown": len(clean_all),
            "training_eligible": 0,
        },
        "per_pn_support": per_pn,
        "target_part_support": {
            raw_pn: per_pn[normalized_identifier(raw_pn)] for raw_pn in TARGET_PNS
        },
        "diagnostics": diagnostics,
        "risk_training_gate": {
            "passed": False,
            "reasons": gate_reasons,
            "triage_experiment": "unavailable: no independently reviewed labels in the fixed corpus",
            "model_training": "blocked: do not fit failure-risk or removal-association models",
        },
        "remaining_required_inputs": [
            "An engineer-approved cycle horizon, warning lead and replacement-policy owner.",
            "Independently reviewed outcome labels per removal/event, including confirmed defect-related failures versus scheduled/serviceable/other/unknown.",
            "Component-level installation origin and reliable event-free observation/censoring through the approved horizon.",
            "A new source snapshot or separately versioned reviewed-label artifact when evidence changes.",
        ],
    }
    return audit, build_split_manifest(candidates, source_sha256, source_name, rows)


def load_ndjson(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    if path.suffix == ".gz":
        import gzip

        raw = gzip.decompress(raw)
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return rows, hashlib.sha256(raw).hexdigest()
