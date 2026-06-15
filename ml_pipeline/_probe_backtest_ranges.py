# -*- coding: utf-8 -*-
"""백테스트 5개 단지의 목표 평형 파일 선정 + 데이터 시작/종료/행수 확인."""
import csv
import glob
import os
import re

BASE = os.path.join("meta_ml", "output", "서울특별시")

# (구, 동, 파일 glob 패턴) — 규칙 통일: 단지별 데이터 최장 평형 1개
TARGETS = [
    ("강남구", "압구정동", "현대6,7차_*.csv"),
    ("강동구", "암사동", "강동롯데캐슬퍼스트_*.csv"),
    ("마포구", "아현동", "마포래미안푸르지오_*.csv"),
    ("서초구", "반포동", "래미안퍼스티지_*.csv"),
    ("송파구", "잠실동", "잠실주공5단지_*.csv"),
]


def read_range(path):
    months = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        ts_col = None
        for c in r.fieldnames:
            if c.strip().lower() in ("header_timestamp", "timestamp"):
                ts_col = c
                break
        if ts_col is None:
            ts_col = r.fieldnames[0]
        for row in r:
            v = (row.get(ts_col) or "").strip()
            if v:
                months.append(v[:7])  # YYYY-MM
    months = sorted(set(months))
    return (months[0] if months else None,
            months[-1] if months else None,
            len(months))


def pick(dirpath, pattern):
    paths = glob.glob(os.path.join(dirpath, pattern))
    if not paths:
        return None, []
    info = []
    for p in paths:
        lo, hi, n = read_range(p)
        info.append((os.path.basename(p), lo, hi, n, p))
    chosen = max(info, key=lambda x: x[3])  # 행(개월) 수 최대 = 최장 시계열
    return chosen, info


print("=" * 90)
for gu, dong, pat in TARGETS:
    dirpath = os.path.join(BASE, gu, dong)
    chosen, info = pick(dirpath, pat)
    print(f"\n[{gu} {dong}]  pattern={pat}")
    for name, lo, hi, n, _ in sorted(info, key=lambda x: -x[3])[:8]:
        mark = " <== 선정(최장)" if chosen and name == chosen[0] else ""
        print(f"   {name:45s} {lo} ~ {hi}  ({n}개월){mark}")

print("\n" + "=" * 90)
print("선정 요약 (공통 구간 산정용):")
starts, ends = [], []
for gu, dong, pat in TARGETS:
    dirpath = os.path.join(BASE, gu, dong)
    chosen, _ = pick(dirpath, pat)
    if chosen:
        name, lo, hi, n, _ = chosen
        starts.append(lo)
        ends.append(hi)
        print(f"   {gu} {dong:8s} {name:45s} {lo} ~ {hi} ({n}개월)")
print(f"\n   공통 시작(가장 늦은 start) = {max(starts)}")
print(f"   공통 종료(가장 이른 end)   = {min(ends)}")
