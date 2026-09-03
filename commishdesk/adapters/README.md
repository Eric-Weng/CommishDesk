# adapters/ — platform adapter zone

This package holds the `Adapter` protocol: how CommishDesk pulls one league's raw data
from a fantasy platform before `ingest/` sanitizes it. The protocol is defined in
[`__init__.py`](__init__.py) and documented, with its signature, in
[`/docs/EXTENDING.md`](../../docs/EXTENDING.md); evaluation material for an adapter
(recorded platform responses, replay fixtures) lives in `tests/eval/adapters/`. This zone
carries **at most one reference implementation**, enforced by
[`tests/test_extension_zones.py`](../../tests/test_extension_zones.py) — today it carries
one: [`sleeper.py`](sleeper.py)'s `SleeperAdapter`, landed in Epic 2. Nothing here
prescribes how to build an adapter.
