"""서울시 구별 월별 메타테이블 빌드.

기준 그리드: 2010-01 ~ 2026-05 (월) × 서울 25개 구 (si=서울특별시).
결측치는 공란으로 둔다.

수집 소스
---------
1. ECOS  (ecos/output/ecos_data_wide.csv)
   base_rate, cd_91d_rate, cpi_housing, m2_avg, mortgage_rate_new, unemployment_rate
   → 시/구 무관 거시지표라 모든 구에 동일 broadcast. 컬럼 ecos__*
2. KB    (kb/output/seoul_gu_price_index_long.csv)
   구별 apt_sale_index / apt_jeonse_index → reb__apt_sale_index / reb__apt_jeonse_index
3. REB   (reb/output/reb_supply_demand_long.csv)
   5개 권역 수급동향 3종 → 권역→구 전개. reb__apt_{sale,jeonse,monthly_rent}_supply_demand
4. Policy (policy/seoul_finance_policy_2010_2026.xlsx, seoul_policy_history_2010_2026.xlsx)
   구별 월별 규제지표 → policy__* 컬럼 그대로 병합

산출물: meta1/output/seoul_gu_meta_table.csv
사용:   python meta1/build_meta_table.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
META_DIR = Path(__file__).resolve().parent
LOOKUP_FILE = META_DIR / "seoul_region_lookup.json"
OUT_DIR = META_DIR / "output"
OUT_FILE = OUT_DIR / "seoul_gu_meta_table.csv"

ECOS_WIDE = ROOT / "ecos" / "output" / "ecos_data_wide.csv"
KB_LONG = ROOT / "kb" / "output" / "seoul_gu_price_index_long.csv"
REB_LONG = ROOT / "reb" / "output" / "reb_supply_demand_long.csv"
POLICY_FINANCE = ROOT / "policy" / "seoul_finance_policy_2010_2026.xlsx"
POLICY_HISTORY = ROOT / "policy" / "seoul_policy_history_2010_2026.xlsx"

START = "2010-01"
END = "2026-05"

ECOS_COLS = [
    "base_rate", "cd_91d_rate", "cpi_housing",
    "m2_avg", "mortgage_rate_new", "unemployment_rate",
]

# REB 5개 권역 → 구 전개
KWONYEOK_TO_GU = {
    "도심권": ["종로구", "중구", "용산구"],
    "동북권": ["성동구", "광진구", "동대문구", "중랑구", "성북구", "강북구", "도봉구", "노원구"],
    "서북권": ["은평구", "서대문구", "마포구"],
    "서남권": ["양천구", "강서구", "구로구", "금천구", "영등포구", "동작구", "관악구"],
    "동남권": ["서초구", "강남구", "송파구", "강동구"],
}


def month_range(start: str, end: str) -> list[str]:
    periods = pd.period_range(start=start, end=end, freq="M")
    return [str(p) for p in periods]  # 'YYYY-MM'


def ts_full(ym: str) -> str:
    return f"{ym}-01"


def load_gu_list() -> tuple[str, list[str]]:
    data = json.loads(LOOKUP_FILE.read_text(encoding="utf-8"))
    si = next(iter(data))
    return si, list(data[si].keys())


def build() -> pd.DataFrame:
    si, gu_list = load_gu_list()
    months = month_range(START, END)

    # 기준 그리드
    grid = pd.MultiIndex.from_product([months, gu_list], names=["ym", "gu"]).to_frame(index=False)
    grid["si"] = si

    # ------------------------------------------------------------------
    # 1) ECOS — 전 구 broadcast
    # ------------------------------------------------------------------
    ecos = pd.read_csv(ECOS_WIDE, dtype={"timestamp": str})
    ecos = ecos.rename(columns={"timestamp": "ym"})
    keep = ["ym"] + [c for c in ECOS_COLS if c in ecos.columns]
    ecos = ecos[keep].rename(columns={c: f"ecos__{c}" for c in ECOS_COLS})
    grid = grid.merge(ecos, on="ym", how="left")

    # ------------------------------------------------------------------
    # 2) KB — 구별 매매/전세 지수
    # ------------------------------------------------------------------
    kb = pd.read_csv(KB_LONG, dtype={"timestamp": str})
    kb = kb.rename(columns={"timestamp": "ym"})
    kb_wide = kb.pivot_table(
        index=["ym", "gu"], columns="series_name", values="index_value", aggfunc="first"
    ).reset_index()
    kb_wide = kb_wide.rename(columns={
        "apt_sale_index": "reb__apt_sale_index",
        "apt_jeonse_index": "reb__apt_jeonse_index",
    })
    grid = grid.merge(kb_wide, on=["ym", "gu"], how="left")

    # ------------------------------------------------------------------
    # 3) REB — 권역 수급동향 → 구 전개
    # ------------------------------------------------------------------
    reb = pd.read_csv(REB_LONG, dtype={"timestamp": str})
    reb = reb.rename(columns={"timestamp": "ym"})
    reb = reb[reb["region"].isin(KWONYEOK_TO_GU.keys())].copy()
    gu_rows = []
    for kwon, gus in KWONYEOK_TO_GU.items():
        sub = reb[reb["region"] == kwon]
        for gu in gus:
            tmp = sub[["ym", "series_name", "value"]].copy()
            tmp["gu"] = gu
            gu_rows.append(tmp)
    reb_gu = pd.concat(gu_rows, ignore_index=True)
    reb_wide = reb_gu.pivot_table(
        index=["ym", "gu"], columns="series_name", values="value", aggfunc="first"
    ).reset_index()
    reb_wide = reb_wide.rename(columns={
        "apt_sale_supply_demand": "reb__apt_sale_supply_demand",
        "apt_jeonse_supply_demand": "reb__apt_jeonse_supply_demand",
        "apt_monthly_rent_supply_demand": "reb__apt_monthly_rent_supply_demand",
    })
    grid = grid.merge(reb_wide, on=["ym", "gu"], how="left")

    # ------------------------------------------------------------------
    # 4) Policy — 구별 월별 규제지표
    # ------------------------------------------------------------------
    for path in (POLICY_FINANCE, POLICY_HISTORY):
        pol = pd.read_excel(path)
        pol["ym"] = pd.to_datetime(pol["timestamp"]).dt.strftime("%Y-%m")
        pol = pol.drop(columns=["timestamp"])
        pol_cols = [c for c in pol.columns if c.startswith("policy__")]
        grid = grid.merge(pol[["ym", "gu", *pol_cols]], on=["ym", "gu"], how="left")

    # ------------------------------------------------------------------
    # 마무리: timestamp 포맷 / 컬럼 순서 / 정렬
    # ------------------------------------------------------------------
    grid["timestamp"] = grid["ym"].map(ts_full)

    ecos_out = [f"ecos__{c}" for c in ECOS_COLS if f"ecos__{c}" in grid.columns]
    kb_out = [c for c in ["reb__apt_sale_index", "reb__apt_jeonse_index"] if c in grid.columns]
    reb_out = [c for c in [
        "reb__apt_sale_supply_demand",
        "reb__apt_jeonse_supply_demand",
        "reb__apt_monthly_rent_supply_demand",
    ] if c in grid.columns]
    policy_out = [c for c in grid.columns if c.startswith("policy__")]

    ordered = ["timestamp", "si", "gu", *ecos_out, *kb_out, *reb_out, *policy_out]
    grid = grid[ordered]

    gu_order = {gu: i for i, gu in enumerate(gu_list)}
    grid = grid.sort_values(
        by=["timestamp", "gu"], key=lambda s: s.map(gu_order) if s.name == "gu" else s
    ).reset_index(drop=True)
    return grid


def main() -> int:
    df = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_FILE, index=False, encoding="utf-8-sig")

    n_months = df["timestamp"].nunique()
    n_gu = df["gu"].nunique()
    print(f"메타테이블 생성 완료: {OUT_FILE}")
    print(f"  행 {len(df)} (월 {n_months} × 구 {n_gu})")
    print(f"  컬럼 {len(df.columns)}개: {list(df.columns)}")
    # 결측 현황
    miss = df.isna().sum()
    miss = miss[miss > 0]
    if not miss.empty:
        print("  결측(공란) 컬럼:")
        for c, n in miss.items():
            print(f"    {c}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
