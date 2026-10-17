"""Pump.fun/PumpSwap copy-trading bot.

Paper mode is the default. Live mode is a high-risk opt-in requiring a local,
dedicated Solana keypair and an explicit arming string. Watchlist monitoring uses
Solana RPC; only watched-wallet transactions invoking Pump or PumpSwap programs
are eligible copy signals.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from solders.pubkey import Pubkey

from market_data import MarketData
from pumpfun_config import PumpFunSettings
from pumpportal import (
    PUMP_PROGRAM_ID,
    PUMPSWAP_PROGRAM_ID,
    PumpPortalExecutor,
)
from solana_rpc import SolanaRPC
from storage import Store

logger = logging.getLogger("pumpfun")


def raw_to_units(amount_raw: int | str, decimals: int) -> float:
    return int(amount_raw) / (10 ** decimals)


def _pubkey_string(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("pubkey", ""))
    return str(value)


class PumpFunBot:
    def __init__(self, settings: PumpFunSettings):
        self.s = settings
        db_path = self.s.database_path
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent / db_path
        self.store = Store(db_path, self.s.starting_paper_cash_usd, mode=self.s.mode)
        self.rpc = SolanaRPC(self.s.rpc_url, self.s.request_timeout_seconds)
        # Aggressive launchpad filters; still require a tracked market pair for price marks.
        self.market = MarketData(self.s.request_timeout_seconds, self.s.min_liquidity_usd, self.s.min_pair_age_seconds)
        self.executor = PumpPortalExecutor(
            self.rpc, self.s.request_timeout_seconds, self.s.pumpportal_priority_fee_sol
        )
        self.keypair = None
        self.wallet_address = None
        if self.s.mode == "live":
            self.keypair = PumpPortalExecutor.load_keypair(self.s.keypair_path)
            self.wallet_address = str(self.keypair.pubkey())
        self._validate_addresses()

    def _validate_addresses(self):
        for wallet in self.s.watch_wallets:
            try:
                Pubkey.from_string(wallet)
            except Exception as exc:
                raise ValueError(f"Invalid WATCH_WALLETS Solana address: {wallet}") from exc

    def _tx_programs(self, tx: dict) -> tuple[set[str], set[str]]:
        """Return all invoked IDs and top-level IDs (parsed or compiled JSON-RPC form)."""
        message = ((tx.get("transaction") or {}).get("message") or {})
        meta = tx.get("meta") or {}
        account_keys = [_pubkey_string(k) for k in message.get("accountKeys", [])]

        def program_id(inst: dict) -> str | None:
            pid = inst.get("programId")
            if pid:
                return _pubkey_string(pid)
            idx = inst.get("programIdIndex")
            if isinstance(idx, int) and 0 <= idx < len(account_keys):
                return account_keys[idx]
            return None

        top = set()
        all_ids = set()
        for inst in message.get("instructions", []) or []:
            if isinstance(inst, dict):
                pid = program_id(inst)
                if pid:
                    top.add(pid)
                    all_ids.add(pid)
        for group in meta.get("innerInstructions", []) or []:
            for inst in group.get("instructions", []) or []:
                if isinstance(inst, dict):
                    pid = program_id(inst)
                    if pid:
                        all_ids.add(pid)
        return all_ids, top

    def detect_pump_signal(self, wallet: str, signature: str) -> list[dict]:
        tx = self.rpc.transaction(signature)
        if not tx or (tx.get("meta") or {}).get("err") is not None:
            return []
        all_programs, top_programs = self._tx_programs(tx)
        # Strictly exclude generic DEX swaps. We copy only Pump bonding-curve/PumpSwap activity.
        if PUMP_PROGRAM_ID not in all_programs and PUMPSWAP_PROGRAM_ID not in all_programs:
            return []
        venue = "pump-amm" if PUMPSWAP_PROGRAM_ID in top_programs else "pump"
        deltas, native_delta = self.rpc.wallet_token_deltas(wallet, tx)
        sol_delta = native_delta / 1_000_000_000
        stable_mints = {
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
            "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
        }
        wsol = "So11111111111111111111111111111111111111112"
        non_quote = {m: d for m, d in deltas.items() if m not in stable_mints and m != wsol}
        quote_deltas = {m: d for m, d in deltas.items() if m in stable_mints}
        added = [(m, d) for m, d in non_quote.items() if d["delta_raw"] > 0]
        removed = [(m, d) for m, d in non_quote.items() if d["delta_raw"] < 0]
        stable_spent = any(d["delta_raw"] < 0 for d in quote_deltas.values())
        stable_received = any(d["delta_raw"] > 0 for d in quote_deltas.values())

        if len(added) == 1 and not removed and (sol_delta < -0.001 or stable_spent):
            mint, data = added[0]
            side = "buy"
        elif len(removed) == 1 and not added and (sol_delta > 0.001 or stable_received):
            mint, data = removed[0]
            side = "sell"
        else:
            return []
        return [{
            "wallet": wallet, "signature": signature, "mint": mint, "side": side,
            "amount_ui": abs(float(data["delta_ui"])),
            "amount_raw": abs(int(data["delta_raw"])),
            "decimals": int(data["decimals"]),
            "native_delta_sol": sol_delta, "block_time": tx.get("blockTime"),
            "venue": venue,
        }]

    def _current_positions_value(self) -> float:
        value = 0.0
        for pos in self.store.list_positions():
            market = self.market.token_market(pos["mint"])
            price = float(market["price_usd"]) if market else float(pos["last_price_usd"])
            value += raw_to_units(pos["amount_raw"], int(pos["decimals"])) * price
        return value

    def _live_equity(self) -> float:
        assert self.wallet_address
        sol_price = self.market.sol_price_usd()
        sol_value = self.rpc.sol_balance_lamports(self.wallet_address) / 1_000_000_000 * sol_price
        return sol_value + self._current_positions_value()

    def equity(self) -> float:
        if self.s.mode == "live":
            return self._live_equity()
        cash = float(self.store.get_state("paper_cash", str(self.s.starting_paper_cash_usd)))
        return cash + self._current_positions_value()

    def _initialize_live_baseline(self):
        if self.s.mode != "live" or self.store.get_state("live_equity_initialized", "0") == "1":
            return
        value = self._live_equity()
        self.store.set_state("initial_equity", value)
        self.store.set_state("live_equity_initialized", "1")
        logger.warning("Recorded live starting equity baseline %.4f USD", value)

    def _refresh_risk_state(self, equity: float):
        self.store.reset_day_if_needed(equity)
        if self.store.get_state("halted", "0") == "1":
            return
        initial = float(self.store.get_state("initial_equity", str(self.s.starting_paper_cash_usd)))
        total_loss = initial - equity
        if total_loss >= self.s.total_loss_limit_usd:
            self.store.set_state("halted", "1")
            self.store.set_state("liquidate_all_pending", "1")
            self.store.set_state("halt_reason", f"Total loss limit reached: drawdown {total_loss:.2f} USD")
            logger.critical("%s; new entries blocked and liquidation will be attempted", self.store.get_state("halt_reason"))
            return
        daily_loss = float(self.store.get_state("day_start_equity", str(equity))) - equity
        if daily_loss >= self.s.daily_loss_limit_usd and self.store.get_state("daily_paused", "0") != "1":
            self.store.set_state("daily_paused", "1")
            logger.error("Daily drawdown limit reached (%.2f USD); entries paused until next UTC day", daily_loss)

    def _can_enter(self, equity: float) -> tuple[bool, str]:
        if self.store.get_state("experiment_expired", "0") == "1":
            return False, "Seven-day experiment is over"
        if self.store.get_state("halted", "0") == "1":
            return False, "Overall loss halt active: " + self.store.get_state("halt_reason")
        if self.store.get_state("daily_paused", "0") == "1":
            return False, "Daily loss pause active"
        if self.store.daily_entries() >= self.s.max_entries_per_day:
            return False, "Daily entry cap reached"
        if len(self.store.list_positions()) >= self.s.max_open_positions:
            return False, "Open position cap reached"
        if equity <= 0:
            return False, "Equity is zero or less"
        exposure = self._current_positions_value()
        room = self.s.max_total_exposure_usd - exposure
        spend = min(self.s.max_entry_usd, room)
        if spend < 0.50:
            return False, "Too little exposure headroom for a practical trade"
        if self.s.mode == "paper":
            cash = float(self.store.get_state("paper_cash", "0"))
            if cash < spend + self.s.paper_swap_cost_usd:
                return False, "Insufficient paper cash after simulated costs"
        else:
            assert self.wallet_address
            sol_price = self.market.sol_price_usd()
            spend_lamports = int(spend / sol_price * 1_000_000_000)
            balance = self.rpc.sol_balance_lamports(self.wallet_address)
            reserve = int((self.s.min_sol_reserve + 0.002 + self.s.pumpportal_priority_fee_sol) * 1_000_000_000)
            if balance < spend_lamports + reserve:
                return False, "SOL balance insufficient for entry plus fee reserve"
        return True, "allowed"

    def buy(self, signal: dict):
        mint = signal["mint"]
        if self.store.get_position(mint):
            return
        market = self.market.token_market(mint)
        eligible, reason = self.market.is_entry_eligible(market)
        if not eligible:
            logger.info("Reject Pump.fun buy %s: %s", mint, reason)
            return
        equity = self.equity()
        self._refresh_risk_state(equity)
        allowed, reason = self._can_enter(equity)
        if not allowed:
            logger.info("Reject buy %s: %s", mint, reason)
            return
        assert market
        spend = min(self.s.max_entry_usd, self.s.max_total_exposure_usd - self._current_positions_value())
        if spend < 0.50:
            return
        decimals = int(signal["decimals"])
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        venue = str(signal.get("venue", "pump"))
        sol_price = self.market.sol_price_usd()

        if self.s.mode == "paper":
            cost = self.s.paper_swap_cost_usd
            raw_out = int(spend / float(market["price_usd"]) * (10 ** decimals))
            if raw_out <= 0:
                logger.info("Reject %s: estimated output rounds to zero", mint)
                return
            cash = float(self.store.get_state("paper_cash", "0"))
            self.store.set_state("paper_cash", cash - spend - cost)
            tx_sig = "PAPER-" + signal["signature"][:20]
            cost_basis = spend + cost
            effective_price = cost_basis / raw_to_units(raw_out, decimals)
            used_pool = venue
        else:
            assert self.keypair is not None and self.wallet_address is not None
            result = self.executor.trade(
                owner=self.wallet_address, keypair=self.keypair, mint=mint, action="buy",
                amount=round(spend / sol_price, 9), denominated_in_sol=True,
                slippage_bps=self.s.slippage_bps, preferred_pool=venue,
            )
            tx_sig = result["signature"]
            used_pool = result["pool"]
            raw_out = int(result["total_output_amount"])
            if raw_out <= 0:
                raise RuntimeError("PumpPortal confirmed a buy without a positive token balance delta")
            effective_spend = max(0, -int(result["sol_delta_lamports"])) / 1_000_000_000 * sol_price
            cost_basis = effective_spend if effective_spend > 0 else spend
            effective_price = cost_basis / raw_to_units(raw_out, decimals)

        position = {
            "mint": mint, "decimals": decimals, "amount_raw": str(raw_out),
            "entry_price_usd": effective_price, "initial_amount_raw": str(raw_out),
            "peak_price_usd": float(market["price_usd"]), "tp1_done": 0, "tp2_done": 0,
            "opened_at": timestamp, "last_price_usd": float(market["price_usd"]),
            "invested_usd": cost_basis, "source_wallet": signal["wallet"],
            "source_signature": signal["signature"],
        }
        self.store.upsert_position(position)
        self.store.set_state(f"pool:{mint}", used_pool)
        self.store.increment_daily_entries()
        self.store.add_trade(mode=self.s.mode, action="BUY", mint=mint, amount_raw=str(raw_out),
                             price_usd=effective_price, value_usd=cost_basis, reason=f"copied Pump.fun {used_pool} buy",
                             source_wallet=signal["wallet"], source_signature=signal["signature"], tx_signature=tx_sig)
        logger.warning("BUY %s raw=%s cost=%.4f USD leader=%s pool=%s", mint, raw_out, cost_basis, signal["wallet"], used_pool)
        if self.s.mode == "live":
            logger.warning("Live transaction confirmed: https://solscan.io/tx/%s", tx_sig)

    def sell(self, mint: str, amount_raw: int, reason: str,
             source_wallet: str | None = None, source_signature: str | None = None) -> bool:
        position = self.store.get_position(mint)
        if not position:
            return False
        full_exit = (reason in {"marked loss stop", "trailing stop"} or
                     reason.startswith("followed wallet ") or reason.startswith("experiment expiry") or
                     reason.startswith("total loss limit") or reason.startswith("pending exit:"))
        pending_key = f"exit_pending:{mint}"
        if full_exit:
            self.store.set_state(pending_key, reason)
        current_raw = int(position["amount_raw"])
        requested_raw = min(max(0, int(amount_raw)), current_raw)
        if requested_raw <= 0:
            return False
        decimals = int(position["decimals"])
        market = self.market.token_market(mint)
        mark_price = float(market["price_usd"]) if market else float(position["last_price_usd"])
        if mark_price <= 0:
            return False

        if self.s.mode == "paper":
            sold_raw = requested_raw
            units_sold = raw_to_units(sold_raw, decimals)
            proceeds = max(0.0, units_sold * mark_price - self.s.paper_swap_cost_usd)
            cash = float(self.store.get_state("paper_cash", "0"))
            self.store.set_state("paper_cash", cash + proceeds)
            tx_sig = "PAPER-SELL"
        else:
            assert self.keypair is not None and self.wallet_address is not None
            available = self.executor.token_balance_raw(self.wallet_address, mint)
            requested_raw = min(requested_raw, available)
            if requested_raw <= 0:
                logger.error("No live token balance for %s; retaining position for manual reconciliation", mint)
                return False
            units_requested = raw_to_units(requested_raw, decimals)
            preferred = self.store.get_state(f"pool:{mint}", "pump")
            result = self.executor.trade(
                owner=self.wallet_address, keypair=self.keypair, mint=mint, action="sell",
                amount=units_requested, denominated_in_sol=False,
                slippage_bps=self.s.slippage_bps, preferred_pool=preferred,
            )
            tx_sig = result["signature"]
            sold_raw = abs(int(result["token_delta_raw"]))
            sol_received = max(0, int(result["sol_delta_lamports"])) / 1_000_000_000
            proceeds = sol_received * self.market.sol_price_usd()
            units_sold = raw_to_units(sold_raw, decimals)
            if sold_raw <= 0:
                raise RuntimeError("PumpPortal confirmed sell without a token balance decrease")

        basis = float(position["invested_usd"]) * (sold_raw / current_raw)
        realized_pnl = proceeds - basis
        remaining = max(0, current_raw - sold_raw)
        remaining_basis = max(0.0, float(position["invested_usd"]) - basis)
        self.store.set_state("daily_realized_pnl", float(self.store.get_state("daily_realized_pnl", "0")) + realized_pnl)
        self.store.add_trade(mode=self.s.mode, action="SELL", mint=mint, amount_raw=str(sold_raw),
                             price_usd=proceeds / units_sold if units_sold else mark_price,
                             value_usd=proceeds, pnl_usd=realized_pnl, reason=reason,
                             source_wallet=position.get("source_wallet") or source_wallet,
                             source_signature=source_signature or position.get("source_signature"), tx_signature=tx_sig)
        if remaining == 0:
            self.store.delete_position(mint)
            self.store.set_state(f"pool:{mint}", "")
        else:
            position["amount_raw"] = str(remaining)
            position["invested_usd"] = remaining_basis
            position["last_price_usd"] = mark_price
            self.store.upsert_position(position)
        self.store.set_state(pending_key, "")
        logger.warning("SELL %s raw=%s proceeds=%.4f PnL=%.4f reason=%s", mint, sold_raw, proceeds, realized_pnl, reason)
        if self.s.mode == "live":
            logger.warning("Live transaction confirmed: https://solscan.io/tx/%s", tx_sig)
        return True

    def manage_positions(self):
        for original in self.store.list_positions():
            mint = original["mint"]
            position = self.store.get_position(mint)
            if not position:
                continue
            pending = self.store.get_state(f"exit_pending:{mint}", "")
            if pending:
                self.sell(mint, int(position["amount_raw"]), f"pending exit: {pending}")
                continue
            market = self.market.token_market(mint)
            price = float(market["price_usd"]) if market else float(position["last_price_usd"])
            if price <= 0:
                continue
            peak = max(float(position["peak_price_usd"]), price)
            position["peak_price_usd"], position["last_price_usd"] = peak, price
            self.store.upsert_position(position)
            current_raw = int(position["amount_raw"])
            initial_raw = max(1, int(position["initial_amount_raw"]))
            entry = float(position["entry_price_usd"])
            if price <= entry * (1 - self.s.max_marked_loss_pct):
                self.store.set_state(f"exit_pending:{mint}", "marked loss stop")
                continue
            if peak > entry and price <= peak * (1 - self.s.trailing_stop_pct):
                self.store.set_state(f"exit_pending:{mint}", "trailing stop")
                continue
            if not int(position["tp1_done"]) and price >= entry * (1 + self.s.take_profit_1_pct):
                qty = min(current_raw, int(initial_raw * self.s.take_profit_1_sell_fraction))
                if self.sell(mint, qty, "take profit 1 (+30% target)"):
                    pos = self.store.get_position(mint)
                    if pos:
                        pos["tp1_done"] = 1
                        self.store.upsert_position(pos)
                continue
            if not int(position["tp2_done"]) and price >= entry * (1 + self.s.take_profit_2_pct):
                qty = min(current_raw, int(initial_raw * self.s.take_profit_2_sell_fraction))
                if self.sell(mint, qty, "take profit 2 (+60% target)"):
                    pos = self.store.get_position(mint)
                    if pos:
                        pos["tp2_done"] = 1
                        self.store.upsert_position(pos)

    def process_signal(self, signal: dict):
        self.store.add_signal(signal["wallet"], signal["signature"], signal["mint"], signal["side"],
                              signal["amount_ui"], signal["decimals"])
        if self.wallet_address and signal["wallet"] == self.wallet_address:
            return
        if signal["side"] == "buy":
            self.buy(signal)
        elif signal["side"] == "sell" and self.store.get_position(signal["mint"]):
            position = self.store.get_position(signal["mint"])
            self.sell(signal["mint"], int(position["amount_raw"]),
                      f"followed wallet {signal['wallet']} sold on {signal['venue']}",
                      signal["wallet"], signal["signature"])

    def poll_wallet(self, wallet: str) -> int:
        sigs = self.rpc.signatures(wallet, limit=25)
        if not sigs:
            return 0
        latest = sigs[0].get("signature")
        cursor = self.store.get_cursor(wallet)
        if cursor is None:
            self.store.set_cursor(wallet, latest)
            logger.info("Initial sync %s at %s; old activity will not be copied", wallet, latest)
            return 0
        new_rows = []
        for row in sigs:
            if row.get("signature") == cursor:
                break
            new_rows.append(row)
        handled = 0
        for row in reversed(new_rows):
            signature = row.get("signature")
            if not signature or self.store.signature_seen(signature):
                continue
            for signal in self.detect_pump_signal(wallet, signature):
                self.process_signal(signal)
            self.store.mark_signature_seen(signature, wallet)
            handled += 1
        self.store.set_cursor(wallet, latest)
        return handled

    def status(self):
        equity = self.equity()
        logger.warning("MODE=%s wallet=%s equity=%.4f USD initial=%.4f halted=%s reason=%s",
                       self.s.mode, self.wallet_address or "(paper)", equity,
                       float(self.store.get_state("initial_equity", "10")),
                       self.store.get_state("halted", "0"), self.store.get_state("halt_reason", ""))
        logger.warning("Cash=%.4f positions=%d entries_today=%d daily_pause=%s",
                       float(self.store.get_state("paper_cash", "0")) if self.s.mode == "paper" else -1.0,
                       len(self.store.list_positions()), self.store.daily_entries(),
                       self.store.get_state("daily_paused", "0"))
        for p in self.store.list_positions():
            logger.warning("POSITION %s qty_raw=%s entry=%.8g mark=%.8g pool=%s",
                           p["mint"], p["amount_raw"], p["entry_price_usd"], p["last_price_usd"],
                           self.store.get_state(f"pool:{p['mint']}", "pump"))
        for t in self.store.recent_trades(10):
            logger.warning("TRADE #%s %s %s value=%.4f pnl=%.4f %s",
                           t["id"], t["action"], t["mint"], t["value_usd"], t["pnl_usd"], t["reason"])

    def run(self, once=False):
        logger.warning("Pump.fun bot mode=%s (Pump bonding curve + PumpSwap only)", self.s.mode)
        if not self.s.watch_wallets and not self.store.list_positions():
            logger.warning("WATCH_WALLETS is empty. Add vetted public wallet addresses in .env.")
            return
        started_at = self.store.get_state("experiment_started_at", "")
        if not once and not started_at:
            started_at = str(time.time())
            self.store.set_state("experiment_started_at", started_at)
            logger.warning("Seven-day experiment timer started.")
        if self.s.mode == "live":
            logger.warning("Live wallet: %s", self.wallet_address)
            self._initialize_live_baseline()
        self._refresh_risk_state(self.equity())
        while True:
            try:
                started_at = self.store.get_state("experiment_started_at", "")
                expired = self.store.get_state("experiment_expired", "0") == "1"
                if not once and started_at:
                    try:
                        expired = expired or (time.time() - float(started_at) >= self.s.experiment_duration_days * 86400)
                    except ValueError:
                        pass
                if expired and self.store.get_state("experiment_expired", "0") != "1":
                    self.store.set_state("experiment_expired", "1")
                    logger.warning("Experiment expiry reached; disabling entries and attempting exit.")
                for wallet in self.s.watch_wallets:
                    try:
                        self.poll_wallet(wallet)
                    except Exception:
                        logger.exception("Wallet monitor failed for %s", wallet)
                if expired:
                    for pos in self.store.list_positions():
                        self.store.set_state(f"exit_pending:{pos['mint']}", "experiment expiry liquidation")
                self.manage_positions()
                self._refresh_risk_state(self.equity())
                if self.store.get_state("halted", "0") == "1":
                    for pos in self.store.list_positions():
                        self.store.set_state(f"exit_pending:{pos['mint']}", "total loss limit liquidation")
                    self.manage_positions()
                open_count = len(self.store.list_positions())
                logger.info("Heartbeat equity=%.4f positions=%d halt=%s daily_pause=%s",
                            self.equity(), open_count, self.store.get_state("halted", "0"),
                            self.store.get_state("daily_paused", "0"))
                if expired and open_count == 0:
                    logger.warning("Seven-day experiment finished; no tracked positions remain.")
                    break
            except KeyboardInterrupt:
                logger.warning("Stopped by user.")
                break
            except Exception:
                logger.exception("Loop error; inspect logs before re-arming after any ambiguous transaction")
                if self.s.mode == "live":
                    self.store.set_state("halted", "1")
                    self.store.set_state("halt_reason", "Runtime exception; manual reconciliation required")
            if once:
                break
            time.sleep(self.s.poll_seconds)

    def close(self):
        self.store.close()


def main():
    parser = argparse.ArgumentParser(description="Pump.fun / PumpSwap wallet copy-trading bot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="validate RPC/data endpoints without trading")
    group.add_argument("--once", action="store_true", help="one polling/management cycle")
    group.add_argument("--run", action="store_true", help="continuous monitoring (default)")
    group.add_argument("--status", action="store_true", help="show local ledger status")
    args = parser.parse_args()
    try:
        settings = PumpFunSettings.load()
        logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        bot = PumpFunBot(settings)
    except Exception as exc:
        print(f"Startup/configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.check:
            slot = bot.rpc.call("getSlot", [{"commitment":"confirmed"}])
            sol_price = bot.market.sol_price_usd()
            logger.warning("RPC OK slot=%s; SOL ref price %.4f USD; mode=%s",
                           slot, sol_price, settings.mode)
            logger.warning("Check mode does not trade.")
        elif args.status:
            bot.status()
        else:
            bot.run(once=args.once)
    except Exception:
        logger.exception("Fatal error; reconcile the wallet before restarting live mode")
        return 1
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
