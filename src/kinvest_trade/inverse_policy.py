from __future__ import annotations

from dataclasses import dataclass


INVERSE_BENCHMARK_ALIGNMENT_VERSION = "product_exact_v1"


@dataclass(frozen=True, slots=True)
class InverseRegimeDecision:
    eligible: bool
    reason: str
    execution_mode: str
    session_date: str
    benchmark_code: str
    benchmark_name: str
    benchmark_source: str
    benchmark_return_pct: float | None
    regime_key: str


def evaluate_inverse_regime(
    config: object,
    regime: dict | None,
    *,
    expected_session_date: str,
    benchmark_unavailable_reason: str = "",
) -> InverseRegimeDecision:
    mode = str(
        getattr(config, "inverse_execution_mode", "disabled") or "disabled"
    ).strip().lower()
    if mode not in {"disabled", "shadow", "live"}:
        mode = "disabled"
    common = {
        "execution_mode": mode,
        "session_date": str((regime or {}).get("session_date") or ""),
        "benchmark_code": str((regime or {}).get("benchmark_code") or ""),
        "benchmark_name": str((regime or {}).get("benchmark_name") or ""),
        "benchmark_source": str((regime or {}).get("source") or ""),
        "benchmark_return_pct": _optional_float(
            (regime or {}).get("return_pct")
        ),
        "regime_key": str((regime or {}).get("regime_key") or "unknown"),
    }
    if not bool(getattr(config, "inverse_regime_enabled", False)):
        return InverseRegimeDecision(
            eligible=False,
            reason="inverse_regime_disabled",
            **common,
        )
    if mode == "disabled":
        return InverseRegimeDecision(
            eligible=False,
            reason="inverse_execution_disabled",
            **common,
        )
    if benchmark_unavailable_reason:
        return InverseRegimeDecision(
            eligible=False,
            reason=str(benchmark_unavailable_reason),
            **common,
        )
    if regime is None:
        return InverseRegimeDecision(
            eligible=False,
            reason="inverse_benchmark_regime_missing",
            **common,
        )
    if (
        bool(getattr(config, "inverse_require_same_session", True))
        and str(regime.get("session_date") or "") != expected_session_date
    ):
        return InverseRegimeDecision(
            eligible=False,
            reason="inverse_benchmark_regime_stale",
            **common,
        )
    benchmark_return = _optional_float(regime.get("return_pct"))
    if benchmark_return is None:
        return InverseRegimeDecision(
            eligible=False,
            reason="inverse_benchmark_return_missing",
            **common,
        )
    configured_threshold = _optional_float(
        getattr(config, "inverse_benchmark_return_threshold_pct", -1.0)
    )
    threshold = -1.0 if configured_threshold is None else configured_threshold
    if benchmark_return > threshold:
        return InverseRegimeDecision(
            eligible=False,
            reason="inverse_benchmark_decline_insufficient",
            **common,
        )
    trend = str(regime.get("trend_regime") or "").strip().lower()
    if trend not in {"down", "strong_down"}:
        return InverseRegimeDecision(
            eligible=False,
            reason="inverse_benchmark_trend_unconfirmed",
            **common,
        )
    return InverseRegimeDecision(
        eligible=True,
        reason=(
            "inverse_regime_shadow"
            if mode == "shadow"
            else "inverse_regime_live"
        ),
        **common,
    )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", "")
        return float(text) if text else None
    except (TypeError, ValueError):
        return None
