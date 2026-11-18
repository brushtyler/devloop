"""Pure validators and calculations shared by the local engine and devloop (ADR 0001, ADR 0004).

``core`` is the only package devloop may import besides the standard library and itself
(``tests/test_devloop_boundary.py``). Its rules:

- **pure functions only**: no I/O, no clock, no randomness, no state kept between calls; a
  caller passes in everything a function reads (the profile, the state, the current time, a
  schema read as data);
- **standard library only**, and never an import of ``ssdlc_*``, ``orchestrator`` or ``devloop``;
- **the engine is unchanged**: these functions are written next to it, not extracted from it,
  and ``tests/test_core_equivalence.py`` checks that they give the engine's results on the same
  cases. The engine will converge on ``core`` only in step 6 of ADR 0001.

Modules:

- ``core.schema``: a small JSON Schema validator for the schemas read as data;
- ``core.budget``: the limits profile, the rules for unknown usage, charges and capacity;
- ``core.telemetry``: the cost telemetry record, its measures kept apart, route admission.
"""
