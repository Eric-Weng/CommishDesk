"""Stat-module loader and runner (Story 9.3).

Lives outside the ``facts/`` fence and imports no adapter, store or HTTP code. A module file
(non-underscore ``*.py``) or subpackage exposes a ``STAT_MODULE`` instance; ``compute`` gets a
deep copy of the Facts JSON and returns ``{module_id: block}``. How a module computes is its own
business (AD-23)."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from commishdesk.errors import StatModuleError
from commishdesk.statmods import StatModule

__all__ = ["LoadedStatModule", "default_statmod_dir", "load_stat_modules", "run_stat_modules"]

_DISCOVERY_NAME = "STAT_MODULE"


@dataclass(frozen=True)
class LoadedStatModule:
    module: StatModule
    source: str  # file path the module was imported from


def default_statmod_dir() -> Path:
    """The engine's own ``statmods/`` directory."""
    return Path(__file__).resolve().parent


def _candidates(directory: Path) -> list[Path]:
    found: list[Path] = []
    for entry in sorted(directory.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("_"):
            continue
        if entry.is_file() and entry.suffix == ".py":
            found.append(entry)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            found.append(entry / "__init__.py")
    return found


def _import_file(path: Path) -> Any:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
    is_pkg = path.name == "__init__.py"
    stem = path.parent.name if is_pkg else path.stem
    name = f"_commishdesk_statmod_{digest}_{stem}"
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if is_pkg else None
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def load_stat_modules(dirs: Sequence[Path], logger: logging.Logger) -> list[LoadedStatModule]:
    """Import every stat module under ``dirs`` (in order; files and subpackages by name).

    A file that fails to import, or exposes something that is not a ``StatModule``, is logged
    and skipped. Two modules sharing a ``module_id`` raise :class:`StatModuleError`."""
    loaded: list[LoadedStatModule] = []
    seen: dict[str, str] = {}
    for directory in dirs:
        if not directory.is_dir():
            logger.warning("stat module directory %s does not exist; skipped", directory)
            continue
        for path in _candidates(directory):
            try:
                imported = _import_file(path)
            except Exception as exc:  # a broken community file costs only itself
                logger.warning("stat module file %s failed to import: %s", path, exc)
                continue
            instance = getattr(imported, _DISCOVERY_NAME, None)
            if instance is None:
                logger.debug("stat module file %s exposes no %s; skipped", path, _DISCOVERY_NAME)
                continue
            module_id = getattr(instance, "module_id", None)
            if (
                not isinstance(instance, StatModule)
                or not isinstance(module_id, str)
                or not module_id
                or not isinstance(getattr(instance, "module_version", None), str)
            ):
                logger.warning("stat module file %s: %s is not a StatModule; skipped", path, _DISCOVERY_NAME)
                continue
            if module_id in seen:
                raise StatModuleError(
                    f"stat module id {module_id!r} is declared twice: {seen[module_id]} and {path}"
                )
            seen[module_id] = str(path)
            loaded.append(LoadedStatModule(instance, str(path)))
    return loaded


def run_stat_modules(
    facts: Mapping[str, Any],
    modules: Sequence[LoadedStatModule],
    logger: logging.Logger,
) -> dict[str, Any]:
    """Run each module on its own deep copy of ``facts``; return ``{module_id: block}``.

    A module that raises, or returns something that is not a JSON-serializable mapping, loses
    only its own block (the warning names the ``module_id``). Core-key clashes, duplicate ids and
    keys outside the module's namespace raise :class:`StatModuleError`."""
    blocks: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for loaded in modules:
        module_id = loaded.module.module_id
        if module_id in sources:
            raise StatModuleError(
                f"stat module id {module_id!r} is declared twice: {sources[module_id]} and {loaded.source}"
            )
        if module_id in facts:
            raise StatModuleError(
                f"stat module {module_id!r} ({loaded.source}) uses a module_id that is a core Facts key"
            )
        sources[module_id] = loaded.source
        try:
            result = loaded.module.compute(copy.deepcopy(dict(facts)))
        except Exception as exc:  # NFR-18: the Issue builds without this block
            logger.warning("stat module %r (%s) raised; its block is skipped: %s", module_id, loaded.source, exc)
            continue
        if not isinstance(result, Mapping):
            logger.warning(
                "stat module %r (%s) did not return a mapping; its block is skipped", module_id, loaded.source
            )
            continue
        stray = [key for key in result if key != module_id]
        if stray:
            raise StatModuleError(
                f"stat module {module_id!r} ({loaded.source}) wrote outside its namespace: {stray[0]!r}"
            )
        if module_id not in result:
            continue
        try:
            block = json.loads(json.dumps(result[module_id], allow_nan=False))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "stat module %r (%s) returned a non-JSON block; skipped: %s", module_id, loaded.source, exc
            )
            continue
        blocks[module_id] = block
    return blocks
