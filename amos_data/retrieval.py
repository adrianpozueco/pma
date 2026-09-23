"""Typed, deterministic historical retrieval contracts and local provider."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from amos_data.embeddings import EmbeddingConfig, validate_vector

RetrievalMethod = Literal["keyword", "vector", "hybrid"]


def _key(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _timestamp(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else None
    if not value:
        return None
    try:
        value = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else None


@dataclass(frozen=True)
class HistoryQuery:
    symptoms: str
    analysis_as_of: dt.datetime
    target_part_numbers: tuple[str, ...] = ()
    aircraft_family: str | None = None
    aircraft_id: str | None = None
    ata_code: str | None = None
    component_class: str | None = None
    position: str | None = None
    exclude_workorder_ids: frozenset[str] = frozenset()
    exclude_text_hashes: frozenset[str] = frozenset()
    corpus_version: str | None = None
    limit: int = 10
    method: RetrievalMethod = "hybrid"

    def __post_init__(self) -> None:
        if not self.symptoms.strip():
            raise ValueError("symptoms must not be blank")
        if self.analysis_as_of.tzinfo is None:
            raise ValueError("analysis_as_of must be timezone-aware")
        if not 1 <= self.limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if self.method not in {"keyword", "vector", "hybrid"}:
            raise ValueError("method must be keyword, vector, or hybrid")


class HistoryProvider(Protocol):
    def search(self, query: HistoryQuery) -> dict[str, Any]: ...


def reciprocal_rank_fusion(*rankings: Iterable[str], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, case_id in enumerate(dict.fromkeys(ranking), start=1):
            scores[case_id] = scores.get(case_id, 0.0) + 1.0 / (k + rank)
    return scores


class LocalHistoryProvider:
    """In-memory provider for deterministic tests and a no-cloud demo.

    Documents are mappings following the retrieval_documents schema. `embed_query`
    is optional and returns a vector for local cosine ranking; it is never called
    for keyword-only searches.
    """

    def __init__(
        self,
        documents: Iterable[Mapping[str, Any]],
        *,
        embed_query: Callable[[str], list[float]] | None = None,
        embedding_config: EmbeddingConfig | None = None,
    ) -> None:
        self._documents = [dict(document) for document in documents]
        self._embed_query = embed_query
        self.embedding_config = embedding_config or getattr(
            getattr(embed_query, "__self__", embed_query), "config", None
        )

    def search(self, query: HistoryQuery) -> dict[str, Any]:
        accepted: list[tuple[str, dict[str, Any], str]] = []
        excluded: dict[str, int] = {}
        for document in self._documents:
            decision = self._eligible(document, query)
            if decision != "eligible":
                excluded[decision] = excluded.get(decision, 0) + 1
                continue
            record_id = str(
                document.get("record_id")
                or document.get("workorder_id")
                or document.get("document_id")
                or ""
            )
            source = str(document.get("source_namespace") or "")
            if not record_id or not source:
                excluded["missing_case_id"] = excluded.get("missing_case_id", 0) + 1
                continue
            case_id = f"{source}:{record_id}"
            accepted.append((case_id, document, self._compatibility(document, query)))
        keyword = self._keyword_ranking(accepted, query.symptoms)
        errors = []
        vector = []
        if query.method != "keyword":
            try:
                vector = self._vector_ranking(accepted, query)
                if not vector and (accepted or self._embed_query is None):
                    errors.append("vector_unavailable")
            except (ValueError, RuntimeError) as exc:
                errors.append(f"vector_error:{type(exc).__name__}")
        rankings = []
        if query.method in {"keyword", "hybrid"}:
            rankings.append([case_id for case_id, _, _ in keyword])
        if query.method in {"vector", "hybrid"}:
            rankings.append([case_id for case_id, _, _ in vector])
        if query.method == "vector" and not vector:
            return self._response([], query, excluded, errors)
        fused = reciprocal_rank_fusion(*rankings)
        by_case: dict[str, tuple[dict[str, Any], str]] = {}
        # Keep the excerpt that actually earned the rank, with the keyword
        # winner preferred when both retrieval methods support a case.
        for case_id, _, document in (
            keyword + vector if query.method != "vector" else vector
        ):
            by_case.setdefault(
                case_id, (document, self._compatibility(document, query))
            )
        ordered = sorted(fused, key=lambda case_id: (-fused[case_id], case_id))
        cases = [
            self._case(case_id, *by_case[case_id], fused[case_id])
            for case_id in ordered[: query.limit]
        ]
        for rank, case in enumerate(cases, 1):
            case["rank"] = rank
            case["history_scope"] = (
                "public_report"
                if case["source_namespace"] == "faa_sdr"
                else "own_aircraft"
                if query.aircraft_id and case.get("aircraft_id") == query.aircraft_id
                else "comparable_amos"
            )
        result = self._response(cases, query, excluded, errors)
        result["truncation"]["truncated"] = len(ordered) > query.limit
        return result

    def _eligible(self, document: Mapping[str, Any], query: HistoryQuery) -> str:
        if document.get("source_namespace") not in {"amos", "faa_sdr"}:
            return "unsupported_source"
        if document.get("split") not in {"train", "development"}:
            return "held_out_split"
        if (
            query.corpus_version
            and document.get("reference_corpus_version") != query.corpus_version
        ):
            return "corpus_version"
        available_at = _timestamp(document.get("available_at"))
        if available_at is None or available_at >= query.analysis_as_of:
            return "unavailable_at_as_of"
        if document.get("availability_status") not in {"available", "snapshot_only"}:
            return "unknown_availability"
        if any(
            str(document.get(field) or "") in query.exclude_workorder_ids
            for field in ("workorder_id", "workorder_number", "record_id")
        ):
            return "excluded_workorder"
        if str(document.get("text_hash") or "") in query.exclude_text_hashes:
            return "excluded_text"
        if (
            query.aircraft_family
            and document.get("aircraft_family")
            and document.get("aircraft_family") != query.aircraft_family
        ):
            return "incompatible_family"
        if (
            query.position
            and document.get("position")
            and document.get("position") != query.position
        ):
            return "incompatible_position"
        if (
            query.ata_code
            and document.get("ata_code")
            and document.get("ata_code") != query.ata_code
        ):
            return "incompatible_ata"
        if (
            query.component_class
            and document.get("component_class")
            and document.get("component_class") != query.component_class
        ):
            return "incompatible_component_class"
        return "eligible"

    def _compatibility(self, document: Mapping[str, Any], query: HistoryQuery) -> str:
        parts = {_key(part) for part in document.get("part_keys") or []}
        parts |= {
            _key(document.get("part_number_key")),
            _key(document.get("component_part_number_key")),
        }
        requested = {_key(part) for part in query.target_part_numbers}
        if requested and requested & parts:
            return "exact_pn_application_unverified"
        if requested:
            return "related_or_unknown_part"
        return (
            "unknown"
            if not (query.component_class or query.ata_code)
            else "filtered_system"
        )

    def _keyword_ranking(
        self, accepted: list[tuple[str, dict[str, Any], str]], text: str
    ) -> list[tuple[str, int, dict[str, Any]]]:
        terms = sorted(set(re.findall(r"[A-Z0-9]{2,}", text.upper())))[:128]
        ranked = []
        exact_ids = {
            str(d.get("document_id"))
            for _, d, compatibility in accepted
            if compatibility == "exact_pn_application_unverified"
        }
        for case_id, document, _ in accepted:
            haystack = str(
                document.get("normalized_text") or document.get("raw_text") or ""
            ).upper()
            # Retain negation verbatim; lexical relevance is not fault classification.
            score = sum(term in haystack for term in terms)
            if score:
                ranked.append((case_id, score, document))
        return sorted(
            ranked,
            key=lambda item: (
                str(item[2].get("document_id")) not in exact_ids,
                -item[1],
                item[0],
                str(item[2].get("document_id")),
            ),
        )

    def _vector_ranking(
        self, accepted: list[tuple[str, dict[str, Any], str]], query: HistoryQuery
    ) -> list[tuple[str, float, dict[str, Any]]]:
        if self._embed_query is None:
            return []
        config = self.embedding_config
        if config is None or config.corpus_version != query.corpus_version:
            raise ValueError(
                "query embedding configuration must match the reference corpus"
            )
        vector = self._embed_query(query.symptoms)
        vector = validate_vector(vector, dimension=config.dimension)
        result = []
        for case_id, document, _ in accepted:
            required = {
                **config.as_metadata(config.document_task),
                "text_preparation_version": config.preparation_version,
                "reference_corpus_version": config.corpus_version,
                "embedding_status": "success",
            }
            if any(document.get(key) != value for key, value in required.items()):
                continue
            candidate = document.get("embedding")
            try:
                candidate = validate_vector(candidate, dimension=config.dimension)
            except RuntimeError:
                continue
            denom = math.sqrt(sum(x * x for x in vector)) * math.sqrt(
                sum(x * x for x in candidate)
            )
            if denom:
                result.append(
                    (
                        case_id,
                        sum(a * b for a, b in zip(vector, candidate, strict=True)) / denom,
                        document,
                    )
                )
        return sorted(
            result,
            key=lambda item: (-item[1], item[0], str(item[2].get("document_id"))),
        )

    def _case(
        self,
        case_id: str,
        document: Mapping[str, Any],
        compatibility: str,
        score: float,
    ) -> dict[str, Any]:
        return {
            "case_id": case_id,
            "document_id": document.get("document_id"),
            "source_namespace": document.get("source_namespace"),
            "record_id": document.get("record_id"),
            "workorder_id": document.get("workorder_id"),
            "text_role": document.get("text_role"),
            "workorder_number": document.get("workorder_number"),
            "aircraft_id": document.get("aircraft_id"),
            "aircraft_family": document.get("aircraft_family"),
            "excerpt": document.get("raw_text"),
            "citation": document.get("source_span"),
            "available_at": document.get("available_at"),
            "availability_status": document.get("availability_status"),
            "compatibility": compatibility,
            "relevance_score": score,
            "score_meaning": "retrieval rank only; not a probability or outcome link",
        }

    def _response(
        self,
        cases: list[dict[str, Any]],
        query: HistoryQuery,
        excluded: dict[str, int],
        errors: list[str],
    ) -> dict[str, Any]:
        return {
            "cases": cases,
            "status": "ok" if not errors else "partial",
            "errors": errors,
            "truncation": {
                "requested_limit": query.limit,
                "returned": len(cases),
                "truncated": len(cases) == query.limit,
            },
            "applied_filters": {
                "as_of": query.analysis_as_of.isoformat(),
                "method": query.method,
                "excluded": excluded,
            },
            "versions": {"corpus_version": query.corpus_version},
        }
