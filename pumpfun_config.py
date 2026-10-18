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
    pumpfun_auth_token: str
    pumpportal_api_key: str
    leaderboard_refresh_seconds: int
    top_traders_count: int
    dashboard_port: int
    auto_copy_on_start: bool
    stream_token_prices: bool
    allow_unindexed_pump_curve: bool
    default_leaderboard_window: str
    rpc_url: str
    solana_logs_ws_enabled: bool
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
    min_hold_before_loss_exit_seconds: int
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
    token_discovery_enabled: bool
    token_discovery_live_enabled: bool
    token_discovery_max_entry_usd: float
    token_discovery_max_watched_tokens: int
    token_discovery_watch_seconds: int
    token_discovery_max_launches_per_minute: int
    token_discovery_min_liquidity_usd: float
    token_discovery_min_score: int
    token_discovery_min_unique_buyers: int
    token_discovery_shadow_window_seconds: int
    token_discovery_max_token_age_hours: int
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
            pumpfun_auth_token=(os.getenv("PUMPFUN_AUTH_TOKEN", "").strip() or os.getenv("PUMPFUN_BEARER_TOKEN", "").strip()),
            pumpportal_api_key=os.getenv("PUMPPORTAL_API_KEY", "").strip(),
            leaderboard_refresh_seconds=max(60, _int("LEADERBOARD_REFRESH_SECONDS", 300)),
            top_traders_count=min(10, max(1, _int("TOP_TRADERS_COUNT", 10))),
            dashboard_port=max(1024, min(65535, _int("DASHBOARD_PORT", 8765))),
            auto_copy_on_start=os.getenv("AUTO_COPY_ON_START", "true").strip().lower() in {"1", "true", "yes", "on"},
            stream_token_prices=os.getenv("PUMPPORTAL_STREAM_TOKEN_PRICES", "false").strip().lower() in {"1", "true", "yes", "on"},
            allow_unindexed_pump_curve=os.getenv("ALLOW_UNINDEXED_PUMP_CURVE", "false").strip().lower() in {"1", "true", "yes", "on"},
            default_leaderboard_window=os.getenv("DEFAULT_LEADERBOARD_WINDOW", "day").strip().lower(),
            rpc_url=os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip(),
            solana_logs_ws_enabled=os.getenv("SOLANA_LOGS_WS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            keypair_path=Path(os.getenv("KEYPAIR_PATH", "./wallet.json")).expanduser(),
            max_entry_usd=_float("MAX_ENTRY_USD", 2.0),
            max_total_exposure_usd=_float("MAX_TOTAL_EXPOSURE_USD", 3.0),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
            max_entries_per_day=_int("MAX_ENTRIES_PER_DAY", 3),
            daily_loss_limit_usd=_float("DAILY_LOSS_LIMIT_USD", 1.5),
            total_loss_limit_usd=_float("TOTAL_LOSS_LIMIT_USD", 3.0),
            min_sol_reserve=_float("MIN_SOL_RESERVE", 0.008),
            starting_paper_cash_usd=_float("STARTING_PAPER_CASH_USD", 10.0),
            min_liquidity_usd=max(5000.0, _float("MIN_LIQUIDITY_USD", 5000.0)),
            min_pair_age_seconds=max(90, _int("MIN_PAIR_AGE_SECONDS", 90)),
            slippage_bps=_int("SLIPPAGE_BPS", 300),
            max_marked_loss_pct=_float("MAX_MARKED_LOSS_PCT", 0.25),
            # Loss exits are held for at least two minutes; local configuration may only increase this.
            min_hold_before_loss_exit_seconds=max(120, _int("MIN_HOLD_BEFORE_LOSS_EXIT_SECONDS", 120)),
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
            # Discovery scanning and live execution have independent opt-ins.
            token_discovery_enabled=os.getenv("TOKEN_DISCOVERY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
            token_discovery_live_enabled=os.getenv("TOKEN_DISCOVERY_LIVE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
            # Hard ceiling: configuration can reduce this but never raise it above $0.50.
            token_discovery_max_entry_usd=min(0.50, _float("TOKEN_DISCOVERY_MAX_ENTRY_USD", 0.50)),
            token_discovery_max_watched_tokens=max(1, min(10, _int("TOKEN_DISCOVERY_MAX_WATCHED_TOKENS", 5))),
            token_discovery_watch_seconds=max(30, min(600, _int("TOKEN_DISCOVERY_WATCH_SECONDS", 180))),
            token_discovery_max_launches_per_minute=max(1, min(60, _int("TOKEN_DISCOVERY_MAX_LAUNCHES_PER_MINUTE", 12))),
            # Conservative discovery floors cannot be lowered by local config.
            token_discovery_min_liquidity_usd=max(5000.0, _float("TOKEN_DISCOVERY_MIN_LIQUIDITY_USD", 5000.0)),
            token_discovery_min_score=max(80, min(100, _int("TOKEN_DISCOVERY_MIN_SCORE", 80))),
            token_discovery_min_unique_buyers=max(8, min(100, _int("TOKEN_DISCOVERY_MIN_UNIQUE_BUYERS", 8))),
            token_discovery_shadow_window_seconds=max(60, min(3600, _int("TOKEN_DISCOVERY_SHADOW_WINDOW_SECONDS", 900))),
            # The scanner only queues launches observed inside this rolling age limit; never allow >24h.
            token_discovery_max_token_age_hours=max(1, min(24, _int("TOKEN_DISCOVERY_MAX_TOKEN_AGE_HOURS", 24))),
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
        if self.token_discovery_live_enabled:
            if not self.token_discovery_enabled:
                raise ValueError("TOKEN_DISCOVERY_LIVE_ENABLED requires TOKEN_DISCOVERY_ENABLED=true")
            if self.mode != "live":
                raise ValueError("TOKEN_DISCOVERY_LIVE_ENABLED requires BOT_MODE=live")
            if not (0.10 <= self.token_discovery_max_entry_usd <= 0.50):
                raise ValueError("TOKEN_DISCOVERY_MAX_ENTRY_USD must be between $0.10 and the $0.50 hard cap")
        if self.mode == "live":
            if self.live_confirmation != "I_ACCEPT_THE_RISK_AND_UNDERSTAND_LIVE_TRADING":
                raise ValueError("Live mode not armed. Review risks and set LIVE_CONFIRMATION locally.")
            if not self.keypair_path.is_file():
                raise ValueError(f"Local keypair file not found: {self.keypair_path}")
            if not self.pumpfun_auth_token:
                raise ValueError("PUMPFUN_AUTH_TOKEN is required for automatic top-trader discovery in live mode")
            if not self.pumpportal_api_key:
                raise ValueError("PUMPPORTAL_API_KEY is required for the near-real-time account-trade feed in live mode")
        if self.mode == "paper" and self.starting_paper_cash_usd <= 0:
            raise ValueError("STARTING_PAPER_CASH_USD must be positive")
