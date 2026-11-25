from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Final

import pandas as pd

from app.config import Settings
from app.data.collector import (
    OUTPUT_COLUMNS,
    CollectionResult,
    empty_ohlcv_frame,
)

__all__ = [
    "REJECTED_REASON_COLUMN",
    "PreprocessingError",
    "PreprocessingReport",
    "PreprocessingResult",
    "CollectionPreprocessingResult",
    "preprocess_ohlcv",
    "preprocess_collection",
]

logger = logging.getLogger(__name__)

REJECTED_REASON_COLUMN: Final[str] = "rejection_reason"
_PRICE_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")
_NUMERIC_COLUMNS: Final[tuple[str, ...]] = (*_PRICE_COLUMNS, "volume")
_DUPLICATE_KEY: Final[tuple[str, str]] = ("ticker", "trading_date")
_PRICE_EQUAL_REL_TOL: Final[float] = 1e-12
_PRICE_EQUAL_ABS_TOL: Final[float] = 1e-12
_OHLC_REL_TOL: Final[float] = 1e-12


class PreprocessingError(ValueError):
    


@dataclass(frozen=True)
class PreprocessingReport:
    

    input_rows: int
    accepted_rows: int
    rejected_rows: int
    duplicate_rows_removed: int
    rejection_counts: Mapping[str, int]


@dataclass(frozen=True)
class PreprocessingResult:
    

    valid: pd.DataFrame
    rejected: pd.DataFrame
    report: PreprocessingReport

    @property
    def ok(self) -> bool:
        
        return self.rejected.empty


@dataclass(frozen=True)
class CollectionPreprocessingResult:
    

    frames: Mapping[str, pd.DataFrame]
    rejected: pd.DataFrame
    reports: Mapping[str, PreprocessingReport]
    failures: Mapping[str, str]

    @property
    def ok(self) -> bool:
        
        return not self.failures and self.rejected.empty

    @property
    def accepted_rows(self) -> int:
        return sum(report.accepted_rows for report in self.reports.values())

    @property
    def rejected_rows(self) -> int:
        return sum(report.rejected_rows for report in self.reports.values())

    @property
    def duplicate_rows_removed(self) -> int:
        return sum(report.duplicate_rows_removed for report in self.reports.values())

    def combined(self) -> pd.DataFrame:
        
        non_empty = [frame for frame in self.frames.values() if not frame.empty]
        if not non_empty:
            return empty_ohlcv_frame()
        return (
            pd.concat(non_empty, ignore_index=True)
            .loc[:, list(OUTPUT_COLUMNS)]
            .sort_values(["ticker", "trading_date"], kind="stable")
            .reset_index(drop=True)
        )

    def combined_rejected(self) -> pd.DataFrame:
        
        if self.rejected.empty:
            return _empty_rejected_frame()
        return self.rejected.reset_index(drop=True)


def _is_missing(value: object) -> bool:
    
    if value is None:
        return True
    try:
        result = pd.isna(value)
        return bool(result) if isinstance(result, (bool,)) else False
    except (TypeError, ValueError):
        return False


def _parse_trading_date(value: object, market_timezone: str) -> date:
    
    if _is_missing(value):
        raise ValueError("missing_trading_date")

    try:
        if isinstance(value, datetime):
            timestamp = pd.Timestamp(value)
        elif isinstance(value, date):
            return value
        else:
            timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_trading_date") from exc

    if pd.isna(timestamp):
        raise ValueError("invalid_trading_date")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(market_timezone)
    return timestamp.date()


def _parse_fetched_at(value: object) -> pd.Timestamp:
    
    if _is_missing(value):
        raise ValueError("missing_fetched_at")
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid_fetched_at") from exc
    if pd.isna(timestamp):
        raise ValueError("invalid_fetched_at")
    if timestamp.tzinfo is None:
        raise ValueError("naive_fetched_at")
    return timestamp.tz_convert("UTC")


def _parse_finite_number(value: object, field_name: str) -> float:
    
    if _is_missing(value):
        raise ValueError(f"missing_{field_name}")
    if isinstance(value, bool):
        raise ValueError(f"invalid_{field_name}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid_{field_name}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non_finite_{field_name}")
    return number


def _normalize_one_row(
    row: Mapping[str, object],
    *,
    allowed_tickers: frozenset[str],
    expected_ticker: str | None,
    expected_source: str,
    market_timezone: str,
    latest_trading_date: date,
) -> tuple[dict[str, object] | None, list[str]]:
    
    reasons: list[str] = []
    normalized: dict[str, object] = {}

    raw_ticker = row.get("ticker")
    ticker: str | None = None
    if _is_missing(raw_ticker):
        reasons.append("missing_ticker")
    elif not isinstance(raw_ticker, str):
        reasons.append("invalid_ticker")
    else:
        ticker = raw_ticker.strip().upper()
        if not ticker:
            reasons.append("missing_ticker")
        elif ticker not in allowed_tickers:
            reasons.append("ticker_not_configured")
        if expected_ticker is not None and ticker != expected_ticker:
            reasons.append("ticker_mismatch")
    if ticker:
        normalized["ticker"] = ticker

    try:
        trading_date = _parse_trading_date(row.get("trading_date"), market_timezone)
        if trading_date > latest_trading_date:
            reasons.append("future_trading_date")
        normalized["trading_date"] = trading_date
    except ValueError as exc:
        reasons.append(str(exc))

    for column in _PRICE_COLUMNS:
        try:
            number = _parse_finite_number(row.get(column), column)
            if number <= 0:
                reasons.append(f"non_positive_{column}")
            normalized[column] = number
        except ValueError as exc:
            reasons.append(str(exc))

    try:
        volume_number = _parse_finite_number(row.get("volume"), "volume")
        if volume_number < 0:
            reasons.append("negative_volume")
        elif not math.isclose(volume_number, round(volume_number), rel_tol=0.0, abs_tol=1e-8):
            reasons.append("fractional_volume")
        else:
            normalized["volume"] = int(round(volume_number))
    except ValueError as exc:
        reasons.append(str(exc))

    if all(column in normalized for column in _PRICE_COLUMNS):
        open_price = float(normalized["open"])
        high_price = float(normalized["high"])
        low_price = float(normalized["low"])
        close_price = float(normalized["close"])
        scale = max(1.0, abs(open_price), abs(high_price), abs(low_price), abs(close_price))
        tolerance = scale * _OHLC_REL_TOL
        if (
            high_price + tolerance < low_price
            or high_price + tolerance < max(open_price, close_price)
            or low_price - tolerance > min(open_price, close_price)
        ):
            reasons.append("inconsistent_ohlc_range")

    raw_source = row.get("source")
    if _is_missing(raw_source):
        reasons.append("missing_source")
    elif not isinstance(raw_source, str) or raw_source.strip() != expected_source:
        reasons.append("unexpected_source")
    else:
        normalized["source"] = raw_source.strip()

    try:
        normalized["fetched_at"] = _parse_fetched_at(row.get("fetched_at"))
    except ValueError as exc:
        reasons.append(str(exc))

    if reasons:
        # Keep unique codes in the order in which checks found them.
        return None, list(dict.fromkeys(reasons))

    return {column: normalized[column] for column in OUTPUT_COLUMNS}, []


def _same_ohlcv(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
   
    prices_equal = all(
        math.isclose(
            float(left[column]),
            float(right[column]),
            rel_tol=_PRICE_EQUAL_REL_TOL,
            abs_tol=_PRICE_EQUAL_ABS_TOL,
        )
        for column in _PRICE_COLUMNS
    )
    return prices_equal and int(left["volume"]) == int(right["volume"])


def _build_valid_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    
    if not rows:
        return empty_ohlcv_frame()

    frame = pd.DataFrame(rows, columns=list(OUTPUT_COLUMNS))
    frame["ticker"] = frame["ticker"].astype("object")
    frame["trading_date"] = pd.Series(frame["trading_date"].tolist(), dtype="object")
    for column in _PRICE_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype("float64")
    frame["volume"] = pd.array(frame["volume"], dtype="Int64")
    frame["source"] = frame["source"].astype("object")
    frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], utc=True)
    return (
        frame.loc[:, list(OUTPUT_COLUMNS)]
        .sort_values(["ticker", "trading_date"], kind="stable")
        .reset_index(drop=True)
    )


def _empty_rejected_frame() -> pd.DataFrame:
    
    frame = empty_ohlcv_frame()
    frame[REJECTED_REASON_COLUMN] = pd.Series(dtype="object")
    return frame.loc[:, [*OUTPUT_COLUMNS, REJECTED_REASON_COLUMN]]


def _build_rejected_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    
    if not rows:
        return _empty_rejected_frame()
    return pd.DataFrame(rows, columns=[*OUTPUT_COLUMNS, REJECTED_REASON_COLUMN]).reset_index(drop=True)


def preprocess_ohlcv(
    raw: pd.DataFrame,
    settings: Settings,
    *,
    expected_ticker: str | None = None,
) -> PreprocessingResult:
   
    if not isinstance(raw, pd.DataFrame):
        raise PreprocessingError(
            f"Đầu vào phải là pandas.DataFrame, nhận được {type(raw).__name__}."
        )

    missing_columns = [column for column in OUTPUT_COLUMNS if column not in raw.columns]
    if missing_columns:
        raise PreprocessingError(
            f"DataFrame thiếu cột bắt buộc {missing_columns}; "
            f"các cột hiện có: {list(raw.columns)}."
        )

    if expected_ticker is not None:
        expected_ticker = expected_ticker.strip().upper()
        if expected_ticker not in settings.tickers:
            raise PreprocessingError(
                f"expected_ticker '{expected_ticker}' không nằm trong Settings.tickers."
            )

    if raw.empty:
        report = PreprocessingReport(0, 0, 0, 0, MappingProxyType({}))
        return PreprocessingResult(empty_ohlcv_frame(), _empty_rejected_frame(), report)

    allowed_tickers = frozenset(settings.tickers)
    # Ngày hiện tại theo múi giờ thị trường; trading_date sau ngày này là tương lai (mục 4.4).
    latest_trading_date = datetime.now(settings.tzinfo).date()
    valid_candidates: list[dict[str, object]] = []
    rejected_rows: list[dict[str, object]] = []
    reason_counts: Counter[str] = Counter()

    # Chỉ đọc các cột theo hợp đồng; không sửa DataFrame đầu vào.
    selected = raw.loc[:, list(OUTPUT_COLUMNS)]
    for values in selected.to_dict(orient="records"):
        normalized, reasons = _normalize_one_row(
            values,
            allowed_tickers=allowed_tickers,
            expected_ticker=expected_ticker,
            expected_source=settings.data.source_label,
            market_timezone=settings.timezone,
            latest_trading_date=latest_trading_date,
        )
        if reasons:
            reason_counts.update(reasons)
            rejected_rows.append(
                {
                    **values,
                    REJECTED_REASON_COLUMN: ";".join(reasons),
                }
            )
        else:
            # _normalize_one_row guarantees every OUTPUT_COLUMNS field exists.
            valid_candidates.append(normalized)  # type: ignore[arg-type]

    # Xử lý trùng chỉ trên những dòng đã hợp lệ.
    grouped: dict[tuple[str, date], list[dict[str, object]]] = defaultdict(list)
    for row in valid_candidates:
        key = (str(row["ticker"]), row["trading_date"])
        grouped[key].append(row)

    accepted_rows: list[dict[str, object]] = []
    duplicate_rows_removed = 0
    for key, group in grouped.items():
        if len(group) == 1:
            accepted_rows.append(group[0])
            continue

        reference = group[0]
        if all(_same_ohlcv(reference, candidate) for candidate in group[1:]):
            # Dữ liệu giống hệt: giữ lần tải mới nhất; nếu cùng timestamp thì giữ
            # lần xuất hiện cuối trong DataFrame để kết quả có tính xác định.
            newest_index = max(range(len(group)), key=lambda index: (group[index]["fetched_at"], index))
            accepted_rows.append(group[newest_index])
            duplicate_rows_removed += len(group) - 1
            continue

        # Hai bản ghi hợp lệ nhưng khác OHLCV cho cùng ticker/date là xung đột.
        # Không tự đoán bản nào đúng: loại toàn bộ nhóm để tránh ghi sai vào DB.
        reason_counts["conflicting_duplicate_ticker_date"] += len(group)
        for candidate in group:
            rejected_rows.append(
                {
                    **candidate,
                    REJECTED_REASON_COLUMN: "conflicting_duplicate_ticker_date",
                }
            )

    valid_frame = _build_valid_frame(accepted_rows)
    rejected_frame = _build_rejected_frame(rejected_rows)
    report = PreprocessingReport(
        input_rows=len(raw),
        accepted_rows=len(valid_frame),
        rejected_rows=len(rejected_frame),
        duplicate_rows_removed=duplicate_rows_removed,
        rejection_counts=MappingProxyType(dict(sorted(reason_counts.items()))),
    )

    if not rejected_frame.empty:
        logger.warning(
            "Preprocessing %s: nhận %d, hợp lệ %d, loại %d, trùng chính xác %d. Lý do: %s",
            expected_ticker or "batch",
            report.input_rows,
            report.accepted_rows,
            report.rejected_rows,
            report.duplicate_rows_removed,
            dict(report.rejection_counts),
        )
    else:
        logger.info(
            "Preprocessing %s: nhận %d, hợp lệ %d, trùng chính xác %d.",
            expected_ticker or "batch",
            report.input_rows,
            report.accepted_rows,
            report.duplicate_rows_removed,
        )

    return PreprocessingResult(valid_frame, rejected_frame, report)


def preprocess_collection(
    collection: CollectionResult,
    settings: Settings,
) -> CollectionPreprocessingResult:
    
    if not isinstance(collection, CollectionResult):
        raise PreprocessingError(
            "collection phải là data.collector.CollectionResult."
        )

    frames: dict[str, pd.DataFrame] = {}
    reports: dict[str, PreprocessingReport] = {}
    failures = dict(collection.failures)
    rejected_frames: list[pd.DataFrame] = []

    for raw_ticker, frame in collection.frames.items():
        ticker = str(raw_ticker).strip().upper()
        try:
            result = preprocess_ohlcv(frame, settings, expected_ticker=ticker)
        except PreprocessingError as exc:
            failures[ticker] = f"Lỗi hợp đồng dữ liệu preprocessing: {exc}"
            logger.exception("Không thể preprocessing dữ liệu của %s.", ticker)
            continue

        frames[ticker] = result.valid
        reports[ticker] = result.report
        if not result.rejected.empty:
            rejected_frames.append(result.rejected)

    rejected = (
        pd.concat(rejected_frames, ignore_index=True)
        if rejected_frames
        else _empty_rejected_frame()
    )

    return CollectionPreprocessingResult(
        frames=MappingProxyType(frames),
        rejected=rejected,
        reports=MappingProxyType(reports),
        failures=MappingProxyType(failures),
    )
