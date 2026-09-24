"""Tunables for the online precursor-to-replacement prediction core (§5.8).

Pure config: no BigQuery client, no ``google.auth``, no ``pm_agent.config``
import at module level. ``PredictionSettings.bq_project`` defaults to
``None`` ("unresolved") - the repository layer (T08) is responsible for
calling ``pm_agent.config.project_id()`` when it is not set, exactly the way
``pm_agent.config`` itself is only imported at call time elsewhere in this
codebase (e.g. ``bq_analytics.queries.build_default_runner``).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from hashlib import sha256

__all__ = [
    "ALLOWED_EMBEDDING_ENDPOINTS",
    "ALLOWED_VOTE_NORMALISATIONS",
    "FOCUS_PART_FALLBACK",
    "POSITION_ALIASES",
    "PredictionSettings",
]

# Fallback focus-part allowlist used only if ``dim_focus_components`` cannot
# be read (§5.8); the live table is always preferred when available.
FOCUS_PART_FALLBACK: tuple[str, ...] = (
    "2085M31G03",
    "4735975",
    "3400010380",
    "3400010360",
    "3400010280",
    "3400010260",
    "3400010390",
    "4503514",
)

# Narrative-position spelling variants -> canonical position key (§5.2/§5.8).
# Keys are matched case-insensitively by callers after upper-casing/stripping.
POSITION_ALIASES: dict[str, str] = {
    "AFTCARGO": "AFT",
    "AFT CARGO": "AFT",
    "ENG1": "#1",
    "ENG 1": "#1",
    "1": "#1",
    "ENG2": "#2",
    "ENG 2": "#2",
    "2": "#2",
}

ALLOWED_EMBEDDING_ENDPOINTS: frozenset[str] = frozenset({"text-embedding-005"})
ALLOWED_VOTE_NORMALISATIONS: frozenset[str] = frozenset({"none", "sqrt"})


@dataclass(frozen=True, slots=True)
class PredictionSettings:
    """Frozen settings bundle for the prediction core.

    Every field has a plan-specified default (§5.8); ``from_env`` overrides
    them from ``PMA_*`` environment variables read at call time only, never
    at import time, so importing this module never touches the environment.
    """

    prediction_enabled: bool = False
    bq_project: str | None = None
    curated_dataset: str = "pma_agent_curated"
    analytics_dataset: str = "pma_agent_analytics"
    embedding_endpoint: str = "text-embedding-005"
    embedding_dim: int = 768
    anchor_sim_threshold: float = 0.84
    anchor_k: int = 20
    vote_normalisation: str = "none"
    min_vote_support: int = 3
    min_vote_share: float = 0.5
    wo_neighbour_k: int = 10
    wo_neighbour_sim_threshold: float = 0.80
    min_sample: int = 5
    max_replacement_interval_share: float = 0.5
    show_supporting_interval: bool = True
    show_projected_window: bool = True
    # Condensed recommendation (approved 2026-09-24, USER REQUEST: base the
    # recommendation on `wo_embeddings` neighbour matching and
    # `fct_lead_time_samples`). `rec_neighbour_k`/`rec_neighbour_min_sim` are
    # deliberately separate from `wo_neighbour_k`/`wo_neighbour_sim_threshold`
    # (O6 evidence uses a smaller k / higher threshold; the recommendation
    # wants a wider net to aggregate lead-time samples over).
    show_recommendation: bool = True
    query_include_actions: bool = True
    rec_neighbour_k: int = 100
    rec_neighbour_min_sim: float = 0.70
    rec_min_sample_high: int = 8
    rec_max_cv: float = 0.5
    rec_strong_sim: float = 0.80
    tac_stale_days: int = 7
    focus_cache_ttl_s: int = 600
    build_max_spread_s: int = 3600
    evidence_limit: int = 5
    focus_part_fallback: tuple[str, ...] = field(default=FOCUS_PART_FALLBACK)

    def __post_init__(self) -> None:
        if self.embedding_endpoint not in ALLOWED_EMBEDDING_ENDPOINTS:
            raise ValueError(
                f"embedding_endpoint {self.embedding_endpoint!r} is not allowlisted"
            )
        if self.vote_normalisation not in ALLOWED_VOTE_NORMALISATIONS:
            raise ValueError(
                f"vote_normalisation {self.vote_normalisation!r} must be one of "
                f"{sorted(ALLOWED_VOTE_NORMALISATIONS)}"
            )
        if self.embedding_dim <= 0:
            raise ValueError("embedding_dim must be positive")
        if self.anchor_k <= 0 or self.wo_neighbour_k <= 0 or self.rec_neighbour_k <= 0:
            raise ValueError(
                "anchor_k, wo_neighbour_k and rec_neighbour_k must be positive"
            )
        if self.min_sample < 1:
            raise ValueError("min_sample must be >= 1")
        if self.rec_min_sample_high < 1:
            raise ValueError("rec_min_sample_high must be >= 1")
        if self.min_vote_support < 1:
            raise ValueError("min_vote_support must be >= 1")
        if self.evidence_limit < 0:
            raise ValueError("evidence_limit must be >= 0")
        if (
            self.tac_stale_days < 0
            or self.focus_cache_ttl_s < 0
            or self.build_max_spread_s < 0
        ):
            raise ValueError("stale/ttl/spread windows must be >= 0")
        for name, value in (
            ("anchor_sim_threshold", self.anchor_sim_threshold),
            ("wo_neighbour_sim_threshold", self.wo_neighbour_sim_threshold),
            ("min_vote_share", self.min_vote_share),
            ("max_replacement_interval_share", self.max_replacement_interval_share),
            ("rec_neighbour_min_sim", self.rec_neighbour_min_sim),
            ("rec_max_cv", self.rec_max_cv),
            ("rec_strong_sim", self.rec_strong_sim),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")

    @classmethod
    def from_env(cls) -> PredictionSettings:
        """Build settings from ``PMA_*`` environment variables.

        Reads ``os.environ`` only when called, never at import time. Any
        field this class defines may be overridden by a same-named
        ``PMA_<FIELD_NAME_UPPER>`` variable (``anchor_sim_threshold`` also
        reads the plan's ``PMA_ONLINE_ANCHOR_SIM_THRESHOLD``, which wins);
        unset variables keep the dataclass default.
        """
        overrides: dict[str, object] = {}
        for f in fields(cls):
            raw = None
            for env_name in (_ENV_ALIASES.get(f.name), f"PMA_{f.name.upper()}"):
                if env_name and (raw := os.environ.get(env_name)) is not None:
                    break
            if raw is None:
                continue
            overrides[f.name] = _coerce(raw, f.name, cls)
        return cls(**overrides)

    @property
    def settings_hash(self) -> str:
        """Short, stable fingerprint of the effective settings.

        Included in ``Provenance`` so a prediction result can be traced back
        to the exact tunables that produced it (§6.2).
        """
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return sha256(payload.encode("utf-8")).hexdigest()[:16]


# Env names the plan (§5.8) documents that differ from PMA_<FIELD_NAME_UPPER>.
# The documented name wins; the generic name is still accepted.
_ENV_ALIASES: dict[str, str] = {
    "anchor_sim_threshold": "PMA_ONLINE_ANCHOR_SIM_THRESHOLD",
}


def _coerce(raw: str, field_name: str, cls: type[PredictionSettings]) -> object:
    """Coerce one raw env-var string to the annotated type of ``field_name``."""
    default = cls.__dataclass_fields__[field_name].default
    if field_name == "bq_project":
        return raw
    if field_name == "focus_part_fallback":
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
