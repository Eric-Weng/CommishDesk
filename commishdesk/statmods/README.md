# statmods/ — community stat-module zone

This package holds the `StatModule` protocol: Facts JSON in, an additive block of computed
values out, keyed by the module's `module_id` and never touching a prompt or a renderer
(AD-23). It is the community stat zone — named `statmods/` so it does not shadow the
`commishdesk/stats/` pipeline compute package. The protocol is defined in
[`__init__.py`](__init__.py) and documented, with its signature, in
[`/docs/EXTENDING.md`](../../docs/EXTENDING.md); stat-module evaluation material lives in
`tests/eval/statmods/`. This zone carries **at most one reference implementation**,
enforced by [`tests/test_extension_zones.py`](../../tests/test_extension_zones.py) — today
it carries none. Nothing here prescribes how to build a stat module.
