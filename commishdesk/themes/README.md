# themes/ — output-surface renderer zone

This package holds the `Renderer` protocol: Facts JSON in, one complete rendered output
surface out — a web page, an email, a chat post. The zone directory is `themes/` but the
protocol is named `Renderer` and owns the whole surface. It is defined in [`__init__.py`](__init__.py) and documented,
with its signature, in [`/docs/EXTENDING.md`](../../docs/EXTENDING.md); renderer evaluation
material (golden output files) lives in `tests/eval/themes/`. This zone carries **at most
one reference implementation**, enforced by
[`tests/test_extension_zones.py`](../../tests/test_extension_zones.py) — today it carries
none. Nothing here prescribes how to build a renderer.
