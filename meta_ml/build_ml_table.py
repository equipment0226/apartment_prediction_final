"""4단계: ML(베이스라인+잔차부스팅)용 매물 단위 메타테이블 빌드.

목표
----
meta2 의 매물별 시세 시계열을 ML 입력 형태로 가공한다.
- 정적 식별자(시/구/동/단지/평형)는 Header_ 접두
- 예측 타깃(시세)은 target
- 시간불변 파생변수(준공/규모/건설사/초품아/역세권/호재)는 Static__ 접두
- 거시 외생변수는 적용 단위(depth)별 접두:
    depth1__ : 전국(ECOS)
    depth2__ : 권역(REB 수급동향)
    depth3__ : 구별(REB 지수, 정책)

입력
----
1. meta2/output/<시>/<구>/<동>/<단지명>_<전용면적>.csv
   컬럼: Timestamp, 시, 구, 동, 아파트명, 전용면적, 시세,
         준공년도, 세대수, 건설사, 초등학교, 인근역, 철도호재, 개발호재
2. meta1/output/seoul_gu_meta_table.csv
   (timestamp, si, gu) 그리드 + ecos__* / reb__* / policy__* 컬럼

산출물
------
meta_ml/output/<시>/<구>/<동>/<단지명>_<전용면적>.csv
  동일한 시/구/동/단지 폴더 구조 유지.

사용:
  python meta_ml/build_ml_table.py
  python meta_ml/build_ml_table.py --limit 20   # 매물 20개만(테스트)
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
META2_DIR = ROOT / "meta2" / "output"
GU_META_CSV = ROOT / "meta1" / "output" / "seoul_gu_meta_table.csv"
OUT_DIR = Path(__file__).resolve().parent / "output"

CURRENT_YEAR = 2026

# 분석기간(이 범위의 Timestamp 만 출력, 범위 밖 과거/미래 시세는 제외)
PERIOD_START = "2010-01-01"
PERIOD_END = "2026-05-01"

# ---------------------------------------------------------------------------
# 거시변수: meta1 컬럼 → depth 접두 매핑
# ---------------------------------------------------------------------------
DEPTH1_COLS = [  # 전국(ECOS)
    "ecos__base_rate", "ecos__cd_91d_rate", "ecos__cpi_housing",
    "ecos__m2_avg", "ecos__mortgage_rate_new", "ecos__unemployment_rate",
]
DEPTH2_COLS = [  # 권역(REB 수급동향)
    "reb__apt_sale_supply_demand", "reb__apt_jeonse_supply_demand",
    "reb__apt_monthly_rent_supply_demand",
]
DEPTH3_COLS = [  # 구별(REB 지수 + 정책)
    "reb__apt_sale_index", "reb__apt_jeonse_index",
    "policy__ltv_tightness", "policy__dsr_severity",
    "policy__is_speculative", "policy__is_overheated", "policy__is_regulated",
]

# ---------------------------------------------------------------------------
# 건설사 시공능력평가(2024 기준) 순위 분류. 명단에 없으면 '소형'.
#   대형: 1~10위 / 중형: 11~50위 / 소형: 51~100위 및 그 외
# 표기 변형(브랜드 사명 변경, (주)/주식회사 등)을 흡수하기 위해 정규화 후 부분일치.
# ---------------------------------------------------------------------------
MAJOR_BUILDERS = [  # 1~10위
    "삼성물산", "현대건설", "대우건설", "디엘이앤씨", "지에스건설",
    "현대엔지니어링", "포스코이앤씨", "롯데건설", "에스케이에코플랜트",
    "에이치디씨현대산업개발",
    # 사명 변경/약칭 흡수
    "디엘", "대림산업", "포스코건설", "에스케이건설", "현대산업개발",
]
MID_BUILDERS = [  # 11~50위
    "한화", "호반건설", "디엘건설", "두산에너빌리티", "계룡건설", "서희건설",
    "제일건설", "코오롱글로벌", "태영건설", "케이씨씨건설",
    "우미건설", "대방건설", "쌍용건설", "금호건설", "두산건설", "한신공영",
    "효성중공업", "동부건설", "에이치엘디앤아이한라", "반도건설",
    "호반산업", "동원개발", "신세계건설", "에이치제이중공업", "자이씨앤에이",
    "삼성이앤에이", "비에스한양", "금강주택", "씨제이대한통운", "동양건설산업",
    "에스지씨이앤씨", "중흥토건", "대광건영", "진흥기업", "라인산업", "라인건설",
    "에이치에스화성", "에스케이에코엔지니어링", "성도이엔지", "서한",
]
SMALL_BUILDERS = [  # 51~100위 (명시적으로 소형)
    "남광토건", "대보건설", "동문건설", "태왕이앤씨", "극동건설", "일성건설",
    "케이알산업", "아이에스동서", "경남기업", "양우건설",
    "시티건설", "중흥건설", "모아주택산업", "우미개발", "동원건설산업",
    "대명건설", "일신건영", "신동아건설", "자이에스앤디", "금성백조건설",
    "대림", "대원", "동아지질", "엘티삼보", "금성백조주택", "미래도건설",
    "금광기업", "삼부토건", "원건설", "강산건설",
    "에이스건설", "풍림산업", "흥화", "서해종합건설", "보광종합건설",
    "대방산업개발", "이수건설", "동아건설산업", "씨에이이앤씨", "삼환기업",
    "화성개발", "신원종합개발", "비에스산업", "요진건설산업", "디에스종합건설",
    "영진종합건설", "우암건설", "한양건설", "이안알앤씨", "위본건설",
]


def _norm_builder(name: str) -> str:
    s = re.sub(r"\(주\)|주식회사|㈜|（주）", "", name or "")
    return re.sub(r"[\s()（）]", "", s)


def classify_builder(name: str) -> str:
    """시공능력평가 순위 명단 기준 대형/중형/소형. 명단 외/미상은 '소형'."""
    if not name:
        return "소형"
    norm = _norm_builder(name)
    for kw in MAJOR_BUILDERS:
        if _norm_builder(kw) in norm:
            return "대형"
    for kw in MID_BUILDERS:
        if _norm_builder(kw) in norm:
            return "중형"
    # 51~100위 및 그 외 전부 소형 (SMALL_BUILDERS 는 가독성/문서화용)
    return "소형"


# ---------------------------------------------------------------------------
# 파생 분류기
# ---------------------------------------------------------------------------
def classify_age(approval_year: str) -> str:
    """준공년도 → 신축(≤5) / 준신축(5~10) / 구축(>10). 미상은 공란."""
    if not approval_year or not re.match(r"\d{4}", approval_year):
        return ""
    age = CURRENT_YEAR - int(approval_year[:4])
    if age <= 5:
        return "신축"
    if age <= 10:
        return "준신축"
    return "구축"


def classify_scale(household: str) -> str:
    """세대수 → 대단지(≥1000) / 중단지(500~999) / 소단지(<500). 미상은 공란."""
    if not household:
        return ""
    try:
        n = int(float(str(household).replace(",", "")))
    except (TypeError, ValueError):
        return ""
    if n >= 1000:
        return "대단지"
    if n >= 500:
        return "중단지"
    return "소단지"


def classify_size(exclusive: str) -> str:
    """전용면적(㎡) → 소형(≤60) / 중형(60~85) / 대형(>85). 미상은 공란."""
    try:
        a = float(str(exclusive))
    except (TypeError, ValueError):
        return ""
    if a <= 60:
        return "소형"
    if a <= 85:
        return "중형"
    return "대형"


def is_school_in_complex(schools: str, max_dist_m: int = 500) -> str:
    """초등학교가 있고 가장 가까운 거리가 max_dist_m 이하면 '초품아', 아니면 공란."""
    if not schools:
        return ""
    dists = [int(m) for m in re.findall(r"거리\s*(\d+)\s*m", schools)]
    if dists and min(dists) <= max_dist_m:
        return "초품아"
    return ""


def count_stations(subways: str) -> int:
    """인근역 ' | ' 항목 수 카운트. 없으면 0."""
    if not subways or not subways.strip():
        return 0
    return len([p for p in subways.split(" | ") if p.strip()])


def count_catalysts(rail: str, devel: str) -> int:
    """철도호재 + 개발호재 ' | ' 항목 합산 카운트. 없으면 0."""
    total = 0
    for field in (rail, devel):
        if field and field.strip():
            total += len([p for p in field.split(" | ") if p.strip()])
    return total


# ---------------------------------------------------------------------------
# 거시 메타테이블 로드
# ---------------------------------------------------------------------------
def load_gu_meta() -> dict[tuple[str, str], dict[str, str]]:
    """(timestamp, gu) -> {거시컬럼: 값}."""
    if not GU_META_CSV.exists():
        print(f"[오류] {GU_META_CSV} 가 없습니다. 먼저 meta1 을 빌드하세요.", file=sys.stderr)
        sys.exit(1)
    table: dict[tuple[str, str], dict[str, str]] = {}
    macro_cols = DEPTH1_COLS + DEPTH2_COLS + DEPTH3_COLS
    with GU_META_CSV.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = (str(row.get("timestamp") or "").strip(), str(row.get("gu") or "").strip())
            table[key] = {c: (row.get(c) or "") for c in macro_cols}
    return table


# ---------------------------------------------------------------------------
# 출력 스키마
# ---------------------------------------------------------------------------
HEADER_COLS = ["Header_시", "Header_구", "Header_동", "Header_Timestamp", "Header_단지명", "Header_평형"]
TARGET_COL = "target"
STATIC_COLS = [
    "Static__준공구분", "Static__세대수구분", "Static__평수구분",
    "Static__건설사등급", "Static__초품아여부", "Static__역세권수", "Static__호재수",
]
DEPTH1_OUT = [f"depth1__{c}" for c in DEPTH1_COLS]
DEPTH2_OUT = [f"depth2__{c}" for c in DEPTH2_COLS]
DEPTH3_OUT = [f"depth3__{c}" for c in DEPTH3_COLS]

OUT_FIELDS = (
    HEADER_COLS + [TARGET_COL] + STATIC_COLS + DEPTH1_OUT + DEPTH2_OUT + DEPTH3_OUT
)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def build(args: argparse.Namespace) -> None:
    if not META2_DIR.exists():
        print(f"[오류] {META2_DIR} 가 없습니다. 먼저 meta2 를 빌드하세요.", file=sys.stderr)
        sys.exit(1)

    gu_meta = load_gu_meta()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    src_files = sorted(META2_DIR.rglob("*.csv"))
    if args.limit:
        src_files = src_files[: args.limit]

    stats = {"files": 0, "rows": 0, "skipped_empty": 0, "skipped_period": 0, "missing_macro": 0}

    for src in src_files:
        with src.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            stats["skipped_empty"] += 1
            continue

        first = rows[0]
        si = first.get("시") or ""
        gu = first.get("구") or ""
        dong = first.get("동") or ""
        apt = first.get("아파트명") or ""
        exclusive = first.get("전용면적") or ""

        # 시간불변 파생(단지/평형 단위로 1회 계산)
        static_vals = {
            "Static__준공구분": classify_age(first.get("준공년도") or ""),
            "Static__세대수구분": classify_scale(first.get("세대수") or ""),
            "Static__평수구분": classify_size(exclusive),
            "Static__건설사등급": classify_builder(first.get("건설사") or ""),
            "Static__초품아여부": is_school_in_complex(first.get("초등학교") or ""),
            "Static__역세권수": count_stations(first.get("인근역") or ""),
            "Static__호재수": count_catalysts(first.get("철도호재") or "", first.get("개발호재") or ""),
        }

        # 출력 경로: meta2 와 동일한 시/구/동/파일명 유지
        rel = src.relative_to(META2_DIR)
        out_path = OUT_DIR / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with out_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
            w.writeheader()
            for r in rows:
                ts = r.get("Timestamp") or ""
                # 분석기간(2010-01 ~ 2026-05) 밖 시세는 제외
                if not (PERIOD_START <= ts <= PERIOD_END):
                    stats["skipped_period"] += 1
                    continue
                macro = gu_meta.get((ts, gu))
                if macro is None:
                    macro = {c: "" for c in DEPTH1_COLS + DEPTH2_COLS + DEPTH3_COLS}
                    stats["missing_macro"] += 1

                out = {
                    "Header_시": si, "Header_구": gu, "Header_동": dong,
                    "Header_Timestamp": ts, "Header_단지명": apt, "Header_평형": exclusive,
                    TARGET_COL: r.get("시세") or "",
                    **static_vals,
                }
                for c in DEPTH1_COLS:
                    out[f"depth1__{c}"] = macro.get(c, "")
                for c in DEPTH2_COLS:
                    out[f"depth2__{c}"] = macro.get(c, "")
                for c in DEPTH3_COLS:
                    out[f"depth3__{c}"] = macro.get(c, "")
                w.writerow(out)
                stats["rows"] += 1

        stats["files"] += 1

    print(
        "\n완료.\n"
        f"  생성 파일: {stats['files']}개 / 총 행: {stats['rows']}\n"
        f"  스킵(빈파일): {stats['skipped_empty']} / 기간밖 제외 행: {stats['skipped_period']} / "
        f"거시매칭실패 행: {stats['missing_macro']}\n"
        f"  산출 위치: {OUT_DIR}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="meta_ml ML 메타테이블 빌더")
    ap.add_argument("--limit", type=int, default=None, help="처리할 매물 csv 수 제한(테스트용)")
    build(ap.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
