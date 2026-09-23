#!/usr/bin/env python3
"""
Generate synthetic curated tables for PMA POC Plan B.

Outputs (data/processed/):
  wo_embeddings.ndjson.gz          – workorder embeddings for online retrieval
  fct_lead_time_samples.ndjson.gz  – lead-time samples per focus component
  dim_focus_components.ndjson.gz   – focus component dimension
  dim_reference_set.ndjson.gz      – reference replacement records

Usage:
  uv run python scripts/generate_synthetic_curated.py
  uv run python scripts/generate_synthetic_curated.py --tier smoke
  uv run python scripts/generate_synthetic_curated.py --tier large --seed 42
"""

import argparse
import gzip
import json
import math
import uuid
from collections import Counter
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).parent.parent
OUT_DIR = REPO_ROOT / "data" / "processed"

EMBEDDING_DIM = 768

# ──────────────────────────────────────────────────────────────────────────────
# Component profiles  (truth model from Plan B spec)
# ──────────────────────────────────────────────────────────────────────────────

COMPONENTS = [
    {
        "component_key": "C1_LDG_NOSE_SHOCK",
        "ata_chapter": "32",
        "p50": 80,
        "p90": 220,
        "symptom_texts": [
            "Nose gear shock absorber found leaking hydraulic fluid. Progressive seal degradation confirmed.",
            "Nose landing gear oleo strut insufficient extension. Strut servicing required.",
            "Nose gear shimmy on landing. Torque link wear beyond serviceable limits.",
            "Oleo piston chrome surface pitting corrosion. Seal replacement mandatory.",
            "Nose gear steering actuator jerky. Hydraulic return pressure anomaly noted.",
            "Oleo strut low pressure on extension. Fluid seepage around lower bearing.",
            "Nose gear door not fully closing. Actuator rod-end bearing worn.",
            "Shimmy damper ineffective. Nose gear oscillation reported during taxi.",
        ],
    },
    {
        "component_key": "C2_FLT_CTRL_AIL_ACT",
        "ata_chapter": "27",
        "p50": 140,
        "p90": 320,
        "symptom_texts": [
            "Aileron actuator internal bypass detected. Control surface sluggish during preflight.",
            "Port aileron PCU excessive null band. Rigging reveals actuator wear beyond limits.",
            "Aileron control surface binding. Actuator rod-end bearing play exceeds limit.",
            "Flight control BITE logged aileron actuator fault. Internal leakage confirmed on bench.",
            "Starboard aileron PCU pressure drop under load. Actuator piston seal failure.",
            "Aileron feel asymmetry reported by crew. PCU servo valve contamination suspected.",
            "Right aileron authority reduced. Hydraulic actuator end-of-travel spongy.",
            "Aileron rigging check: actuator output below required force. Internal wear confirmed.",
        ],
    },
    {
        "component_key": "C3_HYD_PUMP_ENG1",
        "ata_chapter": "29",
        "p50": 60,
        "p90": 180,
        "symptom_texts": [
            "Hydraulic pump 1A pressure fluctuating. Case drain filter bypassed. Pump replaced.",
            "Engine 1 hydraulic pump low pressure caution. Flow below minimum threshold.",
            "Hydraulic system 1 high temperature indication. Pump inefficiency confirmed on test.",
            "Case drain flow excessive on pump 1. Internal wear on pump internals confirmed.",
            "Engine-driven hydraulic pump 1 noise during ground run. Cavitation suspected.",
            "Pump 1 pressure relief valve unseating. System pressure spikes during operation.",
            "Hydraulic pump 1 loud continuous tone. Bearing degradation confirmed.",
            "Pump 1 output 1500 psi below normal. Variable displacement regulator failure.",
        ],
    },
]

# ──────────────────────────────────────────────────────────────────────────────
# Text templates for routine and hard-negative WOs
# ──────────────────────────────────────────────────────────────────────────────

ROUTINE_TEMPLATES = [
    "Performed {interval}-hour check on {system}. All parameters within limits. No defects found.",
    "Lubricated {component} per CMM {cmm_ref}. Torque values verified. Completed satisfactorily.",
    "Inspected {area} for cracks and corrosion. NDT result negative. Serviceable.",
    "Replaced {consumable} at scheduled interval. System tested serviceable.",
    "Checked {fluid} levels and replenished to correct quantity. No leaks detected.",
    "Functional check of {system} completed. Response times within specification.",
    "Cleaned and inspected {component}. No abnormal wear observed. Returned to service.",
    "Operational check of {equipment} satisfactory. No faults found.",
    "Borescope inspection of {engine_zone}. No erosion or damage. Within limits.",
    "Torque check on {fastener_area} fasteners. All within limits. No re-torque required.",
]

ROUTINE_WORDS = {
    "interval": ["200", "400", "600", "1200", "2400", "A-check", "C-check"],
    "system": ["landing gear", "hydraulic system", "flight control", "fuel system", "avionics bay cooling", "ECS"],
    "component": ["MLG door hinges", "nose gear torque links", "aileron hinges", "rudder quadrant bearings", "elevator feel unit"],
    "cmm_ref": ["32-10-11", "27-11-00", "29-10-03", "28-20-01", "21-51-00"],
    "area": ["fuselage skin lap joint", "wing lower surface", "pressure bulkhead", "belly fairing attachment"],
    "consumable": ["hydraulic filter element", "fuel filter", "oil filter", "air cycle machine filter"],
    "fluid": ["hydraulic fluid", "engine oil", "brake fluid"],
    "equipment": ["ACARS", "GPWS", "TCAS II", "ELT", "FDR", "CVR"],
    "engine_zone": ["HPT blade path", "LPT stage 1", "combustion liner", "fan blade leading edge"],
    "fastener_area": ["wing leading edge slat track", "flap track", "main gear beam"],
}

HARD_NEG_TEMPLATES = [
    "Inspection of {adj_system} satisfactory. No evidence of {symptom} noted in primary component.",
    "{ata_label} component visual check: surface condition acceptable. Minor {minor_finding} noted. Monitoring.",
    "Similar appearance to {wo_ref} but confirmed unrelated to {comp_desc}. Root cause: normal wear.",
    "Crew report of {symptom}: investigation found source in {adj_system}. Primary component serviceable.",
]

HARD_NEG_WORDS = {
    "adj_system": ["adjacent hydraulic line", "nearby wiring loom", "surrounding structure", "neighbouring servo valve"],
    "symptom": ["fluid seepage", "binding", "excessive play", "pressure fluctuation", "vibration"],
    "ata_label": ["ATA 21", "ATA 24", "ATA 28", "ATA 36", "ATA 52"],
    "minor_finding": ["tooling mark", "surface oxidation", "paint chip within limits", "slight dent within limits"],
    "wo_ref": ["previous WO-10445", "WO-09231", "similar WO last C-check"],
    "comp_desc": ["shock absorber", "actuator assembly", "pump body", "servo valve", "control rod"],
}

AIRCRAFT_PREFIXES = ["CS-T", "CS-T", "EC-M", "EC-N", "OE-L", "G-E", "D-A", "F-G", "I-B", "PH-B"]
AIRCRAFT_SUFFIX_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

# Target mix for the WO corpus (symptom includes seed rows from lead-time samples)
MIX_SYMPTOM = 0.20
MIX_HARD_NEG = 0.15

TIERS = {
    "smoke":  {"n_aircraft": 30,  "n_wo": 5_000,   "lt_per_comp": 50},
    "medium": {"n_aircraft": 300, "n_wo": 50_000,  "lt_per_comp": 120},
    "large":  {"n_aircraft": 500, "n_wo": 200_000, "lt_per_comp": 250},
}

# ──────────────────────────────────────────────────────────────────────────────
# Real top part numbers loaded from top_part_numbers.md
# ──────────────────────────────────────────────────────────────────────────────

TOP_PARTS_FILE = REPO_ROOT / "top_part_numbers.md"

TOP_PART_ATA_POOL = ["21", "27", "28", "29", "32", "36", "49", "78"]

TOP_PART_TEMPLATES = [
    "Component P/N {pn} unserviceable during scheduled check. Removed and replaced per CMM. Ops check satisfactory.",
    "Part {pn} showed excessive wear at inspection interval. Replacement fitted and system tested.",
    "P/N {pn} internal leakage detected on functional test. Removed for overhaul. New unit installed.",
    "Component P/N {pn} reported inoperative by crew. Troubleshooting isolated to unit. Replaced serviceable.",
    "Part {pn} out-of-spec on bench verification. New unit fitted. System restored to normal.",
    "P/N {pn} failed periodic integrity check. Replacement scheduled and completed. No further defect.",
    "Component P/N {pn} exceeded life limit at MEL threshold. Removed at scheduled interval. Fitted new.",
    "P/N {pn} intermittent operation reported. Bench test confirmed defect. Replaced and released.",
]


# ──────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────────────────

def lognormal_params(p50: float, p90: float):
    """Return (mu, sigma) of lognormal whose 50th/90th percentiles hit targets."""
    mu = math.log(p50)
    sigma = (math.log(p90) - math.log(p50)) / 1.2816  # z(0.90) = 1.2816
    return mu, sigma


def make_aircraft_regs(n: int, rng: np.random.Generator):
    regs = set()
    while len(regs) < n:
        pfx = str(rng.choice(AIRCRAFT_PREFIXES))
        sfx = "".join(str(c) for c in rng.choice(list(AIRCRAFT_SUFFIX_CHARS), size=2))
        regs.add(f"{pfx}{sfx}")
    return sorted(regs)


def fill_template(template: str, word_bank: dict, rng: np.random.Generator) -> str:
    import re
    result = template
    for key in re.findall(r"\{(\w+)\}", template):
        if key in word_bank:
            result = result.replace("{" + key + "}", str(rng.choice(word_bank[key])), 1)
    return result


def unit_vector(dim: int, rng: np.random.Generator) -> np.ndarray:
    v = rng.standard_normal(dim)
    return v / np.linalg.norm(v)


def noisy_embedding(centroid: np.ndarray, noise_scale: float, rng: np.random.Generator):
    v = centroid + rng.standard_normal(len(centroid)) * noise_scale
    v = v / np.linalg.norm(v)
    return v.tolist()


def make_uuid_str(rng: np.random.Generator) -> str:
    b = rng.integers(0, 256, size=16, dtype=np.uint8).tobytes()
    return str(uuid.UUID(bytes=b))


def make_wo_id(counter: int) -> str:
    return f"WO-{counter:06d}"


def write_ndjson_gz(rows, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    print(f"  wrote {len(rows):,} rows  ->  {path.name}")


# ──────────────────────────────────────────────────────────────────────────────
# Top-parts profile builder
# ──────────────────────────────────────────────────────────────────────────────

def load_top_parts(path: Path) -> list[str]:
    """Parse tab-separated top_part_numbers.md → sorted list of unique P/Ns.

    File format per row: <rank>\\t<part_number>|<position>\\t<count1>\\t<count2>
    We only need column 2 up to the '|' separator.
    """
    parts: set[str] = set()
    if not path.exists():
        return []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        pn = cols[1].split("|", 1)[0].strip()
        if pn:
            parts.add(pn)
    return sorted(parts)


def build_top_part_components(part_numbers: list[str], rng) -> list[dict]:
    """Build synthetic component profiles keyed by real part number.

    Each profile mirrors the shape of COMPONENTS (component_key + ata_chapter +
    p50/p90 + symptom_texts) so the existing generators can consume it unchanged.
    ATA chapter is deterministic per part; p50/p90 are sampled per part so
    distributions vary across the 12 rows.
    """
    profiles = []
    for pn in part_numbers:
        ata = TOP_PART_ATA_POOL[abs(hash(pn)) % len(TOP_PART_ATA_POOL)]
        p50 = int(rng.integers(60, 180))
        p90 = p50 + int(rng.integers(80, 260))
        profiles.append({
            "component_key": pn,
            "ata_chapter": ata,
            "p50": p50,
            "p90": p90,
            "symptom_texts": [tpl.format(pn=pn) for tpl in TOP_PART_TEMPLATES],
        })
    return profiles


# ──────────────────────────────────────────────────────────────────────────────
# Generators
# ──────────────────────────────────────────────────────────────────────────────

def generate_lead_time_and_seed_wos(aircraft_regs, components, centroids, lt_per_comp: int, rng):
    """
    Returns (lt_rows, seed_wo_rows).
    seed_wo_rows are precursor WOs (symptom class) that must appear in wo_embeddings.
    Hard constraint: replacement_tac = precursor_tac + lead_cycles.
    """
    lt_rows = []
    seed_wo_rows = []
    wo_counter = 1

    for idx, comp in enumerate(components):
        mu, sigma = lognormal_params(comp["p50"], comp["p90"])
        centroid = centroids[idx]

        for _ in range(lt_per_comp):
            aircraft = str(rng.choice(aircraft_regs))
            precursor_tac = int(rng.integers(5_000, 25_000))
            lead_cycles = max(1, min(int(rng.lognormal(mu, sigma)), 5_000))
            replacement_tac = precursor_tac + lead_cycles  # enforced constraint

            precursor_wo_uuid = make_uuid_str(rng)
            replacement_wo_uuid = make_uuid_str(rng)
            precursor_wo_id = make_wo_id(wo_counter)
            wo_counter += 1

            seed_wo_rows.append({
                "wo_uuid": precursor_wo_uuid,
                "wo_id": precursor_wo_id,
                "aircraft_reg": aircraft,
                "ata_chapter": comp["ata_chapter"],
                "tac": precursor_tac,
                "content": str(rng.choice(comp["symptom_texts"])),
                "embedding": {"status": "", "result": noisy_embedding(centroid, 0.05, rng)},
            })

            lt_rows.append({
                "component_key": comp["component_key"],
                "aircraft_reg": aircraft,
                "precursor_wo_id": precursor_wo_id,
                "precursor_wo_uuid": precursor_wo_uuid,
                "precursor_tac": precursor_tac,
                "replacement_wo_uuid": replacement_wo_uuid,
                "replacement_tac": replacement_tac,
                "lead_cycles": lead_cycles,
                "sim": float(round(float(rng.uniform(0.70, 0.95)), 4)),
                "verdict": "symptom",
                "llm_conf": float(round(float(rng.uniform(0.60, 0.98)), 4)),
            })

    return lt_rows, seed_wo_rows


def generate_wo_embeddings(aircraft_regs, components, centroids, n_wo: int, seed_wo_rows, rng):
    rows = list(seed_wo_rows)
    n_seed = len(rows)
    wo_counter = n_seed + 1

    n_symptom_extra = max(0, int(n_wo * MIX_SYMPTOM) - n_seed)
    n_hard_neg = int(n_wo * MIX_HARD_NEG)
    n_routine = max(0, n_wo - n_seed - n_symptom_extra - n_hard_neg)

    # Extra symptom WOs (not tied to lead-time samples)
    for _ in range(n_symptom_extra):
        comp_idx = int(rng.integers(0, len(components)))
        comp = components[comp_idx]
        rows.append({
            "wo_uuid": make_uuid_str(rng),
            "wo_id": make_wo_id(wo_counter),
            "aircraft_reg": str(rng.choice(aircraft_regs)),
            "ata_chapter": comp["ata_chapter"],
            "tac": int(rng.integers(2_000, 30_000)),
            "content": str(rng.choice(comp["symptom_texts"])),
            "embedding": {"status": "", "result": noisy_embedding(centroids[comp_idx], 0.08, rng)},
        })
        wo_counter += 1

    # Hard negatives (similar wording, wrong component context — vector between centroid and noise)
    for _ in range(n_hard_neg):
        comp_idx = int(rng.integers(0, len(components)))
        comp = components[comp_idx]
        noise_v = unit_vector(EMBEDDING_DIM, rng)
        mixed = centroids[comp_idx] * 0.5 + noise_v * 0.5
        mixed = mixed / np.linalg.norm(mixed)
        rows.append({
            "wo_uuid": make_uuid_str(rng),
            "wo_id": make_wo_id(wo_counter),
            "aircraft_reg": str(rng.choice(aircraft_regs)),
            "ata_chapter": comp["ata_chapter"],
            "tac": int(rng.integers(2_000, 30_000)),
            "content": fill_template(str(rng.choice(HARD_NEG_TEMPLATES)), HARD_NEG_WORDS, rng),
            "embedding": {"status": "", "result": noisy_embedding(mixed, 0.15, rng)},
        })
        wo_counter += 1

    # Routine / unrelated WOs (random vectors, far from all component centroids)
    routine_atas = ["05", "06", "12", "21", "22", "23", "24", "25", "26", "28", "30", "31", "34", "38"]
    for _ in range(n_routine):
        rows.append({
            "wo_uuid": make_uuid_str(rng),
            "wo_id": make_wo_id(wo_counter),
            "aircraft_reg": str(rng.choice(aircraft_regs)),
            "ata_chapter": str(rng.choice(routine_atas)),
            "tac": int(rng.integers(2_000, 30_000)),
            "content": fill_template(str(rng.choice(ROUTINE_TEMPLATES)), ROUTINE_WORDS, rng),
            "embedding": {"status": "", "result": unit_vector(EMBEDDING_DIM, rng).tolist()},
        })
        wo_counter += 1

    # Shuffle so class ordering is not preserved
    indices = rng.permutation(len(rows))
    rows = [rows[int(i)] for i in indices]
    return rows


def generate_dim_focus_components(lt_rows):
    comp_count = Counter(r["component_key"] for r in lt_rows)
    comp_aircraft = {}
    for r in lt_rows:
        comp_aircraft.setdefault(r["component_key"], set()).add(r["aircraft_reg"])

    sorted_keys = sorted(comp_count, key=lambda k: -comp_count[k])
    return [
        {
            "component_key": ck,
            "replacement_count": comp_count[ck],
            "aircraft_with_replacement": len(comp_aircraft[ck]),
            "freq_rank": rank + 1,
        }
        for rank, ck in enumerate(sorted_keys)
    ]


def generate_dim_reference_set(lt_rows):
    return [
        {
            "component_key": r["component_key"],
            "aircraft_reg": r["aircraft_reg"],
            "replacement_wo_id": f"REPL-{i:06d}",
            "replacement_wo_uuid": r["replacement_wo_uuid"],
            "replacement_tac": r["replacement_tac"],
            "replacement_date": None,
            "anchor_text": f"Component {r['component_key']} replaced after {r['lead_cycles']} cycles.",
        }
        for i, r in enumerate(lt_rows)
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────────────

def validate(lt_rows, wo_rows, components):
    print("\nValidation summary:")

    # Lead-time constraint
    bad = [r for r in lt_rows if r["replacement_tac"] - r["precursor_tac"] != r["lead_cycles"]]
    status = "OK" if not bad else f"FAIL ({len(bad)} violations)"
    print(f"  replacement_tac = precursor_tac + lead_cycles : {status}")

    # Per-component distribution vs targets
    by_comp = {}
    for r in lt_rows:
        by_comp.setdefault(r["component_key"], []).append(r["lead_cycles"])
    for comp in components:
        ck = comp["component_key"]
        arr = np.array(by_comp.get(ck, []))
        if len(arr) == 0:
            print(f"  {ck}: NO ROWS")
            continue
        p50_act = float(np.percentile(arr, 50))
        p90_act = float(np.percentile(arr, 90))
        print(f"  {ck}: n={len(arr):4d}  p50={p50_act:6.0f} (target {comp['p50']})  p90={p90_act:6.0f} (target {comp['p90']})")

    # Embedding integrity
    null_emb = sum(1 for r in wo_rows if not r["embedding"]["result"])
    dims = set(len(r["embedding"]["result"]) for r in wo_rows)
    print(f"  Null embeddings : {null_emb}")
    print(f"  Embedding dims  : {dims}")
    print(f"  wo_embeddings total: {len(wo_rows):,}")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate synthetic curated tables for Plan B POC")
    parser.add_argument("--tier", choices=list(TIERS), default="medium", help="Dataset size tier")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    tier = TIERS[args.tier]
    rng = np.random.default_rng(args.seed)

    top_parts = load_top_parts(TOP_PARTS_FILE)
    top_components = build_top_part_components(top_parts, rng)
    all_components = COMPONENTS + top_components

    print(f"tier={args.tier}  seed={args.seed}  n_wo={tier['n_wo']:,}  n_aircraft={tier['n_aircraft']}")
    print(f"components: {len(COMPONENTS)} synthetic + {len(top_components)} real part numbers = {len(all_components)} total")

    aircraft_regs = make_aircraft_regs(tier["n_aircraft"], rng)
    centroids = [unit_vector(EMBEDDING_DIM, rng) for _ in all_components]

    print("Generating fct_lead_time_samples ...")
    lt_rows, seed_wo_rows = generate_lead_time_and_seed_wos(
        aircraft_regs, all_components, centroids, tier["lt_per_comp"], rng
    )

    print("Generating wo_embeddings ...")
    wo_rows = generate_wo_embeddings(aircraft_regs, all_components, centroids, tier["n_wo"], seed_wo_rows, rng)

    print("Generating dimension tables ...")
    dim_focus = generate_dim_focus_components(lt_rows)
    dim_ref = generate_dim_reference_set(lt_rows)

    print("\nWriting output files ...")
    write_ndjson_gz(wo_rows, OUT_DIR / "wo_embeddings.ndjson.gz")
    write_ndjson_gz(lt_rows, OUT_DIR / "fct_lead_time_samples.ndjson.gz")
    write_ndjson_gz(dim_focus, OUT_DIR / "dim_focus_components.ndjson.gz")
    write_ndjson_gz(dim_ref, OUT_DIR / "dim_reference_set.ndjson.gz")

    validate(lt_rows, wo_rows, all_components)


if __name__ == "__main__":
    main()
