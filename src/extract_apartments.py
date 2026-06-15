"""
1단계: data/sample 내 2010~2025 실거래가 CSV에서 F열(단지명)을 추출하여
서울시 전체 아파트 단지명 목록(중복 제거)을 output/apartments.csv 로 저장한다.

CSV 특징:
- 인코딩: EUC-KR(cp949)
- 실제 컬럼 헤더 위에 안내문/검색조건 메타데이터 라인이 존재
- 헤더는 "NO","시군구","번지",... 로 시작하며 F열(6번째) = "단지명"
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

# 프로젝트 루트 기준 경로
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "sample"
OUTPUT_DIR = ROOT / "output"
OUTPUT_FILE = OUTPUT_DIR / "apartments.csv"

ENCODING = "cp949"  # EUC-KR 상위호환
APT_COLUMN = "단지명"  # F열


def find_header_index(rows: list[list[str]]) -> int:
    """실제 데이터 헤더("NO","시군구",...) 가 위치한 행 인덱스를 찾는다."""
    for i, row in enumerate(rows):
        if row and row[0].strip() == "NO" and APT_COLUMN in row:
            return i
    raise ValueError("데이터 헤더 행(NO, ... , 단지명)을 찾지 못했습니다.")


def extract_from_file(path: Path) -> set[str]:
    """단일 CSV에서 단지명 집합을 추출한다."""
    names: set[str] = set()
    with path.open("r", encoding=ENCODING, errors="replace", newline="") as f:
        rows = list(csv.reader(f))

    header_idx = find_header_index(rows)
    header = [c.strip() for c in rows[header_idx]]
    apt_idx = header.index(APT_COLUMN)

    for row in rows[header_idx + 1 :]:
        if len(row) <= apt_idx:
            continue
        name = row[apt_idx].strip()
        if name and name != "-":
            names.add(name)
    return names


def main() -> int:
    if not DATA_DIR.exists():
        print(f"[오류] 데이터 폴더가 없습니다: {DATA_DIR}", file=sys.stderr)
        return 1

    csv_files = sorted(DATA_DIR.glob("*.csv"))
    if not csv_files:
        print(f"[오류] CSV 파일이 없습니다: {DATA_DIR}", file=sys.stderr)
        return 1

    all_names: set[str] = set()
    for path in csv_files:
        try:
            names = extract_from_file(path)
            all_names |= names
            print(f"  - {path.name}: {len(names):>6}개 단지 (누적 {len(all_names)})")
        except Exception as exc:  # noqa: BLE001
            print(f"[경고] {path.name} 처리 실패: {exc}", file=sys.stderr)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sorted_names = sorted(all_names)
    with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["apt_name"])
        for name in sorted_names:
            writer.writerow([name])

    print(f"\n완료: 고유 단지명 {len(sorted_names)}개 -> {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
