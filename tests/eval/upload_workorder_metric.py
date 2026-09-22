"""Deterministic contract checks for the ADK XML upload renderer."""


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_text(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_text(v) for v in value)
    return str(value or "")


def evaluate(instance):
    prompt = _text(instance.get("prompt", ""))
    response = _text(instance.get("response", ""))
    checks = [
        "DEMO-XML-ONLY-2085" in response,
        "uploaded XML" in response,
        "probability" in response.lower() and "unavailable" in response.lower(),
        "replacement deadline" in response.lower()
        and "unavailable" in response.lower(),
        "BigQuery and the knowledge base were not queried" in response,
    ]
    early = "analysis_as_of=2026-09-01T09:15:00Z" in prompt
    if early:
        checks.extend(
            [
                "2026-09-01T09:15:00Z" in response,
                "No symptom text is available at this cutoff." in response,
                "COPPER-FINCH-41" not in response,
                "DEMO-OFF-41" not in response,
                "DEMO-ON-42" not in response,
            ]
        )
    else:
        checks.extend(
            [
                "2085M31G03" in response,
                "COPPER-FINCH-41" in response,
                "DEMO-OFF-41" in response,
                "DEMO-ON-42" in response,
            ]
        )
    score = sum(checks) / len(checks)
    return {
        "score": score,
        "explanation": f"{sum(checks)}/{len(checks)} upload contract checks passed.",
    }
