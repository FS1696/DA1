from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Final
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "ConfigError",
    "DEFAULT_TICKERS",
    "DEFAULT_DATA_SOURCE",
    "SUPPORTED_DATA_SOURCES",
    "SOURCE_CREDENTIAL_ENV_VARS",
    "REQUIRED_DATABASE_SCHEME",
    "TICKER_PROFILES",
    "TickerProfile",
    "DataSourceSettings",
    "ScheduleSettings",
    "IndicatorSettings",
    "WyckoffSettings",
    "SignalSettings",
    "TelegramSettings",
    "Settings",
    "load_settings",
    "get_settings",
]

# ---------------------------------------------------------------------------
# Giá trị mặc định (UPPER_SNAKE_CASE theo quy ước dự án)
# ---------------------------------------------------------------------------

# Danh sách khởi đầu 12 mã ngân hàng (đặc tả mục 1.2 và 4.1).
# Có thể ghi đè bằng biến môi trường TICKERS, ví dụ: TICKERS=ACB,MBB,VCB
DEFAULT_TICKERS: Final[tuple[str, ...]] = (
    "ACB", "BID", "CTG", "HDB", "MBB", "SHB",
    "STB", "TCB", "TPB", "VCB", "VIB", "VPB",
)
DEFAULT_INDUSTRY: Final[str] = "Ngân hàng"

DEFAULT_TIMEZONE: Final[str] = "Asia/Ho_Chi_Minh"
DEFAULT_LOG_LEVEL: Final[str] = "INFO"

# Lịch chạy: thứ Hai - thứ Sáu, 16:00 (đặc tả mục 4.5).
DEFAULT_SCHEDULE_DAY_OF_WEEK: Final[str] = "mon-fri"
DEFAULT_SCHEDULE_HOUR: Final[int] = 16
DEFAULT_SCHEDULE_MINUTE: Final[int] = 0

# Nguồn dữ liệu: CHỈ dùng vnstock thế hệ 6 (requirements.txt ghim
# vnstock==6.0.1 và vnstock-oapi==0.1.2) với cú pháp
# Market(source).equity(ticker).ohlcv(...). Không trộn với API thế hệ cũ
# (Vnstock().stock(...), Quote(...), nguồn VCI/KBS).
DEFAULT_DATA_PROVIDER: Final[str] = "vnstock"
# Nguồn hỗ trợ equity.ohlcv() theo tài liệu vnstock 6.0.1. Kiểm tra lại khi
# nâng cấp vnstock (sources.readers("market.equity.ohlcv")).
SUPPORTED_DATA_SOURCES: Final[tuple[str, ...]] = ("dnse", "fhsc", "ssi", "tcbs")
# Lựa chọn tạm thời của nhóm (khớp env.example). v6 không có nguồn mặc định nên
# vẫn phải khai báo rõ; đổi nguồn thì đổi DATA_SOURCE và khoá tương ứng.
DEFAULT_DATA_SOURCE: Final[str] = "fhsc"
# Biến môi trường chứa khoá của từng nguồn (theo tài liệu vnstock-oapi).
# Lưu ý: tcbs còn đòi Smart OTP gõ tay mỗi lần mở phiên (tối đa 24 giờ) nên
# không phù hợp với job chạy tự động theo lịch.
SOURCE_CREDENTIAL_ENV_VARS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "dnse": ("DNSE_API_KEY", "DNSE_API_SECRET"),
        "fhsc": ("FHSC_API_KEY",),
        "ssi": ("SSI_API_KEY", "SSI_API_SECRET"),
        "tcbs": ("TCBS_API_KEY",),
    }
)
# Hồ sơ đầu ra thống nhất giữa các nguồn (tham số compat của vnstock 6).
VNSTOCK_COMPAT: Final[str] = "v6"
DEFAULT_BACKFILL_YEARS: Final[int] = 5
DEFAULT_RECONCILE_BUFFER_DAYS: Final[int] = 7
DEFAULT_FETCH_DELAY_SECONDS: Final[float] = 1.0
DEFAULT_FETCH_MAX_RETRIES: Final[int] = 3
# Chờ trước lần thử lại đầu tiên khi lỗi tạm thời; các lần sau nhân đôi.
DEFAULT_FETCH_RETRY_BACKOFF_SECONDS: Final[float] = 5.0
# Tài liệu vnstock khuyên chờ khoảng một phút khi nguồn giới hạn tần suất.
DEFAULT_RATE_LIMIT_WAIT_SECONDS: Final[float] = 60.0
# Độ dài tối đa (ngày lịch) của mỗi lần gọi khi nạp lịch sử, để tránh vượt giới
# hạn số dòng/lần gọi của nguồn (đặc tả mục 4.3).
DEFAULT_MAX_DAYS_PER_REQUEST: Final[int] = 366

# Chỉ báo kỹ thuật (đặc tả mục 6.1).
DEFAULT_MA_SHORT_PERIOD: Final[int] = 20
DEFAULT_MA_LONG_PERIOD: Final[int] = 50
DEFAULT_RSI_PERIOD: Final[int] = 14
DEFAULT_RSI_OVERBOUGHT: Final[float] = 70.0
DEFAULT_RSI_OVERSOLD: Final[float] = 30.0
DEFAULT_MACD_FAST_PERIOD: Final[int] = 12
DEFAULT_MACD_SLOW_PERIOD: Final[int] = 26
DEFAULT_MACD_SIGNAL_PERIOD: Final[int] = 9
DEFAULT_VOLUME_MA_PERIOD: Final[int] = 20

# Wyckoff (đặc tả mục 6.2 - 6.4). Các ngưỡng này chỉ là giả thuyết ban đầu,
# cần backtest trước khi coi là tham số chính thức.
DEFAULT_WYCKOFF_LOOKBACK_SESSIONS: Final[int] = 50
DEFAULT_PIVOT_CONFIRM_BARS: Final[int] = 3
DEFAULT_RANGE_TOLERANCE_PCT: Final[float] = 0.015
VOLUME_SPIKE_MULTIPLIER: Final[float] = 1.5
DEFAULT_MIN_BOUNDARY_TOUCHES: Final[int] = 2

# Chấm điểm tín hiệu (đặc tả mục 7.3). Cũng chỉ là giả thuyết ban đầu.
DEFAULT_RULE_VERSION: Final[str] = "v1"

# Telegram (đặc tả mục 8.4).
DEFAULT_TELEGRAM_TIMEOUT_SECONDS: Final[float] = 10.0
DEFAULT_TELEGRAM_MAX_RETRIES: Final[int] = 3
DEFAULT_TELEGRAM_RETRY_BACKOFF_SECONDS: Final[float] = 2.0
# Telegram khuyến nghị tối đa ~20 tin/phút trong một group -> cách nhau >= 3 giây.
DEFAULT_TELEGRAM_MIN_INTERVAL_SECONDS: Final[float] = 3.0

# Phải là driver psycopg 3 (psycopg[binary]). Dạng "postgresql://" khiến
# SQLAlchemy 2.x mặc định tìm psycopg2, vốn không có trong requirements.txt.
REQUIRED_DATABASE_SCHEME: Final[str] = "postgresql+psycopg"

_TICKER_PATTERN: Final = re.compile(r"^[A-Z0-9]{3,10}$")  # khớp VARCHAR(10)
_DAY_OF_WEEK_PATTERN: Final = re.compile(
    r"^(\*|mon|tue|wed|thu|fri|sat|sun|[0-6])"
    r"([-,](mon|tue|wed|thu|fri|sat|sun|[0-6]))*$",
    re.IGNORECASE,
)
_TELEGRAM_TOKEN_PATTERN: Final = re.compile(r"^\d+:[\w-]+$")
_TELEGRAM_CHAT_ID_PATTERN: Final = re.compile(r"^(-?\d+|@\w+)$")
_VALID_LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
# Giá trị mẫu trong env.example (change_me, put_token_here,
# put_your_fhsc_api_key_here, ...) hoặc để trống: coi như chưa cấu hình.
_PLACEHOLDER_PATTERN: Final = re.compile(r"^(|change_?me|put_\w*_here)$", re.IGNORECASE)


class ConfigError(ValueError):
   


def _is_placeholder(value: str) -> bool:
   
    return _PLACEHOLDER_PATTERN.match(value.strip()) is not None


# ---------------------------------------------------------------------------
# Thông tin mô tả từng mã (dùng để nạp bảng `stocks`)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickerProfile:
    

    company_name: str
    exchange: str
    industry: str = DEFAULT_INDUSTRY


# Chỉ là metadata mô tả; nên đối chiếu lại sàn niêm yết/tên công ty trước khi chạy chính thức.
TICKER_PROFILES: Final[Mapping[str, TickerProfile]] = MappingProxyType(
    {
        "ACB": TickerProfile("Ngân hàng TMCP Á Châu", "HOSE"),
        "BID": TickerProfile("Ngân hàng TMCP Đầu tư và Phát triển Việt Nam", "HOSE"),
        "CTG": TickerProfile("Ngân hàng TMCP Công Thương Việt Nam", "HOSE"),
        "HDB": TickerProfile("Ngân hàng TMCP Phát triển Thành phố Hồ Chí Minh", "HOSE"),
        "MBB": TickerProfile("Ngân hàng TMCP Quân đội", "HOSE"),
        "SHB": TickerProfile("Ngân hàng TMCP Sài Gòn - Hà Nội", "HOSE"),
        "STB": TickerProfile("Ngân hàng TMCP Sài Gòn Thương Tín", "HOSE"),
        "TCB": TickerProfile("Ngân hàng TMCP Kỹ thương Việt Nam", "HOSE"),
        "TPB": TickerProfile("Ngân hàng TMCP Tiên Phong", "HOSE"),
        "VCB": TickerProfile("Ngân hàng TMCP Ngoại thương Việt Nam", "HOSE"),
        "VIB": TickerProfile("Ngân hàng TMCP Quốc tế Việt Nam", "HOSE"),
        "VPB": TickerProfile("Ngân hàng TMCP Việt Nam Thịnh Vượng", "HOSE"),
    }
)


# ---------------------------------------------------------------------------
# Các nhóm cấu hình
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataSourceSettings:
    

    provider: str = DEFAULT_DATA_PROVIDER
    source: str = DEFAULT_DATA_SOURCE
    compat: str = VNSTOCK_COMPAT
    # Tên (không phải giá trị) các biến khoá còn thiếu/còn là giá trị mẫu của
    # nguồn đã chọn. Loader điền vào; không giữ khoá trong đối tượng cấu hình.
    missing_credentials: tuple[str, ...] = ()
    backfill_years: int = DEFAULT_BACKFILL_YEARS
    # Cập nhật hằng ngày lấy lùi thêm vài ngày để đối soát dữ liệu đã lưu.
    reconcile_buffer_days: int = DEFAULT_RECONCILE_BUFFER_DAYS
    fetch_delay_seconds: float = DEFAULT_FETCH_DELAY_SECONDS
    fetch_max_retries: int = DEFAULT_FETCH_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_FETCH_RETRY_BACKOFF_SECONDS
    rate_limit_wait_seconds: float = DEFAULT_RATE_LIMIT_WAIT_SECONDS
    max_days_per_request: int = DEFAULT_MAX_DAYS_PER_REQUEST

    @property
    def source_label(self) -> str:
        
        return f"{self.provider}:{self.source}"


@dataclass(frozen=True)
class ScheduleSettings:
    

    day_of_week: str = DEFAULT_SCHEDULE_DAY_OF_WEEK
    hour: int = DEFAULT_SCHEDULE_HOUR
    minute: int = DEFAULT_SCHEDULE_MINUTE


@dataclass(frozen=True)
class IndicatorSettings:
    

    ma_short_period: int = DEFAULT_MA_SHORT_PERIOD
    ma_long_period: int = DEFAULT_MA_LONG_PERIOD
    rsi_period: int = DEFAULT_RSI_PERIOD
    rsi_overbought: float = DEFAULT_RSI_OVERBOUGHT
    rsi_oversold: float = DEFAULT_RSI_OVERSOLD
    macd_fast_period: int = DEFAULT_MACD_FAST_PERIOD
    macd_slow_period: int = DEFAULT_MACD_SLOW_PERIOD
    macd_signal_period: int = DEFAULT_MACD_SIGNAL_PERIOD
    volume_ma_period: int = DEFAULT_VOLUME_MA_PERIOD


@dataclass(frozen=True)
class WyckoffSettings:
    

    # Cửa sổ lịch sử để tìm vùng hỗ trợ/kháng cự (đặc tả đề xuất 40-60 phiên).
    lookback_sessions: int = DEFAULT_WYCKOFF_LOOKBACK_SESSIONS
    # Đỉnh/đáy cục bộ chỉ được xác nhận sau N phiên kế tiếp. Ngày phát hiện
    # phải là ngày xác nhận, không gán ngược về quá khứ (tránh look-ahead bias).
    pivot_confirm_bars: int = DEFAULT_PIVOT_CONFIRM_BARS
    # Dung sai khi gom các đỉnh/đáy gần nhau thành một vùng giá (tỷ lệ so với giá).
    range_tolerance_pct: float = DEFAULT_RANGE_TOLERANCE_PCT
    # Volume > multiplier * Volume MA20 được coi là khối lượng đột biến (SOS/SOW).
    volume_spike_multiplier: float = VOLUME_SPIKE_MULTIPLIER
    min_boundary_touches: int = DEFAULT_MIN_BOUNDARY_TOUCHES


@dataclass(frozen=True)
class SignalSettings:
    

    trend_weight: int = 2
    macd_weight: int = 1
    rsi_weight: int = 1
    wyckoff_weight: int = 2
    volume_weight: int = 1
    bullish_threshold: int = 4
    bearish_threshold: int = -4
    # Đưa vào dedup_key: ticker|trading_date|alert_type|rule_version.
    rule_version: str = DEFAULT_RULE_VERSION


@dataclass(frozen=True)
class TelegramSettings:
    

    bot_token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    timeout_seconds: float = DEFAULT_TELEGRAM_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_TELEGRAM_MAX_RETRIES
    retry_backoff_seconds: float = DEFAULT_TELEGRAM_RETRY_BACKOFF_SECONDS
    min_interval_seconds: float = DEFAULT_TELEGRAM_MIN_INTERVAL_SECONDS
    send_daily_summary: bool = True
    max_alerts_per_ticker_per_day: int = 1


@dataclass(frozen=True)
class Settings:
    

    tickers: tuple[str, ...]
    database_url: str = field(repr=False)
    timezone: str = DEFAULT_TIMEZONE
    log_level: str = DEFAULT_LOG_LEVEL
    # Tên các biến POSTGRES_* không khớp với DATABASE_URL (loader điền vào).
    database_mismatches: tuple[str, ...] = ()
    data: DataSourceSettings = field(default_factory=DataSourceSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    indicators: IndicatorSettings = field(default_factory=IndicatorSettings)
    wyckoff: WyckoffSettings = field(default_factory=WyckoffSettings)
    signals: SignalSettings = field(default_factory=SignalSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)

    @property
    def tzinfo(self) -> ZoneInfo:
        
        return ZoneInfo(self.timezone)

    @property
    def min_history_sessions(self) -> int:
        
        ind, wyk = self.indicators, self.wyckoff
        return max(
            ind.ma_long_period,
            ind.macd_slow_period + ind.macd_signal_period,
            ind.rsi_period + 1,
            ind.volume_ma_period,
            wyk.lookback_sessions + wyk.pivot_confirm_bars,
        )

    @property
    def masked_database_url(self) -> str:
        
        try:
            parts = urlsplit(self.database_url)
            if parts.password is None:
                return self.database_url
            port = f":{parts.port}" if parts.port else ""
            netloc = f"{parts.username or ''}:***@{parts.hostname or ''}{port}"
        except ValueError:
            return "<invalid DATABASE_URL>"
        return parts._replace(netloc=netloc).geturl()

    def problems(self, *, require_telegram: bool = True) -> list[str]:
        
        issues = [
            *self._ticker_problems(),
            *self._database_problems(),
            *self._data_source_problems(),
            *self._schedule_problems(),
            *self._analysis_problems(),
        ]
        if self.log_level not in _VALID_LOG_LEVELS:
            issues.append(f"LOG_LEVEL '{self.log_level}' không hợp lệ.")
        if require_telegram:
            issues.extend(self._telegram_problems())
        return issues

    def validate(self, *, require_telegram: bool = True) -> None:
       
        issues = self.problems(require_telegram=require_telegram)
        if issues:
            raise ConfigError(_format_errors(issues))

    # -- kiểm tra theo nhóm -------------------------------------------------

    def _ticker_problems(self) -> list[str]:
        if not self.tickers:
            return ["Danh sách TICKERS đang rỗng."]
        invalid = [t for t in self.tickers if not _TICKER_PATTERN.match(t)]
        if invalid:
            return [f"Mã cổ phiếu không hợp lệ (cần 3-10 ký tự A-Z/0-9): {invalid}"]
        return []

    def _database_problems(self) -> list[str]:
        url = self.database_url
        if not url:
            return ["DATABASE_URL chưa được cấu hình."]
        try:
            parts = urlsplit(url)
            _ = parts.port  # ép kiểm tra cổng hợp lệ
        except ValueError:
            return ["DATABASE_URL không đúng định dạng URL."]
        if parts.scheme != REQUIRED_DATABASE_SCHEME:
            return [
                f"DATABASE_URL phải bắt đầu bằng {REQUIRED_DATABASE_SCHEME}:// "
                f"(hiện là '{parts.scheme}://'). Dạng postgresql:// làm SQLAlchemy "
                "tìm driver psycopg2, vốn không được cài."
            ]
        if not parts.hostname or not parts.path.strip("/"):
            return ["DATABASE_URL thiếu host hoặc tên database."]

        issues: list[str] = []
        if parts.password is not None and _is_placeholder(unquote(parts.password)):
            issues.append("Mật khẩu trong DATABASE_URL đang trống hoặc còn là giá trị mẫu.")
        if self.database_mismatches:
            names = ", ".join(self.database_mismatches)
            issues.append(
                f"DATABASE_URL không khớp {names}: container db được tạo bằng các "
                "biến POSTGRES_* nên app sẽ không đăng nhập được."
            )
        return issues

    def _data_source_problems(self) -> list[str]:
        source = self.data.source
        choices = ", ".join(SUPPORTED_DATA_SOURCES)
        if source not in SUPPORTED_DATA_SOURCES:
            return [
                f"DATA_SOURCE '{source}' không nằm trong nguồn hỗ trợ equity.ohlcv() "
                f"của vnstock 6.0.1. Chọn một trong: {choices}."
            ]
        if self.data.missing_credentials:
            names = ", ".join(self.data.missing_credentials)
            return [
                f"Thiếu khoá truy cập cho nguồn '{source}': hãy đặt {names} trong .env "
                "(giá trị mẫu put_..._here chưa được thay)."
            ]
        return []

    def _schedule_problems(self) -> list[str]:
        issues: list[str] = []
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            issues.append(
                f"TIMEZONE '{self.timezone}' không hợp lệ "
                "(ví dụ: Asia/Ho_Chi_Minh); kiểm tra gói tzdata đã được cài."
            )
        if not _DAY_OF_WEEK_PATTERN.match(self.schedule.day_of_week):
            issues.append(
                f"SCHEDULE_DAY_OF_WEEK '{self.schedule.day_of_week}' không hợp lệ "
                "(ví dụ: mon-fri)."
            )
        if not 0 <= self.schedule.hour <= 23:
            issues.append("SCHEDULE_HOUR phải nằm trong khoảng 0-23.")
        if not 0 <= self.schedule.minute <= 59:
            issues.append("SCHEDULE_MINUTE phải nằm trong khoảng 0-59.")
        return issues

    def _analysis_problems(self) -> list[str]:
        ind, wyk, sig = self.indicators, self.wyckoff, self.signals
        periods = (
            ind.ma_short_period, ind.ma_long_period, ind.rsi_period,
            ind.macd_fast_period, ind.macd_slow_period, ind.macd_signal_period,
            ind.volume_ma_period, wyk.lookback_sessions, wyk.pivot_confirm_bars,
        )
        checks = (
            (all(p > 0 for p in periods), "Các chu kỳ chỉ báo/Wyckoff phải là số dương."),
            (ind.ma_short_period < ind.ma_long_period, "MA ngắn phải nhỏ hơn MA dài."),
            (ind.macd_fast_period < ind.macd_slow_period, "MACD fast phải nhỏ hơn slow."),
            (0 < ind.rsi_oversold < ind.rsi_overbought < 100, "Ngưỡng RSI không hợp lệ."),
            (wyk.volume_spike_multiplier > 1.0, "volume_spike_multiplier phải lớn hơn 1."),
            (0 < wyk.range_tolerance_pct < 0.1, "range_tolerance_pct phải trong (0, 0.1)."),
            (sig.bearish_threshold < 0 < sig.bullish_threshold,
             "Ngưỡng tín hiệu phải thỏa bearish < 0 < bullish."),
        )
        return [message for ok, message in checks if not ok]

    def _telegram_problems(self) -> list[str]:
        tg = self.telegram
        issues: list[str] = []
        token, chat_id = tg.bot_token.strip(), tg.chat_id.strip()
        if _is_placeholder(token):
            issues.append("TELEGRAM_BOT_TOKEN chưa được cấu hình (đang trống/giá trị mẫu).")
        elif not _TELEGRAM_TOKEN_PATTERN.match(token):
            issues.append("TELEGRAM_BOT_TOKEN không đúng định dạng '<số>:<chuỗi>'.")
        if _is_placeholder(chat_id):
            issues.append("TELEGRAM_CHAT_ID chưa được cấu hình (đang trống/giá trị mẫu).")
        elif not _TELEGRAM_CHAT_ID_PATTERN.match(chat_id):
            issues.append("TELEGRAM_CHAT_ID phải là số (group thường âm) hoặc @tên_kênh.")
        return issues


# ---------------------------------------------------------------------------
# Đọc biến môi trường
# ---------------------------------------------------------------------------


def _format_errors(issues: list[str]) -> str:
   
    return "Cấu hình không hợp lệ:\n" + "\n".join(f"  - {issue}" for issue in issues)


class _EnvReader:
    

    _TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
    _FALSE_VALUES: Final = frozenset({"0", "false", "no", "off"})

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._environ = environ
        self.errors: list[str] = []

    def text(self, name: str, default: str = "") -> str:
       
        value = self._environ.get(name, "").strip()
        return value or default

    def integer(
        self, name: str, default: int, *, minimum: int | None = None, maximum: int | None = None
    ) -> int:
        
        return self._number(name, default, int, "số nguyên", minimum, maximum)

    def decimal(
        self, name: str, default: float, *, minimum: float | None = None, maximum: float | None = None
    ) -> float:
        
        return self._number(name, default, float, "số", minimum, maximum)

    def boolean(self, name: str, default: bool) -> bool:
        
        raw = self.text(name).lower()
        if not raw:
            return default
        if raw in self._TRUE_VALUES:
            return True
        if raw in self._FALSE_VALUES:
            return False
        self.errors.append(f"{name}: '{raw}' không phải giá trị đúng/sai (true/false).")
        return default

    def _number[T: (int, float)](
        self,
        name: str,
        default: T,
        parse: Callable[[str], T],
        kind: str,
        minimum: T | None,
        maximum: T | None,
    ) -> T:
        raw = self.text(name)
        if not raw:
            return default
        try:
            value = parse(raw)
        except ValueError:
            self.errors.append(f"{name}: '{raw}' không phải {kind}.")
            return default
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            self.errors.append(f"{name}: {value} nằm ngoài khoảng cho phép [{minimum}, {maximum}].")
            return default
        return value


def _parse_tickers(raw: str) -> tuple[str, ...]:
    
    tokens = re.split(r"[,;\s]+", raw.upper())
    return tuple(dict.fromkeys(token for token in tokens if token))


def _missing_credentials(env: _EnvReader, source: str) -> tuple[str, ...]:
    
    names = SOURCE_CREDENTIAL_ENV_VARS.get(source, ())
    return tuple(name for name in names if _is_placeholder(env.text(name)))


def _database_mismatches(env: _EnvReader, database_url: str) -> tuple[str, ...]:
    
    try:
        parts = urlsplit(database_url)
        actual = {
            "POSTGRES_USER": unquote(parts.username or ""),
            "POSTGRES_PASSWORD": unquote(parts.password or ""),
            "POSTGRES_DB": unquote(parts.path.lstrip("/")),
        }
    except ValueError:
        return ()
    return tuple(
        name for name, value in actual.items() if env.text(name) and env.text(name) != value
    )


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    
    env = _EnvReader(os.environ if environ is None else environ)

    database_url = env.text("DATABASE_URL")
    source = env.text("DATA_SOURCE", DEFAULT_DATA_SOURCE).lower()

    data = DataSourceSettings(
        source=source,
        missing_credentials=_missing_credentials(env, source),
        backfill_years=env.integer("BACKFILL_YEARS", DEFAULT_BACKFILL_YEARS, minimum=1, maximum=10),
        reconcile_buffer_days=env.integer(
            "RECONCILE_BUFFER_DAYS", DEFAULT_RECONCILE_BUFFER_DAYS, minimum=1, maximum=30
        ),
        fetch_delay_seconds=env.decimal(
            "FETCH_DELAY_SECONDS", DEFAULT_FETCH_DELAY_SECONDS, minimum=0, maximum=60
        ),
        fetch_max_retries=env.integer(
            "FETCH_MAX_RETRIES", DEFAULT_FETCH_MAX_RETRIES, minimum=0, maximum=10
        ),
    )
    schedule = ScheduleSettings(
        day_of_week=env.text("SCHEDULE_DAY_OF_WEEK", DEFAULT_SCHEDULE_DAY_OF_WEEK).lower(),
        hour=env.integer("SCHEDULE_HOUR", DEFAULT_SCHEDULE_HOUR, minimum=0, maximum=23),
        minute=env.integer("SCHEDULE_MINUTE", DEFAULT_SCHEDULE_MINUTE, minimum=0, maximum=59),
    )
    telegram = TelegramSettings(
        bot_token=env.text("TELEGRAM_BOT_TOKEN"),
        chat_id=env.text("TELEGRAM_CHAT_ID"),
        timeout_seconds=env.decimal(
            "TELEGRAM_TIMEOUT_SECONDS", DEFAULT_TELEGRAM_TIMEOUT_SECONDS, minimum=1, maximum=60
        ),
        max_retries=env.integer(
            "TELEGRAM_MAX_RETRIES", DEFAULT_TELEGRAM_MAX_RETRIES, minimum=0, maximum=10
        ),
        send_daily_summary=env.boolean("SEND_DAILY_SUMMARY", True),
    )
    settings = Settings(
        tickers=_parse_tickers(env.text("TICKERS")) or DEFAULT_TICKERS,
        database_url=database_url,
        timezone=env.text("TIMEZONE", DEFAULT_TIMEZONE),
        log_level=env.text("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
        database_mismatches=_database_mismatches(env, database_url),
        data=data,
        schedule=schedule,
        telegram=telegram,
    )

    if env.errors:
        raise ConfigError(_format_errors(env.errors))
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    
    return load_settings()
