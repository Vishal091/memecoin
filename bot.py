"""Solana memecoin copy-trading bot. Live execution is opt-in and local-keypair only."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from solders.pubkey import Pubkey

from config import Settings
from jupiter import JupiterExecutor, NoExecutableRoute, WSOL_MINT
from market_data import MarketData
from solana_rpc import SolanaRPC
from storage import Store

logger = logging.getLogger("memecoin")


def raw_to_units(amount_raw: int | str, decimals: int) -> float:
    return int(amount_raw) / (10 ** decimals)


class CopyBot:
    def __init__(self, settings: Settings):
        self.s = settings
        db_path = self.s.database_path
        if not db_path.is_absolute():
            db_path = Path(__file__).resolve().parent / db_path
        self.store = Store(db_path, self.s.starting_paper_cash_usd, mode=self.s.mode)
        self.rpc = SolanaRPC(self.s.rpc_url, self.s.request_timeout_seconds)
        self.market = MarketData(self.s.request_timeout_seconds, self.s.min_liquidity_usd, self.s.min_pair_age_seconds)
        self.jupiter = JupiterExecutor(self.s.jupiter_api_key, self.s.request_timeout_seconds) if self.s.jupiter_api_key else None
        self.keypair = None
        self.wallet_address = None
        if self.s.mode == "live":
            assert self.jupiter is not None
            self.keypair = self.jupiter.load_keypair(self.s.keypair_path)
            self.wallet_address = str(self.keypair.pubkey())
        self._validate_addresses()

    def _validate_addresses(self) -> None:
        for wallet in self.s.watch_wallets:
            try:
                Pubkey.from_string(wallet)
            except Exception as exc:
                raise ValueError(f"Invalid WATCH_WALLETS Solana address: {wallet}") from exc

    def _live_equity(self) -> float:
        assert self.wallet_address
        sol_price = self.market.sol_price_usd()
        total = self.rpc.sol_balance_lamports(self.wallet_address) / 1_000_000_000 * sol_price
        for p in self.store.list_positions():
            m = self.market.token_market(p["mint"])
            price = float(m["price_usd"]) if m else float(p["last_price_usd"])
            total += raw_to_units(p["amount_raw"], int(p["decimals"])) * price
        return total

    def equity(self) -> float:
        if self.s.mode == "paper":
            cash = float(self.store.get_state("paper_cash", str(self.s.starting_paper_cash_usd)))
            total = cash
            for p in self.store.list_positions():
                m = self.market.token_market(p["mint"])
                price = float(m["price_usd"]) if m else float(p["last_price_usd"])
                total += raw_to_units(p["amount_raw"], int(p["decimals"])) * price
            return total
        return self._live_equity()

    def initialize_live_baseline(self) -> None:
        if self.s.mode != "live" or self.store.get_state("live_equity_initialized", "0") == "1":
            return
        current = self._live_equity()
        self.store.set_state("initial_equity", current)
        self.store.set_state("live_equity_initialized", "1")
        logger.warning("Initialized live starting equity baseline: %.4f USD", current)

    def _refresh_risk_state(self, equity: float) -> None:
        self.store.reset_day_if_needed(equity)
        if self.store.get_state("halted", "0") == "1":
            return
        initial = float(self.store.get_state("initial_equity", str(self.s.starting_paper_cash_usd)))
        total_loss = initial - equity
        if total_loss >= self.s.total_loss_limit_usd:
            self.store.set_state("halted", "1")
            self.store.set_state("liquidate_all_pending", "1")
            self.store.set_state("halt_reason", f"Total loss limit reached: drawdown {total_loss:.2f} USD")
            logger.critical("TOTAL LOSS HALT: %s; attempting to liquidate tracked positions", self.store.get_state("halt_reason"))
            return
        day_start = float(self.store.get_state("day_start_equity", str(equity)))
        daily_loss = day_start - equity
        if daily_loss >= self.s.daily_loss_limit_usd and self.store.get_state("daily_paused", "0") != "1":
            self.store.set_state("daily_paused", "1")
            logger.error("Daily loss threshold reached; new entries paused until next UTC day. Drawdown: %.2f USD", daily_loss)

    def _can_enter(self, equity: float, market_price: float) -> tuple[bool, str]:
        snap = self.store.equity_snapshot()
        if self.store.get_state("experiment_expired", "0") == "1":
            return False, "Seven-day experiment is over; no new entries allowed"
        if snap["halted"]:
            return False, f"Overall halt: {snap['halt_reason']}"
        if self.store.get_state("daily_paused", "0") == "1":
            return False, "Daily loss pause active"
        if self.store.daily_entries() >= self.s.max_entries_per_day:
            return False, "Daily entry cap reached"
        if len(self.store.list_positions()) >= self.s.max_open_positions:
            return False, "Open-position cap reached"
        if equity <= 0:
            return False, "Equity is zero or negative"
        positions_value = 0.0
        for p in self.store.list_positions():
            m = self.market.token_market(p["mint"])
            px = float(m["price_usd"]) if m else float(p["last_price_usd"])
            positions_value += raw_to_units(p["amount_raw"], int(p["decimals"])) * px
        room = self.s.max_total_exposure_usd - positions_value
        spend = min(self.s.max_entry_usd, room)
        if spend < 0.50:
            return False, "Too little exposure headroom for a practical micro-trade"
        if self.s.mode == "paper":
            cash = float(self.store.get_state("paper_cash", "0"))
            if cash < spend + self.s.paper_swap_cost_usd:
                return False, "Insufficient paper cash after simulated execution cost"
        else:
            assert self.wallet_address is not None
            sol_price = self.market.sol_price_usd()
            lamports = int(spend / sol_price * 1_000_000_000)
            balance = self.rpc.sol_balance_lamports(self.wallet_address)
            reserve = int((self.s.min_sol_reserve + 0.002) * 1_000_000_000)
            if balance < lamports + reserve:
                return False, "SOL balance too low for entry plus fee reserve"
        return True, f"entry approved for {spend:.2f} USD"

    def _quote_shortfall(self, out_raw: int, decimals: int, price_usd: float, spend_usd: float) -> float:
        estimated_value = raw_to_units(out_raw, decimals) * price_usd
        return (spend_usd - estimated_value) / spend_usd if spend_usd > 0 else 1.0

    def buy(self, signal: dict) -> None:
        mint = signal["mint"]
        if self.store.get_position(mint):
            logger.info("Already holding %s; no averaging up/down", mint)
            return
        market = self.market.token_market(mint)
        eligible, reason = self.market.is_entry_eligible(market)
        if not eligible:
            logger.info("Reject buy %s: %s", mint, reason)
            return
        assert market is not None
        equity = self.equity()
        self._refresh_risk_state(equity)
        can, why = self._can_enter(equity, market["price_usd"])
        if not can:
            logger.info("Reject buy %s: %s", mint, why)
            return

        existing_exposure = sum(
            raw_to_units(p["amount_raw"], int(p["decimals"])) *
            (float((self.market.token_market(p["mint"]) or {}).get("price_usd") or p["last_price_usd"]))
            for p in self.store.list_positions()
        )
        spend = min(self.s.max_entry_usd, self.s.max_total_exposure_usd - existing_exposure)
        if spend < 0.50:
            logger.info("Reject buy %s: exposure headroom too small", mint)
            return

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if self.s.mode == "paper":
            cost = self.s.paper_swap_cost_usd
            raw_out = int(spend / market["price_usd"] * (10 ** int(signal["decimals"])))
            if raw_out <= 0:
                logger.info("Reject buy %s: output quantity rounds to zero", mint)
                return
            cash = float(self.store.get_state("paper_cash", "0"))
            self.store.set_state("paper_cash", cash - spend - cost)
            tx_sig = "PAPER-" + signal["signature"][:20]
            cost_basis = spend + cost
            effective_price = cost_basis / raw_to_units(raw_out, int(signal["decimals"]))
        else:
            assert self.jupiter is not None and self.keypair is not None and self.wallet_address is not None
            sol_price = self.market.sol_price_usd()
            amount_lamports = int(spend / sol_price * 1_000_000_000)
            try:
                order = self.jupiter.order(input_mint=WSOL_MINT, output_mint=mint, amount_raw=amount_lamports,
                                           taker=self.wallet_address, slippage_bps=self.s.slippage_bps)
            except NoExecutableRoute as exc:
                logger.info("Reject buy %s: %s", mint, exc)
                return
            raw_out = int(order.get("outAmount") or 0)
            if raw_out <= 0:
                logger.info("Reject buy %s: Jupiter returned zero output", mint)
                return
            impact = abs(float(order.get("priceImpactPct") or 0))
            if impact > 0.02:
                logger.warning("Reject buy %s: Jupiter price impact %.2f%% exceeds 2%%", mint, impact * 100)
                return
            shortfall = self._quote_shortfall(raw_out, int(signal["decimals"]), market["price_usd"], spend)
            if shortfall > self.s.max_quote_shortfall_pct:
                logger.warning("Reject buy %s: quoted output is %.2f%% below market estimate", mint, shortfall * 100)
                return
            # Preflight the expected exit route before risking capital in the token.
            try:
                exit_quote = self.jupiter.order(
                    input_mint=mint, output_mint=WSOL_MINT, amount_raw=raw_out,
                    taker=None, slippage_bps=self.s.slippage_bps, require_transaction=False)
            except NoExecutableRoute as exc:
                logger.warning("Reject buy %s: no credible reverse quote (%s)", mint, exc)
                return
            exit_lamports = int(exit_quote.get("outAmount") or 0)
            expected_exit_usd = raw_to_units(raw_out, int(signal["decimals"])) * market["price_usd"]
            quoted_exit_usd = exit_lamports / 1_000_000_000 * sol_price
            reverse_shortfall = ((expected_exit_usd - quoted_exit_usd) / expected_exit_usd
                                 if expected_exit_usd > 0 else 1.0)
            if exit_lamports <= 0 or reverse_shortfall > self.s.max_quote_shortfall_pct:
                logger.warning("Reject buy %s: reverse quote shortfall %.2f%% exceeds limit", mint, reverse_shortfall * 100)
                return
            result = self.jupiter.execute(order, self.keypair)
            tx_sig = result["signature"]
            actual_output = int(result.get("total_output_amount") or raw_out)
            if actual_output > 0:
                raw_out = actual_output
            cost_basis = spend
            effective_price = cost_basis / raw_to_units(raw_out, int(signal["decimals"]))

        position = {
            "mint": mint, "decimals": int(signal["decimals"]), "amount_raw": str(raw_out),
            "entry_price_usd": effective_price, "initial_amount_raw": str(raw_out),
            "peak_price_usd": float(market["price_usd"]), "tp1_done": 0, "tp2_done": 0,
            "opened_at": now, "last_price_usd": float(market["price_usd"]), "invested_usd": cost_basis,
            "source_wallet": signal["wallet"], "source_signature": signal["signature"],
        }
        self.store.upsert_position(position)
        self.store.increment_daily_entries()
        self.store.add_trade(mode=self.s.mode, action="BUY", mint=mint, amount_raw=str(raw_out),
                             price_usd=effective_price, value_usd=spend, reason="copied buy signal",
                             source_wallet=signal["wallet"], source_signature=signal["signature"], tx_signature=tx_sig)
        logger.warning("BUY %s qty_raw=%s spend=%.2f USD via %s", mint, raw_out, spend, signal["wallet"])
        if tx_sig.startswith("PAPER-"):
            logger.info("Paper entry only; no transaction was signed or sent")
        else:
            logger.warning("LIVE BUY submitted/confirmed: %s", tx_sig)

    def sell(self, mint: str, amount_raw: int, reason: str,
             source_wallet: str | None = None, source_signature: str | None = None) -> bool:
        p = self.store.get_position(mint)
        if not p:
            return False
        full_exit = (reason in {"marked loss stop", "trailing stop"} or reason.startswith("followed wallet ")
                     or reason.startswith("experiment expiry") or reason.startswith("total loss limit")
                     or reason.startswith("pending exit:"))
        pending_key = f"exit_pending:{mint}"
        if full_exit:
            self.store.set_state(pending_key, reason)
        current_raw = int(p["amount_raw"])
        sell_raw = min(max(0, amount_raw), current_raw)
        if sell_raw <= 0:
            return False
        decimals = int(p["decimals"])
        units_sold = raw_to_units(sell_raw, decimals)
        market = self.market.token_market(mint)
        price = float(market["price_usd"]) if market else float(p["last_price_usd"])
        if price <= 0:
            logger.error("No usable price for exit %s; will retry on next loop", mint)
            return False

        basis = float(p["invested_usd"]) * (sell_raw / current_raw)
        if self.s.mode == "paper":
            value = units_sold * price
            proceeds = max(0.0, value - self.s.paper_swap_cost_usd)
            cash = float(self.store.get_state("paper_cash", "0"))
            self.store.set_state("paper_cash", cash + proceeds)
            tx_sig = "PAPER-SELL"
        else:
            assert self.jupiter is not None and self.keypair is not None and self.wallet_address is not None
            # Reconcile tracked size with the actual token balance before asking Jupiter to sell.
            token_result = self.rpc.call("getTokenAccountsByOwner", [
                self.wallet_address, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}
            ]) or {}
            available = 0
            for acct in token_result.get("value", []):
                try:
                    available += int(acct["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                except (KeyError, TypeError, ValueError):
                    continue
            sell_raw = min(sell_raw, available)
            if sell_raw <= 0:
                logger.error("No token balance available to sell %s; leaving position for reconciliation", mint)
                return False
            units_sold = raw_to_units(sell_raw, decimals)
            basis = float(p["invested_usd"]) * (sell_raw / current_raw)
            try:
                order = self.jupiter.order(input_mint=mint, output_mint=WSOL_MINT, amount_raw=sell_raw,
                                           taker=self.wallet_address, slippage_bps=self.s.slippage_bps)
            except NoExecutableRoute as exc:
                logger.error("No exit route for %s; will retry: %s", mint, exc)
                return False
            out_lamports = int(order.get("outAmount") or 0)
            if out_lamports <= 0:
                logger.error("Jupiter returned no exit quote for %s; will retry", mint)
                return False
            result = self.jupiter.execute(order, self.keypair)
            tx_sig = result["signature"]
            actual_out_lamports = int(result.get("total_output_amount") or out_lamports)
            proceeds = actual_out_lamports / 1_000_000_000 * self.market.sol_price_usd()
            value = proceeds

        realized_pnl = proceeds - basis
        remaining = current_raw - sell_raw
        remaining_basis = max(0.0, float(p["invested_usd"]) - basis)
        self.store.set_state("daily_realized_pnl", float(self.store.get_state("daily_realized_pnl", "0")) + realized_pnl)
        self.store.add_trade(mode=self.s.mode, action="SELL", mint=mint, amount_raw=str(sell_raw),
                             price_usd=(proceeds / units_sold if units_sold else price), value_usd=value,
                             pnl_usd=realized_pnl, reason=reason,
                             source_wallet=p.get("source_wallet") or source_wallet,
                             source_signature=source_signature or p.get("source_signature"), tx_signature=tx_sig)
        if remaining <= 0:
            self.store.delete_position(mint)
        else:
            p["amount_raw"] = str(remaining)
            p["invested_usd"] = remaining_basis
            p["last_price_usd"] = price
            self.store.upsert_position(p)
        self.store.set_state(pending_key, "")
        logger.warning("SELL %s units_raw=%s proceeds=%.4f USD PnL=%.4f reason=%s", mint, sell_raw, proceeds, realized_pnl, reason)
        if self.s.mode == "live":
            logger.warning("LIVE SELL submitted/confirmed: %s", tx_sig)
        return True

    def manage_positions(self) -> None:
        for original in self.store.list_positions():
            mint = original["mint"]
            p = self.store.get_position(mint)
            if not p:
                continue
            pending_reason = self.store.get_state(f"exit_pending:{mint}", "")
            if pending_reason:
                self.sell(mint, int(p["amount_raw"]), f"pending exit: {pending_reason}")
                continue
            market = self.market.token_market(mint)
            price = float(market["price_usd"]) if market else float(p["last_price_usd"])
            if price <= 0:
                continue
            peak = max(float(p["peak_price_usd"]), price)
            p["peak_price_usd"] = peak
            p["last_price_usd"] = price
            self.store.upsert_position(p)
            current_raw = int(p["amount_raw"])
            initial_raw = max(1, int(p["initial_amount_raw"]))
            entry = float(p["entry_price_usd"])
            # Emergency exits take priority over profit-taking.
            if price <= entry * (1 - self.s.max_marked_loss_pct):
                self.sell(mint, current_raw, "marked loss stop")
                continue
            if peak > entry and price <= peak * (1 - self.s.trailing_stop_pct):
                self.sell(mint, current_raw, "trailing stop")
                continue
            if not int(p["tp1_done"]) and price >= entry * (1 + self.s.take_profit_1_pct):
                qty = min(current_raw, int(initial_raw * self.s.take_profit_1_sell_fraction))
                sold = self.sell(mint, qty, "take profit 1 (+30% target)")
                p = self.store.get_position(mint)
                if sold and p:
                    p["tp1_done"] = 1
                    self.store.upsert_position(p)
                continue
            if not int(p["tp2_done"]) and price >= entry * (1 + self.s.take_profit_2_pct):
                qty = min(current_raw, int(initial_raw * self.s.take_profit_2_sell_fraction))
                sold = self.sell(mint, qty, "take profit 2 (+60% target)")
                p = self.store.get_position(mint)
                if sold and p:
                    p["tp2_done"] = 1
                    self.store.upsert_position(p)

    def process_signal(self, signal: dict) -> None:
        self.store.add_signal(signal["wallet"], signal["signature"], signal["mint"], signal["side"],
                              signal["amount_ui"], signal["decimals"])
        if self.wallet_address and signal["wallet"] == self.wallet_address:
            return
        if signal["side"] == "buy":
            self.buy(signal)
        elif signal["side"] == "sell":
            if self.store.get_position(signal["mint"]):
                self.sell(signal["mint"], int(self.store.get_position(signal["mint"])["amount_raw"]),
                          f"followed wallet {signal['wallet']} sold token", signal["wallet"], signal["signature"])

    def poll_wallet(self, wallet: str) -> int:
        signatures = self.rpc.signatures(wallet, limit=25)
        if not signatures:
            return 0
        latest = signatures[0]["signature"]
        cursor = self.store.get_cursor(wallet)
        if cursor is None:
            self.store.set_cursor(wallet, latest)
            logger.info("Initial sync for %s at %s (old transactions will not be copied)", wallet, latest)
            return 0
        new_sigs = []
        found_cursor = False
        for row in signatures:
            if row.get("signature") == cursor:
                found_cursor = True
                break
            new_sigs.append(row)
        if not found_cursor and cursor != latest:
            logger.warning("Cursor for %s fell outside latest 25 signatures; some activity may be missed", wallet)
        handled = 0
        for row in reversed(new_sigs):
            sig = row.get("signature")
            if not sig or self.store.signature_seen(sig):
                continue
            detected = self.rpc.detect_wallet_signal(wallet, sig)
            for signal in detected:
                self.process_signal(signal)
            self.store.mark_signature_seen(sig, wallet)
            handled += 1
        # Advance only after the complete batch has been processed without an exception.
        self.store.set_cursor(wallet, latest)
        return handled

    def status(self) -> None:
        equity = self.equity()
        snap = self.store.equity_snapshot()
        logger.warning("MODE=%s wallet=%s equity=%.4f initial=%.4f halted=%s reason=%s",
                       self.s.mode, self.wallet_address or "(paper)", equity, snap["initial_equity"],
                       snap["halted"], snap["halt_reason"])
        logger.warning("Cash=%.4f open_positions=%d entries_today=%d daily_paused=%s",
                       float(self.store.get_state("paper_cash", "0")) if self.s.mode == "paper" else -1.0,
                       len(self.store.list_positions()), self.store.daily_entries(),
                       self.store.get_state("daily_paused", "0"))
        for p in self.store.list_positions():
            logger.warning("POSITION %s raw=%s entry=%.8g mark=%.8g qty=%.8g",
                           p["mint"], p["amount_raw"], p["entry_price_usd"], p["last_price_usd"],
                           raw_to_units(p["amount_raw"], int(p["decimals"])))
        for t in self.store.recent_trades(10):
            logger.warning("TRADE #%s %s %s value=%.4f pnl=%.4f %s",
                           t["id"], t["action"], t["mint"], t["value_usd"], t["pnl_usd"], t["reason"])
        for score in self.store.trader_performance():
            logger.warning("COPY SCORE %s realized_pnl=%.4f exits=%s wins=%s losses=%s",
                           score["source_wallet"], score["realized_pnl"] or 0.0, score["sells"],
                           score["winning_exits"], score["losing_exits"])

    def run(self, once: bool = False) -> None:
        logger.warning("Starting Solana copy bot: mode=%s; live transactions %s",
                       self.s.mode, "ENABLED" if self.s.mode == "live" else "disabled (paper mode)")
        if not self.s.watch_wallets and not self.store.list_positions():
            logger.warning("WATCH_WALLETS is empty and there are no positions to manage. Add researched public trader addresses to .env.")
            return
        if not self.s.watch_wallets:
            logger.warning("WATCH_WALLETS is empty; entries are disabled, but existing positions will still be managed.")
        if not once:
            started_at = self.store.get_state("experiment_started_at", "")
            if not started_at:
                started_at = str(time.time())
                self.store.set_state("experiment_started_at", started_at)
                logger.warning("Seven-day experiment timer started.")
        if self.s.mode == "live":
            assert self.wallet_address
            logger.warning("Live signer pubkey: %s", self.wallet_address)
            logger.warning("Ensure this is a fresh, separately funded wallet before allowing the bot to run.")
        self.initialize_live_baseline()
        # Prime all market prices and risk state before watching.
        self._refresh_risk_state(self.equity())
        cycles = 0
        while True:
            cycles += 1
            try:
                started_at = self.store.get_state("experiment_started_at", "")
                expired = self.store.get_state("experiment_expired", "0") == "1"
                if not once and started_at:
                    try:
                        elapsed = time.time() - float(started_at)
                    except ValueError:
                        elapsed = 0
                    if elapsed >= self.s.experiment_duration_days * 86400 and not expired:
                        expired = True
                        self.store.set_state("experiment_expired", "1")
                        logger.warning("Experiment duration reached. Disabling entries and attempting to exit all remaining positions.")
                for wallet in self.s.watch_wallets:
                    try:
                        n = self.poll_wallet(wallet)
                        if n:
                            logger.info("Handled %d new transactions for %s", n, wallet)
                    except Exception:
                        logger.exception("Watcher cycle failed for %s; cursor will retry on next poll where possible", wallet)
                liquidate_all = self.store.get_state("liquidate_all_pending", "0") == "1"
                if liquidate_all:
                    for p in self.store.list_positions():
                        self.sell(p["mint"], int(p["amount_raw"]), "total loss limit liquidation")
                if expired:
                    for p in self.store.list_positions():
                        self.sell(p["mint"], int(p["amount_raw"]), "experiment expiry liquidation")
                self.manage_positions()
                if liquidate_all and not self.store.list_positions():
                    self.store.set_state("liquidate_all_pending", "0")
                equity = self.equity()
                self._refresh_risk_state(equity)
                open_count = len(self.store.list_positions())
                logger.info("Heartbeat equity=%.4f positions=%d total_halt=%s daily_pause=%s",
                            equity, open_count, self.store.get_state("halted", "0"),
                            self.store.get_state("daily_paused", "0"))
                if expired and open_count == 0:
                    logger.warning("Seven-day experiment complete; no positions remain. Bot is shutting down.")
                    break
            except KeyboardInterrupt:
                logger.warning("Stopped by user.")
                break
            except Exception:
                logger.exception("Main cycle error; new trades are not intentionally forced")
            if once:
                break
            time.sleep(self.s.poll_seconds)

    def close(self) -> None:
        self.store.close()


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Solana memecoin copy-trading bot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="check configuration/RPC/market data; never submit swaps")
    group.add_argument("--once", action="store_true", help="run one polling and position-management cycle")
    group.add_argument("--run", action="store_true", help="run continuously")
    group.add_argument("--status", action="store_true", help="show local portfolio state and recent trades")
    args = parser.parse_args()
    try:
        settings = Settings.load()
        configure_logging(settings.log_level)
        bot = CopyBot(settings)
    except Exception as exc:
        print(f"Configuration/startup error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.check:
            slot = bot.rpc.call("getSlot", [{"commitment":"confirmed"}])
            sol_price = bot.market.sol_price_usd()
            logger.warning("RPC OK slot=%s; SOL reference price %.4f USD", slot, sol_price)
            logger.warning("Mode=%s; watched wallets=%d; configured keypair=%s",
                           settings.mode, len(settings.watch_wallets),
                           "loaded locally" if bot.wallet_address else "not loaded (paper mode)")
            logger.warning("Check mode does not submit swaps.")
        elif args.status:
            bot.status()
        else:
            bot.run(once=args.once)
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
    except Exception:
        logger.exception("Fatal runtime error")
        return 1
    finally:
        bot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
