"""Environment-based configuration. Secrets live only here (server side) and are never serialised to clients."""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AGENT_NAMES = ("scout", "news", "technical", "analyst", "skeptic", "risk_manager")
LIVE_CONFIRMATION_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"


class TradingMode(StrEnum):
    PAPER = "paper"      # real market data, simulated fills
    TESTNET = "testnet"  # real orders on Binance Spot Testnet
    LIVE = "live"        # real money; gated, see Settings.assert_mode_allowed


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+asyncpg://aegis:aegis@localhost:5432/aegis"
    redis_url: str = "redis://localhost:6379/0"

    # --- security -----------------------------------------------------------------
    secret_key: SecretStr = SecretStr("")
    admin_username: str = "admin"
    admin_password: SecretStr = SecretStr("")
    access_token_minutes: int = 60
    cors_origins: str = "http://localhost:5173"
    allowed_ips: str = ""  # comma separated CIDRs; empty = no IP restriction
    trust_proxy_headers: bool = False
    rate_limit_per_minute: int = 240
    login_rate_limit_per_minute: int = 10

    # --- LLM providers ------------------------------------------------------------
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_default_model: str = "gpt-4o-mini"
    xai_api_key: SecretStr = SecretStr("")
    xai_base_url: str = "https://api.x.ai/v1"
    xai_default_model: str = "grok-3-mini"
    # optional third OpenAI-compatible endpoint (OpenRouter, Together, local vLLM, ...)
    custom_llm_api_key: SecretStr = SecretStr("")
    custom_llm_base_url: str = ""
    custom_llm_default_model: str = ""
    llm_default_provider: str = "openai"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_daily_call_budget: int = 2000  # hard cap on LLM calls per UTC day (cost control)
    # per agent override, "provider:model", e.g. AGENT_SCOUT_MODEL=xai:grok-3-mini
    agent_scout_model: str = ""
    agent_news_model: str = ""
    agent_technical_model: str = ""
    agent_analyst_model: str = ""
    agent_skeptic_model: str = ""
    agent_risk_manager_model: str = ""

    # --- exchange / market data -----------------------------------------------------
    trading_mode: TradingMode = TradingMode.PAPER
    quote_asset: str = "USDT"
    market_data_source: str = ""  # "binance" | "binance_testnet"; default derives from trading mode
    binance_rest_url: str = "https://api.binance.com"
    binance_ws_url: str = "wss://stream.binance.com:9443"
    binance_testnet_rest_url: str = "https://testnet.binance.vision"
    binance_testnet_ws_url: str = "wss://stream.testnet.binance.vision"
    binance_testnet_api_key: SecretStr = SecretStr("")
    binance_testnet_api_secret: SecretStr = SecretStr("")
    binance_live_api_key: SecretStr = SecretStr("")
    binance_live_api_secret: SecretStr = SecretStr("")
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""
    exchange_timeout_seconds: float = 15.0

    universe_max_symbols: int = 40          # top-N by quote volume tracked in depth
    min_quote_volume_24h: Decimal = Decimal("5000000")
    universe_exclude: str = ""             # extra base assets to ignore, e.g. tokenised stocks: "NVDAB,SOXLB"
    candle_timeframes: str = "15m,1h,4h"
    stale_ticker_seconds: int = 30

    # --- news -------------------------------------------------------------------------
    news_rss_feeds: str = (
        "https://www.coindesk.com/arc/outboundfeeds/rss/,"
        "https://cointelegraph.com/rss,"
        "https://decrypt.co/feed"
    )
    cryptopanic_api_key: SecretStr = SecretStr("")
    fear_greed_url: str = "https://api.alternative.me/fng/?limit=1"
    news_max_age_hours: int = 24

    # --- orchestration ------------------------------------------------------------------
    cycle_interval_seconds: int = 900
    max_candidates_per_cycle: int = 2
    agent_timeout_seconds: float = 90.0
    symbol_cooldown_minutes: int = 120
    approved_trade_ttl_seconds: int = 300

    # --- paper trading / risk defaults (runtime-editable copies live in the DB) -------------
    starting_capital: Decimal = Decimal("1000")
    risk_per_trade_pct: Decimal = Decimal("1.0")
    max_daily_loss_pct: Decimal = Decimal("3.0")
    max_drawdown_pct: Decimal = Decimal("10.0")
    max_exposure_pct: Decimal = Decimal("30.0")
    max_open_positions: int = 5
    min_risk_reward: Decimal = Decimal("2.0")
    fee_rate: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")
    max_entry_deviation_pct: Decimal = Decimal("0.5")

    # --- notifications ----------------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    notify_webhook_url: SecretStr = SecretStr("")

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _lower(cls, v: object) -> object:
        return v.lower() if isinstance(v, str) else v

    # ---------------------------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def timeframes(self) -> list[str]:
        return [t.strip() for t in self.candle_timeframes.split(",") if t.strip()]

    @property
    def rss_feeds(self) -> list[str]:
        return [f.strip() for f in self.news_rss_feeds.split(",") if f.strip()]

    @property
    def data_source(self) -> str:
        if self.market_data_source:
            return self.market_data_source
        # prices must come from the venue where fills happen, so SL/TP track executable prices
        return "binance_testnet" if self.trading_mode == TradingMode.TESTNET else "binance"

    @property
    def is_paper(self) -> bool:
        return self.trading_mode == TradingMode.PAPER

    def agent_model_spec(self, agent: str) -> str:
        return getattr(self, f"agent_{agent}_model", "") or ""

    def provider_configured(self, provider: str) -> bool:
        key = {
            "openai": self.openai_api_key,
            "xai": self.xai_api_key,
            "custom": self.custom_llm_api_key,
        }.get(provider)
        if key is None:
            return False
        if provider == "custom" and not self.custom_llm_base_url:
            return False
        return bool(key.get_secret_value())

    def mode_problems(self) -> list[str]:
        """Reasons the configured trading mode must not run. Empty list = allowed."""
        problems: list[str] = []
        if self.trading_mode == TradingMode.TESTNET:
            if not (self.binance_testnet_api_key.get_secret_value() and self.binance_testnet_api_secret.get_secret_value()):
                problems.append("TESTNET mode requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET")
        if self.trading_mode == TradingMode.LIVE:
            if not self.live_trading_enabled:
                problems.append("LIVE mode requires LIVE_TRADING_ENABLED=true")
            if self.live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
                problems.append("LIVE mode requires LIVE_TRADING_CONFIRMATION to equal the documented phrase")
            if not (self.binance_live_api_key.get_secret_value() and self.binance_live_api_secret.get_secret_value()):
                problems.append("LIVE mode requires separate BINANCE_LIVE_API_KEY / BINANCE_LIVE_API_SECRET")
            live, test = self.binance_live_api_key.get_secret_value(), self.binance_testnet_api_key.get_secret_value()
            if live and live == test:
                problems.append("live and testnet credentials must differ")
        return problems

    def security_problems(self) -> list[str]:
        problems: list[str] = []
        if len(self.secret_key.get_secret_value()) < 32:
            problems.append("SECRET_KEY must be set to at least 32 random characters")
        if len(self.admin_password.get_secret_value()) < 12:
            problems.append("ADMIN_PASSWORD must be set to at least 12 characters")
        return problems


class RiskParams(BaseModel):
    """Runtime-editable risk parameters. Hard bounds are enforced here so neither the UI nor an agent can loosen
    limits beyond what the code considers sane."""
    model_config = ConfigDict(extra="ignore")

    starting_capital: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(gt=0, le=Decimal("2"))
    max_daily_loss_pct: Decimal = Field(gt=0, le=Decimal("10"))
    max_drawdown_pct: Decimal = Field(gt=0, le=Decimal("30"))
    max_exposure_pct: Decimal = Field(gt=0, le=Decimal("100"))
    max_open_positions: int = Field(ge=1, le=20)
    min_risk_reward: Decimal = Field(ge=Decimal("1"), le=Decimal("10"))
    fee_rate: Decimal = Field(ge=0, le=Decimal("0.01"))
    slippage_bps: Decimal = Field(ge=0, le=Decimal("100"))
    max_entry_deviation_pct: Decimal = Field(gt=0, le=Decimal("5"))
    min_stop_distance_pct: Decimal = Field(default=Decimal("0.2"), gt=0, le=Decimal("5"))
    max_stop_distance_pct: Decimal = Field(default=Decimal("15"), gt=0, le=Decimal("50"))

    @classmethod
    def from_settings(cls, s: Settings) -> RiskParams:
        return cls(
            starting_capital=s.starting_capital, risk_per_trade_pct=s.risk_per_trade_pct,
            max_daily_loss_pct=s.max_daily_loss_pct, max_drawdown_pct=s.max_drawdown_pct,
            max_exposure_pct=s.max_exposure_pct, max_open_positions=s.max_open_positions,
            min_risk_reward=s.min_risk_reward, fee_rate=s.fee_rate, slippage_bps=s.slippage_bps,
            max_entry_deviation_pct=s.max_entry_deviation_pct,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
