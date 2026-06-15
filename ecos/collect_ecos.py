"""ECOS 카탈로그 지표를 2010-01 ~ 2026-06 기간으로 수집해 CSV로 저장.

- ecos_catalog.DEFAULT_ECOS_SERIES 의 각 지표를 조회한다.
- 주기(cycle)에 맞춰 조회 구간을 만든다(월=MM, 분기=QQ, 연=YY).
- timestamp(월 기준)와 항목(item)별로 정리한 long-format 테이블을 만들고,
  보기 편한 wide-format(피벗) 테이블도 함께 저장한다.

산출물:
  ecos/output/ecos_data_long.csv   (timestamp/항목별 1행)
  ecos/output/ecos_data_wide.csv   (timestamp x 지표 피벗)

사용:
  python ecos/collect_ecos.py
  python ecos/collect_ecos.py --start 2010-01 --end 2026-06
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

# 패키지(상대 import) / 단독 실행(직접 import) 모두 지원
try:
    from .config import load_settings
    from .ecos_api import EcosApiClient, EcosApiError
    from .ecos_catalog import DEFAULT_ECOS_SERIES, EcosSeriesConfig
except ImportError:  # 단독 실행 (python ecos/collect_ecos.py)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ecos.config import load_settings
    from ecos.ecos_api import EcosApiClient, EcosApiError
    from ecos.ecos_catalog import DEFAULT_ECOS_SERIES, EcosSeriesConfig

OUT_DIR = Path(__file__).resolve().parent / "output"
LONG_FILE = OUT_DIR / "ecos_data_long.csv"
WIDE_FILE = OUT_DIR / "ecos_data_wide.csv"

LONG_FIELDS = [
    "timestamp",       # 월 기준 YYYY-MM
    "year", "month",
    "frequency",       # M / Q / A
    "name",            # 내부 식별 이름
    "description",     # 한글 설명
    "stat_code",
    "item_code1", "item_name1",
    "item_code2", "item_name2",
    "unit",
    "value",
    "interpolated",    # 1 = 결측 보간값, 0 = 원본
    "time_raw",        # ECOS 원본 TIME (201001, 2010Q1 등)
]

CYCLE_LABEL = {"M": "M", "Q": "Q", "A": "A"}


# ---------------------------------------------------------------------------
# 조회 구간 생성
# ---------------------------------------------------------------------------
def build_range(cycle: str, start_ym: tuple[int, int], end_ym: tuple[int, int]) -> tuple[str, str]:
    """주기에 맞는 ECOS 조회 시작/끝 문자열을 만든다."""
    (sy, sm), (ey, em) = start_ym, end_ym
    cy = EcosApiClient.CYCLE_MAP.get(cycle.upper(), cycle)
    if cy == "M":
        return f"{sy}{sm:02d}", f"{ey}{em:02d}"
    if cy == "Q":
        sq = (sm - 1) // 3 + 1
        eq = (em - 1) // 3 + 1
        return f"{sy}Q{sq}", f"{ey}Q{eq}"
    if cy == "A":
        return f"{sy}", f"{ey}"
    # 일간 등 기타: 월 포맷으로 시도
    return f"{sy}{sm:02d}", f"{ey}{em:02d}"


# ---------------------------------------------------------------------------
# TIME 정규화 -> (timestamp, year, month)
# ---------------------------------------------------------------------------
def normalize_time(time_raw: str) -> tuple[str, int, int]:
    t = (time_raw or "").strip()
    if "Q" in t:  # 분기 2010Q1
        y, q = t.split("Q")
        year = int(y)
        month = (int(q) - 1) * 3 + 1
    elif len(t) == 6:  # 월 201001
        year = int(t[:4])
        month = int(t[4:6])
    elif len(t) == 4:  # 연 2010
        year = int(t)
        month = 1
    elif len(t) == 8:  # 일 20100101
        year = int(t[:4])
        month = int(t[4:6])
    else:
        return t, 0, 0
    return f"{year:04d}-{month:02d}", year, month


def to_value(raw: str | None) -> str:
    if raw is None:
        return ""
    raw = str(raw).strip()
    if raw == "":
        return ""
    try:
        f = float(raw)
        return str(int(f)) if f.is_integer() else str(f)
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
def collect_series(
    client: EcosApiClient,
    series: EcosSeriesConfig,
    start_ym: tuple[int, int],
    end_ym: tuple[int, int],
) -> list[dict]:
    start_date, end_date = build_range(series.cycle_type, start_ym, end_ym)
    freq = EcosApiClient.CYCLE_MAP.get(series.cycle_type.upper(), series.cycle_type)
    rows = client.fetch_all_pages(
        stat_code=series.stat_code,
        cycle_type=series.cycle_type,
        start_date=start_date,
        end_date=end_date,
        item_code1=series.item_code1,
        item_code2=series.item_code2,
        item_code3=series.item_code3,
        item_code4=series.item_code4,
    )
    records: list[dict] = []
    for r in rows:
        ts, year, month = normalize_time(r.get("TIME", ""))
        records.append({
            "timestamp": ts,
            "year": year,
            "month": month,
            "frequency": CYCLE_LABEL.get(freq, freq),
            "name": series.name,
            "description": series.description,
            "stat_code": series.stat_code,
            "item_code1": r.get("ITEM_CODE1") or "",
            "item_name1": r.get("ITEM_NAME1") or "",
            "item_code2": r.get("ITEM_CODE2") or "",
            "item_name2": r.get("ITEM_NAME2") or "",
            "unit": r.get("UNIT_NAME") or "",
            "value": to_value(r.get("DATA_VALUE")),
            "time_raw": r.get("TIME") or "",
        })
    return records


def write_long(records: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with LONG_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LONG_FIELDS)
        w.writeheader()
        for rec in records:
            w.writerow(rec)


# ---------------------------------------------------------------------------
# 월 그리드 / 선형 보간
# ---------------------------------------------------------------------------
def month_grid(start_ym: tuple[int, int], end_ym: tuple[int, int]) -> list[str]:
    (sy, sm), (ey, em) = start_ym, end_ym
    out: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _linear_fill(n: int, xs: list[int], ys: list[float]) -> list[float | None]:
    """관측 인덱스 xs/값 ys 를 바탕으로 길이 n 시계열을 선형 보간한다.

    - 내부 결측: 양옆 관측값으로 선형 보간
    - 양 끝(관측 이전/이후): 가장 가까운 관측값으로 채움(상수)
    """
    out: list[float | None] = [None] * n
    if not xs:
        return out
    for x, y in zip(xs, ys):
        out[x] = y
    for k in range(len(xs) - 1):
        x0, x1 = xs[k], xs[k + 1]
        y0, y1 = ys[k], ys[k + 1]
        if x1 - x0 > 1:
            for x in range(x0 + 1, x1):
                out[x] = y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    for x in range(0, xs[0]):
        out[x] = ys[0]
    for x in range(xs[-1] + 1, n):
        out[x] = ys[-1]
    return out


def interpolate_records(all_records: list[dict], grid: list[str]) -> list[dict]:
    """지표별로 월 그리드 전체를 채우고 결측치를 선형 보간한다.

    각 행에 interpolated 플래그(1=보간, 0=원본)를 부여한다.
    """
    groups: dict[tuple, dict] = {}
    for r in all_records:
        key = (r["name"], r["item_code1"], r["item_code2"])
        g = groups.setdefault(key, {"meta": r, "by_ts": {}})
        g["by_ts"][r["timestamp"]] = r

    result: list[dict] = []
    for _key, g in groups.items():
        meta = g["meta"]
        by_ts = g["by_ts"]

        xs: list[int] = []
        ys: list[float] = []
        for i, ts in enumerate(grid):
            rec = by_ts.get(ts)
            if rec and rec["value"] != "":
                try:
                    ys.append(float(rec["value"]))
                    xs.append(i)
                except ValueError:
                    pass  # 비수치 값은 보간 대상에서 제외

        filled = _linear_fill(len(grid), xs, ys)

        for i, ts in enumerate(grid):
            year, month = int(ts[:4]), int(ts[5:7])
            orig = by_ts.get(ts)
            is_orig = orig is not None and orig["value"] != ""
            if is_orig:
                value = orig["value"]
                interpolated = 0
                time_raw = orig.get("time_raw", "")
            else:
                fv = filled[i]
                if fv is None:
                    continue  # 채울 수 없는 경우(관측 전무) 생략
                value = to_value(fv)
                interpolated = 1
                time_raw = ""
            result.append({
                "timestamp": ts,
                "year": year,
                "month": month,
                "frequency": meta["frequency"],
                "name": meta["name"],
                "description": meta["description"],
                "stat_code": meta["stat_code"],
                "item_code1": meta["item_code1"],
                "item_name1": meta["item_name1"],
                "item_code2": meta["item_code2"],
                "item_name2": meta["item_name2"],
                "unit": meta["unit"],
                "value": value,
                "interpolated": interpolated,
                "time_raw": time_raw,
            })
    return result


def write_wide(records: list[dict]) -> None:
    """timestamp(월) 행 x 지표(name) 열 피벗. 같은 지표에 항목이 여러 개면
    name+item_name1 으로 열을 구분한다."""
    # 열 키 결정: 지표별 항목 수 파악
    items_per_name: dict[str, set[str]] = {}
    for rec in records:
        items_per_name.setdefault(rec["name"], set()).add(rec["item_name1"])

    def col_key(rec: dict) -> str:
        if len(items_per_name.get(rec["name"], set())) > 1:
            return f"{rec['name']}|{rec['item_name1']}"
        return rec["name"]

    timestamps = sorted({rec["timestamp"] for rec in records})
    columns: list[str] = []
    seen: set[str] = set()
    for rec in records:
        c = col_key(rec)
        if c not in seen:
            seen.add(c)
            columns.append(c)

    table: dict[str, dict[str, str]] = {ts: {} for ts in timestamps}
    for rec in records:
        table[rec["timestamp"]][col_key(rec)] = rec["value"]

    with WIDE_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", *columns])
        for ts in timestamps:
            w.writerow([ts, *(table[ts].get(c, "") for c in columns)])


def parse_ym(text: str) -> tuple[int, int]:
    text = text.strip().replace("/", "-")
    parts = text.split("-")
    return int(parts[0]), int(parts[1])


def main() -> int:
    ap = argparse.ArgumentParser(description="ECOS 카탈로그 지표 수집")
    ap.add_argument("--start", default="2010-01", help="시작 연-월 (기본 2010-01)")
    ap.add_argument("--end", default="2026-06", help="끝 연-월 (기본 2026-06)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    start_ym = parse_ym(args.start)
    end_ym = parse_ym(args.end)

    client = EcosApiClient(load_settings())
    all_records: list[dict] = []

    print(f"ECOS 수집 {args.start} ~ {args.end} (지표 {len(DEFAULT_ECOS_SERIES)}개)\n")
    for series in DEFAULT_ECOS_SERIES:
        try:
            recs = collect_series(client, series, start_ym, end_ym)
        except EcosApiError as exc:
            print(f"  [실패] {series.name}: {exc}")
            continue
        all_records.extend(recs)
        print(f"  [OK] {series.name:28s} {len(recs):4d}행  ({series.description})")

    if not all_records:
        print("\n수집된 데이터가 없습니다.")
        return 1

    # 월 그리드 전체로 확장 + 결측치 선형 보간
    grid = month_grid(start_ym, end_ym)
    filled_records = interpolate_records(all_records, grid)
    filled_records.sort(key=lambda r: (r["name"], r["timestamp"]))

    write_long(filled_records)
    write_wide(filled_records)

    n_interp = sum(1 for r in filled_records if r["interpolated"] == 1)
    print(f"\n완료. 총 {len(filled_records)}행 (보간 {n_interp}행)")
    print(f"  long: {LONG_FILE}")
    print(f"  wide: {WIDE_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
