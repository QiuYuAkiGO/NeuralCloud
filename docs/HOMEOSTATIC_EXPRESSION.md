# Homeostatic expression contract

This document defines the semantic boundary between the deterministic
Homeostasis service and model-facing Cognitive code. The source of truth remains
the versioned Homeostasis snapshot; prompts receive only a compact derivative.

## Contract and severity

`HomeostaticInteroceptiveSummary` uses schema
`homeostatic-interoception-v1` and is committed in
`homeostasis-state-v2`. It carries the profile and snapshot revisions, a
salience tier, up to three dominant needs, an optional compatible emotion hint,
policy identifiers, and a deterministic episode signature.

For each dimension, normalized deviation is zero inside its target band. Below
or above the band it is the distance from the nearest target edge divided by the
distance from that edge to the corresponding hard bound, clamped to `[0, 1]`.

- `noticeable` enters when any deviation is at least `0.25`.
- `marked` enters when any deviation is at least `0.50`, or at least two are
  `0.25` or greater.
- `critical` is reserved for a non-empty watchdog result.
- Noticeable and marked exit at `0.15` and `0.35` respectively, so small changes
  do not make the prompt oscillate.

The internal record retains typed dimensions and numeric evidence. The prompt
renderer maps those fields to fixed natural-language needs and never renders
dimension identifiers, belief values, A/B/C matrices, EFE details, thresholds,
or arbitrary text supplied through an event. Grounded embodiment readings use
the same boundary: the model receives only a semantic need, never a resource
label, ratio, confidence, timestamp, or time-to-limit estimate.

## Runtime behavior

The manifest setting `homeostatic_expression.mode` controls rollout:

- `off`: legacy behavior.
- `shadow`: derive and journal summaries without prompt injection.
- `responses`: inject the same-revision summary into every user and autonomous
  response preparation.
- `full`: also schedule one high-priority autonomous assessment when a non-user
  event enters a new marked episode.

Marked assessments have lower priority than CONTROL and USER work and higher
priority than ordinary perception. A pending user message supersedes an
autonomous attempt. The same episode signature is rate-limited by
`marked_cooldown_seconds`, which defaults to 1800 seconds.

The assessor's rationale is audit evidence only. It is neither copied into the
main-model prompt nor written into model-retrievable memory. Unsafe cached or
fresh assessor output is discarded; if both auxiliary and main fallbacks are
unsafe, the autonomous decision fails closed to `SILENT`.

Prompts treat noticeable state as tone/choice context and marked state as a
salient feeling, need, or boundary. Direct wording is never mandatory: implicit
expression and autonomous silence remain valid. Absence of explicit feeling is
therefore not a Validator violation.

A watchdog result is different from marked expression. Runtime synchronously
pauses before autonomous assessment, response preparation, model generation, or
delivery. It preserves the accepted event and committed Homeostasis evidence for
recovery instead of applying the event twice. Action outcomes use non-user
causal provenance, and marked triggers deferred while booting or recovering are
journaled so a restart can schedule them after the lifecycle returns to RUNNING.

Mechanism-leak and unsupported-physiology checks remain active in every rollout
mode, including `off` and `shadow`. A functional proxy cannot claim hunger,
pain, or another concrete bodily condition without matching grounded sensor
evidence.

## Evidence and acceptance

Every enabled path journals the Homeostasis revision, summary hash, salience,
episode signature, assessment decision, prepared response, and delivery result.
Compiler provenance uses the `homeostatic_interoception` segment kind.

Deterministic tests must cover thresholds, hysteresis, migration, ordinary and
autonomous prompt wiring, user supersession, marked deduplication, and zero model
or delivery calls after a watchdog. Live paired evaluation and the 30-minute
soak are release gates; historical revision-1 state is diagnostic evidence only.
