"""Configuration for the Pump.fun-specific bot entrypoint."""
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


@dataclass(frozen=True)
class PumpFunSettings:
    mode: str
    live_confirmation: str
    watch_wallets: tuple[str, ...]
    pumpfun_bearer_token: str
    pumpportal_api_key: str
    leaderboard_refresh_seconds: int
    top_traders_count: int
    dashboard_port: int
    auto_copy_on_start: bool
    stream_token_prices: bool
    allow_unindexed_pump_curve: bool
    default_leaderboard_window: str
    rpc_url: str
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
    pumpportal_priority_fee_sol: float
    database_path: Path
    log_level: str

    @classmethod
    def load(cls) -> "PumpFunSettings":
        mode = os.getenv("BOT_MODE", "paper").strip().lower()
        if mode not in {"paper", "live"}:
            raise ValueError("BOT_MODE must be 'paper' or 'live'")
        wallets = tuple(dict.fromkeys(w.strip() for w in os.getenv("WATCH_WALLETS", "").split(",") if w.strip()))
        obj = cls(
            mode=mode,
            live_confirmation=os.getenv("LIVE_CONFIRMATION", "").strip(),
            watch_wallets=wallets,
            pumpfun_bearer_token=os.getenv("PUMPFUN_BEARER_TOKEN", "").strip(),
            pumpportal_api_key=os.getenv("PUMPPORTAL_API_KEY", "").strip(),
            leaderboard_refresh_seconds=max(60, _int("LEADERBOARD_REFRESH_SECONDS", 300)),
            top_traders_count=min(10, max(1, _int("TOP_TRADERS_COUNT", 10))),
            dashboard_port=max(1024, min(65535, _int("DASHBOARD_PORT", 8765))),
            auto_copy_on_start=os.getenv("AUTO_COPY_ON_START", "true").strip().lower() in {"1", "true", "yes", "on"},
            stream_token_prices=os.getenv("PUMPPORTAL_STREAM_TOKEN_PRICES", "false").strip().lower() in {"1", "true", "yes", "on"},
            allow_unindexed_pump_curve=os.getenv("ALLOW_UNINDEXED_PUMP_CURVE", "true").strip().lower() in {"1", "true", "yes", "on"},
            default_leaderboard_window=os.getenv("DEFAULT_LEADERBOARD_WINDOW", "day").strip().lower(),
            rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip(),
            keypair_path=Path(os.getenv("KEYPAIR_PATH", "./wallet.json")).expanduser(),
            max_entry_usd=_float("MAX_ENTRY_USD", 2.0),
            max_total_exposure_usd=_float("MAX_TOTAL_EXPOSURE_USD", 3.0),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
            max_entries_per_day=_int("MAX_ENTRIES_PER_DAY", 3),
            daily_loss_limit_usd=_float("DAILY_LOSS_LIMIT_USD", 1.5),
            total_loss_limit_usd=_float("TOTAL_LOSS_LIMIT_USD", 3.0),
            min_sol_reserve=_float("MIN_SOL_RESERVE", 0.008),
            starting_paper_cash_usd=_float("STARTING_PAPER_CASH_USD", 10.0),
            min_liquidity_usd=_float("MIN_LIQUIDITY_USD", 1000.0),
            min_pair_age_seconds=_int("MIN_PAIR_AGE_SECONDS", 10),
            slippage_bps=_int("SLIPPAGE_BPS", 300),
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
            pumpportal_priority_fee_sol=_float("PUMPPORTAL_PRIORITY_FEE_SOL", 0.00001),
            database_path=Path(os.getenv("DATABASE_PATH", "memecoin-pumpfun.db")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        obj.validate()
        return obj

    def validate(self) -> None:
        if not self.rpc_url.startswith("https://"):
            raise ValueError("SOLANA_RPC_URL must use HTTPS")
        if self.default_leaderboard_window not in {"overall", "day", "week", "month"}:
            raise ValueError("DEFAULT_LEADERBOARD_WINDOW must be overall, day, week or month")
        if self.max_entry_usd <= 0 or self.max_total_exposure_usd <= 0:
            raise ValueError("Entry and exposure limits must be positive")
        if not (0 <= self.slippage_bps <= 500):
            raise ValueError("SLIPPAGE_BPS must be between 0 and 500 (5%)")
        if not (0 < self.trailing_stop_pct < 1):
            raise ValueError("TRAILING_STOP_PCT must be between 0 and 1")
        if self.take_profit_1_sell_fraction + self.take_profit_2_sell_fraction >= 1:
            raise ValueError("Take-profit fractions must sum to less than 1")
        if self.mode == "live":
            if self.live_confirmation != "I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING":
                raise ValueError("Live mode not armed. Review risks and set LIVE_CONFIRMATION locally.")
            if not self.keypair_path.is_file():
                raise ValueError(f"Local keypair file not found: {self.keypair_path}")
            if not self.pumpfun_bearer_token:
                raise ValueError("PUMPFUN_BEARER_TOKEN is required for automatic top-trader discovery in live mode")
            if not self.pumpportal_api_key:
                raise ValueError("PUMPPORTAL_API_KEY is required for the near-real-time account-trade feed in live mode")
        if self.mode == "paper" and self.starting_paper_cash_usd <= 0:
            raise ValueError("STARTING_PAPER_CASH_USD must be positive")
