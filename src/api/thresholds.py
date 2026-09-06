"""The run thresholds a client is allowed to change, and their limits.

A tier already decides how LONG a run goes and how MANY channels it can
finish. These are the other axis: what counts as worth including at all.
They were fixed in .env, which meant "only channels above 50k subscribers"
was a property of the deployment rather than of the question being asked --
fine while every run was the same two verticals, wrong as soon as a client
researches a niche whose channels are smaller.

Each entry is the single definition of that threshold: the console renders
the form from it and the launcher validates against it, so a bound cannot
drift between what the UI offers and what the API accepts.

Bounds are deliberately conservative. A subscriber floor of zero would
admit every channel YouTube has ever hosted into a run sized for a few
dozen, and the run would spend its whole window hydrating noise.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.config import get_config


@dataclass(frozen=True)
class Threshold:
    id: str
    #: The HarnessConfig field, upper-cased -- this is what reaches the run
    #: as an environment variable, so it must match a real field name or the
    #: override silently does nothing.
    env: str
    label: str
    help: str
    kind: str            # "int" | "float"
    minimum: float
    maximum: float
    step: float
    #: Rendered beside the value, e.g. "subscribers".
    unit: str = ""


THRESHOLDS: list[Threshold] = [
    Threshold(
        id="subscriber_floor",
        env="SUBSCRIBER_FLOOR",
        label="Subscriber floor",
        help=(
            "Channels below this are discovered but not enriched, and do not "
            "reach the workbook. Lower it to study a niche of smaller "
            "channels; raise it to look only at established ones."
        ),
        kind="int",
        minimum=1_000,
        maximum=1_000_000,
        step=1_000,
        unit="subscribers",
    ),
    Threshold(
        id="min_subscribers_for_expansion",
        env="MIN_SUBSCRIBERS_FOR_EXPANSION",
        label="Traverse from channels above",
        help=(
            "Discovery follows references out of channels this size and up. "
            "Smaller channels are still collected, they are just not used as "
            "stepping stones -- they rarely list others."
        ),
        kind="int",
        minimum=0,
        maximum=500_000,
        step=500,
        unit="subscribers",
    ),
    Threshold(
        id="saturation_novelty_threshold",
        env="SATURATION_NOVELTY_THRESHOLD",
        label="Stop when novelty falls below",
        help=(
            "A round's novelty is the share of what it found that was new. "
            "Once it drops under this the topic is considered exhausted and "
            "the run stops looking. Raise it to stop sooner."
        ),
        kind="float",
        minimum=0.01,
        maximum=0.5,
        step=0.01,
    ),
    Threshold(
        id="max_channels_per_run",
        env="MAX_CHANNELS_PER_RUN",
        label="Channel cap",
        help=(
            "The most channels this run will carry through to enrichment. "
            "Defaults to what the chosen depth can finish; raising it past "
            "that trades completeness for breadth, and columns the run runs "
            "out of time to fill arrive empty."
        ),
        kind="int",
        minimum=5,
        maximum=10_000,
        step=1,
        unit="channels",
    ),
]

BY_ID: dict[str, Threshold] = {t.id: t for t in THRESHOLDS}


def catalog(depth_id: str | None = None) -> list[dict[str, Any]]:
    """Every threshold with the value this run would use if left alone.

    `max_channels_per_run` defaults to the chosen tier rather than to
    HarnessConfig, because that is what the run would actually get -- the
    tier sets it as a governor. Showing the config default there would
    display a number the run was never going to use.
    """
    harness = get_config().harness
    tier_cap = None
    if depth_id:
        from src.api.depth import get_tier

        tier = get_tier(depth_id)
        if tier is not None:
            tier_cap = tier.max_channels

    out: list[dict[str, Any]] = []
    for t in THRESHOLDS:
        row = asdict(t)
        if t.id == "max_channels_per_run" and tier_cap is not None:
            row["default"] = tier_cap
        else:
            row["default"] = getattr(harness, t.id, None)
        out.append(row)
    return out


def validate(overrides: dict[str, Any] | None) -> dict[str, str]:
    """Turn client-supplied overrides into env vars, or raise.

    Returns env-var name -> string value, ready to merge into the child's
    environment. An unknown id or an out-of-range value is refused rather
    than clamped: silently substituting a different number than the one the
    client set is worse than telling them it was not accepted.
    """
    if not overrides:
        return {}

    env: dict[str, str] = {}
    for key, raw in overrides.items():
        spec = BY_ID.get(key)
        if spec is None:
            raise ValueError(f"Unknown threshold: {key}")
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{spec.label} must be a number.")
        if value < spec.minimum or value > spec.maximum:
            fmt = "{:,.0f}" if spec.kind == "int" else "{:g}"
            raise ValueError(
                f"{spec.label} must be between {fmt.format(spec.minimum)} and "
                f"{fmt.format(spec.maximum)}."
            )
        env[spec.env] = str(int(value) if spec.kind == "int" else value)
    return env
