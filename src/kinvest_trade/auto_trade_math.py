from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class TradeFeeEstimate:
    notional_usd: float
    commission_usd: float
    sec_fee_usd: float

    @property
    def total_fees_usd(self) -> float:
        return self.commission_usd + self.sec_fee_usd


def estimate_trade_fees(
    side: str,
    qty: int,
    price: float,
    commission_rate: float,
    sec_fee_rate: float,
) -> TradeFeeEstimate:
    notional_usd = max(float(qty), 0.0) * max(price, 0.0)
    commission_usd = notional_usd * max(commission_rate, 0.0)
    sec_fee_usd = notional_usd * max(sec_fee_rate, 0.0) if side.lower() == "sell" else 0.0
    return TradeFeeEstimate(
        notional_usd=notional_usd,
        commission_usd=commission_usd,
        sec_fee_usd=sec_fee_usd,
    )


def estimate_fx_fee_krw(notional_usd: float, fx_rate_krw: float, fx_fee_rate: float) -> float:
    if notional_usd <= 0 or fx_rate_krw <= 0 or fx_fee_rate <= 0:
        return 0.0
    return notional_usd * fx_rate_krw * fx_fee_rate


def estimate_capital_gains_tax_krw(
    cumulative_net_pnl_krw: float,
    annual_tax_free_allowance_krw: int,
    capital_gains_tax_rate: float,
) -> float:
    taxable_base = max(cumulative_net_pnl_krw - float(annual_tax_free_allowance_krw), 0.0)
    if taxable_base <= 0 or capital_gains_tax_rate <= 0:
        return 0.0
    return taxable_base * capital_gains_tax_rate
