"""KB부동산 서울시 구별 아파트 매매·전세 가격지수 수집.

kbreb_api.collect_kbreb_data 를 사용해 서울 25개 구의 월별
매매지수(apt_sale_index)·전세지수(apt_jeonse_index)를 수집한다.
기간: 2010-01 ~ 2026-06 (KB 발표 시점까지 존재).
지수는 기준 2026.1 = 100 의 무단위 값이다.

산출물 (kb/output/):
  seoul_gu_price_index_long.csv   구별·월별 long-format (매매+전세)
  apt_sale_index_wide.csv         timestamp(월) x 25개 구 (매매)
  apt_jeonse_index_wide.csv       timestamp(월) x 25개 구 (전세)

사용:
  python kb/collect_seoul_gu_index.py
  python kb/collect_seoul_gu_index.py --start 2010-01 --end 2026-06
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

try:
    from .kbreb_api import collect_kbreb_data
except ImportError:  # 단독 실행 (python kb/collect_seoul_gu_index.py)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from kb.kbreb_api import collect_kbreb_data

OUT_DIR = Path(__file__).resolve().parent / "output"
LONG_FILE = OUT_DIR / "seoul_gu_price_index_long.csv"
SALE_WIDE_FILE = OUT_DIR / "apt_sale_index_wide.csv"
JEONSE_WIDE_FILE = OUT_DIR / "apt_jeonse_index_wide.csv"

LONG_FIELDS = ["timestamp", "year", "month", "gu", "series_name", "index_value", "wrttime"]

SERIES_LABEL = {"apt_sale_index": "매매", "apt_jeonse_index": "전세"}


def parse_ym(text: str) -> str:
    """'2010-01' / '2010/01' / '201001' -> 'YYYYMM'."""
    t = text.strip().replace("/", "-")
    if "-" in t:
        y, m = t.split("-")[:2]
        return f"{int(y):04d}{int(m):02d}"
    return t


def fmt_value(v: float) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:.6f}".rstrip("0").rstrip(".")


def write_long(records: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with LONG_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LONG_FIELDS)
        w.writeheader()
        w.writerows(records)


def write_wide(records: list[dict], series_name: str, path: Path) -> int:
    subset = [r for r in records if r["series_name"] == series_name]
    if not subset:
        return 0
    timestamps = sorted({r["timestamp"] for r in subset})
    gus = sorted({r["gu"] for r in subset})
    table: dict[str, dict[str, str]] = {ts: {} for ts in timestamps}
    for r in subset:
        table[r["timestamp"]][r["gu"]] = r["index_value"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", *gus])
        for ts in timestamps:
            w.writerow([ts, *(table[ts].get(gu, "") for gu in gus)])
    return len(timestamps)


def main() -> int:
    ap = argparse.ArgumentParser(description="KB 서울 구별 매매·전세 지수 수집")
    ap.add_argument("--start", default="2010-01", help="시작 연-월 (기본 2010-01)")
    ap.add_argument("--end", default="2026-06", help="끝 연-월 (기본 2026-06)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    start_wrttime = parse_ym(args.start)
    end_wrttime = parse_ym(args.end)

    print(f"KB 서울 구별 매매·전세 지수 수집 {args.start} ~ {args.end}\n")
    df = collect_kbreb_data(start_wrttime=start_wrttime, end_wrttime=end_wrttime)

    records: list[dict] = []
    for row in df.itertuples(index=False):
        wrttime = str(row.wrttime)
        records.append({
            "timestamp": f"{wrttime[:4]}-{wrttime[4:6]}",
            "year": int(wrttime[:4]),
            "month": int(wrttime[4:6]),
            "gu": row.cls_nm,
            "series_name": row.series_name,
            "index_value": fmt_value(row.value),
            "wrttime": wrttime,
        })

    records.sort(key=lambda r: (r["series_name"], r["gu"], r["timestamp"]))
    write_long(records)

    n_sale = write_wide(records, "apt_sale_index", SALE_WIDE_FILE)
    n_jeonse = write_wide(records, "apt_jeonse_index", JEONSE_WIDE_FILE)

    gus = sorted({r["gu"] for r in records})
    months = sorted({r["timestamp"] for r in records})
    for sname, label in SERIES_LABEL.items():
        cnt = sum(1 for r in records if r["series_name"] == sname)
        print(f"  [OK] {label}지수({sname}) {cnt}행")

    print(f"\n완료. 총 {len(records)}행 | 구 {len(gus)}개 | 기간 {months[0]}~{months[-1]}")
    print(f"  long      : {LONG_FILE}")
    print(f"  매매 wide : {SALE_WIDE_FILE} ({n_sale}개월)")
    print(f"  전세 wide : {JEONSE_WIDE_FILE} ({n_jeonse}개월)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
