# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
        # 东财分红明细 stock_fhps_detail_em 的列名
        "现金分红-现金分红比例",
        "现金分红比例",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    # 东财分红明细 stock_fhps_detail_em：`现金分红-现金分红比例` 为每 10 股派息金额 → 每股 = 值/10
    ratio_col = next(
        (c for c in row.index if "现金分红-现金分红比例" in str(c) or "现金分红比例" in str(c)),
        None,
    )
    if ratio_col is not None:
        ratio = _safe_float(row.get(ratio_col))
        if ratio is not None and ratio > 0:
            return ratio / 10.0

    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


# ---------------------------------------------------------------------------
# Wide financial table parsing (akshare stock_financial_abstract returns a
# table with indicator names as rows and report periods as columns).
# ---------------------------------------------------------------------------
_REPORT_DATE_COL_RE = re.compile(r"^\d{6,8}$")


def _a_share_market(code: str) -> Optional[str]:
    """Map an A-share code to its market tag (sh / sz / bj)."""
    c = _normalize_code(code)
    if c.startswith(("60", "68", "9")):
        return "sh"
    if c.startswith(("00", "30", "20")):
        return "sz"
    if c.startswith(("8", "4", "92")):
        return "bj"
    return None


def _parse_cn_amount(value: Any) -> Optional[float]:
    """Parse Chinese amount strings like '1.39亿' / '2909.03万' / raw number
    (in yuan) into a float (yuan)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("+", "")
    if not s or s in ("-", "nan", "None", "NaN", "--", "0.00"):
        return None
    try:
        if s.endswith("亿"):
            return float(s[:-1]) * 1e8
        if s.endswith("万"):
            return float(s[:-1]) * 1e4
        return float(s)
    except (TypeError, ValueError):
        return None


def _format_cn_amount(value: Any) -> str:
    """Format a float (yuan) into a human-readable Chinese amount string."""
    v = _safe_float(value)
    if v is None:
        return "N/A"
    abs_v = abs(v)
    if abs_v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if abs_v >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:.0f}元"


def _latest_report_quarter(reference: Optional[datetime] = None) -> str:
    """Most recently ended report quarter in YYYYMMDD form (0331/0630/0930/1231)."""
    now = reference or datetime.now()
    if now.month <= 3:
        return f"{now.year}0331"
    if now.month <= 6:
        return f"{now.year}0630"
    if now.month <= 9:
        return f"{now.year}0930"
    return f"{now.year}1231"


def _previous_report_quarter(reference: Optional[datetime] = None) -> str:
    """Quarter before the latest ended report quarter, in YYYYMMDD form."""
    now = reference or datetime.now()
    latest = _latest_report_quarter(now)
    if latest.endswith("0331"):
        return f"{now.year - 1}1231"
    if latest.endswith("0630"):
        return f"{now.year}0331"
    if latest.endswith("0930"):
        return f"{now.year}0630"
    return f"{now.year}0930"


def _is_wide_financial_table(df: pd.DataFrame) -> bool:
    """Detect akshare wide financial tables where the first two columns are
    option/indicator labels and following columns are report periods."""
    if df is None or df.empty:
        return False
    cols = [str(c) for c in df.columns]
    if len(cols) < 3:
        return False
    head = f"{cols[0]}{cols[1]}"
    has_label_col = any(k in head for k in ("选项", "指标", "item"))
    report_cols = [c for c in cols[2:] if _REPORT_DATE_COL_RE.match(c)]
    return bool(has_label_col) and len(report_cols) >= 2


def _first_nonempty_in_report_cols(row: pd.Series, report_cols: List[str]) -> Tuple[Optional[float], Optional[str]]:
    """First non-null value scanning report columns left-to-right (newest first)."""
    for col in report_cols:
        v = row.get(col)
        if v is None:
            continue
        if isinstance(v, float) and pd.isna(v):
            continue
        s = str(v).strip()
        if s in ("", "-", "nan", "None", "--"):
            continue
        return _safe_float(v), str(col)
    return None, None


def _wide_indicator_value(
    df: pd.DataFrame,
    keywords: List[str],
    *,
    exact_first: bool = True,
) -> Tuple[Optional[float], Optional[str]]:
    """Look up an indicator row in a wide financial table.

    Indicator names live in the second column; report periods follow as column
    headers ordered newest → oldest. Exact match is preferred, then substring
    match. Returns (latest_value, report_period).
    """
    if df is None or df.empty:
        return None, None
    cols = list(df.columns)
    if len(cols) < 3:
        return None, None
    label_col = str(cols[1])
    report_cols = [str(c) for c in cols[2:] if _REPORT_DATE_COL_RE.match(str(c))]
    if not report_cols:
        return None, None

    def _exact(label: str) -> bool:
        return any(k and label == k for k in keywords)

    def _substr(label: str) -> bool:
        return any(k and k in label for k in keywords)

    for use_substr in (False, True) if exact_first else (True,):
        for _, row in df.iterrows():
            label = _safe_str(row.get(label_col))
            if (use_substr and _substr(label)) or (not use_substr and _exact(label)):
                value, col = _first_nonempty_in_report_cols(row, report_cols)
                if col is not None and value is not None:
                    return value, col
    return None, None


def _wide_value_at_col(df: pd.DataFrame, keywords: List[str], col: str) -> Optional[float]:
    """Fetch an indicator's value at a specific report-period column."""
    if df is None or df.empty or len(df.columns) < 3:
        return None
    label_col = str(df.columns[1])
    for _, row in df.iterrows():
        label = _safe_str(row.get(label_col))
        if any(k and label == k for k in keywords):
            v = _safe_float(row.get(col))
            if v is not None:
                return v
    return None


def _compute_wide_yoy(df: pd.DataFrame, keywords: List[str], latest_col: Optional[str]) -> Optional[float]:
    """Compute YoY growth from the wide table by comparing the latest report
    period with the same period one year earlier (4 columns back)."""
    if latest_col is None or not _REPORT_DATE_COL_RE.match(latest_col):
        return None
    value = _wide_value_at_col(df, keywords, latest_col)
    if value is None:
        return None
    cols = list(df.columns)
    try:
        idx = cols.index(latest_col)
    except ValueError:
        return None
    year_ago_idx = idx + 4
    if year_ago_idx >= len(cols) or not _REPORT_DATE_COL_RE.match(str(cols[year_ago_idx])):
        return None
    old_value = _wide_value_at_col(df, keywords, str(cols[year_ago_idx]))
    if old_value is None or old_value == 0:
        return None
    return (value / old_value - 1.0) * 100.0


def _report_date_from_col(col: Optional[str]) -> Optional[str]:
    if not col or not _REPORT_DATE_COL_RE.match(str(col)):
        return None
    s = str(col)
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) == 6:
        return f"{s[:4]}-{s[4:6]}-01"
    return None


def _extract_financial_summary_from_wide_table(df: pd.DataFrame) -> Dict[str, Any]:
    """Extract normalized financial metrics from a wide financial table."""
    revenue, rev_col = _wide_indicator_value(df, ["营业总收入"], exact_first=True)
    net_profit, _ = _wide_indicator_value(df, ["归母净利润"], exact_first=True)
    ocf, _ = _wide_indicator_value(df, ["经营现金流量净额"], exact_first=True)
    roe, _ = _wide_indicator_value(df, ["净资产收益率", "ROE"], exact_first=False)
    gross_margin, _ = _wide_indicator_value(df, ["毛利率"], exact_first=True)
    bps, _ = _wide_indicator_value(df, ["每股净资产"], exact_first=True)
    debt_ratio, _ = _wide_indicator_value(df, ["资产负债率"], exact_first=True)
    eps, _ = _wide_indicator_value(df, ["基本每股收益"], exact_first=True)

    revenue_yoy, _ = _wide_indicator_value(df, ["营业总收入增长率"], exact_first=True)
    if revenue_yoy is None:
        revenue_yoy = _compute_wide_yoy(df, ["营业总收入"], rev_col)
    profit_yoy, _ = _wide_indicator_value(df, ["归属母公司净利润增长率"], exact_first=True)
    if profit_yoy is None:
        profit_yoy = _compute_wide_yoy(df, ["归母净利润"], rev_col)

    return {
        "report_date": _report_date_from_col(rev_col),
        "revenue": revenue,
        "net_profit_parent": net_profit,
        "operating_cash_flow": ocf,
        "roe": roe,
        "gross_margin": gross_margin,
        "revenue_yoy": revenue_yoy,
        "net_profit_yoy": profit_yoy,
        "bps": bps,
        "debt_ratio": debt_ratio,
        "eps": eps,
    }


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _run_with_call_timeout(self, fn: Any, kwargs: Dict[str, Any], timeout: float) -> Any:
        """Run an akshare call in a daemon thread with a hard timeout so slow /
        blocked endpoints (e.g. eastmoney when unreachable) fail fast instead of
        eating the whole fundamental stage budget."""
        import threading

        result: Dict[str, Any] = {}

        def _target() -> None:
            try:
                result["df"] = fn(**kwargs)
            except Exception as exc:  # noqa: BLE001 - surfaced below
                result["error"] = exc

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(f"akshare call timed out after {timeout:.1f}s")
        if "error" in result:
            raise result["error"]
        return result.get("df")

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
        *,
        per_call_timeout: Optional[float] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                if per_call_timeout is not None:
                    df = self._run_with_call_timeout(fn, kwargs, per_call_timeout)
                else:
                    df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # Financial indicators
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_abstract_ths", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            # akshare stock_financial_abstract returns a wide table (indicator
            # rows × report-period columns) which the keyword-based picker cannot
            # read; use the dedicated wide-table parser when detected.
            wide_summary = (
                _extract_financial_summary_from_wide_table(fin_df)
                if _is_wide_financial_table(fin_df)
                else None
            )
            if wide_summary is not None and any(v is not None for v in wide_summary.values()):
                result["growth"] = {
                    "revenue_yoy": wide_summary.get("revenue_yoy"),
                    "net_profit_yoy": wide_summary.get("net_profit_yoy"),
                    "roe": wide_summary.get("roe"),
                    "gross_margin": wide_summary.get("gross_margin"),
                }
                financial_report_payload = {
                    "report_date": wide_summary.get("report_date"),
                    "revenue": wide_summary.get("revenue"),
                    "net_profit_parent": wide_summary.get("net_profit_parent"),
                    "operating_cash_flow": wide_summary.get("operating_cash_flow"),
                    "roe": wide_summary.get("roe"),
                }
                # Additive extra metrics exposed by the wide table.
                for key in ("gross_margin", "eps", "bps", "debt_ratio"):
                    if wide_summary.get(key) is not None:
                        financial_report_payload[key] = wide_summary.get(key)
                if any(v is not None for v in financial_report_payload.values()):
                    result["earnings"]["financial_report"] = financial_report_payload
                result["source_chain"].append(f"growth:{fin_source}")
            else:
                row = _extract_latest_row(fin_df, stock_code)
                if row is not None:
                    revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                    profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                    roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                    gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                    report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                    revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                    net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                    operating_cash_flow = _safe_float(
                        _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                    )
                    result["growth"] = {
                        "revenue_yoy": revenue_yoy,
                        "net_profit_yoy": profit_yoy,
                        "roe": roe,
                        "gross_margin": gross_margin,
                    }
                    financial_report_payload = {
                        "report_date": report_date,
                        "revenue": revenue,
                        "net_profit_parent": net_profit_parent,
                        "operating_cash_flow": operating_cash_flow,
                        "roe": roe,
                    }
                    if any(v is not None for v in financial_report_payload.values()):
                        result["earnings"]["financial_report"] = financial_report_payload
                    result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast (akshare stock_yjyg_em / stock_yjbb_em expect a
        # `date` argument, not `symbol`). These return the whole market's
        # announcements for a date, so they are slow and get a short timeout;
        # the authoritative single-stock outlook comes from CNINFO announcements
        # in the report intel path, so this block is best-effort only.
        forecast_df, forecast_source, forecast_errors = self._call_df_candidates(
            [
                ("stock_yjyg_em", {"date": _latest_report_quarter()}),
                ("stock_yjyg_em", {"date": _previous_report_quarter()}),
            ],
            per_call_timeout=1.5,
        )
        result["errors"].extend(forecast_errors)
        if forecast_df is not None:
            row = _extract_latest_row(forecast_df, stock_code)
            if row is not None:
                result["earnings"]["forecast_summary"] = _safe_str(
                    _pick_by_keywords(row, ["预告", "业绩变动", "内容", "摘要", "公告"])
                )[:200]
                result["source_chain"].append(f"earnings_forecast:{forecast_source}")

        # Earnings quick report (best-effort, same budget guard as forecast)
        quick_df, quick_source, quick_errors = self._call_df_candidates(
            [
                ("stock_yjkb_em", {"date": _latest_report_quarter()}),
                ("stock_yjkb_em", {"date": _previous_report_quarter()}),
            ],
            per_call_timeout=1.5,
        )
        result["errors"].extend(quick_errors)
        if quick_df is not None:
            row = _extract_latest_row(quick_df, stock_code)
            if row is not None:
                result["earnings"]["quick_report_summary"] = _safe_str(
                    _pick_by_keywords(row, ["快报", "摘要", "公告", "说明"])
                )[:200]
                result["source_chain"].append(f"earnings_quick:{quick_source}")

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders
        inst_df, inst_source, inst_errors = self._call_df_candidates([
            ("stock_institute_hold", {}),
            ("stock_institute_recommend", {}),
        ])
        result["errors"].extend(inst_errors)
        if inst_df is not None:
            row = _extract_latest_row(inst_df, stock_code)
            if row is not None:
                inst_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "变动", "持股变化"]))
                result["institution"]["institution_holding_change"] = inst_change
                result["source_chain"].append(f"institution:{inst_source}")

        top10_df, top10_source, top10_errors = self._call_df_candidates([
            ("stock_gdfx_top_10_em", {"symbol": stock_code}),
            ("stock_gdfx_top_10_em", {}),
            ("stock_zh_a_gdhs_detail_em", {"symbol": stock_code}),
            ("stock_zh_a_gdhs_detail_em", {}),
        ])
        result["errors"].extend(top10_errors)
        if top10_df is not None:
            row = _extract_latest_row(top10_df, stock_code)
            if row is not None:
                holder_change = _safe_float(_pick_by_keywords(row, ["增减", "变化", "持股变化", "变动"]))
                result["institution"]["top10_holder_change"] = holder_change
                result["source_chain"].append(f"top10:{top10_source}")

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        stock_flow, stock_source, stock_errors = self._fetch_stock_fund_flow(stock_code)
        result["errors"].extend(stock_errors)
        if stock_flow:
            # 融资融券明细（仅当资金流源提供了交易日时附加；取不到则 fail-open）。
            market = _a_share_market(stock_code)
            trade_date = stock_flow.get("date")
            if market and trade_date:
                margin_payload = self._fetch_margin_flow(stock_code, market, trade_date)
                if margin_payload:
                    stock_flow["margin"] = margin_payload
            result["stock_flow"] = stock_flow
            result["source_chain"].append(f"capital_stock:{stock_source}")

        sector_df, sector_source, sector_errors = self._call_df_candidates(
            [
                ("stock_sector_fund_flow_rank", {}),
                ("stock_sector_fund_flow_summary", {}),
            ],
            per_call_timeout=1.5,
        )
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def _fetch_stock_fund_flow(self, stock_code: str) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
        """Single-stock capital flow with 东财 → 新浪 → 同花顺/腾讯 fallback.

        East Money gives the richest per-order breakdown; Sina is a fast
        single-stock fallback (reachable even when push2his.eastmoney.com is
        blocked); the 同花顺/腾讯 whole-market snapshots are slow and only
        surface when both faster sources fail.
        """
        market = _a_share_market(stock_code)
        errors: List[str] = []

        # 1) East Money: per-stock fund flow with full order breakdown.
        if market is not None:
            df, source, errs = self._call_df_candidates(
                [("stock_individual_fund_flow", {"stock": stock_code, "market": market})],
                per_call_timeout=3.0,
            )
            errors.extend(errs)
            if df is not None and not df.empty:
                flow = self._parse_em_stock_fund_flow(df)
                if flow:
                    return flow, source, errors

        # 2) Sina: single-stock money flow via JSON API (fast, reachable when
        #    EM push2his is blocked).
        sina_flow = self._fetch_sina_stock_fund_flow(stock_code)
        if sina_flow:
            return sina_flow, "sina_money_flow", errors

        # 3) 同花顺: whole-market snapshot filtered by code (net amounts are
        #    Chinese strings like '1.39亿').
        try:
            import akshare as ak
            ths_df = ak.stock_fund_flow_individual(symbol="即时")
            if ths_df is not None and not ths_df.empty:
                flow = self._parse_ths_stock_fund_flow(ths_df, stock_code)
                if flow:
                    return flow, "stock_fund_flow_individual", errors
        except Exception as exc:
            errors.append(f"stock_fund_flow_individual:{type(exc).__name__}")

        # 4) Tencent: whole-market spot snapshot; zljlr field is in 万元.
        try:
            import akshare as ak
            tx_df = ak.stock_zh_a_spot_tx()
            if tx_df is not None and not tx_df.empty:
                flow = self._parse_tencent_stock_fund_flow(tx_df, stock_code)
                if flow:
                    return flow, "stock_zh_a_spot_tx", errors
        except Exception as exc:
            errors.append(f"stock_zh_a_spot_tx:{type(exc).__name__}")

        return {}, None, errors

    def _fetch_sina_stock_fund_flow(self, stock_code: str) -> Dict[str, Any]:
        """Fetch Sina single-stock money flow via JSON API."""
        market = _a_share_market(stock_code)
        if market is None:
            return {}
        daima = f"{market}{_normalize_code(stock_code)}"
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"MoneyFlow.ssl_qsfx_zjlrqs?daima={daima}&count=10"
        )
        try:
            import requests
            resp = requests.get(
                url,
                timeout=8,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.sina.com.cn/",
                },
            )
            if resp.status_code != 200:
                return {}
            payload = resp.json()
        except Exception as exc:
            logger.debug("[capital_flow] 新浪资金流失败 %s: %s", stock_code, exc)
            return {}
        return self._parse_sina_stock_fund_flow(payload)

    def _parse_sina_stock_fund_flow(self, payload: Any) -> Dict[str, Any]:
        """Parse Sina money-flow JSON (newest first)."""
        if not isinstance(payload, list) or not payload:
            return {}
        records = [r for r in payload if isinstance(r, dict) and r.get("netamount") is not None]
        if not records:
            return {}
        rec = records[0]
        net_inflow = _safe_float(rec.get("netamount"))
        if net_inflow is None:
            return {}
        main_pct = _safe_float(rec.get("ratioamount"))
        super_large = _safe_float(rec.get("r0_net"))
        net_values = [_safe_float(r.get("netamount")) for r in records[:10]]
        net_values = [v for v in net_values if v is not None]
        flow = {
            "main_net_inflow": net_inflow,
            "main_net_inflow_pct": main_pct,
            "super_large_net_inflow": super_large,
            "inflow_5d": sum(net_values[:5]) if len(net_values) >= 5 else None,
            "inflow_10d": sum(net_values[:10]) if len(net_values) >= 10 else None,
            "date": _safe_str(rec.get("opendate")),
        }
        flow["main_net_inflow_display"] = _format_cn_amount(net_inflow)
        return flow

    def _parse_em_stock_fund_flow(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Parse East Money per-stock fund flow (newest day + 5/10-day sums)."""
        if df is None or df.empty:
            return {}
        row = df.iloc[-1]
        net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入-净额", "主力净流入", "净流入", "净额"]))
        main_pct = _safe_float(_pick_by_keywords(row, ["主力净流入-净占比", "净占比"]))
        super_large = _safe_float(_pick_by_keywords(row, ["超大单净流入-净额", "超大单净流入"]))
        large = _safe_float(_pick_by_keywords(row, ["大单净流入-净额", "大单净流入"]))
        medium = _safe_float(_pick_by_keywords(row, ["中单净流入-净额", "中单净流入"]))
        small = _safe_float(_pick_by_keywords(row, ["小单净流入-净额", "小单净流入"]))
        date = _safe_str(_pick_by_keywords(row, ["日期"]))

        inflow_5d = None
        inflow_10d = None
        main_col = next((c for c in df.columns if "主力净流入-净额" in str(c)), None)
        if main_col is not None:
            try:
                vals = pd.to_numeric(df[main_col], errors="coerce").dropna()
                inflow_5d = float(vals.tail(5).sum())
                inflow_10d = float(vals.tail(10).sum())
            except Exception:
                inflow_5d = inflow_10d = None

        flow = {
            "main_net_inflow": net_inflow,
            "main_net_inflow_pct": main_pct,
            "super_large_net_inflow": super_large,
            "large_net_inflow": large,
            "medium_net_inflow": medium,
            "small_net_inflow": small,
            "inflow_5d": inflow_5d,
            "inflow_10d": inflow_10d,
            "date": date,
        }
        flow["main_net_inflow_display"] = _format_cn_amount(net_inflow)
        flow["large_net_inflow_display"] = _format_cn_amount(large)
        flow["small_net_inflow_display"] = _format_cn_amount(small)
        return flow

    def _parse_ths_stock_fund_flow(self, df: pd.DataFrame, stock_code: str) -> Dict[str, Any]:
        """Parse 同花顺 whole-market fund-flow snapshot for one stock."""
        code_col = next((c for c in df.columns if any(k in str(c) for k in ("股票代码", "代码", "code"))), None)
        if code_col is None:
            return {}
        target = _normalize_code(stock_code)
        matched = df[df[code_col].astype(str).map(_normalize_code) == target]
        if matched.empty:
            return {}
        row = matched.iloc[-1]
        net_inflow = _parse_cn_amount(_pick_by_keywords(row, ["净额", "主力净流入"]))
        if net_inflow is None:
            return {}
        return {
            "main_net_inflow": net_inflow,
            "main_net_inflow_display": _format_cn_amount(net_inflow),
            "inflow_5d": None,
            "inflow_10d": None,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }

    def _parse_tencent_stock_fund_flow(self, df: pd.DataFrame, stock_code: str) -> Dict[str, Any]:
        """Parse Tencent spot snapshot (zljlr in 万元) for one stock."""
        code_col = next((c for c in df.columns if any(k in str(c) for k in ("代码", "code"))), None)
        if code_col is None:
            return {}
        target = _normalize_code(stock_code)
        matched = df[df[code_col].astype(str).str.contains(target, na=False)]
        if matched.empty:
            return {}
        row = matched.iloc[-1]
        zljlr = _safe_float(row.get("zljlr"))
        if zljlr is None:
            return {}
        net_inflow = zljlr * 1e4
        zllr_d5 = _safe_float(row.get("zllr_d5")) or 0.0
        zllc_d5 = _safe_float(row.get("zllc_d5")) or 0.0
        inflow_5d = (zllr_d5 - zllc_d5) * 1e4
        flow = {
            "main_net_inflow": net_inflow,
            "main_net_inflow_display": _format_cn_amount(net_inflow),
            "inflow_5d": inflow_5d if abs(inflow_5d) > 0 else None,
            "inflow_10d": None,
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
        return flow

    def _fetch_margin_flow(
        self,
        stock_code: str,
        market: Optional[str],
        trade_date: Optional[str],
    ) -> Dict[str, Any]:
        """Fetch margin trading (融资融券) detail for an A-share stock.

        The margin endpoint lags the quote date by one trading day (the current
        day's balances are published after close), so we scan back up to 5
        recent trading days.
        """
        if market is None or not trade_date:
            return {}
        func_name = {
            "sh": "stock_margin_detail_sse",
            "sz": "stock_margin_detail_szse",
            "bj": "stock_margin_detail_bse",
        }.get(market)
        if func_name is None:
            return {}
        import akshare as ak
        fn = getattr(ak, func_name, None)
        if fn is None:
            return {}
        target = _normalize_code(stock_code)

        candidates = self._margin_date_candidates(trade_date)
        for date_candidate in candidates:
            try:
                df = fn(date=date_candidate)
            except Exception as exc:
                logger.debug("[margin] %s(%s) 失败: %s", func_name, date_candidate, exc)
                continue
            if df is None or df.empty:
                continue
            code_col = next(
                (c for c in df.columns if any(k in str(c) for k in ("代码", "证券代码", "code"))),
                None,
            )
            if code_col is None:
                continue
            matched = df[df[code_col].astype(str).map(_normalize_code) == target]
            if matched.empty:
                continue
            row = matched.iloc[0]
            margin_balance = _safe_float(_pick_by_keywords(row, ["融资余额", "本日融资余额"]))
            margin_buy = _safe_float(_pick_by_keywords(row, ["融资买入额", "本日融资买入额"]))
            short_balance = _safe_float(_pick_by_keywords(row, ["融券余量", "本日融券余量"]))
            payload: Dict[str, Any] = {
                "date": date_candidate,
                "margin_balance": margin_balance,
                "margin_buy_amount": margin_buy,
                "short_balance": short_balance,
            }
            payload["margin_balance_display"] = (
                _format_cn_amount(margin_balance) if margin_balance is not None else "N/A"
            )
            return payload
        return {}

    @staticmethod
    def _margin_date_candidates(trade_date: Optional[str]) -> List[str]:
        """Return up to 5 recent weekday candidates ending at trade_date."""
        if not trade_date:
            return []
        try:
            base = pd.to_datetime(str(trade_date).replace("-", ""))
        except Exception:
            return [str(trade_date).replace("-", "")]
        out: List[str] = []
        d = base
        while len(out) < 5:
            if d.weekday() < 5:
                out.append(d.strftime("%Y%m%d"))
            d = d - timedelta(days=1)
        return out

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
