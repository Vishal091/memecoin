"""Configuration and safety validation for the local Solana copy bot."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be 'true' or 'false'")
    return raw == "true"


@dataclass(frozen=True)
class Settings:
    mode: str
    live_confirmation: str
    live_entries_approved: bool
    watch_wallets: tuple[str, ...]
    rpc_url: str
    jupiter_api_key: str
    keypair_path: Path
    max_entry_usd: float
    max_total_exposure_usd: float
    max_open_positions: int
    max_entries_per_day: int
    daily_loss_limit_usd: float
    total_loss_limit_usd: float
    min_sol_reserve: float
    starting_paper_cash_usd: float
    min_liquidity_usd: float
    min_pair_age_seconds: int
    slippage_bps: int
    max_quote_shortfall_pct: float
    max_marked_loss_pct: float
    trailing_stop_pct: float
    take_profit_1_pct: float
    take_profit_2_pct: float
    take_profit_1_sell_fraction: float
    take_profit_2_sell_fraction: float
    poll_seconds: int
    experiment_duration_days: int
    request_timeout_seconds: int
    paper_swap_cost_usd: float
    database_path: Path
    log_level: str

    @classmethod
    def load(cls) -> "Settings":
        mode = os.getenv("BOT_MODE", "paper").strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError("BOT_MODE must be 'paper' or 'live'")
        wallets = tuple(dict.fromkeys(
            w.strip() for w in os.getenv("WATCH_WALLETS", "").split(",") if w.strip()
        ))
        settings = cls(
            mode=mode,
            live_confirmation=os.getenv("LIVE_CONFIRMATION", "").strip(),
            live_entries_approved=_bool("LIVE_ENTRIES_APPROVED", False),
            watch_wallets=wallets,
            rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip(),
            jupiter_api_key=os.getenv("JUPITER_API_KEY", "").strip(),
            keypair_path=Path(os.getenv("KEYPAIR_PATH", "./wallet.json")).expanduser(),
            max_entry_usd=_float("MAX_ENTRY_USD", 2.0),
            max_total_exposure_usd=_float("MAX_TOTAL_EXPOSURE_USD", 3.0),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
            max_entries_per_day=_int("MAX_ENTRIES_PER_DAY", 3),
            daily_loss_limit_usd=_float("DAILY_LOSS_LIMIT_USD", 1.5),
            total_loss_limit_usd=_float("TOTAL_LOSS_LIMIT_USD", 3.0),
            min_sol_reserve=_float("MIN_SOL_RESERVE", 0.008),
            starting_paper_cash_usd=_float("STARTING_PAPER_CASH_USD", 10.0),
            min_liquidity_usd=_float("MIN_LIQUIDITY_USD", 5000.0),
            min_pair_age_seconds=_int("MIN_PAIR_AGE_SECONDS", 90),
            slippage_bps=_int("SLIPPAGE_BPS", 150),
            max_quote_shortfall_pct=_float("MAX_QUOTE_SHORTFALL_PCT", 0.04),
            max_marked_loss_pct=_float("MAX_MARKED_LOSS_PCT", 0.25),
            trailing_stop_pct=_float("TRAILING_STOP_PCT", 0.20),
            take_profit_1_pct=_float("TAKE_PROFIT_1_PCT", 0.30),
            take_profit_2_pct=_float("TAKE_PROFIT_2_PCT", 0.60),
            take_profit_1_sell_fraction=_float("TAKE_PROFIT_1_SELL_FRACTION", 0.40),
            take_profit_2_sell_fraction=_float("TAKE_PROFIT_2_SELL_FRACTION", 0.30),
            poll_seconds=max(2, _int("POLL_SECONDS", 4)),
            experiment_duration_days=max(1, _int("EXPERIMENT_DURATION_DAYS", 7)),
            request_timeout_seconds=max(3, _int("REQUEST_TIMEOUT_SECONDS", 12)),
            paper_swap_cost_usd=_float("PAPER_SWAP_COST_USD", 0.02),
            database_path=Path(os.getenv("DATABASE_PATH", "memecoin.db")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.rpc_url.startswith("https://"):
            raise ValueError("SOLANA_RPC_URL must use HTTPS")
        if self.max_entry_usd <= 0 or self.max_total_exposure_usd <= 0:
            raise ValueError("Entry and exposure limits must be greater than zero")
        if not (0 <= self.slippage_bps <= 300):
            raise ValueError("SLIPPAGE_BPS must be between 0 and 300 (3%)")
        if not (0 < self.take_profit_1_sell_fraction <= 1):
            raise ValueError("TAKE_PROFIT_1_SELL_FRACTION must be in (0, 1]")
        if not (0 < self.take_profit_2_sell_fraction <= 1):
            raise ValueError("TAKE_PROFIT_2_SELL_FRACTION must be in (0, 1]")
        if self.take_profit_1_sell_fraction + self.take_profit_2_sell_fraction >= 1:
            raise ValueError("Staged sell fractions must sum to less than 1 to leave a runner")
        if not (0 < self.trailing_stop_pct < 1):
            raise ValueError("TRAILING_STOP_PCT must be between 0 and 1")
        if self.mode == "live":
            if not self.live_entries_approved:
                raise ValueError(
                    "Live mode is disabled by default. Set LIVE_ENTRIES_APPROVED=true only "
                    "after reviewing and accepting the bounded live-canary limits."
                )
            if self.live_confirmation != "I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING":
                raise ValueError("Live mode not armed. Set LIVE_CONFIRMATION locally only after reviewing the risks.")
            if _bool("TRENDING_DISCOVERY_LIVE_ENABLED", False) and not self.jupiter_api_key:
                raise ValueError("JUPITER_API_KEY is required when trending discovery live buys are enabled")
            if not self.keypair_path.is_file():
                raise ValueError(f"Local keypair file not found: {self.keypair_path}")

            # Bounded first canary: keep the initial real-money exposure deliberately tiny.
            if self.max_entry_usd > 0.50:
                raise ValueError("Live canary requires MAX_ENTRY_USD <= 0.50.")
            if self.max_total_exposure_usd > 5.50:
                raise ValueError("Live trial requires MAX_TOTAL_EXPOSURE_USD <= 5.50 for up to ten reserved positions; actual fees/rent can be higher.")
            if not (1 <= self.max_open_positions <= 10):
                raise ValueError("Live trial requires MAX_OPEN_POSITIONS between 1 and 10.")
            if not (1 <= self.max_entries_per_day <= 10):
                raise ValueError("Live canary requires MAX_ENTRIES_PER_DAY between 1 and 10.")
            if not (0 < self.daily_loss_limit_usd <= 0.50):
                raise ValueError("Live canary requires DAILY_LOSS_LIMIT_USD in (0, 0.50].")
            if not (0 < self.total_loss_limit_usd <= 0.50):
                raise ValueError("Live canary requires TOTAL_LOSS_LIMIT_USD in (0, 0.50].")
            if self.min_sol_reserve < 0.025:
                raise ValueError("Live canary requires MIN_SOL_RESERVE >= 0.025 SOL.")
            if self.max_marked_loss_pct > 0.18:
                raise ValueError("Live canary requires MAX_MARKED_LOSS_PCT <= 0.18.")
            if self.trailing_stop_pct > 0.12:
                raise ValueError("Live canary requires TRAILING_STOP_PCT <= 0.12.")
            if self.max_quote_shortfall_pct > 0.04:
                raise ValueError("Live canary requires MAX_QUOTE_SHORTFALL_PCT <= 0.04.")
            if _bool("TOKEN_DISCOVERY_LIVE_ENABLED", False):
                raise ValueError("Initial live canary is leader-copy only; TOKEN_DISCOVERY_LIVE_ENABLED must be false.")
            if _bool("TRENDING_DISCOVERY_LIVE_ENABLED", False):
                raise ValueError("Initial live canary is leader-copy only; TRENDING_DISCOVERY_LIVE_ENABLED must be false.")
        if self.mode == "paper" and self.starting_paper_cash_usd <= 0:
            raise ValueError("STARTING_PAPER_CASH_USD must be greater than zero")
