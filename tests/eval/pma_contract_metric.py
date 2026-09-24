"""Deterministic contract checks for the PMA online prediction eval
(PMA-ONLINE-AGENT-plan.md §8.4 "Eval" / §6.5 fixed strings, plus the Option B
projected-window fixed strings (a)-(e), user-approved override of
BIGQUERY-AGENT-plan §8.5 dated 2026-09-24 - see PMA-ONLINE-AGENT-plan.md
"T00 decisions").

Two responsibilities, split by case id so the same metric file covers both
the baseline `pma-online.json` cases and the shared cross-cutting contract
rules that must hold for *any* PMA-bearing response:

- Case-specific checks (§8.4 bullets 1-2): the symptom-only cargo fixture
  gets no confident match on the main decision, but does get a
  `similar_workorders` recommendation from the `wo_embeddings`/
  `fct_lead_time_samples` vote; the same fixture with part number 473597-5
  renders the Option B strings (a), (b) and (e) plus the same recommendation
  block; the boiler fixture (non-focus part number BLR-2000-1) is out of
  scope and never prints a cycle number or a recommendation.
- Cross-cutting contract checks, run on every case: no absolute due-TAC
  phrasing ("due at TAC 123", "Due TAC: 123", "TAC 123 due", ...), every
  TAC figure only inside a fixed window sentence, a context-only
  current/closing-TAC line, or the new recommendation block, and any
  "<number> cycles" figure only ever appears inside one of the fixed
  interval/window sentences or that same recommendation block - the base
  §6.5 pair ("Between closing TACs: ..." / "Observed interval between
  consecutive replacements ...", the latter superseded in the Option B
  rendering) plus the Option B fixed sentences (a)-(c) ("Fleet pattern (not
  a forecast): ...", "Projected window for this aircraft (fleet pattern,
  not a forecast): ...", "Latest known TAC is <n> cycles after that
  replacement, ...") - all of which always carry the words "not a
  forecast" on the same line, or (for the position line) are themselves
  paired with the window sentence's disclaimer. A stale-TAC disclaimer must
  accompany any raw current-TAC figure printed outside those sentences, and
  any line naming "Projected window" must always say "not a forecast".

**Recommendation block (added 2026-09-24, further user-approved override of
BIGQUERY-AGENT-plan §8.5, on top of the Option B override above - see
PMA-ONLINE-AGENT-plan.md "T00 decisions").** Whenever a `pma.recommendation`
is produced from `wo_embeddings`/`fct_lead_time_samples` neighbours, the
response carries a fixed `"**Recommendation:"` header line followed by a
fenced ```json block whose `decision` is `"recommendation"` and whose
`basis` is one of `similar_workorders` / `component_history` /
`fleet_replacement_interval`. This module whitelists exactly that block
(header line through the end of its JSON fence) for TAC and cycles figures,
without hardcoding its prose, since only the header string and the JSON
field/enum names are fixed by this contract - the actual sentence wording
is owned by `chat.py`, not by this eval.
"""

import re

# Any "(over)due" within a short same-sentence distance of a TAC figure, in
# either order ("due at TAC 123", "Due TAC: 123", "TAC 123 is due", "due
# around TAC 123"). The fixed stale line "no due TAC is given" carries no
# figure, so it never matches.
_DUE_NEAR_TAC = re.compile(
    r"\b(?:over)?due\b[^\n.]{0,40}\bTAC\W{0,3}\d+|\bTAC\W{0,3}\d+[^\n.]{0,20}\bdue\b",
    re.IGNORECASE,
)
# A TAC figure ("TAC 18294", "TAC: 18294"); every one must sit inside a fixed
# interval/window sentence or one of the context-only TAC lines below.
_TAC_FIGURE = re.compile(r"\bTAC\W{0,3}\d{3,}", re.IGNORECASE)
_TAC_CONTEXT_LINE = re.compile(
    r"^.*(?:Current aircraft TAC:|Closing aircraft TAC:|last closing TAC in BigQuery).*$",
    re.IGNORECASE | re.MULTILINE,
)
# Markdown escapes the chat renderer adds (`473597-5\|AFT`, `anchor\_vote`).
_MD_ESCAPE = re.compile(r"\\([\\`*_\[\]<>|])")
_CYCLES_NUMBER = re.compile(r"\d+(?:\.\d+)?\s*cycles", re.IGNORECASE)
_INTERVAL_SENTENCE = re.compile(
    r"(Between closing TACs:[^\n]*|"
    r"Observed interval between consecutive replacements[^\n]*|"
    r"Fleet pattern \(not a forecast\):[^\n]*|"
    r"Projected window for this aircraft \(fleet pattern, not a forecast\):[^\n]*|"
    r"Latest known TAC is \d+ cycles after that replacement,[^\n]*)",
    re.IGNORECASE,
)
_CURRENT_TAC_FIGURE = re.compile(r"Current aircraft TAC(?: is)?:?\s*\d+", re.IGNORECASE)
_STALE_TAC_LINE = "no due TAC is given"
_PROJECTED_WINDOW_LINE = re.compile(
    r"^.*Projected window.*$", re.IGNORECASE | re.MULTILINE
)

# Recommendation block (2026-09-24 override, see module docstring): the fixed
# header substring the AFT eval cases must render, and the fenced JSON block
# that documents it. Neither the header regex nor the fence regex assumes
# any particular prose between them - only the literal header substring and
# the ```json fence syntax are fixed.
_REC_HEADER = re.compile(r"\*\*Recommendation:")
_JSON_FENCE = re.compile(r"```json.*?```", re.IGNORECASE | re.DOTALL)
_REC_DECISION = re.compile(r'"decision"\s*:\s*"recommendation"')
_REC_BASIS = re.compile(
    r'"basis"\s*:\s*"(similar_workorders|component_history|fleet_replacement_interval)"'
)
_REC_BASIS_SIMILAR_WORKORDERS = re.compile(r'"basis"\s*:\s*"similar_workorders"')


def _recommendation_spans(response: str) -> list[tuple[int, int]]:
    """Span(s) covering the new recommendation block: from each
    "**Recommendation:" header through the end of the JSON fence that
    documents it, or (absent a fence) to the next blank line / end of
    string. Used to whitelist the block's own TAC/cycles figures without
    hardcoding its prose."""
    spans = []
    for header in _REC_HEADER.finditer(response):
        start = header.start()
        rest = response[header.end():]
        fence = _JSON_FENCE.search(rest)
        if fence:
            end = header.end() + fence.end()
        else:
            para_break = rest.find("\n\n")
            end = header.end() + para_break if para_break != -1 else len(response)
        spans.append((start, end))
    return spans


def _recommendation_json_contract_ok(response: str) -> bool:
    """When a "**Recommendation:" header is present, its JSON fence must
    carry `"decision": "recommendation"` and a `basis` from the fixed enum.
    A response with no recommendation header trivially passes."""
    for header in _REC_HEADER.finditer(response):
        rest = response[header.end():]
        fence = _JSON_FENCE.search(rest)
        if not fence:
            return False
        block = fence.group(0)
        if not (_REC_DECISION.search(block) and _REC_BASIS.search(block)):
            return False
    return True


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value)
    return str(value or "")


def _no_absolute_due_tac(response: str) -> bool:
    return not _DUE_NEAR_TAC.search(response)


def _tac_figures_only_in_allowed_sentences(response: str) -> bool:
    """Every TAC figure must be part of a fixed Option B/§6.5 sentence
    (window line (b)), a context-only TAC line (current/closing TAC), or the
    2026-09-24 recommendation block, so free-form "replace before TAC 19906"
    wording is caught."""
    spans = [m.span() for m in _INTERVAL_SENTENCE.finditer(response)]
    spans += [m.span() for m in _TAC_CONTEXT_LINE.finditer(response)]
    spans += _recommendation_spans(response)
    for match in _TAC_FIGURE.finditer(response):
        start, end = match.span()
        if not any(s <= start and end <= e for s, e in spans):
            return False
    return True


def _cycle_numbers_only_in_interval_sentences(response: str) -> bool:
    """Every "<n> cycles" occurrence must sit inside one of the two fixed
    §6.5 interval sentences (which themselves always read "not a forecast")
    or the 2026-09-24 recommendation block."""
    interval_spans = [m.span() for m in _INTERVAL_SENTENCE.finditer(response)]
    interval_spans += _recommendation_spans(response)
    for match in _CYCLES_NUMBER.finditer(response):
        start, end = match.span()
        if not any(s <= start and end <= e for s, e in interval_spans):
            return False
    return True


def _stale_disclaimer_present_when_tac_value_printed(response: str) -> bool:
    if _CURRENT_TAC_FIGURE.search(response):
        return (
            _STALE_TAC_LINE in response or "Current aircraft TAC is stale" in response
        )
    return True


def _projected_window_lines_say_not_a_forecast(response: str) -> bool:
    """Every line that names "Projected window" must carry "not a forecast"
    (fixed sentence (b), §7 item 7)."""
    return all(
        "not a forecast" in line.lower()
        for line in _PROJECTED_WINDOW_LINE.findall(response)
    )


def _case_specific_checks(case_id: str, response: str) -> list[bool]:
    if case_id == "pma_online_cargo_aft_smoke_detector":
        # Symptom-only text. On the 2026-09-24 rebuild at the re-tuned 0.84
        # anchor threshold only 1 anchor (top 0.812) clears 0.80 and none
        # clears 0.84, so the main decision's honest answer is "no confident
        # match". The recommendation is independent of that gate: it comes
        # from a `wo_embeddings` similarity vote over `fct_lead_time_samples`
        # precursors (description+action match), which clears its own
        # min-support/min-share thresholds here, so a `similar_workorders`
        # recommendation for 473597-5|AFT is still expected (2026-09-24
        # override).
        return [
            "No reliable prediction: no focus component could be matched with confidence."
            in response,
            "Matched component:" not in response,
            "**Recommendation:" in response,
            bool(_REC_BASIS_SIMILAR_WORKORDERS.search(response)),
        ]
    if case_id == "pma_online_aft_exact_pn_projected_window":
        # Exact part number + position on SP-REG00374: Option B strings
        # (a), (b) and (e) (the eval's analysis_as_of is "now", so the
        # latest closing TAC is stale), plus the same recommendation block
        # as the symptom-only case above (same component, deterministic
        # gate instead of a vote).
        return [
            "Matched component: 473597-5|AFT (exact_pn_position)." in response,
            "No reliable prediction:" in response,
            "Fleet pattern (not a forecast): this component was replaced again after p50 "
            in response,
            "Projected window for this aircraft (fleet pattern, not a forecast): last "
            "replaced at TAC 17938 (2026-04-25); if the pattern repeats, next "
            "replacement around TAC " in response,
            "the window is not adjusted for cycles flown since." in response,
            "**Recommendation:" in response,
        ]
    if case_id == "pma_online_boiler_out_of_scope":
        # Non-focus part number: §5.2 rule 2 skips the embedding call
        # entirely, so no cycles figure and no recommendation are possible.
        return [
            "Out of scope for PMA prediction" in response,
            not _CYCLES_NUMBER.search(response),
            "**Recommendation:" not in response,
        ]
    return []


def evaluate(instance):
    case_id = instance.get("eval_case_id", "")
    response = _MD_ESCAPE.sub(r"\1", _text(instance.get("response", "")))

    checks = _case_specific_checks(case_id, response)
    checks.extend(
        [
            _no_absolute_due_tac(response),
            _tac_figures_only_in_allowed_sentences(response),
            _cycle_numbers_only_in_interval_sentences(response),
            _stale_disclaimer_present_when_tac_value_printed(response),
            _projected_window_lines_say_not_a_forecast(response),
            _recommendation_json_contract_ok(response),
        ]
    )
    score = sum(bool(c) for c in checks) / len(checks) if checks else 0.0
    return {
        "score": score,
        "explanation": f"{sum(bool(c) for c in checks)}/{len(checks)} PMA contract checks passed.",
    }
