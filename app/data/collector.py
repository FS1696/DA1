from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Final, Protocol

import pandas as pd

from app.config import ConfigError, DataSourceSettings, Settings, get_settings

__all__ = [
    "OUTPUT_COLUMNS",
    "CollectorError",
    "SourceError",
    "SchemaError",
    "OhlcvReader",
    "VnstockOhlcvReader",
    "CollectionResult",
    "StockCollector",
    "backfill_start",
    "resolve_fetch_range",
    "split_date_range",
    "normalize_ohlcv",
    "empty_ohlcv_frame",
]

logger = logging.getLogger(__name__)

# Schema chuẩn nội bộ (đặc tả mục 4.2).
OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    "ticker", "trading_date", "open", "high", "low", "close", "volume", "source", "fetched_at",
)
# Các cột mà nguồn phải trả về cho nến ngày.
RAW_COLUMNS: Final[tuple[str, ...]] = ("time", "open", "high", "low", "close", "volume")
PRICE_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")

DAILY_INTERVAL: Final[str] = "1D"
# floating mặc định của vnstock là 2 chữ số, sẽ làm tròn giá đã điều chỉnh về
# bội của 10 đồng. None giữ nguyên số gốc; việc làm tròn theo NUMERIC(18,4)
# do bước chuẩn hóa/lưu trữ đảm nhiệm.
PRICE_FLOATING: Final[int | None] = None
# Mã lỗi ổn định của vnstock 6 (so khớp theo mã, không theo câu chữ thông báo).
VNSTOCK_RATE_LIMIT_CODE: Final[str] = "VNSTOCK_SOURCE_RATE_LIMITED"
VNSTOCK_SYMBOL_ERROR_PREFIX: Final[str] = "VNSTOCK_INPUT_SYMBOL"


# ---------------------------------------------------------------------------
# Ngoại lệ
# ---------------------------------------------------------------------------


class CollectorError(RuntimeError):
  
    ticker_specific: bool = False


class SourceError(CollectorError):

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool = False,
        rate_limited: bool = False,
        ticker_specific: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.rate_limited = rate_limited
        self.ticker_specific = ticker_specific


class SchemaError(CollectorError):


# ---------------------------------------------------------------------------
# Adapter nguồn dữ liệu
# ---------------------------------------------------------------------------


class OhlcvReader(Protocol):
    

    def fetch_ohlcv(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        


class VnstockOhlcvReader:
    

    def __init__(self, source: str, compat: str) -> None:
        try:
            from vnstock import Market, errors
        except ImportError as exc:
            raise ImportError(
                "Không import được vnstock. Kiểm tra requirements.txt "
                "(vnstock==6.0.1 và --extra-index-url của Vnstock)."
            ) from exc
        self._market_cls = Market
        self._errors = errors
        self._source = source
        self._compat = compat

    def fetch_ohlcv(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        
        try:
            # Tạo đối tượng mới mỗi lần gọi để không giữ phiên cũ trong job chạy dài ngày.
            frame = (
                self._market_cls(self._source)
                .equity(ticker)
                .ohlcv(
                    start=start.isoformat(),
                    end=end.isoformat(),
                    interval=DAILY_INTERVAL,
                    compat=self._compat,
                    floating=PRICE_FLOATING,
                )
            )
        except self._errors.VnstockError as exc:
            raise self._translate(exc) from exc
        if not isinstance(frame, pd.DataFrame):
            raise SchemaError(
                f"{ticker}: vnstock trả về {type(frame).__name__}, không phải DataFrame."
            )
        return _drop_unfinished_bar(frame, ticker)

    @staticmethod
    def _translate(exc: Exception) -> SourceError:
        
        code = getattr(exc, "code", None)
        rate_limited = code == VNSTOCK_RATE_LIMIT_CODE
        message = f"vnstock báo lỗi {code or type(exc).__name__}: {exc}"
        action = getattr(exc, "action", None)
        if action:
            message += f" (gợi ý: {action})"
        return SourceError(
            message,
            code=code,
            retryable=bool(getattr(exc, "retryable", False)) or rate_limited,
            rate_limited=rate_limited,
            # Mã không tồn tại/sai dạng chỉ ảnh hưởng một mã; khoá sai, nguồn tắt...
            # ảnh hưởng tất cả các mã.
            ticker_specific=isinstance(code, str) and code.startswith(VNSTOCK_SYMBOL_ERROR_PREFIX),
        )


def _drop_unfinished_bar(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    
    if not frame.attrs.get("last_bar_partial") or frame.empty or "time" not in frame.columns:
        return frame
    latest = frame["time"].max()
    logger.info("%s: bỏ nến %s vì chưa đóng (last_bar_partial).", ticker, latest)
    return frame.loc[frame["time"] != latest]


# ---------------------------------------------------------------------------
# Hàm thuần: khoảng ngày và chuẩn hóa schema
# ---------------------------------------------------------------------------


def backfill_start(today: date, years: int) -> date:
    
    return (pd.Timestamp(today) - pd.DateOffset(years=years)).date()


def resolve_fetch_range(
    last_stored_date: date | None, today: date, settings: DataSourceSettings
) -> tuple[date, date]:
    
    first_day = backfill_start(today, settings.backfill_years)
    if last_stored_date is None:
        return first_day, today
    start = last_stored_date - timedelta(days=settings.reconcile_buffer_days)
    return min(max(start, first_day), today), today


def split_date_range(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    
    if max_days < 1:
        raise ValueError("max_days phải >= 1.")
    if start > end:
        raise ValueError(f"start ({start}) đứng sau end ({end}).")
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=max_days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def empty_ohlcv_frame() -> pd.DataFrame:
    
    return pd.DataFrame(
        {
            "ticker": pd.Series(dtype="object"),
            "trading_date": pd.Series(dtype="object"),
            "open": pd.Series(dtype="float64"),
            "high": pd.Series(dtype="float64"),
            "low": pd.Series(dtype="float64"),
            "close": pd.Series(dtype="float64"),
            "volume": pd.Series(dtype="Int64"),
            "source": pd.Series(dtype="object"),
            "fetched_at": pd.Series(dtype="datetime64[ns, UTC]"),
        }
    )


def normalize_ohlcv(
    raw: pd.DataFrame,
    *,
    ticker: str,
    source_label: str,
    fetched_at: pd.Timestamp,
    market_timezone: str,
) -> pd.DataFrame:
    
    missing = [column for column in RAW_COLUMNS if column not in raw.columns]
    if missing:
        raise SchemaError(
            f"{ticker}: nguồn thiếu cột {missing}; cột nhận được: {list(raw.columns)}"
        )
    if raw.empty:
        return empty_ohlcv_frame()

    raw = raw.reset_index(drop=True)
    try:
        trading_date = _to_trading_dates(raw["time"], market_timezone)
        prices = raw[list(PRICE_COLUMNS)].astype("float64")
        volume = pd.to_numeric(raw["volume"]).astype("Int64")
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{ticker}: dữ liệu OHLCV không đúng kiểu ({exc}).") from exc

    frame = pd.DataFrame(
        {"trading_date": trading_date, **{c: prices[c] for c in PRICE_COLUMNS}, "volume": volume}
    )
    frame.insert(0, "ticker", ticker)
    frame["source"] = source_label
    frame["fetched_at"] = fetched_at
    frame = frame[list(OUTPUT_COLUMNS)]
    return frame.sort_values("trading_date", kind="stable").reset_index(drop=True)


def _to_trading_dates(times: pd.Series, market_timezone: str) -> pd.Series:
    
    parsed = pd.to_datetime(times)
    if parsed.dt.tz is not None:
        parsed = parsed.dt.tz_convert(market_timezone)
    return parsed.dt.date


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionResult:
    
    frames: Mapping[str, pd.DataFrame]
    failures: Mapping[str, str]

    @property
    def ok(self) -> bool:
        
        return not self.failures

    @property
    def row_count(self) -> int:
        
        return sum(len(frame) for frame in self.frames.values())

    def combined(self) -> pd.DataFrame:
        
        non_empty = [frame for frame in self.frames.values() if not frame.empty]
        return pd.concat(non_empty, ignore_index=True) if non_empty else empty_ohlcv_frame()


class StockCollector:
   
    def __init__(
        self,
        settings: Settings,
        reader: OhlcvReader | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._reader = (
            reader
            if reader is not None
            else VnstockOhlcvReader(settings.data.source, settings.data.compat)
        )
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._requests_made = 0

    def market_today(self) -> date:
        
        return self._now().astimezone(self._settings.tzinfo).date()

    def fetch_daily_ohlcv(self, ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
        
        symbol = ticker.strip().upper()
        if not symbol:
            raise ValueError("ticker rỗng.")
        data = self._settings.data
        windows = split_date_range(start_date, end_date, data.max_days_per_request)
        fetched_at = pd.Timestamp(self._now())

        frames: list[pd.DataFrame] = []
        for window_start, window_end in windows:
            raw = self._fetch_window(symbol, window_start, window_end)
            frame = normalize_ohlcv(
                raw,
                ticker=symbol,
                source_label=data.source_label,
                fetched_at=fetched_at,
                market_timezone=self._settings.timezone,
            )
            # Giữ đúng cửa sổ đã xin: nguồn có thể trả dư và làm trùng giữa hai cửa sổ.
            frame = frame[frame["trading_date"].between(window_start, window_end)]
            if not frame.empty:
                frames.append(frame)

        if not frames:
            logger.info("%s: không có dữ liệu trong %s..%s.", symbol, start_date, end_date)
            return empty_ohlcv_frame()
        result = pd.concat(frames, ignore_index=True)
        logger.info(
            "%s: lấy %d dòng (%s..%s) từ %s qua %d lần gọi.",
            symbol, len(result), result["trading_date"].min(), result["trading_date"].max(),
            data.source_label, len(windows),
        )
        return result

    def collect(self, ranges: Mapping[str, tuple[date, date]]) -> CollectionResult:
        
        frames: dict[str, pd.DataFrame] = {}
        failures: dict[str, str] = {}
        pending = list(ranges.items())
        for index, (ticker, (start, end)) in enumerate(pending):
            try:
                frames[ticker] = self.fetch_daily_ohlcv(ticker, start, end)
            except CollectorError as exc:
                failures[ticker] = str(exc)
                if exc.ticker_specific:
                    continue
                logger.error(
                    "%s: lỗi ảnh hưởng mọi mã, dừng thu thập các mã còn lại: %s", ticker, exc
                )
                for skipped, _ in pending[index + 1:]:
                    failures[skipped] = f"Bỏ qua vì lỗi nguồn dữ liệu khi lấy {ticker}: {exc}"
                break
        return CollectionResult(frames=frames, failures=failures)

    # -- gọi nguồn có retry -------------------------------------------------

    def _fetch_window(self, ticker: str, start: date, end: date) -> pd.DataFrame:
       
        data = self._settings.data
        attempt = 0
        while True:
            attempt += 1
            self._pause_between_requests()
            try:
                return self._reader.fetch_ohlcv(ticker, start, end)
            except SourceError as exc:
                if exc.retryable and attempt <= data.fetch_max_retries:
                    wait = self._retry_wait(exc, attempt)
                    logger.warning(
                        "%s [%s..%s]: lỗi tạm thời %s (lần %d/%d), thử lại sau %.0f giây.",
                        ticker, start, end, exc.code or "?",
                        attempt, data.fetch_max_retries + 1, wait,
                    )
                    self._sleep(wait)
                    continue
                logger.error(
                    "%s [%s..%s]: thu thập thất bại sau %d lần gọi (code=%s): %s",
                    ticker, start, end, attempt, exc.code, exc,
                )
                raise

    def _pause_between_requests(self) -> None:
        
        delay = self._settings.data.fetch_delay_seconds
        if self._requests_made > 0 and delay > 0:
            self._sleep(delay)
        self._requests_made += 1

    def _retry_wait(self, error: SourceError, attempt: int) -> float:
        
        data = self._settings.data
        if error.rate_limited:
            return data.rate_limit_wait_seconds
        return data.retry_backoff_seconds * 2 ** (attempt - 1)


# ---------------------------------------------------------------------------
# Chạy thử với nguồn thật
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    
    parser = argparse.ArgumentParser(description="Smoke test của collector với nguồn dữ liệu thật.")
    parser.add_argument(
        "ticker", nargs="?", help="Mã cần lấy (mặc định: mã đầu tiên trong TICKERS)."
    )
    parser.add_argument("--days", type=int, default=10, help="Số ngày lịch lấy lùi (mặc định 10).")
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
        settings.validate(require_telegram=False)
    except ConfigError as exc:
        print(exc)
        return 2
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    collector = StockCollector(settings)
    ticker = args.ticker or settings.tickers[0]
    end = collector.market_today()
    try:
        frame = collector.fetch_daily_ohlcv(ticker, end - timedelta(days=args.days), end)
    except CollectorError as exc:
        logger.error("Smoke test thất bại: %s", exc)
        return 1
    print(frame.to_string(index=False))
    print("\n" + frame.dtypes.to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
