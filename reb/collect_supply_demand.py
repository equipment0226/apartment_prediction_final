"""REB(한국부동산원) 아파트 수급동향 3개 지표를 수집해 CSV로 저장.

대상 지표 (stat_catalog.DEFAULT_FEATURE_SERIES):
  - apt_sale_supply_demand          (A_2024_00076) 매매수급동향_아파트
  - apt_jeonse_supply_demand        (A_2024_00077) 전세수급동향_아파트
  - apt_monthly_rent_supply_demand  (A_2024_00078) 월세수급동향_아파트

기간: 2010-01 ~ 2026-06 (실제 데이터는 발표 시점부터 존재)
지역: REB가 제공하는 전 지역(전국/시도/권역 등)을 그대로 포함하며
      각 행에 region(CLS_NM) / region_full(CLS_FULLNM) 을 둔다.

산출물 (reb/output/):
  apt_sale_supply_demand.csv
  apt_jeonse_supply_demand.csv
  apt_monthly_rent_supply_demand.csv
  reb_supply_demand_long.csv        (3개 지표 통합 long-format)

사용:
  python reb/collect_supply_demand.py
  python reb/collect_supply_demand.py --start 2010-01 --end 2026-06
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path
from typing import Any

try:
    from .config import load_settings
    from .reb_api import REBApiClient, REBApiError
    from .stat_catalog import DEFAULT_FEATURE_SERIES, SeriesConfig
except ImportError:  # 단독 실행 (python reb/collect_supply_demand.py)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from reb.config import load_settings
    from reb.reb_api import REBApiClient, REBApiError
    from reb.stat_catalog import DEFAULT_FEATURE_SERIES, SeriesConfig

OUT_DIR = Path(__file__).resolve().parent / "output"
LONG_FILE = OUT_DIR / "reb_supply_demand_long.csv"

FIELDS = [
    "timestamp",      # YYYY-MM
    "year", "month",
    "series_name",
    "statbl_id",
    "region",         # CLS_NM (도심권/서울/전국 등)
    "region_full",    # CLS_FULLNM (서울>강북지역>도심권 등)
    "item_name",      # ITM_NM (지수 등)
    "unit",           # UI_NM
    "value",
    "wrttime",        # 원본 WRTTIME_IDTFR_ID
]

MAX_PAGES = 1000


def parse_ym(text: str) -> str:
    """'2010-01' / '2010/01' / '201001' -> 'YYYYMM'."""
    t = text.strip().replace("/", "-")
    if "-" in t:
        y, m = t.split("-")[:2]
        return f"{int(y):04d}{int(m):02d}"
    return t


def ts_from_wrttime(wrttime: str) -> tuple[str, int, int]:
    t = (wrttime or "").strip()
    if len(t) == 6 and t.isdigit():
        return f"{t[:4]}-{t[4:6]}", int(t[:4]), int(t[4:6])
    return t, 0, 0


def to_value(raw: Any) -> str:
    if raw is None:
        return ""
    try:
        f = float(raw)
        return str(int(f)) if f.is_integer() else str(f)
    except (ValueError, TypeError):
        return str(raw).strip()


def fetch_series(
    client: REBApiClient,
    series: SeriesConfig,
    start_wrttime: str,
    end_wrttime: str,
) -> list[dict]:
    """한 지표의 전 페이지를 수집해 정규화한 행 목록을 반환한다."""
    collected: list[dict] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        try:
            rows = client.fetch_table_data(
                statbl_id=series.statbl_id,
                dtacycle_cd=series.dtacycle_cd,
                page_index=page,
                itm_id=series.itm_id,
                start_wrttime=start_wrttime,
                end_wrttime=end_wrttime,
            )
        except REBApiError as exc:
            logging.warning("[reb] page error series=%s page=%s err=%s", series.name, page, exc)
            break
        if not rows:
            break

        for r in rows:
            wrttime = str(r.get("WRTTIME_IDTFR_ID", ""))
            ts, year, month = ts_from_wrttime(wrttime)
            key = "|".join([wrttime, str(r.get("CLS_ID", "")), str(r.get("ITM_ID", ""))])
            if key in seen:
                continue
            seen.add(key)
            collected.append({
                "timestamp": ts,
                "year": year,
                "month": month,
                "series_name": series.name,
                "statbl_id": series.statbl_id,
                "region": r.get("CLS_NM") or "",
                "region_full": r.get("CLS_FULLNM") or "",
                "item_name": r.get("ITM_NM") or "",
                "unit": r.get("UI_NM") or "",
                "value": to_value(r.get("DTA_VAL")),
                "wrttime": wrttime,
            })

        if len(rows) < client.settings.page_size:
            break

    collected.sort(key=lambda x: (x["region_full"], x["timestamp"]))
    return collected


def write_csv(path: Path, records: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)


def main() -> int:
    ap = argparse.ArgumentParser(description="REB 아파트 수급동향 3개 지표 수집")
    ap.add_argument("--start", default="2010-01", help="시작 연-월 (기본 2010-01)")
    ap.add_argument("--end", default="2026-06", help="끝 연-월 (기본 2026-06)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    start_wrttime = parse_ym(args.start)
    end_wrttime = parse_ym(args.end)

    client = REBApiClient(load_settings())
    all_records: list[dict] = []

    print(f"REB 수급동향 수집 {args.start} ~ {args.end} (지표 {len(DEFAULT_FEATURE_SERIES)}개)\n")
    for series in DEFAULT_FEATURE_SERIES:
        records = fetch_series(client, series, start_wrttime, end_wrttime)
        if not records:
            print(f"  [실패] {series.name}: 데이터 없음")
            continue
        per_file = OUT_DIR / f"{series.name}.csv"
        write_csv(per_file, records)
        all_records.extend(records)
        months = sorted({r["timestamp"] for r in records})
        regions = len({r["region_full"] for r in records})
        print(
            f"  [OK] {series.name:30s} {len(records):5d}행  "
            f"({months[0]}~{months[-1]}, 지역 {regions}개)  -> {per_file.name}"
        )

    if not all_records:
        print("\n수집된 데이터가 없습니다.")
        return 1

    all_records.sort(key=lambda r: (r["series_name"], r["region_full"], r["timestamp"]))
    write_csv(LONG_FILE, all_records)

    print(f"\n완료. 총 {len(all_records)}행")
    print(f"  통합 long: {LONG_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
