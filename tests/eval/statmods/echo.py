"""Test-only stat module (Story 9.3): echoes a few core Facts values under ``echo``."""

from __future__ import annotations

from collections.abc import Mapping

from commishdesk.facts import FactsJSON


class EchoModule:
    module_id = "echo"
    module_version = "1"

    def compute(self, facts: FactsJSON) -> Mapping[str, object]:
        return {"echo": {"week": facts["week"], "team_count": len(facts["teams"])}}


STAT_MODULE = EchoModule()
