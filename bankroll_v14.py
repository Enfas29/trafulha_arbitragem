"""Pure integer budgeting for the virtual Paper wallets.

Accounting equity means free cents plus the cost of still-reserved virtual
operations. It is not a mark-to-market valuation or a guaranteed payout.
No database, network, order API or wallet credential is used by this module.

The policy keeps a reserve in EACH wallet, caps one operation by a fraction
of combined equity and bounds aggregate reserved exposure by another
fraction. These are proportional limits; there is no fixed euro trade cap.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import os
import re


_BPS_PER_WHOLE = 10_000
_PERCENT_TEXT = re.compile(r"[0-9]+(?:[.,][0-9]{1,2})?\Z")
_PLATFORMS = frozenset(("poly", "sx"))


def _bounded_bps(name, value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be integer basis points in [{minimum}, {maximum}]")


@dataclass(frozen=True)
class BankrollPolicy:
    """Percentages as exact basis points: 3,000 bps = 30 percent."""

    reserve_bps: int = 3_000
    max_operation_bps: int = 2_000
    max_exposure_bps: int = 7_000

    def __post_init__(self):
        _bounded_bps("reserve_bps", self.reserve_bps, 1_000, 9_000)
        _bounded_bps("max_operation_bps", self.max_operation_bps, 100, 5_000)
        _bounded_bps("max_exposure_bps", self.max_exposure_bps, 1_000, 9_000)
        if self.max_exposure_bps > _BPS_PER_WHOLE - self.reserve_bps:
            raise ValueError("Maximum exposure must not exceed 100 percent minus the reserve")


@dataclass(frozen=True)
class EntryBudget:
    """Budget for one candidate, to be recomputed under the ledger lock.

    Every field ending in ``_cents`` is a nonnegative integer. Individual
    available amounts must BOTH be respected, in addition to the total cap.
    A quote may use less than this budget when depth or margin is limiting.
    ``dataclasses.asdict`` produces an exact serializable audit snapshot.
    """

    total_cap_cents: int
    poly_available_cents: int
    sx_available_cents: int
    poly_reserve_cents: int
    sx_reserve_cents: int
    poly_equity_cents: int
    sx_equity_cents: int
    total_equity_cents: int
    open_exposure_cents: int
    operation_cap_cents: int
    exposure_cap_cents: int
    exposure_remaining_cents: int
    policy: BankrollPolicy


def _percent_to_bps(name, value):
    # An explicitly empty or malformed setting is an error, never a default.
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a percentage string with at most two decimal places")
    text = value.strip()
    if not _PERCENT_TEXT.fullmatch(text):
        raise ValueError(f"{name} must be a percentage string with at most two decimal places")
    return int(Decimal(text.replace(",", ".")) * 100)


def read_policy(environ=None):
    """Read strict percentage settings once; callers may inject a mapping.

    Defaults: 30% reserve per wallet, 20% combined equity per operation,
    70% aggregate exposure. Invalid combinations fail during configuration.
    Both decimal separators are supported; more than two decimal places,
    scientific notation and nonfinite values are rejected.
    """
    source = os.environ if environ is None else environ
    if not isinstance(source, Mapping):
        raise ValueError("Bankroll settings must be provided as a mapping")
    return BankrollPolicy(
        reserve_bps=_percent_to_bps(
            "PAPER_BANKROLL_RESERVE_PCT", source.get("PAPER_BANKROLL_RESERVE_PCT", "30")),
        max_operation_bps=_percent_to_bps(
            "PAPER_BANKROLL_MAX_OPERATION_PCT", source.get("PAPER_BANKROLL_MAX_OPERATION_PCT", "20")),
        max_exposure_bps=_percent_to_bps(
            "PAPER_BANKROLL_MAX_EXPOSURE_PCT", source.get("PAPER_BANKROLL_MAX_EXPOSURE_PCT", "70")),
    )


# The running process has one immutable policy. A restart is required after
# changing configuration; scanning never rereads the environment per quote.
POLICY = read_policy()


def _wallet_cents(name, values):
    if not isinstance(values, Mapping) or set(values) != _PLATFORMS:
        raise ValueError(f"{name} must contain exactly the poly and sx wallets")
    result = {}
    for platform in ("poly", "sx"):
        amount = values[platform]
        if type(amount) is not int or amount < 0:
            raise ValueError(f"{name}.{platform} must be nonnegative integer cents")
        result[platform] = amount
    return result


def budget_for_entry(free, reserved, policy=None):
    """Calculate a conservative proportional budget, using only integer cents.

    ``reserved`` contains original debits of open/review operations in the
    current virtual bankroll. Settled or archived operations are excluded by
    the caller. The ledger must recompute this result under its write lock;
    an earlier snapshot cannot authorize spending after another transaction.
    """
    policy = POLICY if policy is None else policy
    if type(policy) is not BankrollPolicy:
        raise ValueError("policy must be a validated BankrollPolicy")
    free_cents = _wallet_cents("free", free)
    reserved_cents = _wallet_cents("reserved", reserved)
    equity = {platform: free_cents[platform] + reserved_cents[platform]
              for platform in ("poly", "sx")}
    reserve = {
        platform: (equity[platform] * policy.reserve_bps + _BPS_PER_WHOLE - 1)
                  // _BPS_PER_WHOLE
        for platform in ("poly", "sx")
    }
    available = {platform: max(0, free_cents[platform] - reserve[platform])
                 for platform in ("poly", "sx")}
    total_equity = sum(equity.values())
    open_exposure = sum(reserved_cents.values())
    operation_cap = total_equity * policy.max_operation_bps // _BPS_PER_WHOLE
    exposure_cap = total_equity * policy.max_exposure_bps // _BPS_PER_WHOLE
    remaining = max(0, exposure_cap - open_exposure)
    total_cap = min(operation_cap, remaining, sum(available.values()))
    return EntryBudget(
        total_cap_cents=total_cap,
        poly_available_cents=available["poly"],
        sx_available_cents=available["sx"],
        poly_reserve_cents=reserve["poly"],
        sx_reserve_cents=reserve["sx"],
        poly_equity_cents=equity["poly"],
        sx_equity_cents=equity["sx"],
        total_equity_cents=total_equity,
        open_exposure_cents=open_exposure,
        operation_cap_cents=operation_cap,
        exposure_cap_cents=exposure_cap,
        exposure_remaining_cents=remaining,
        policy=policy,
    )
