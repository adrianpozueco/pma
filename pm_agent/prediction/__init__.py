"""Online precursor-to-replacement prediction core (PMA-ONLINE-AGENT-plan.md).

Deliberately docstring-only: importing ``pm_agent.prediction`` must never
have side effects (no BigQuery client, no ``google.auth``, no env reads), so
that ``import pm_agent.prediction`` alone can never fail offline or leak
credentials into an unrelated import graph. Consumers import the specific
submodule they need directly:

- ``pm_agent.prediction.contracts`` - typed inputs/outputs, the
  ``Repository``/``Predictor`` Protocols, reasons, exceptions.
- ``pm_agent.prediction.settings`` - ``PredictionSettings.from_env()`` and
  the ``PMA_*`` tunables (§5.8 of the plan).
- ``pm_agent.prediction.policy`` - pure decision functions (§5.2-§5.6).
- ``pm_agent.prediction.repository`` - the live BigQuery ``Repository``
  implementation (does I/O; not imported by the two modules above).
- ``pm_agent.prediction.service`` - ``PrecursorPredictor`` and
  ``default_predictor()``, the orchestrator wired into
  ``WorkOrderAnalysisService.analyze_xml``.
"""
