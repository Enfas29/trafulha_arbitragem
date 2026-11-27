"""Verified fee metadata for PAPER only; reads never place orders.

SX rates are per account. A missing/expired response is not a zero rate.
Gamma's market-specific fee schedule is frozen with each virtual entry.
Only the background worker reads HTTP; context_for_pair is a cache lookup.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass

LOG = logging.getLogger("market_costs_v15")
SX_FEES_URL = "https://api.sx.bet/user/fees-v3"
POLY_MARKETS_URL = "https://gamma-api.polymarket.com/markets/"


class FeeDataError(ValueError):
    pass


def _number(value, name, maximum=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise FeeDataError(name)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FeeDataError(name) from exc
    if not math.isfinite(number) or not 0 <= number <= maximum:
        raise FeeDataError(name)
    return number


def parse_sx_fees(response):
    """null is an explicit official zero; an absent field is unknown."""
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise FeeDataError("sx_fees_response_invalid")
    if "status" in response and response["status"] != "success":
        raise FeeDataError("sx_fees_response_invalid")
    data = response["data"]
    if "takerPayoutFee" not in data:
        raise FeeDataError("sx_taker_fee_missing")
    raw = data["takerPayoutFee"]
    return 0.0 if raw is None else _number(raw, "sx_taker_fee_invalid")


def parse_poly_fees(market, expected_id=None):
    if not isinstance(market, dict):
        raise FeeDataError("poly_fee_response_invalid")
    if expected_id is not None and str(market.get("id")) != str(expected_id):
        raise FeeDataError("poly_fee_market_mismatch")
    enabled = market.get("feesEnabled")
    if type(enabled) is not bool:
        raise FeeDataError("poly_fees_enabled_missing")
    if not enabled:
        return 0.0, 1.0
    schedule = market.get("feeSchedule")
    if not isinstance(schedule, dict) or type(schedule.get("takerOnly")) is not bool:
        raise FeeDataError("poly_fee_schedule_missing")
    rate = _number(schedule.get("rate"), "poly_fee_rate_invalid")
    exponent = _number(schedule.get("exponent"), "poly_fee_exponent_invalid", maximum=5)
    # Current official sports formula is C * rate * p * (1-p). Other curves
    # need an explicit, separately validated adapter before PAPER can use them.
    if exponent != 1.0:
        raise FeeDataError("poly_fee_curve_unsupported")
    return rate, exponent


@dataclass(frozen=True)
class FeeContext:
    poly_market_id: str
    poly_rate: float
    poly_exponent: float
    sx_taker_payout_rate: float
    observed_epoch: float
    expires_epoch: float

    def valid(self, now_epoch=None, poly_market_id=None):
        now = time.time() if now_epoch is None else now_epoch
        values = (self.poly_rate, self.poly_exponent, self.sx_taker_payout_rate,
                  self.observed_epoch, self.expires_epoch, now)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            return False
        return (isinstance(self.poly_market_id, str) and bool(self.poly_market_id)
                and (poly_market_id is None or self.poly_market_id == str(poly_market_id))
                and 0 <= self.poly_rate <= 1 and self.poly_exponent == 1
                and 0 <= self.sx_taker_payout_rate <= 1
                and 0 < self.observed_epoch <= now + 5
                and self.observed_epoch < self.expires_epoch
                and now < self.expires_epoch)

    def evidence(self):
        return {"method": "official_fees_v15", **asdict(self),
                "poly_source": POLY_MARKETS_URL + self.poly_market_id,
                "sx_source": SX_FEES_URL}


class FeeProvider:
    """Bounded background fee refresh, never HTTP inside a quote/ledger call."""
    def __init__(self, get_json=None, refresh_seconds=300, ttl_seconds=900,
                 retry_seconds=30, max_markets=512):
        if get_json is None:
            import trafulha_arbitragem as bot
            get_json = bot.get_json
        self.get_json = get_json
        self.refresh_seconds = max(10.0, float(refresh_seconds))
        self.ttl_seconds = max(self.refresh_seconds + 1, float(ttl_seconds))
        self.retry_seconds = max(1.0, float(retry_seconds))
        self.max_markets = max(1, min(2000, int(max_markets)))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.thread = None
        self._wanted = set()
        self._poly = {}  # id -> (rate, exponent, observed epoch)
        self._poly_errors = {}
        self._poly_attempts = {}
        self._sx = None  # (rate, observed epoch)
        self._sx_error = "sx_fees_pending"
        self._sx_attempt = 0.0

    def update_pairs(self, pairs):
        metadata = {}
        now = time.time()
        for pair in pairs:
            market = pair.get("poly", {})
            market_id = str(market.get("id", ""))
            if market_id.isdecimal() and len(market_id) <= 30:
                metadata[market_id] = market
            if len(metadata) >= self.max_markets:
                break
        with self._lock:
            self._wanted = set(metadata)
            for target in (self._poly, self._poly_errors, self._poly_attempts):
                for market_id in set(target) - self._wanted:
                    del target[market_id]
            for market_id, market in metadata.items():
                observed = market.get("fees_fetched_epoch")
                if (type(observed) not in (int, float) or not math.isfinite(observed)
                        or not 0 < observed <= now + 5 or now - observed >= self.ttl_seconds):
                    continue
                try:
                    rate, exponent = parse_poly_fees(market, market_id)
                except FeeDataError:
                    continue  # fetch the full official market in the worker
                previous = self._poly.get(market_id)
                if previous is None or observed >= previous[2]:
                    self._poly[market_id] = (rate, exponent, observed)
                    self._poly_errors.pop(market_id, None)
        self._wake.set()

    def context_for_pair(self, pair, now_epoch=None):
        now = time.time() if now_epoch is None else now_epoch
        market_id = str(pair.get("poly", {}).get("id", ""))
        with self._lock:
            sx, poly = self._sx, self._poly.get(market_id)
            sx_error = self._sx_error
            poly_error = self._poly_errors.get(market_id, "poly_fees_pending")
            wanted = market_id in self._wanted
        if not wanted:
            return None, "fees_market_not_watched"
        if sx is None:
            return None, sx_error
        if now - sx[1] >= self.ttl_seconds or sx[1] > now + 5:
            return None, "sx_fees_expired"
        if poly is None:
            return None, poly_error
        if now - poly[2] >= self.ttl_seconds or poly[2] > now + 5:
            return None, "poly_fees_expired"
        context = FeeContext(market_id, poly[0], poly[1], sx[0],
                             min(poly[2], sx[1]),
                             min(poly[2], sx[1]) + self.ttl_seconds)
        return (context, "fees_verified") if context.valid(now, market_id) else (None, "fee_context_invalid")

    def refresh_once(self, now_epoch=None):
        """Only called by the worker (or an explicitly synchronous unit test)."""
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            refresh_sx = (self._sx is None or now - self._sx[1] >= self.refresh_seconds)
            refresh_sx = refresh_sx and now - self._sx_attempt >= self.retry_seconds
            if refresh_sx:
                self._sx_attempt = now
            wanted = tuple(sorted(self._wanted))
        if refresh_sx and not self._stop.is_set():
            try:
                rate = parse_sx_fees(self.get_json(SX_FEES_URL, timeout=4))
                observed = time.time() if now_epoch is None else now
                with self._lock:
                    self._sx = (rate, observed)
                    self._sx_error = "fees_verified"
                LOG.info("PAPER_CUSTOS_SX | estado=verificado | taxa_lucro_pct=%.4f | virtual", rate * 100)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, FeeDataError) else "sx_fees_unavailable"
                with self._lock:
                    self._sx_error = reason
                status = getattr(getattr(exc, "response", None), "status_code", None)
                LOG.warning("PAPER_CUSTOS_SX | estado=%s | tipo=%s | http_status=%s | "
                            "novas entradas exigem taxas válidas", reason, type(exc).__name__, status)
        for market_id in wanted:
            if self._stop.is_set():
                break
            with self._lock:
                old = self._poly.get(market_id)
                due = old is None or now - old[2] >= self.refresh_seconds
                due = due and now - self._poly_attempts.get(market_id, 0) >= self.retry_seconds
                if not due or market_id not in self._wanted:
                    continue
                self._poly_attempts[market_id] = now
            try:
                market = self.get_json(POLY_MARKETS_URL + market_id, timeout=4)
                rate, exponent = parse_poly_fees(market, market_id)
                observed = time.time() if now_epoch is None else now
                with self._lock:
                    if market_id in self._wanted:
                        self._poly[market_id] = (rate, exponent, observed)
                        self._poly_errors.pop(market_id, None)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, FeeDataError) else "poly_fees_unavailable"
                with self._lock:
                    if market_id in self._wanted:
                        self._poly_errors[market_id] = reason

    def snapshot(self, now_epoch=None):
        now = time.time() if now_epoch is None else now_epoch
        with self._lock:
            return {"watched": len(self._wanted),
                    "poly_verified": sum(0 <= now - item[2] < self.ttl_seconds
                                         for item in self._poly.values()),
                    "sx_verified": self._sx is not None and 0 <= now - self._sx[1] < self.ttl_seconds,
                    "sx_taker_payout_rate": self._sx[0] if self._sx else None,
                    "sx_last_error": self._sx_error,
                    "poly_errors": dict(self._poly_errors),
                    "ttl_seconds": self.ttl_seconds}

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.refresh_once()
            self._wake.wait(timeout=min(self.retry_seconds, 10))

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="PaperFeeMetadata", daemon=True)
            self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self.thread is not None:
            self.thread.join(timeout=5)

