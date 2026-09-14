"""Typed configuration loading.

Two properties this module exists to guarantee:

1. **No trading constant is a code literal.** Engines take a config object.
2. **A malformed config fails at load, not mid-scan.** Weight blocks that do
   not sum to 1.0 are the specific hazard: they silently rescale every score
   in the system, and the resulting numbers look entirely plausible.

Precedence: explicit argument > VRIDDHIX_CONFIG env var > packaged default.
Individual values may be overridden with VRIDDHIX__SECTION__KEY env vars.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "vriddhix.yaml"

#: Blocks whose values are weights and must sum to 1.0. Checked at load.
_WEIGHT_BLOCKS: tuple[tuple[str, ...], ...] = (
    ("scoring", "vcp"),
    ("scoring", "confluence"),
    ("regime", "weights"),
    ("rs", "horizon_weights"),
)

_WEIGHT_TOLERANCE = 1e-6

#: Distinguishes "no default supplied" from "default is None", so that a
#: genuinely-None config value and a missing key behave differently.
_MISSING = object()


class ConfigError(ValueError):
    """Raised for a structurally invalid configuration."""


@dataclass(frozen=True)
class Config:
    """Immutable view over the configuration tree.

    Frozen because a service mutating config mid-run would make the scan
    non-reproducible while still appearing to succeed.
    """

    _data: dict[str, Any]
    source: Path | None = None

    # -- access ----------------------------------------------------------

    def section(self, name: str) -> dict[str, Any]:
        try:
            value = self._data[name]
        except KeyError:
            raise ConfigError(f"missing config section: {name!r}") from None
        if not isinstance(value, dict):
            raise ConfigError(f"config section {name!r} is not a mapping")
        return value

    def get(self, path: str, default: Any = _MISSING) -> Any:
        """Fetch by dotted path, e.g. ``get("vcp.min_base_days")``.

        A missing key raises unless a default is supplied -- a typo in a
        threshold name should not silently become ``None`` and then compare
        false against every price.
        """
        node: Any = self._data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                if default is not _MISSING:
                    return default
                raise ConfigError(f"missing config key: {path!r}")
            node = node[part]
        return node

    def __contains__(self, path: str) -> bool:
        try:
            self.get(path)
        except ConfigError:
            return False
        return True

    # -- derived paths ---------------------------------------------------

    @property
    def cache_dir(self) -> Path:
        raw = Path(self.get("data.cache_dir"))
        return raw if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()

    @property
    def database_url(self) -> str:
        """Deployment target is PostgreSQL; local dev falls back to SQLite.

        Schema and migrations stay in the portable subset so this is a URL
        change rather than a rewrite (see docs/01 §9).
        """
        env = os.environ.get("VRIDDHIX_DATABASE_URL")
        if env:
            return env
        state = PROJECT_ROOT / "state"
        state.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{state / 'vriddhix.db'}"


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """VRIDDHIX__VCP__MIN_BASE_DAYS=40 overrides vcp.min_base_days.

    Values are parsed as YAML scalars so ``true``/``1.5``/``[1,2]`` arrive
    with the type the config file would have given them.
    """
    for key, raw in os.environ.items():
        if not key.startswith("VRIDDHIX__"):
            continue
        parts = [p.lower() for p in key[len("VRIDDHIX__") :].split("__") if p]
        if len(parts) < 2:
            continue
        node = data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"env override {key} targets a non-mapping")
        try:
            node[parts[-1]] = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"env override {key} is not valid YAML: {exc}") from exc
    return data


def _validate(data: dict[str, Any]) -> None:
    required = ("market", "data", "features", "universe", "quality")
    missing = [s for s in required if s not in data]
    if missing:
        raise ConfigError(f"missing required config sections: {missing}")

    for path in _WEIGHT_BLOCKS:
        node: Any = data
        for part in path:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        if node is None:
            continue
        if not isinstance(node, dict):
            raise ConfigError(f"weight block {'.'.join(path)} is not a mapping")
        total = sum(float(v) for v in node.values())
        if abs(total - 1.0) > _WEIGHT_TOLERANCE:
            # This is the check that matters most in the whole loader. A weight
            # block summing to 0.95 rescales every score by 5% -- invisible in
            # the output, corrupting in comparison across versions.
            raise ConfigError(
                f"weights in {'.'.join(path)} sum to {total:.6f}, expected 1.0"
            )

    ema = data["features"].get("ema_periods", [])
    if not ema or any(int(p) <= 0 for p in ema):
        raise ConfigError("features.ema_periods must be positive integers")
    if int(data["features"].get("atr_period", 0)) <= 0:
        raise ConfigError("features.atr_period must be positive")


def load_config(path: str | Path | None = None) -> Config:
    resolved = Path(path) if path else Path(
        os.environ.get("VRIDDHIX_CONFIG", DEFAULT_CONFIG_PATH)
    )
    if not resolved.exists():
        raise ConfigError(f"config file not found: {resolved}")

    with resolved.open() as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping: {resolved}")

    data = _apply_env_overrides(data)
    _validate(data)
    return Config(_data=data, source=resolved)


_cached: Config | None = None


def get_config(reload: bool = False) -> Config:
    """Process-wide config. ``reload=True`` in tests that mutate the environment."""
    global _cached
    if _cached is None or reload:
        _cached = load_config()
    return _cached
