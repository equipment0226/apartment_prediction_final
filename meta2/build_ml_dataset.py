"""3단계: ML 분석용 매물 단위 시세 데이터셋 적재.

입력
----
1. output/downloads/<구>/<단지코드>_<단지명>/<면적일련번호>_<전용면적>.xlsx
   - KB 과거시세 엑셀. 헤더 블록 + 월별 시계열.
   - 헤더 블록:  '대표번지'(→시/구/동),  '공급/전용면적'(→전용면적)
   - 시계열:     A열 '시세기준월'(YYYYMM),  C열 매매가 '일반평균가'
   - 전용면적은 파일명(<면적일련번호>_<전용면적>.xlsx)에서도 추출 가능.
2. naverland/output/complex_metadata.csv
   - source_complex_no == 다운로드 폴더의 단지코드 (정확 일치 조인 키)
   - match_status == 'no_seoul_match' 인 단지는 수집 실패/정보 없음 → 생성하지 않음
3. meta1/seoul_region_lookup.json  (시/구/동 정규 분류)

산출물
------
meta2/output/<시>/<구>/<동>/<단지명>_<전용면적>.csv
  컬럼: Timestamp, 시, 구, 동, 아파트명, 전용면적, 시세,
        세대수, 건설사, 초등학교, 인근역, 철도호재, 개발호재
  - Timestamp / 시세 시계열에 단지 메타데이터를 행마다 broadcast.
  - 철도호재 = developments 의 '[철도]' 항목,  개발호재 = 그 외('[지구]' 등) 항목.
  - ',' 로 여러 개인 값(건설사 등)은 원본 그대로 유지.

사용:
  python meta2/build_ml_dataset.py
  python meta2/build_ml_dataset.py --limit 5   # 단지 5개만(테스트)
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import warnings
from pathlib import Path

import openpyxl

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

ROOT = Path(__file__).resolve().parents[1]
DOWNLOAD_DIR = ROOT / "output" / "downloads"
META_CSV = ROOT / "naverland" / "output" / "complex_metadata.csv"
LOOKUP_FILE = ROOT / "meta1" / "seoul_region_lookup.json"
OUT_DIR = Path(__file__).resolve().parent / "output"

SI = "서울특별시"

OUT_FIELDS = [
    "Timestamp", "시", "구", "동", "아파트명", "전용면적", "시세",
    "준공년도", "세대수", "건설사", "초등학교", "인근역", "철도호재", "개발호재",
]

# 시세 시계열 헤더의 라벨(이 행 다음부터 데이터)
PRICE_HEADER_LABEL = "시세기준월"
ADDR_LABEL = "대표번지"
AREA_LABEL = "공급/전용면적"


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "")
    name = name.strip().rstrip(".")
    return name[:80] or "_"


def load_lookup() -> dict[str, set[str]]:
    """{구: {동, ...}} 형태로 서울 정규 시/구/동 로드."""
    data = json.loads(LOOKUP_FILE.read_text(encoding="utf-8"))
    si = next(iter(data))
    return {gu: set(dongs) for gu, dongs in data[si].items()}


def load_metadata() -> dict[str, dict[str, str]]:
    """source_complex_no -> metadata row (no_seoul_match 포함, 필터는 호출부에서)."""
    meta: dict[str, dict[str, str]] = {}
    if not META_CSV.exists():
        print(f"[오류] {META_CSV} 가 없습니다.", file=sys.stderr)
        sys.exit(1)
    with META_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = str(row.get("source_complex_no") or "").strip()
            if key:
                meta[key] = row
    return meta


def split_developments(dev: str) -> tuple[str, str]:
    """developments 컬럼을 (철도호재, 개발호재) 로 분리.

    '[철도] ... | [지구] ... | ...' 형태. ' | ' 기준 분리 후
    '[철도]' 로 시작하면 철도, 그 외('[지구]' 등)는 개발.
    """
    if not dev:
        return "", ""
    rail, devel = [], []
    for item in str(dev).split(" | "):
        item = item.strip()
        if not item:
            continue
        if item.startswith("[철도]"):
            rail.append(item)
        else:
            devel.append(item)
    return " | ".join(rail), " | ".join(devel)


def parse_gu_dong(addr: str) -> tuple[str | None, str | None]:
    """'서울특별시 송파구 가락동 913' -> ('송파구', '가락동').

    구는 '구' 로, 동은 '동' 을 포함하는 토큰(예: 가락동, 성수동1가)으로 인식.
    """
    if not addr:
        return None, None
    toks = str(addr).replace("\r", " ").replace("\n", " ").split()
    gu = dong = None
    for t in toks:
        if gu is None and t.endswith("구"):
            gu = t
            continue
        if gu is not None and dong is None and "동" in t:
            dong = t
            break
    return gu, dong


def exclusive_from_filename(fname: str) -> str:
    """'190446_59.96.xlsx' -> '59.96'."""
    stem = Path(fname).stem
    parts = stem.split("_", 1)
    return parts[1] if len(parts) == 2 else stem


# ---------------------------------------------------------------------------
# xlsx 파싱
# ---------------------------------------------------------------------------
def parse_price_xlsx(path: Path) -> tuple[dict[str, str], list[tuple[str, float]]]:
    """KB 시세 xlsx -> (헤더필드, [(YYYYMM, 일반평균가), ...])."""
    # 일부 파일은 dimension 메타가 깨져 있어(A1:A1) read_only 모드에서 행을
    # 읽지 못한다. 일반 모드로 로드한다(파일이 작아 부담 없음).
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    header: dict[str, str] = {}
    series: list[tuple[str, float]] = []

    in_series = False
    for row in ws.iter_rows(values_only=True):
        a = row[0] if len(row) > 0 else None
        a_str = str(a).strip() if a is not None else ""

        if not in_series:
            # 헤더 블록: A=구분 라벨, B=내용
            b = row[1] if len(row) > 1 else None
            if a_str.startswith(PRICE_HEADER_LABEL):
                in_series = True
                continue
            if a_str in (ADDR_LABEL, AREA_LABEL) and b is not None:
                header[a_str] = str(b).strip()
            continue

        # 시계열: A=YYYYMM, C(index2)=매매 일반평균가
        if not re.fullmatch(r"\d{6}", a_str):
            continue
        price = row[2] if len(row) > 2 else None
        if price is None or price == "":
            continue
        try:
            series.append((a_str, float(price)))
        except (TypeError, ValueError):
            continue

    wb.close()
    return header, series


def ym_to_timestamp(ym: str) -> str:
    """'202606' -> '2026-06-01'."""
    return f"{ym[:4]}-{ym[4:6]}-01"


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def build(args: argparse.Namespace) -> None:
    lookup = load_lookup()
    metadata = load_metadata()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    stats = {
        "complexes": 0, "skipped_no_meta": 0, "skipped_no_seoul": 0,
        "files": 0, "skipped_empty": 0, "skipped_addr": 0,
    }

    complex_dirs: list[Path] = []
    for gu_dir in sorted(p for p in DOWNLOAD_DIR.iterdir() if p.is_dir()):
        complex_dirs.extend(sorted(p for p in gu_dir.iterdir() if p.is_dir()))
    if args.limit:
        complex_dirs = complex_dirs[: args.limit]

    for cdir in complex_dirs:
        complex_no = cdir.name.split("_", 1)[0]
        folder_name = cdir.name.split("_", 1)[1] if "_" in cdir.name else cdir.name

        meta = metadata.get(complex_no)
        if meta is None:
            stats["skipped_no_meta"] += 1
            continue
        if str(meta.get("match_status") or "").strip() == "no_seoul_match":
            stats["skipped_no_seoul"] += 1
            continue

        apt_name = meta.get("complex_name") or folder_name
        rail, devel = split_developments(meta.get("developments") or "")
        approval = str(meta.get("use_approval_date") or "").strip()
        approval_year = approval[:4] if re.match(r"\d{4}", approval) else ""
        meta_cols = {
            "아파트명": apt_name,
            "준공년도": approval_year,
            "세대수": meta.get("total_household") or "",
            "건설사": meta.get("construction_company") or "",
            "초등학교": meta.get("elementary_schools") or "",
            "인근역": meta.get("subways") or "",
            "철도호재": rail,
            "개발호재": devel,
        }

        produced_any = False
        used_names: set[str] = set()

        for xlsx in sorted(cdir.glob("*.xlsx")):
            header, series = parse_price_xlsx(xlsx)
            if not series:
                stats["skipped_empty"] += 1
                continue

            gu, dong = parse_gu_dong(header.get(ADDR_LABEL, ""))
            if gu is None or dong is None or gu not in lookup:
                stats["skipped_addr"] += 1
                continue

            exclusive = exclusive_from_filename(xlsx.name)

            # 출력 경로: meta2/output/<시>/<구>/<동>/<단지명>_<전용면적>.csv
            out_dir = OUT_DIR / sanitize(SI) / sanitize(gu) / sanitize(dong)
            out_dir.mkdir(parents=True, exist_ok=True)
            base = sanitize(f"{apt_name}_{exclusive}")
            fname = base
            # 동일 전용면적이 여러 평형(면적일련번호)으로 존재 → 충돌 시 일련번호 부가
            if fname in used_names:
                area_no = xlsx.name.split("_", 1)[0]
                fname = sanitize(f"{apt_name}_{exclusive}_{area_no}")
            used_names.add(fname)
            out_path = out_dir / f"{fname}.csv"

            with out_path.open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
                w.writeheader()
                for ym, price in series:
                    w.writerow({
                        "Timestamp": ym_to_timestamp(ym),
                        "시": SI, "구": gu, "동": dong,
                        "전용면적": exclusive, "시세": price,
                        **meta_cols,
                    })

            stats["files"] += 1
            produced_any = True

        if produced_any:
            stats["complexes"] += 1

    print(
        "\n완료.\n"
        f"  생성 단지: {stats['complexes']}개 / 매물 파일: {stats['files']}개\n"
        f"  스킵 - 메타없음 {stats['skipped_no_meta']} / "
        f"no_seoul_match {stats['skipped_no_seoul']} / "
        f"시세없음 {stats['skipped_empty']} / 주소파싱실패 {stats['skipped_addr']}\n"
        f"  산출 위치: {OUT_DIR}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="meta2 ML 데이터셋 빌더")
    ap.add_argument("--limit", type=int, default=None, help="처리할 단지 수 제한(테스트용)")
    build(ap.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
