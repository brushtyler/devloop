"""Pure validators and calculations of devloop, written on the rules of the SSDLC engine (ADR
0001, ADR 0004).

``core`` is the only package devloop may import besides the standard library and itself
(``tests/test_devloop_boundary.py``). Its rules:

- **pure functions only**: no I/O, no clock, no randomness, no state kept between calls; a
  caller passes in everything a function reads (the profile, the state, the current time, a
  schema read as data);
- **standard library only**, and never an import of the engine or of ``devloop``;
- **written next to the engine**, not extracted from it: until the engine left ``main`` (ADR
  0007, R1; it stays in the tag ``ssdlc-engine-final``) a test checked that these functions give
  the engine's results on the same cases.

Modules:

- ``core.schema``: a small JSON Schema validator for the schemas read as data;
- ``core.budget``: the limits profile, the rules for unknown usage, charges and capacity;
- ``core.telemetry``: the cost telemetry record, its measures kept apart, route admission.
"""
