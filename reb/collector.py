"""REB 및 ECOS 데이터 수집 모듈."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .config import APISettings, EcosAPISettings
from .ecos_api import EcosApiClient, EcosApiError
from .ecos_catalog import EcosSeriesConfig, DEFAULT_ECOS_SERIES
from .kbreb_api import KBRebApiError, collect_kbreb_data
from .reb_api import REBApiClient, REBApiError
from .stat_catalog import (
    DEFAULT_FEATURE_SERIES,
    DEFAULT_TARGET_SERIES,
    SEOUL_GU_TO_KWONYEOK,
    SEOUL_KWONYEOK_NAMES,
    SeriesConfig,
)

logger = logging.getLogger(__name__)

# 메타테이블에는 핵심 정책 지표만 반영한다.
DEFAULT_META_POLICY_INDICATORS = [
    "LTV",
    "DTI",
    "DSR",
    "양도세",
    "종부세",
    "취득세",
    "투기과열지구",
    "조정대상지역",
    "투기지역",
    "부동산대책",
]

def collect_reb_data(
    api_settings: APISettings,
    output_csv_path: str,
    target_series: SeriesConfig = DEFAULT_TARGET_SERIES,
    feature_series: list[SeriesConfig] | None = None,
    start_wrttime: str | None = None,
    end_wrttime: str | None = None,
    max_pages: int = 1000,
    include_kbreb: bool = True,
) -> pd.DataFrame:
    """REB API에서 원시 시계열 데이터를 수집하고 CSV로 저장한다."""
    logger.info(
        "[collect] start target=%s features=%s range=%s~%s",
        target_series.name,
        len(feature_series or DEFAULT_FEATURE_SERIES),
        start_wrttime,
        end_wrttime,
    )
    client = REBApiClient(api_settings)
    features = feature_series or DEFAULT_FEATURE_SERIES
    # target_series 가 features 에 이미 포함될 수 있으므로 series_name 기준으로 중복 제거한다.
    all_series = []
    _seen_names: set[str] = set()
    for series in [target_series, *features]:
        if series.name in _seen_names:
            continue
        _seen_names.add(series.name)
        all_series.append(series)
    frames: list[pd.DataFrame] = []

    for series in all_series:
        logger.info("[collect] fetching series=%s (%s)", series.name, series.statbl_id)
        rows = _fetch_all_pages(
            client=client,
            series=series,
            start_wrttime=start_wrttime,
            end_wrttime=end_wrttime,
            max_pages=max_pages,
        )
        if not rows:
            logger.warning("[collect] no rows for series=%s", series.name)
            continue

        frame = pd.DataFrame(rows)
        if frame.empty:
            continue

        frame["series_name"] = series.name
        frame["series_code"] = series.statbl_id
        frames.append(frame)
        logger.info("[collect] fetched rows=%s series=%s", len(frame), series.name)

    if not frames:
        raise ValueError("No data collected from REB API. Check API key and parameters.")

    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.lower() for c in df.columns]
    df = df.rename(columns={"wrttime_idtfr_id": "wrttime", "dta_val": "value"})

    df["date"] = df["wrttime"].astype(str).map(_parse_wrttime)
    df = df.dropna(subset=["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    _validate_reb_collection_range(df, start_wrttime=start_wrttime, end_wrttime=end_wrttime)

    preferred_cols = ["date", "wrttime", "grp_id", "grp_nm", "cls_id", "cls_nm", "series_name", "series_code", "value"]
    kept_cols = [col for col in preferred_cols if col in df.columns]
    df = df[kept_cols].copy()

    # 서울 5개 권역(도심권/동북권/서북권/서남권/동남권)만 수집한다.
    # '서울 전체' 등 시·도 단위 행이 모든 구/동에 broadcast 되던 문제를 제거하기 위함.
    df = _filter_seoul_kwonyeok(df)
    if df.empty:
        raise ValueError(
            "REB 수집 결과에 서울 5개 권역(도심권/동북권/서북권/서남권/동남권) 데이터가 없습니다. "
            "statbl_id/지역명 구성을 확인하세요."
        )

    # apt_sale_index / apt_jeonse_index 두 컬럼(아파트 매매/전세 가격지수)은 REB(권역 단위) 대신
    # KB부동산(구 단위) 데이터로 대체한다. KB long-format 행은 cls_nm=구명 이라
    # build_dong_level_meta_table 에서 gu 수준으로 분류되어 구별로 매핑된다.
    if include_kbreb:
        try:
            kb_df = collect_kbreb_data(
                start_wrttime=start_wrttime,
                end_wrttime=end_wrttime,
                timeout_seconds=api_settings.timeout_seconds,
            )
        except KBRebApiError as exc:
            logger.error("[collect] KB부동산 수집 실패: %s", exc)
            raise
        kb_df = kb_df[[c for c in df.columns if c in kb_df.columns]].copy()
        df = pd.concat([df, kb_df], ignore_index=True)
        logger.info("[collect] KB부동산 구 단위 rows=%s 병합 완료", len(kb_df))

    df = df.sort_values([c for c in ["series_name", "grp_id", "cls_id", "date"] if c in df.columns])

    output_path = Path(output_csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("[collect] saved rows=%s path=%s", len(df), output_path)
    return df


# ---------------------------------------------------------------------------
# ECOS 수집
# ---------------------------------------------------------------------------

def collect_ecos_data(
    api_settings: EcosAPISettings,
    output_csv_path: str,
    series_list: list[EcosSeriesConfig] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """ECOS API에서 거시지표 시계열 데이터를 수집하고 CSV로 저장한다.

    Parameters
    ----------
    start_date / end_date:
        YYYYMM 형식. 분기 지표(QQ)는 YYYYQN이 아닌 YYYYMM으로도 조회 가능.
    """
    targets = series_list or DEFAULT_ECOS_SERIES
    client = EcosApiClient(api_settings)
    frames: list[pd.DataFrame] = []

    for series in targets:
        logger.info("[ecos] fetching series=%s stat_code=%s", series.name, series.stat_code)
        try:
            rows = client.fetch_all_pages(
                stat_code=series.stat_code,
                cycle_type=series.cycle_type,
                start_date=start_date or "200001",
                end_date=end_date or "203001",
                item_code1=series.item_code1,
                item_code2=series.item_code2,
                item_code3=series.item_code3,
                item_code4=series.item_code4,
            )
        except EcosApiError as exc:
            logger.warning("[ecos] failed series=%s err=%s", series.name, exc)
            continue

        if not rows:
            logger.warning("[ecos] no rows for series=%s", series.name)
            continue

        frame = pd.DataFrame(rows)
        frame.columns = [c.upper() for c in frame.columns]

        # TIME → date (YYYYMM or YYYYMMDD)
        frame["date"] = frame["TIME"].astype(str).map(_parse_ecos_time)
        frame = frame.dropna(subset=["date"])

        frame["value"] = pd.to_numeric(frame.get("DATA_VALUE"), errors="coerce")
        frame = frame.dropna(subset=["value"])

        frame["series_name"] = series.name
        frame["series_code"] = series.stat_code
        frame["source"] = "ecos"

        # 항목 정보를 grp_id / cls_id 에 매핑 (차원 통일)
        frame["grp_id"] = frame.get("ITEM_CODE1", pd.Series("ALL", index=frame.index)).fillna("ALL").astype(str)
        frame["grp_nm"] = frame.get("ITEM_NAME1", pd.Series("", index=frame.index)).fillna("").astype(str)
        frame["cls_id"] = frame.get("ITEM_CODE2", pd.Series("ALL", index=frame.index)).fillna("ALL").astype(str)
        frame["cls_nm"] = frame.get("ITEM_NAME2", pd.Series("", index=frame.index)).fillna("").astype(str)

        preferred = ["date", "series_name", "series_code", "source",
                     "grp_id", "grp_nm", "cls_id", "cls_nm", "value"]
        frame = frame[[c for c in preferred if c in frame.columns]].copy()
        frames.append(frame)
        logger.info("[ecos] fetched rows=%s series=%s", len(frame), series.name)

    if not frames:
        raise ValueError("ECOS에서 수집된 데이터가 없습니다. API 키와 지표 설정을 확인하세요.")

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values([c for c in ["series_name", "grp_id", "cls_id", "date"] if c in df.columns])

    output_path = Path(output_csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("[ecos] saved rows=%s path=%s", len(df), output_path)
    return df


def build_monthly_indicator_table(
    reb_df: pd.DataFrame,
    ecos_df: pd.DataFrame,
    policy_wide_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """원시 시계열을 월별 와이드 테이블로 변환한다.

    Output schema:
    - timestamp: month start datetime
    - reb__{series_name}: REB 월 평균값
    - ecos__{series_name}: ECOS 월 평균값
    """
    wide_frames: list[pd.DataFrame] = []

    def _to_wide(df: pd.DataFrame, source: str) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()

        work = df.copy()
        if "date" not in work.columns or "series_name" not in work.columns or "value" not in work.columns:
            return pd.DataFrame()

        work["date"] = pd.to_datetime(work["date"], errors="coerce")
        work["value"] = pd.to_numeric(work["value"], errors="coerce")
        work = work.dropna(subset=["date", "series_name", "value"])
        if work.empty:
            return pd.DataFrame()

        # 월 기준으로 맞추고, 동일 월/지표의 중복값은 평균으로 집계.
        work["timestamp"] = work["date"].dt.to_period("M").dt.to_timestamp()
        grouped = (
            work.groupby(["timestamp", "series_name"], as_index=False)["value"]
            .mean()
            .pivot(index="timestamp", columns="series_name", values="value")
            .sort_index()
        )
        grouped.columns = [f"{source}__{str(col)}" for col in grouped.columns]
        return grouped

    reb_wide = _to_wide(reb_df, "reb")
    if not reb_wide.empty:
        wide_frames.append(reb_wide)

    ecos_wide = _to_wide(ecos_df, "ecos")
    if not ecos_wide.empty:
        wide_frames.append(ecos_wide)

    if not wide_frames:
        return pd.DataFrame(columns=["timestamp"])

    merged = wide_frames[0]
    for frame in wide_frames[1:]:
        merged = merged.join(frame, how="outer")

    merged = merged.reset_index().sort_values("timestamp")

    if policy_wide_df is not None and not policy_wide_df.empty:
        policy = policy_wide_df.copy()
        if "timestamp" in policy.columns:
            policy["timestamp"] = pd.to_datetime(policy["timestamp"], errors="coerce")
            policy = policy.dropna(subset=["timestamp"])
            merged = merged.merge(policy, on="timestamp", how="left")
            policy_cols = [c for c in merged.columns if str(c).startswith("policy__")]
            if policy_cols:
                merged[policy_cols] = merged[policy_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)

    return merged


def collect_policy_regulation_data(
    history_csv_path: str | list[str],
    output_csv_path: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Load policy history CSV and expand it to monthly regulation states.

    Input CSV columns expected (alias supported):
    - 적용 년월
    - 지역
    - 규제명
    - 규제 종류 또는 강화/완화 (강화/완화)
    - 세부 내용 (optional)
    """
    paths = [history_csv_path] if isinstance(history_csv_path, str) else list(history_csv_path)
    if not paths:
        raise ValueError("at least one policy history csv path is required")

    works: list[pd.DataFrame] = []
    for one_path in paths:
        history_path = Path(one_path)
        if not history_path.exists():
            logger.warning("[policy] history csv not found, skipping path=%s", history_path)
            continue

        raw: pd.DataFrame | None = None
        last_err: Exception | None = None
        for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
            try:
                raw = pd.read_csv(history_path, encoding=enc)
                break
            except Exception as exc:  # pragma: no cover - depends on local file encoding
                last_err = exc

        if raw is None:
            raise ValueError(f"failed to read policy history csv: {history_path}") from last_err

        one = raw.copy()
        one.columns = [str(c).strip() for c in one.columns]

        ym_col = _first_existing_column(one, ["적용 년월", "적용년월", "시작 년월", "시작년월", "일시"])
        region_col = _first_existing_column(one, ["지역", "구", "시군구"])
        indicator_col = _first_existing_column(one, ["규제명", "지표명", "항목"])
        regulation_col = _first_existing_column(one, ["규제 종류", "규제", "강화/완화"])
        detail_col = _first_existing_column(one, ["세부 내용", "세부내용", "비고"])

        if not ym_col or not region_col or not indicator_col or not regulation_col:
            raise ValueError(
                "policy history csv missing required columns: "
                f"path={history_path} cols={list(one.columns)}"
            )

        rename_map = {
            ym_col: "applied_ym",
            region_col: "region",
            indicator_col: "indicator_name",
            regulation_col: "regulation",
        }
        if detail_col:
            rename_map[detail_col] = "detail"
        one = one.rename(columns=rename_map)
        one["policy_source"] = history_path.name
        works.append(one)

    if not works:
        raise FileNotFoundError("no policy history csv files found")

    work = pd.concat(works, ignore_index=True)

    work["timestamp"] = work["applied_ym"].map(_parse_policy_year_month)
    work["region"] = work["region"].map(_normalize_policy_region)
    work["indicator_name"] = work["indicator_name"].map(_normalize_policy_indicator)
    work["regulation"] = work["regulation"].map(_normalize_policy_regulation)
    work["detail"] = work.get("detail", pd.Series("", index=work.index)).fillna("").astype(str)
    work["policy_source"] = work.get("policy_source", pd.Series("", index=work.index)).fillna("").astype(str)

    work = work.dropna(subset=["timestamp"])
    work = work[(work["indicator_name"] != "") & (work["region"] != "")]
    if work.empty:
        return pd.DataFrame(
            columns=[
                "timestamp",
                "region",
                "indicator_name",
                "regulation",
                "regulation_value",
                "applied_ym",
                "detail",
                "policy_source",
            ]
        )

    # Explicit 상태만 사용한다. 빈 셀은 baseline/참고용으로 무시한다.
    work = work[work["regulation"].isin(["강화", "완화", "유지"])].copy()
    work = work.sort_values(["region", "indicator_name", "timestamp"]).reset_index(drop=True)

    overall_start = _parse_yyyymm_month_start(start_date) if start_date else work["timestamp"].min()
    overall_end = _parse_yyyymm_month_start(end_date) if end_date else work["timestamp"].max()
    if pd.isna(overall_start) or pd.isna(overall_end):
        return pd.DataFrame(
            columns=[
                "timestamp",
                "region",
                "indicator_name",
                "regulation",
                "regulation_value",
                "applied_ym",
                "detail",
                "policy_source",
            ]
        )

    overall_start = pd.Timestamp(overall_start)
    overall_end = pd.Timestamp(overall_end)
    if overall_start > overall_end:
        raise ValueError("start_date is later than end_date for policy expansion")

    end_exclusive = overall_end + pd.offsets.MonthBegin(1)
    rows: list[dict[str, Any]] = []

    for (region, indicator), grp in work.groupby(["region", "indicator_name"], sort=False):
        g = grp.sort_values("timestamp").reset_index(drop=True)
        for i, row in g.iterrows():
            event_start = pd.Timestamp(row["timestamp"])
            next_start = pd.Timestamp(g.loc[i + 1, "timestamp"]) if i + 1 < len(g) else end_exclusive

            seg_start = max(event_start, overall_start)
            seg_end = min(next_start, end_exclusive)
            if seg_start >= seg_end:
                continue

            month_range = pd.date_range(start=seg_start, end=seg_end - pd.offsets.MonthBegin(1), freq="MS")
            regulation = str(row["regulation"])
            reg_value = 1.0 if regulation == "강화" else (-1.0 if regulation == "완화" else 0.0)
            applied_ym = pd.Timestamp(event_start).strftime("%Y%m")
            detail = str(row.get("detail", ""))

            for ts in month_range:
                rows.append(
                    {
                        "timestamp": ts,
                        "region": region,
                        "indicator_name": indicator,
                        "regulation": regulation,
                        "regulation_value": reg_value,
                        "applied_ym": applied_ym,
                        "detail": detail,
                        "policy_source": str(row.get("policy_source", "")),
                    }
                )

    result = pd.DataFrame(rows)
    if result.empty:
        result = pd.DataFrame(
            columns=[
                "timestamp",
                "region",
                "indicator_name",
                "regulation",
                "regulation_value",
                "applied_ym",
                "detail",
                "policy_source",
            ]
        )
    else:
        result = result.sort_values(["indicator_name", "timestamp"]).reset_index(drop=True)

    output_path = Path(output_csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("[policy] saved rows=%s path=%s", len(result), output_path)
    return result


def build_monthly_policy_wide_table(policy_df: pd.DataFrame) -> pd.DataFrame:
    """Convert monthly policy long table to wide numeric features."""
    if policy_df.empty:
        return pd.DataFrame(columns=["timestamp"])

    work = policy_df.copy()
    if "timestamp" not in work.columns or "indicator_name" not in work.columns or "regulation_value" not in work.columns:
        return pd.DataFrame(columns=["timestamp"])

    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work["regulation_value"] = pd.to_numeric(work["regulation_value"], errors="coerce")
    work = work.dropna(subset=["timestamp", "indicator_name", "regulation_value"])
    if work.empty:
        return pd.DataFrame(columns=["timestamp"])

    wide = (
        work.groupby(["timestamp", "region", "indicator_name"], as_index=False)["regulation_value"]
        .last()
        .pivot(index="timestamp", columns=["region", "indicator_name"], values="regulation_value")
        .sort_index()
    )

    wide.columns = [
        f"policy__{_sanitize_feature_name(str(region))}__{_policy_indicator_column_key(str(indicator))}_regime"
        for region, indicator in wide.columns
    ]
    return wide.reset_index()


def build_monthly_policy_by_gu_table(
    policy_df: pd.DataFrame,
    allowed_indicators: list[str] | None = None,
) -> pd.DataFrame:
    """Build compact policy table keyed by month+gu with indicator-only columns.

    Output columns:
    - timestamp
    - gu
    - policy__{indicator}_regime ...
    """
    if policy_df.empty:
        return pd.DataFrame(columns=["timestamp", "gu"])

    work = policy_df.copy()
    needed = {"timestamp", "region", "indicator_name", "regulation_value"}
    if not needed.issubset(set(work.columns)):
        return pd.DataFrame(columns=["timestamp", "gu"])

    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work["region"] = work["region"].astype(str).str.strip()
    work["indicator_name"] = work["indicator_name"].astype(str).str.strip()
    work["regulation_value"] = pd.to_numeric(work["regulation_value"], errors="coerce")
    work = work.dropna(subset=["timestamp", "regulation_value"])
    work = work[(work["region"] != "") & (work["indicator_name"] != "")]
    if work.empty:
        return pd.DataFrame(columns=["timestamp", "gu"])

    allowed = allowed_indicators or DEFAULT_META_POLICY_INDICATORS
    allowed_norm = {_normalize_policy_indicator(v) for v in allowed if str(v).strip()}
    if allowed_norm:
        work = work[work["indicator_name"].isin(allowed_norm)].copy()
    if work.empty:
        return pd.DataFrame(columns=["timestamp", "gu"])

    wide = (
        work.groupby(["timestamp", "region", "indicator_name"], as_index=False)["regulation_value"]
        .last()
        .pivot(index=["timestamp", "region"], columns="indicator_name", values="regulation_value")
        .sort_index()
    )

    wide.columns = [f"policy__{_policy_indicator_column_key(str(ind))}_regime" for ind in wide.columns]
    wide = wide.reset_index().rename(columns={"region": "gu"})
    return wide


def build_seoul_dong_meta_table(
    apartment_csv_path: str,
    monthly_wide_df: pd.DataFrame,
    output_csv_path: str,
    policy_df: pd.DataFrame | None = None,
    policy_indicators: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> pd.DataFrame:
    """Build monthly Seoul gu/dong meta table and merge indicator features.

    Output columns include:
    - timestamp, gu, dong
    - apartment monthly aggregates (count/avg/median/min/max)
    - merged monthly indicators (reb/ecos/policy...)
    """
    apt_path = Path(apartment_csv_path)
    if not apt_path.exists():
        raise FileNotFoundError(f"apartment csv not found: {apt_path}")

    raw: pd.DataFrame | None = None
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            raw = pd.read_csv(apt_path, dtype=str, encoding=enc)
            break
        except Exception as exc:  # pragma: no cover - file encoding depends on source
            last_err = exc

    if raw is None:
        raise ValueError(f"failed to read apartment csv: {apt_path}") from last_err

    cols = {str(c).strip(): c for c in raw.columns}
    required = ["시군구", "거래금액(만원)", "계약년월"]
    if all(c in cols for c in required):
        work = pd.DataFrame(
            {
                "sigungu": raw[cols["시군구"]].fillna("").astype(str).str.strip(),
                "price_manwon": raw[cols["거래금액(만원)"]].fillna("").astype(str),
                "contract_ym": raw[cols["계약년월"]].fillna("").astype(str).str.strip(),
            }
        )
    elif raw.shape[1] >= 10:
        # Fallback for mojibake headers with stable column order.
        work = pd.DataFrame(
            {
                "sigungu": raw.iloc[:, 1].fillna("").astype(str).str.strip(),
                "price_manwon": raw.iloc[:, 9].fillna("").astype(str),
                "contract_ym": raw.iloc[:, 7].fillna("").astype(str).str.strip(),
            }
        )
    else:
        raise ValueError("apartment csv does not contain required columns")

    parts = work["sigungu"].str.split(r"\s+", regex=True)
    work["gu"] = parts.map(lambda x: x[1] if isinstance(x, list) and len(x) > 1 else "")
    work["dong"] = parts.map(lambda x: x[2] if isinstance(x, list) and len(x) > 2 else "")
    work["timestamp"] = work["contract_ym"].map(_parse_yyyymm_month_start)
    work["price_manwon"] = pd.to_numeric(work["price_manwon"].str.replace(",", "", regex=False), errors="coerce")

    # Keep only Seoul and valid gu/dong rows.
    work = work[work["sigungu"].str.contains("서울", na=False)]
    work = work[(work["gu"].str.strip() != "") & (work["dong"].str.strip() != "")]
    work = work.dropna(subset=["timestamp", "price_manwon"])

    if start_date:
        s = _parse_yyyymm_month_start(start_date)
        if not pd.isna(s):
            work = work[work["timestamp"] >= pd.Timestamp(s)]
    if end_date:
        e = _parse_yyyymm_month_start(end_date)
        if not pd.isna(e):
            work = work[work["timestamp"] <= pd.Timestamp(e)]

    if work.empty:
        meta = pd.DataFrame(columns=["timestamp", "gu", "dong"])
    else:
        meta = (
            work.groupby(["timestamp", "gu", "dong"], as_index=False)
            .agg(
                apt_txn_count=("price_manwon", "size"),
                apt_price_avg_manwon=("price_manwon", "mean"),
                apt_price_median_manwon=("price_manwon", "median"),
                apt_price_min_manwon=("price_manwon", "min"),
                apt_price_max_manwon=("price_manwon", "max"),
            )
            .sort_values(["timestamp", "gu", "dong"])
        )

    monthly = monthly_wide_df.copy() if monthly_wide_df is not None else pd.DataFrame(columns=["timestamp"])
    if "timestamp" in monthly.columns:
        monthly["timestamp"] = pd.to_datetime(monthly["timestamp"], errors="coerce")
        monthly = monthly.dropna(subset=["timestamp"])
    else:
        monthly = pd.DataFrame(columns=["timestamp"])

    if not meta.empty and not monthly.empty:
        result = meta.merge(monthly, on="timestamp", how="left")
    elif not meta.empty:
        result = meta
    else:
        result = monthly

    # 정책은 (월, 구) 기준으로 조인하여 지표별 1/0/-1 컬럼만 노출한다.
    if policy_df is not None and not policy_df.empty and not result.empty and "gu" in result.columns:
        policy_gu = build_monthly_policy_by_gu_table(
            policy_df=policy_df,
            allowed_indicators=policy_indicators,
        )
        if not policy_gu.empty:
            result = result.merge(policy_gu, on=["timestamp", "gu"], how="left")
            pcols = [c for c in result.columns if str(c).startswith("policy__")]
            if pcols:
                result[pcols] = result[pcols].apply(pd.to_numeric, errors="coerce").fillna(0.0)

    output_path = Path(output_csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info("[meta] saved rows=%s path=%s", len(result), output_path)
    return result


# ---------------------------------------------------------------------------
# 동 단위 메타테이블 (VAR-TFT / Diffusion-TFT 분석용)
# ---------------------------------------------------------------------------

def load_apartment_transactions_frame(apartment_csv_path: str) -> pd.DataFrame:
    """원시 apartment.csv를 (sigungu, gu, dong, complex_name, building, floor,
    price_manwon, contract_ym, contract_day) 컬럼의 표준 DataFrame으로 변환한다.

    Seoul 행만 유지하고 mojibake / 빈 행을 정리한다. 이 결과를 ChromaDB
    apartment_transactions 컬렉션에 적재해 분석 파이프라인이 DB 단일 소스를
    사용할 수 있게 한다.
    """
    apt_path = Path(apartment_csv_path)
    if not apt_path.exists():
        raise FileNotFoundError(f"apartment csv not found: {apt_path}")

    raw: pd.DataFrame | None = None
    last_err: Exception | None = None
    fallback_raw: pd.DataFrame | None = None
    required = ["시군구", "단지명", "동", "층", "거래금액(만원)", "계약년월", "계약일"]
    cols: dict[str, str] = {}
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            candidate = pd.read_csv(apt_path, dtype=str, encoding=enc)
            cand_cols = {str(c).strip(): c for c in candidate.columns}
            if all(c in cand_cols for c in required):
                raw = candidate
                cols = cand_cols
                break
            if fallback_raw is None and candidate.shape[1] >= 11:
                fallback_raw = candidate
        except Exception as exc:
            last_err = exc
    if raw is None and fallback_raw is not None:
        raw = fallback_raw
    if raw is None:
        raise ValueError(f"failed to read apartment csv: {apt_path}") from last_err

    def _safe(v: Any) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip()

    def _parse_price(v: Any) -> float | None:
        if v is None:
            return None
        try:
            s = str(v).replace(",", "").strip()
            if not s:
                return None
            return float(s)
        except Exception:
            return None

    if all(c in cols for c in required):
        df = pd.DataFrame(
            {
                "sigungu": raw[cols["시군구"]].map(_safe),
                "complex_name": raw[cols["단지명"]].map(_safe),
                "building": raw[cols["동"]].map(_safe),
                "floor": raw[cols["층"]].map(_safe),
                "price_manwon": raw[cols["거래금액(만원)"]].map(_parse_price),
                "contract_ym": raw[cols["계약년월"]].map(_safe),
                "contract_day": raw[cols["계약일"]].map(_safe),
            }
        )
    else:
        df = pd.DataFrame(
            {
                "sigungu": raw.iloc[:, 1].map(_safe),
                "complex_name": raw.iloc[:, 5].map(_safe),
                "building": raw.iloc[:, 2].map(_safe),
                "floor": raw.iloc[:, 10].map(_safe),
                "price_manwon": raw.iloc[:, 9].map(_parse_price),
                "contract_ym": raw.iloc[:, 7].map(_safe),
                "contract_day": raw.iloc[:, 8].map(_safe),
            }
        )

    parts = df["sigungu"].str.split(r"\s+", regex=True)
    df["gu"] = parts.map(lambda x: x[1] if isinstance(x, list) and len(x) > 1 else "")
    df["dong"] = parts.map(lambda x: x[2] if isinstance(x, list) and len(x) > 2 else "")
    seoul_mask = df["sigungu"].str.contains("서울", na=False)
    if seoul_mask.any():
        df = df[seoul_mask].copy()
    df = df[(df["gu"].astype(str).str.strip() != "")
            & (df["dong"].astype(str).str.strip() != "")].copy()
    df = df[df["price_manwon"].notna()].copy()
    return df.reset_index(drop=True)


def load_seoul_region_lookup(lookup_path: str) -> pd.DataFrame:
    """seoul_region_lookup.json을 (si, gu, dong) 형태의 DataFrame으로 읽는다.

    Parameters
    ----------
    lookup_path : 'data/raw/seoul_region_lookup.json' 경로

    Returns
    -------
    DataFrame with columns: si, gu, dong
    """
    path = Path(lookup_path)
    if not path.exists():
        raise FileNotFoundError(f"seoul_region_lookup.json not found: {path}")

    with path.open(encoding="utf-8") as f:
        lookup: dict[str, dict[str, list[str]]] = json.load(f)

    rows: list[dict[str, str]] = []
    for si, gu_map in lookup.items():
        for gu, dong_list in gu_map.items():
            for dong in dong_list:
                rows.append({"si": si, "gu": gu, "dong": dong})

    return pd.DataFrame(rows, columns=["si", "gu", "dong"])


def _filter_seoul_kwonyeok(df: pd.DataFrame) -> pd.DataFrame:
    """REB 수집 결과를 서울 5개 권역(도심권/동북권/서북권/서남권/동남권) 행만 남긴다.

    권역의 CLS_ID(지역코드)는 statbl_id마다 다르지만 CLS_NM(권역명)은 항상 동일하므로
    지역명을 기준으로 필터링한다. (예: '경부1권', '중부산권' 등 타 시·도 권역은 제외)
    """
    if df.empty:
        return df

    def _row_kwon(row: pd.Series) -> bool:
        for col in ("cls_nm", "grp_nm"):
            name = str(row.get(col, "") or "").strip()
            if name in SEOUL_KWONYEOK_NAMES:
                return True
        return False

    mask = df.apply(_row_kwon, axis=1)
    return df[mask].copy()


def _classify_reb_region(grp_nm: str | None, cls_nm: str | None) -> tuple[str, str]:
    """REB raw row의 지역명을 (level, region_name)으로 분류한다.

    Returns
    -------
    level : 'kwon' | 'dong' | 'gu' | 'si' | 'national'
    region_name : 해당 수준의 지역명 (정제된 문자열)
    """
    def _clean(v: Any) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip()

    cls = _clean(cls_nm)
    grp = _clean(grp_nm)

    # cls_nm 우선 — 더 세부적인 정보가 담겨 있는 경우가 많다.
    for name in (cls, grp):
        if not name:
            continue
        # 서울 5개 권역 (도심권/동북권/서북권/서남권/동남권)
        if name in SEOUL_KWONYEOK_NAMES:
            return "kwon", name
        if name.endswith("동") or name.endswith("洞"):
            return "dong", name
        if name.endswith("구") or name.endswith("區"):
            return "gu", name
        if ("특별시" in name or "광역시" in name or "특별자치시" in name
                or name in ("서울", "전국")):
            return "si", name

    # 분류 불가 → national (전 지역에 broadcast)
    return "national", ""


def _normalize_reb_region_name(
    level: str,
    name: str,
    known_gu_set: set[str],
    known_dong_set: set[str],
) -> str:
    """REB 지역명을 lookup 기준 표준값으로 정규화한다.

    - 서울/서울시/서울특별시 계열은 모두 '서울특별시'로 정규화
    - '서울 강남구', '서울특별시강남구'처럼 접두가 섞인 구/동도 lookup 기반 매칭
    """
    raw = str(name or "").strip()
    if not raw:
        return ""

    compact = re.sub(r"\s+", "", raw)

    if level == "kwon":
        # 권역명(도심권/동북권/...)은 이미 표준값이므로 그대로 사용
        return raw

    if level == "si":
        if "서울" in compact:
            return "서울특별시"
        return raw

    if level == "gu":
        for gu in known_gu_set:
            gu_key = re.sub(r"\s+", "", str(gu))
            if gu_key and (gu_key in compact or compact in gu_key):
                return str(gu)
        m = re.search(r"([가-힣A-Za-z0-9]+구)$", compact)
        if m:
            return m.group(1)
        return raw

    if level == "dong":
        for dong in known_dong_set:
            dong_key = re.sub(r"\s+", "", str(dong))
            if dong_key and (dong_key in compact or compact in dong_key):
                return str(dong)
        m = re.search(r"([가-힣A-Za-z0-9]+동)$", compact)
        if m:
            return m.group(1)
        return raw

    return raw


def load_apartment_avg_price_frame(apartment_avg_csv_path: str) -> pd.DataFrame:
    """집계된 apartment.csv(동별·월별 전용면적당 평균거래금액)를 표준 형태로 읽는다.

    입력 CSV 컬럼: si, gu, dong, contract_ym(YYYYMM), avg_price_per_m2_manwon, n_transactions
    반환 컬럼: timestamp(월초), si, gu, dong, actual_avg_price
    """
    path = Path(apartment_avg_csv_path)
    if not path.exists():
        raise FileNotFoundError(f"apartment avg csv not found: {path}")

    raw: pd.DataFrame | None = None
    last_err: Exception | None = None
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            raw = pd.read_csv(path, dtype=str, encoding=enc)
            break
        except Exception as exc:  # pragma: no cover - encoding probe
            last_err = exc
    if raw is None:
        raise ValueError(f"failed to read apartment avg csv: {path}") from last_err

    cols = {str(c).strip(): c for c in raw.columns}
    required = ["si", "gu", "dong", "contract_ym", "avg_price_per_m2_manwon"]
    missing = [c for c in required if c not in cols]
    if missing:
        raise ValueError(
            f"apartment avg csv missing columns: {missing} (found {list(cols)})"
        )

    out = pd.DataFrame(
        {
            "si": raw[cols["si"]].fillna("").astype(str).str.strip(),
            "gu": raw[cols["gu"]].fillna("").astype(str).str.strip(),
            "dong": raw[cols["dong"]].fillna("").astype(str).str.strip(),
            "timestamp": raw[cols["contract_ym"]].astype(str).str.strip().map(_parse_yyyymm_month_start),
            "actual_avg_price": pd.to_numeric(raw[cols["avg_price_per_m2_manwon"]], errors="coerce"),
        }
    )
    out = out[(out["si"] != "") & (out["gu"] != "") & (out["dong"] != "")]
    out = out.dropna(subset=["timestamp", "actual_avg_price"])
    return out[["timestamp", "si", "gu", "dong", "actual_avg_price"]]


def rekey_meta_to_apartment_dongs(
    meta_df: pd.DataFrame,
    apartment_avg_df: pd.DataFrame,
) -> pd.DataFrame:
    """구 단위로 broadcast 된 메타테이블을 apartment 실거래 동 단위로 재구성한다.

    기존 dong_level_meta_table 은 같은 구의 모든 동이 동일한 feature 값을 갖는다(구/권역/거시
    단위 broadcast). 이 함수는:
      1) (timestamp, si, gu) 단위로 feature 값을 집계(구 내 동일하므로 first)하고,
      2) apartment.csv 의 실제 (timestamp, si, gu, dong) 거래 row 를 기준 축으로 삼아,
      3) ``actual_avg_price`` (전용면적당 평균 거래금액)을 동별 실측치로 부여한 뒤,
      4) 구 단위 feature 를 (timestamp, si, gu) 기준으로 붙인다.

    결과 컬럼 순서:
        timestamp, si, gu, dong, actual_avg_price, <나머지 모든 feature 컬럼>
    """
    if meta_df is None or meta_df.empty:
        raise ValueError("rekey: meta_df 가 비어 있습니다.")
    if apartment_avg_df is None or apartment_avg_df.empty:
        raise ValueError("rekey: apartment_avg_df 가 비어 있습니다.")

    feat = meta_df.copy()
    feat["timestamp"] = pd.to_datetime(feat["timestamp"], errors="coerce")
    feat = feat.dropna(subset=["timestamp", "gu"])
    if "si" not in feat.columns:
        feat["si"] = "서울특별시"

    # actual_avg_price 가 기존 메타에 이미 있으면 동 단위 실측치로 덮어쓰므로 제거.
    feature_cols = [
        c for c in feat.columns
        if c not in {"timestamp", "si", "gu", "dong", "actual_avg_price"}
    ]
    # 구 내 동일 값 → first 로 (timestamp, si, gu) 단위 대표값 추출.
    gu_feat = (
        feat.groupby(["timestamp", "si", "gu"], as_index=False)[feature_cols].first()
        if feature_cols else
        feat[["timestamp", "si", "gu"]].drop_duplicates()
    )

    apt = apartment_avg_df.copy()
    apt["timestamp"] = pd.to_datetime(apt["timestamp"], errors="coerce")
    apt = apt.dropna(subset=["timestamp", "si", "gu", "dong", "actual_avg_price"])

    merged = apt.merge(gu_feat, on=["timestamp", "si", "gu"], how="left")
    ordered = ["timestamp", "si", "gu", "dong", "actual_avg_price"] + feature_cols
    merged = merged[[c for c in ordered if c in merged.columns]]
    merged = merged.sort_values(["timestamp", "si", "gu", "dong"]).reset_index(drop=True)
    return merged


def build_dong_level_meta_table(
    reb_df: pd.DataFrame,
    ecos_df: pd.DataFrame,
    policy_df: pd.DataFrame | None,
    seoul_region_lookup_path: str,
    output_csv_path: str,
    policy_indicators: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    apartment_avg_csv_path: str | None = None,
) -> pd.DataFrame:
    """VAR-TFT / Diffusion-TFT 분석용 동 단위 메타테이블을 생성한다.

    서울시 전체 (시, 구, 동) 조합을 skeleton으로 구성한 뒤
    각 데이터 소스를 규정된 granularity 규칙에 따라 expand/broadcast 한다.

    확장 규칙
    ---------
    - ecos_stats          : 지역 없음 → 전 동에 동일 값 broadcast
    - reb_stats 동 단위   : 해당 (gu, dong) 정확 매칭
    - reb_stats 구 단위   : 해당 gu 소속 전 동에 값 broadcast
    - reb_stats 시 단위   : 서울시 전 동에 broadcast
    - regulation (policy) : region 컬럼이 구 → 해당 gu 소속 전 동에 broadcast

    Parameters
    ----------
    reb_df, ecos_df : collect_reb_data / collect_ecos_data 반환 DataFrame
    policy_df : collect_policy_regulation_data 반환 DataFrame (None 허용)
    seoul_region_lookup_path : seoul_region_lookup.json 경로
    output_csv_path : 결과 CSV 저장 경로
    policy_indicators : 반영할 정책 지표명 리스트 (None → DEFAULT_META_POLICY_INDICATORS)
    start_date / end_date : 'YYYYMM' 형식 필터 (None → 데이터 전체 범위)

    Returns
    -------
    DataFrame with columns:
        timestamp, si, gu, dong,
        ecos__{series_name}...,
        reb__{series_name}...,
        policy__{indicator}_regime...
    """
    # ── 1. 지역 skeleton 로드 ────────────────────────────────────────────────
    region_df = load_seoul_region_lookup(seoul_region_lookup_path)
    if region_df.empty:
        raise ValueError("seoul_region_lookup.json에 지역 데이터가 없습니다.")

    known_gu_set: set[str] = set(region_df["gu"].unique())
    known_dong_set: set[str] = set(region_df["dong"].unique())
    # (gu, dong) → si 매핑
    gu_dong_to_si: dict[tuple[str, str], str] = {
        (row.gu, row.dong): row.si
        for row in region_df.itertuples(index=False)
    }

    # ── 2. 날짜 범위 결정 ───────────────────────────────────────────────────
    def _all_dates(df: pd.DataFrame) -> pd.Series:
        if df.empty or "date" not in df.columns:
            return pd.Series(dtype="datetime64[ns]")
        return pd.to_datetime(df["date"], errors="coerce").dropna()

    all_dates: list[pd.Timestamp] = []
    for df_src in (reb_df, ecos_df):
        dates = _all_dates(df_src)
        if not dates.empty:
            all_dates.extend(dates.tolist())
    if policy_df is not None and not policy_df.empty and "timestamp" in policy_df.columns:
        all_dates.extend(pd.to_datetime(policy_df["timestamp"], errors="coerce").dropna().tolist())

    if not all_dates:
        raise ValueError("수집된 데이터에 날짜 정보가 없습니다.")

    ts_min = pd.Timestamp(min(all_dates)).replace(day=1)
    ts_max = pd.Timestamp(max(all_dates)).replace(day=1)

    if start_date:
        s = _parse_yyyymm_month_start(start_date)
        if not pd.isna(s):
            ts_min = max(ts_min, pd.Timestamp(s))
    if end_date:
        e = _parse_yyyymm_month_start(end_date)
        if not pd.isna(e):
            ts_max = min(ts_max, pd.Timestamp(e))

    month_range = pd.date_range(start=ts_min, end=ts_max, freq="MS")
    if month_range.empty:
        raise ValueError(f"날짜 범위가 비어 있습니다: {ts_min} ~ {ts_max}")

    # ── 3. skeleton 구성: (timestamp × si × gu × dong) ─────────────────────
    ts_df = pd.DataFrame({"timestamp": month_range})
    skeleton = ts_df.merge(region_df, how="cross")  # pandas >= 1.2 cross join
    # 각 동이 속한 서울 5개 권역 라벨 (reb 권역 데이터 broadcast용)
    skeleton["_kwon"] = skeleton["gu"].map(SEOUL_GU_TO_KWONYEOK)

    # ── 4. ecos 처리 (지역 없음 → 전 동에 broadcast) ──────────────────────
    ecos_feature_cols: list[str] = []
    if not ecos_df.empty and "series_name" in ecos_df.columns and "value" in ecos_df.columns:
        ecos_work = ecos_df.copy()
        ecos_work["date"] = pd.to_datetime(ecos_work["date"], errors="coerce")
        ecos_work["timestamp"] = ecos_work["date"].dt.to_period("M").dt.to_timestamp()
        ecos_work["value"] = pd.to_numeric(ecos_work["value"], errors="coerce")
        ecos_work = ecos_work.dropna(subset=["timestamp", "series_name", "value"])

        ecos_wide = (
            ecos_work.groupby(["timestamp", "series_name"], as_index=False)["value"]
            .mean()
            .pivot(index="timestamp", columns="series_name", values="value")
            .sort_index()
        )
        ecos_wide.columns = [f"ecos__{c}" for c in ecos_wide.columns]
        ecos_wide = ecos_wide.reset_index()
        ecos_feature_cols = [c for c in ecos_wide.columns if c != "timestamp"]

        skeleton = skeleton.merge(ecos_wide, on="timestamp", how="left")

    # ── 5. reb 처리 (지역 granularity에 따라 join 방식 분기) ──────────────
    reb_feature_cols: list[str] = []
    if not reb_df.empty and "series_name" in reb_df.columns and "value" in reb_df.columns:
        reb_work = reb_df.copy()
        reb_work["date"] = pd.to_datetime(reb_work["date"], errors="coerce")
        reb_work["timestamp"] = reb_work["date"].dt.to_period("M").dt.to_timestamp()
        reb_work["value"] = pd.to_numeric(reb_work["value"], errors="coerce")
        reb_work = reb_work.dropna(subset=["timestamp", "series_name", "value"])

        # 지역 수준 분류
        reb_work[["_region_level", "_region_name"]] = reb_work.apply(
            lambda r: pd.Series(_classify_reb_region(r.get("grp_nm"), r.get("cls_nm"))),
            axis=1,
        )
        reb_work["_region_name"] = reb_work.apply(
            lambda r: _normalize_reb_region_name(
                level=str(r.get("_region_level", "")),
                name=str(r.get("_region_name", "")),
                known_gu_set=known_gu_set,
                known_dong_set=known_dong_set,
            ),
            axis=1,
        )

        # 시리즈별로 처리 (같은 시리즈라도 행마다 granularity가 다를 수 있음)
        for series_name, series_grp in reb_work.groupby("series_name"):
            col_name = f"reb__{series_name}"
            reb_feature_cols.append(col_name)

            # 수준별로 분리
            si_rows = series_grp[series_grp["_region_level"].isin(["si", "national"])].copy()
            gu_rows = series_grp[series_grp["_region_level"] == "gu"].copy()
            dong_rows = series_grp[series_grp["_region_level"] == "dong"].copy()
            kwon_rows = series_grp[series_grp["_region_level"] == "kwon"].copy()

            # 5-A. 동 단위 — (timestamp, gu, dong) 정확 매칭
            if not dong_rows.empty:
                dong_agg = (
                    dong_rows.groupby(["timestamp", "_region_name"], as_index=False)["value"]
                    .mean()
                    .rename(columns={"_region_name": "dong", "value": col_name})
                )
                # gu는 region_lookup으로 보정 (dong이 여러 구에 있을 수 있으므로 lookup 우선)
                skeleton = skeleton.merge(
                    dong_agg, on=["timestamp", "dong"], how="left", suffixes=("", "_dong_tmp")
                )
                if col_name + "_dong_tmp" in skeleton.columns:
                    skeleton[col_name] = skeleton[col_name].combine_first(
                        skeleton.pop(col_name + "_dong_tmp")
                    )

            # 5-B. 구 단위 — (timestamp, gu) 매칭 → 해당 gu 전 동에 broadcast
            if not gu_rows.empty:
                gu_agg = (
                    gu_rows.groupby(["timestamp", "_region_name"], as_index=False)["value"]
                    .mean()
                    .rename(columns={"_region_name": "gu", "value": col_name + "_gu_src"})
                )
                skeleton = skeleton.merge(gu_agg, on=["timestamp", "gu"], how="left")
                if col_name not in skeleton.columns:
                    skeleton[col_name] = pd.NA
                skeleton[col_name] = skeleton[col_name].combine_first(skeleton.pop(col_name + "_gu_src"))

            # 5-B2. 권역 단위 — (timestamp, _kwon) 매칭 → 권역 소속 전 구/동에 broadcast
            if not kwon_rows.empty:
                kwon_agg = (
                    kwon_rows.groupby(["timestamp", "_region_name"], as_index=False)["value"]
                    .mean()
                    .rename(columns={"_region_name": "_kwon", "value": col_name + "_kwon_src"})
                )
                skeleton = skeleton.merge(kwon_agg, on=["timestamp", "_kwon"], how="left")
                if col_name not in skeleton.columns:
                    skeleton[col_name] = pd.Series(pd.NA, index=skeleton.index, dtype="float64")
                skeleton[col_name] = skeleton[col_name].combine_first(skeleton.pop(col_name + "_kwon_src"))

            # 5-C. 시/전국 단위 — timestamp만 매칭 → 전 동에 broadcast
            if not si_rows.empty:
                si_agg = (
                    si_rows.groupby("timestamp", as_index=False)["value"]
                    .mean()
                    .rename(columns={"value": col_name + "_si_src"})
                )
                skeleton = skeleton.merge(si_agg, on="timestamp", how="left")
                if col_name not in skeleton.columns:
                    skeleton[col_name] = pd.NA
                skeleton[col_name] = skeleton[col_name].combine_first(skeleton.pop(col_name + "_si_src"))

    # ── 6. 정책 처리 (구 단위 → 해당 gu의 전 동에 broadcast) ──────────────
    policy_feature_cols: list[str] = []
    if policy_df is not None and not policy_df.empty:
        allowed = policy_indicators or DEFAULT_META_POLICY_INDICATORS
        allowed_norm = {_normalize_policy_indicator(v) for v in allowed if str(v).strip()}

        pol_work = policy_df.copy()
        pol_work["timestamp"] = pd.to_datetime(pol_work["timestamp"], errors="coerce")
        pol_work["regulation_value"] = pd.to_numeric(pol_work["regulation_value"], errors="coerce")
        pol_work = pol_work.dropna(subset=["timestamp", "regulation_value"])
        pol_work["region"] = pol_work["region"].astype(str).str.strip()
        pol_work["indicator_name"] = pol_work["indicator_name"].astype(str).str.strip()
        pol_work = pol_work[
            (pol_work["region"] != "") & (pol_work["indicator_name"] != "")
        ]
        if allowed_norm:
            pol_work = pol_work[pol_work["indicator_name"].isin(allowed_norm)]

        if not pol_work.empty:
            pol_wide = (
                pol_work.groupby(["timestamp", "region", "indicator_name"], as_index=False)["regulation_value"]
                .last()
                .pivot(index=["timestamp", "region"], columns="indicator_name", values="regulation_value")
                .sort_index()
            )
            pol_wide.columns = [
                f"policy__{_policy_indicator_column_key(str(ind))}_regime"
                for ind in pol_wide.columns
            ]
            pol_wide = pol_wide.reset_index().rename(columns={"region": "gu"})
            policy_feature_cols = [c for c in pol_wide.columns if c not in {"timestamp", "gu"}]
            skeleton = skeleton.merge(pol_wide, on=["timestamp", "gu"], how="left")
            if policy_feature_cols:
                skeleton[policy_feature_cols] = (
                    skeleton[policy_feature_cols]
                    .apply(pd.to_numeric, errors="coerce")
                    .fillna(0.0)
                )

    # ── 7. 정리 및 저장 ─────────────────────────────────────────────────────
    if "_kwon" in skeleton.columns:
        skeleton = skeleton.drop(columns=["_kwon"])
    skeleton = skeleton.sort_values(["timestamp", "si", "gu", "dong"]).reset_index(drop=True)

    # ── 7-b. apartment 실거래 동 단위 재구성 ────────────────────────────────
    # region_lookup 으로 만든 동 skeleton 은 같은 구의 모든 동이 동일한 feature 값을
    # 갖는다(구/권역/거시 broadcast). apartment.csv 가 주어지면 실제 거래가 발생한
    # (timestamp, si, gu, dong) 축으로 재구성하고 actual_avg_price(전용면적당 평균
    # 거래금액) 동별 실측치를 부여한다.
    if apartment_avg_csv_path:
        apartment_avg_df = load_apartment_avg_price_frame(apartment_avg_csv_path)
        skeleton = rekey_meta_to_apartment_dongs(skeleton, apartment_avg_df)
        logger.info(
            "[dong_meta] apartment 동 단위 재구성 적용: rows=%s dong=%s",
            len(skeleton),
            skeleton["dong"].nunique() if "dong" in skeleton.columns else 0,
        )

    output_path = Path(output_csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    skeleton.to_csv(output_path, index=False, encoding="utf-8-sig")
    logger.info(
        "[dong_meta] saved rows=%s cols=%s path=%s",
        len(skeleton),
        len(skeleton.columns),
        output_path,
    )
    return skeleton


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_all_pages(
    client: REBApiClient,
    series: SeriesConfig,
    start_wrttime: str | None,
    end_wrttime: str | None,
    max_pages: int,
) -> list[dict[str, Any]]:
    cycle_candidates = [series.dtacycle_cd, "M", "MM", "MONTH", "YY", "Q"]
    tried: set[str] = set()

    for cycle_code in cycle_candidates:
        if not cycle_code or cycle_code in tried:
            continue
        tried.add(cycle_code)

        collected: list[dict[str, Any]] = []
        seen_row_keys: set[str] = set()
        prev_page_signature: str | None = None
        consecutive_no_new = 0

        for page in range(1, max_pages + 1):
            try:
                rows = client.fetch_table_data(
                    statbl_id=series.statbl_id,
                    dtacycle_cd=cycle_code,
                    page_index=page,
                    itm_id=series.itm_id,
                    start_wrttime=start_wrttime,
                    end_wrttime=end_wrttime,
                )
            except REBApiError as exc:
                logger.warning("[collect] page error series=%s cycle=%s page=%s err=%s", series.name, cycle_code, page, exc)
                break

            if not rows:
                break

            signature = _page_signature(rows)
            if signature and signature == prev_page_signature:
                logger.warning("[collect] repeated page, stopping series=%s", series.name)
                break
            prev_page_signature = signature

            new_count = 0
            for row in rows:
                key = _row_unique_key(row)
                if key not in seen_row_keys:
                    seen_row_keys.add(key)
                    collected.append(row)
                    new_count += 1

            if new_count == 0:
                consecutive_no_new += 1
            else:
                consecutive_no_new = 0

            if consecutive_no_new >= 2:
                break

            if len(rows) < client.settings.page_size:
                break

        if collected:
            return collected

    return []


def _row_unique_key(row: dict[str, Any]) -> str:
    return "|".join([
        str(row.get("WRTTIME_IDTFR_ID", "")),
        str(row.get("GRP_ID", "")),
        str(row.get("CLS_ID", "")),
        str(row.get("ITM_ID", "")),
        str(row.get("DTA_VAL", "")),
    ])


def _page_signature(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    return "#".join(_row_unique_key(r) for r in rows[:3])


def _parse_wrttime(value: str) -> Any:
    text = str(value).strip()
    if not text:
        return pd.NaT
    if len(text) == 6 and text.isdigit():
        return pd.to_datetime(text + "01", format="%Y%m%d", errors="coerce")
    if len(text) == 8 and text.isdigit():
        return pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    if len(text) == 4 and text.isdigit():
        return pd.to_datetime(text + "0101", format="%Y%m%d", errors="coerce")
    if len(text) == 6 and text[:4].isdigit() and text[4].upper() == "Q" and text[5].isdigit():
        month = int(text[5]) * 3
        return pd.to_datetime(f"{text[:4]}{month:02d}01", format="%Y%m%d", errors="coerce")
    return pd.to_datetime(text, errors="coerce")


def _validate_reb_collection_range(
    df: pd.DataFrame,
    start_wrttime: str | None,
    end_wrttime: str | None,
) -> None:
    if df.empty or not end_wrttime or "date" not in df.columns:
        return

    expected_end = _parse_wrttime(end_wrttime)
    actual_end = pd.to_datetime(df["date"], errors="coerce").max()
    if pd.isna(expected_end) or pd.isna(actual_end):
        return

    month_gap = (expected_end.year - actual_end.year) * 12 + (expected_end.month - actual_end.month)
    if month_gap < 6:
        return

    latest_wrttime = actual_end.strftime("%Y%m")
    raise ValueError(
        "REB collection appears truncated: "
        f"requested end={end_wrttime}, latest collected={latest_wrttime}. "
        "Increase max_pages or verify the REB statbl_id/dtacycle_cd configuration."
    )


def _parse_ecos_time(value: str) -> Any:
    """ECOS TIME 필드 파싱 (YYYYMM, YYYYMMDD, YYYYQN 등)."""
    text = str(value).strip()
    if not text:
        return pd.NaT
    # YYYYMM
    if len(text) == 6 and text.isdigit():
        return pd.to_datetime(text + "01", format="%Y%m%d", errors="coerce")
    # YYYYMMDD
    if len(text) == 8 and text.isdigit():
        return pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    # YYYY (연간)
    if len(text) == 4 and text.isdigit():
        return pd.to_datetime(text + "0101", format="%Y%m%d", errors="coerce")
    # YYYYQN 분기 (예: 2024Q1)
    if len(text) == 6 and text[4].upper() == "Q" and text[5].isdigit():
        month = int(text[5]) * 3
        return pd.to_datetime(f"{text[:4]}{month:02d}01", format="%Y%m%d", errors="coerce")
    return pd.to_datetime(text, errors="coerce")


def _parse_policy_year_month(value: str) -> Any:
    text = str(value).strip()
    if not text:
        return pd.NaT

    # Examples: 2018.1 / 2018.01 / 2018-01 / 201801
    m = re.match(r"^(\d{4})[^0-9]?(\d{1,2})$", text)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        if 1 <= month <= 12:
            return pd.Timestamp(year=year, month=month, day=1)

    digits = re.sub(r"\D", "", text)
    if len(digits) == 6:
        year = int(digits[:4])
        month = int(digits[4:])
        if 1 <= month <= 12:
            return pd.Timestamp(year=year, month=month, day=1)

    return pd.NaT


def _parse_yyyymm_month_start(value: str | None) -> Any:
    if not value:
        return pd.NaT
    text = str(value).strip()
    if len(text) != 6 or not text.isdigit():
        return pd.NaT
    year = int(text[:4])
    month = int(text[4:])
    if not (1 <= month <= 12):
        return pd.NaT
    return pd.Timestamp(year=year, month=month, day=1)


def _normalize_policy_regulation(value: str) -> str:
    text = str(value).strip()
    if text == "강화":
        return "강화"
    if text == "완화":
        return "완화"
    if text == "유지":
        return "유지"
    return "기타"


def _normalize_policy_region(value: str) -> str:
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    return text


def _normalize_policy_indicator(value: str) -> str:
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"\s+", " ", text)
    if re.fullmatch(r"[A-Za-z0-9_\- ]+", text):
        return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_").upper()
    return text


def _policy_indicator_column_key(value: str) -> str:
    """정책 지표명을 컬럼명용 영문 키로 변환한다."""
    text = _normalize_policy_indicator(value)
    if not text:
        return "unknown"

    translation = {
        "양도세": "capital_gains_tax",
        "취득세": "acquisition_tax",
        "종부세": "comprehensive_real_estate_tax",
        "부동산대책": "real_estate_policy",
        "부동산정책": "real_estate_policy",
        "투기과열지구": "speculative_overheated_district",
        "조정대상지역": "adjustment_target_area",
        "투기지역": "speculative_zone",
    }
    if text in translation:
        return translation[text]

    return _sanitize_feature_name(text)


def _sanitize_feature_name(value: str) -> str:
    raw = re.sub(r"\s+", "", str(value).strip().lower())
    text = re.sub(r"[^a-z0-9_]+", "_", raw)
    text = re.sub(r"_+", "_", text).strip("_")
    if text:
        return text

    # If ASCII slug is empty (e.g., Korean labels), keep unicode word characters.
    text = re.sub(r"[^\w]+", "_", raw, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "unknown"


def _first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None
