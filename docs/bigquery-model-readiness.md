# BigQuery model readiness: fixed-corpus result

The fixed AMOS snapshot audit has not established support for a failure-risk model. The frozen audit
uses source `data/processed/wo_workorders.ndjson.gz` with uncompressed SHA-256
`8de019b32907e9da954e812e0b9d8c68d46b8ba514655b0afe634066b9cda359`.

It found 1,535 observed normalized-PN/exact-serial on-to-off candidates across
all PNs. After aircraft, timestamp/TAC order, position and intervening-conflict
screens, 1,371 candidates remain in 1,017 related episode groups. These are
installation-to-removal observations only. They do not establish continuous
installation, a component origin, a defect-related failure, or healthy follow-up.

For the requested target PNs, the unflagged candidate/independent-group support
is 5/1 for `2085M31G03`, 294/223 for `62197-301-001`, and 129/94 for
`8201-11-0000-01`. Family support remains visible: the 129 oven candidates
include 3 `32S`, 62 `B737-8` and 64 `M73-82` candidates. These counts are not
directly comparable with the earlier target-only profile: it used an NG/MAX-only
oven subset and did not apply the later exact-serial, repeated-endpoint and
intervening-WO counter-regression screens. Each has zero independently reviewed failure labels, zero
reliable component-level event-free horizons, zero usable positives, zero usable
negatives and zero training-eligible rows.

The risk-training gate is therefore **failed**. The corpus has a confirmed
failure objective, but no approved cycle horizon, warning lead or replacement
policy; no independently reviewed outcome labels; no verified complete component
origins/service histories; and no reliable component-level event-free exposure.
An observed removal is not labelled as a failure. Later aircraft activity is not
treated as healthy component follow-up.

The frozen calendar split has a 2026-02-09 train cutoff and 2026-05-11
development cutoff. Its 40 newer target episode groups are reserved for final
evaluation, but are candidate-only and cannot be used for evaluation until labels
are reviewed and mature. The companion per-WO index preserves all-PN retrieval
provenance: closed-snapshot text is historical context only after the
export-envelope timestamp, while prediction-time symptom availability is
unverified and therefore false. Closing action/removal text remains excluded from
prediction-time features.

The BigQuery logistic SQL is a guarded template, not a validated experiment. It
will remain blocked until a reviewed, horizon-matured cohort has both classes and
a consistent horizon, feature version and simulated cutoff.

Needed before fitting:

- Approve the cycle horizon, warning lead and policy owner.
- Review each relevant removal/outcome as confirmed defect-related failure,
  scheduled/serviceable, other or unknown.
- Establish component installation origins and observed component-level
  event-free/censoring follow-up through that horizon.
- Produce a new versioned reviewed cohort and freeze its manifest before tuning.
