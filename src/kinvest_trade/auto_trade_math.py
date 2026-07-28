from __future__ import annotations

from dataclasses import dataclass


DOMESTIC_COST_CALCULATION_VERSION = "domestic_product_tax_v2"
OVERSEAS_COST_CALCULATION_VERSION = "overseas_fee_v1"
_DOMESTIC_SELL_TAX_EXEMPT_PRODUCT_MARKERS = ("ETF", "ETN", "ELW")


@dataclass(slots=True)
class TradeFeeEstimate:
    notional_usd: float
    commission_usd: float
    sec_fee_usd: float

    @property
    def total_fees_usd(self) -> float:
        return self.commission_usd + self.sec_fee_usd


@dataclass(slots=True)
class DomesticTradeCostEstimate:
    gross_pnl_krw: float
    net_pnl_krw: float
    buy_commission_krw: float
    sell_commission_krw: float
    sell_tax_krw: float

    @property
    def sell_cost_krw(self) -> float:
        return self.sell_commission_krw + self.sell_tax_krw


def is_domestic_sell_tax_exempt(product_type: str) -> bool:
    normalized = str(product_type or "").strip().upper()
    return any(
        marker in normalized
        for marker in _DOMESTIC_SELL_TAX_EXEMPT_PRODUCT_MARKERS
    )


def estimate_domestic_trade_costs(
    *,
    entry_price: float,
    exit_price: float,
    qty: int,
    commission_rate: float,
    stock_sell_tax_rate: float,
    product_type: str,
) -> DomesticTradeCostEstimate:
    safe_entry = max(float(entry_price), 0.0)
    safe_exit = max(float(exit_price), 0.0)
    safe_qty = max(int(qty), 0)
    safe_commission_rate = max(float(commission_rate), 0.0)
    safe_stock_tax_rate = max(float(stock_sell_tax_rate), 0.0)
    sell_tax_rate = (
        0.0
        if is_domestic_sell_tax_exempt(product_type)
        else safe_stock_tax_rate
    )
    gross = (safe_exit - safe_entry) * safe_qty
    buy_commission = safe_entry * safe_qty * safe_commission_rate
    sell_commission = safe_exit * safe_qty * safe_commission_rate
    sell_tax = safe_exit * safe_qty * sell_tax_rate
    return DomesticTradeCostEstimate(
        gross_pnl_krw=round(gross, 2),
        net_pnl_krw=round(
            gross - buy_commission - sell_commission - sell_tax,
            2,
        ),
        buy_commission_krw=round(buy_commission, 2),
        sell_commission_krw=round(sell_commission, 2),
        sell_tax_krw=round(sell_tax, 2),
    )


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
