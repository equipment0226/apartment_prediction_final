"""
네이버 부동산(fin.land.naver.com) 단지 정보 수집기.

입력: output/downloads/_complex_index.csv (앞 단계에서 수집한 서울 아파트 단지 목록)
대상: 각 단지명을 네이버 부동산에서 검색 → 서울 단지만 → '단지정보' 데이터 수집

수집 항목 (단지당 1행, 메타데이터 단일 테이블):
  - 사용승인일, 세대수(+임대), 동수
  - 최고층(가장 낮은 동 / 가장 높은 동), 세대당 주차대수, 총 주차대수
  - 건설사
  - 배정 초등학교 + 거리 (여러개면 모두)
  - 개발예정 (종류/단계/기간, 모두)
  - 주변분양 (단지명/주소/분양시기/세대수, 모두)
  - 주변대중교통 지하철 (역명/노선/거리, 모두)

다중값 항목은 한 셀에 ' | ' 로 직렬화하여 단일 테이블 1행/단지를 유지한다.

차단 대비/일관성을 위해 실제 Chromium(Playwright) 컨텍스트의 fetch 로 내부 API를 호출한다.

산출물:
  naverland/output/complex_metadata.csv

사용 예:
  python naverland/crawl_naverland.py --limit 3        # 앞 3개만(테스트)
  python naverland/crawl_naverland.py --headless        # 전체
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Page

ROOT = Path(__file__).resolve().parents[1]
INDEX_FILE = ROOT / "output" / "downloads" / "_complex_index.csv"
OUT_DIR = Path(__file__).resolve().parent / "output"
OUT_FILE = OUT_DIR / "complex_metadata.csv"

API = "https://fin.land.naver.com/front-api/v1"
# /map 페이지를 거쳐야 front-api 세션(쿠키/핸드셰이크)이 형성된다.
SESSION_URL = "https://fin.land.naver.com/map"

# 네이버는 기본 번들 Chromium 핑거프린트를 봇으로 차단(429)한다.
# 실제 설치된 브라우저 채널을 우선 사용하면 정상 세션을 얻을 수 있다.
BROWSER_CHANNELS = ["msedge", "chrome", None]
_STEALTH_JS = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"

FIELDS = [
    "source_complex_no", "naver_complex_no", "complex_name", "sigungu",
    "legal_division", "address_jibun", "road_name",
    "use_approval_date", "approval_elapsed_year",
    "total_household", "lease_household", "dong_count",
    "lowest_dong_floor", "highest_dong_floor",
    "total_parking", "parking_per_household",
    "floor_area_ratio", "building_coverage_ratio",
    "construction_company",
    "elementary_schools", "developments", "surrounding_presales", "subways",
    "match_status",
]


# ---------------------------------------------------------------------------
# 브라우저 컨텍스트 내 fetch
# ---------------------------------------------------------------------------
def fetch_json(page: Page, url: str, retries: int = 3) -> dict[str, Any] | None:
    js = """async (url) => {
        try {
            const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
            return { status: r.status, body: await r.text() };
        } catch (e) { return { status: -1, body: String(e) }; }
    }"""
    for attempt in range(retries):
        try:
            res = page.evaluate(js, url)
            if res and res.get("status") == 200:
                try:
                    return json.loads(res["body"])
                except json.JSONDecodeError:
                    return None
        except Exception:
            pass
        time.sleep(0.8 + attempt)
    return None


def q(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


# 단지명 끝의 "1.2차", "2차", "3단지" 같은 접미사 (검색 실패 시 제거해 재시도)
_SUFFIX_RE = re.compile(r"\s*\d+(?:\.\d+)*\s*(?:차|단지)\s*$")


def clean_keyword(name: str) -> str:
    """끝의 차/단지 접미사를 제거한 검색 키워드를 반환."""
    out = _SUFFIX_RE.sub("", name).strip()
    return out or name


# ---------------------------------------------------------------------------
# API 래퍼
# ---------------------------------------------------------------------------
def search_complex(page: Page, keyword: str) -> list[dict]:
    url = f"{API}/search/autocomplete/complexes?keyword={q(keyword)}&size=10&page=0"
    data = fetch_json(page, url)
    if not data or not data.get("isSuccess"):
        return []
    return (data.get("result") or {}).get("list") or []


def get_complex_detail(page: Page, cn: int | str) -> dict | None:
    data = fetch_json(page, f"{API}/complex?complexNumber={cn}")
    if not data or not data.get("isSuccess"):
        return None
    return data.get("result")


def get_schools(page: Page, cn: int | str) -> list[dict]:
    data = fetch_json(page, f"{API}/complex/school?complexNumber={cn}&itemType=complex")
    if not data or not data.get("isSuccess"):
        return []
    return data.get("result") or []


def get_development(page: Page, cn: int | str) -> dict:
    data = fetch_json(page, f"{API}/development?type=complex&itemId={cn}")
    if not data or not data.get("isSuccess"):
        return {}
    return data.get("result") or {}


def get_surrounding_presale(page: Page, legal_division_no: str) -> list[dict]:
    if not legal_division_no:
        return []
    url = f"{API}/preSale/surroundingPreSale?legalDivisionNumber={legal_division_no}&userChannelType=PC"
    data = fetch_json(page, url)
    if not data or not data.get("isSuccess"):
        return []
    res = data.get("result") or {}
    return (res.get("promotionData") or []) + (res.get("nonPromotionData") or [])


def get_subways(page: Page, cn: int | str) -> list[dict]:
    data = fetch_json(page, f"{API}/article/transport?itemType=complex&itemId={cn}")
    if not data or not data.get("isSuccess"):
        return []
    return (data.get("result") or {}).get("subwayList") or []


# ---------------------------------------------------------------------------
# 직렬화 헬퍼 (다중값 -> 단일 셀)
# ---------------------------------------------------------------------------
def fmt_use_approval(raw: str | None) -> str:
    if not raw:
        return ""
    if len(raw) == 8:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) == 6:
        return f"{raw[0:4]}-{raw[4:6]}"
    return raw


def serialize_schools(schools: list[dict]) -> str:
    parts = []
    for s in schools:
        name = s.get("name") or ""
        dist = s.get("distance")
        walk = s.get("walkingMinute")
        parts.append(f"{name}(거리 {dist}m, 도보 {walk}분)")
    return " | ".join(parts)


def serialize_developments(dev: dict) -> str:
    parts = []
    for r in dev.get("railList") or []:
        # 종류: railName / 단계: (노선 개통) / 기간: openDate
        parts.append(
            f"[철도] {r.get('railName','')} {r.get('stationName','')} "
            f"(개통 {r.get('openDate','')}, 거리 {r.get('distance','')}m)"
        )
    for jg in dev.get("jiguList") or []:
        # 종류: typeName / 단계: step
        parts.append(
            f"[지구] {jg.get('name','')} (종류 {jg.get('typeName','')}, 단계 {jg.get('step','')})"
        )
    return " | ".join(parts)


def serialize_presales(presales: list[dict]) -> str:
    parts = []
    for p in presales:
        name = p.get("preSaleComplexName") or ""
        addr = p.get("preSaleAddress") or ""
        detail = p.get("preSaleDetailAddress") or ""
        stage = p.get("preSaleStageDetails") or ""
        total = p.get("totalHouseholdsNumber")
        parts.append(f"{name}({addr} {detail}, 분양시기 {stage}, {total}세대)")
    return " | ".join(parts)


def serialize_subways(subways: list[dict]) -> str:
    parts = []
    for s in subways:
        station = s.get("stationName") or ""
        for t in s.get("typeList") or []:
            line = t.get("name") or ""
            dist = t.get("walkingDistance")
            dur = t.get("walkingDuration")
            parts.append(f"{station} {line}(거리 {dist}m, 도보 {dur}분)")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# 매칭: 검색 결과 중 서울 + 시군구/동 일치하는 단지 선택
# ---------------------------------------------------------------------------
def pick_complex(results: list[dict], sigungu: str, addr: str) -> tuple[dict | None, str]:
    seoul = [r for r in results if (r.get("legalDivisionName") or "").startswith("서울")]
    if not seoul:
        return None, "no_seoul_match"

    dong = ""
    parts = (addr or "").split()
    if len(parts) >= 3:
        dong = parts[2]  # 예: 서울특별시 송파구 가락동 -> 가락동

    # 1) 시군구 + 동 일치
    for r in seoul:
        ld = r.get("legalDivisionName") or ""
        if sigungu and sigungu in ld and dong and dong in ld:
            return r, "matched_sigungu_dong"
    # 2) 시군구만 일치
    for r in seoul:
        if sigungu and sigungu in (r.get("legalDivisionName") or ""):
            return r, "matched_sigungu"
    # 3) 서울 첫 결과
    return seoul[0], "matched_seoul_first"


# ---------------------------------------------------------------------------
# 입출력
# ---------------------------------------------------------------------------
def read_index(limit: int | None) -> list[dict]:
    if not INDEX_FILE.exists():
        print(f"[오류] {INDEX_FILE} 가 없습니다. 먼저 kbland 단계를 실행하세요.", file=sys.stderr)
        sys.exit(1)
    rows: list[dict] = []
    with INDEX_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    if limit:
        rows = rows[:limit]
    return rows


def load_done() -> set[str]:
    done: set[str] = set()
    if OUT_FILE.exists():
        with OUT_FILE.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                done.add(row.get("source_complex_no", ""))
    return done


def ensure_header() -> None:
    if not OUT_FILE.exists():
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with OUT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def append_row(row: dict) -> None:
    with OUT_FILE.open("a", encoding="utf-8-sig", newline="") as f:
        csv.DictWriter(f, fieldnames=FIELDS).writerow(row)


def launch_browser(pw, args: argparse.Namespace):
    """실제 설치 브라우저(Edge/Chrome) 채널을 우선 사용해 봇 차단을 회피한다."""
    launch_args = ["--disable-blink-features=AutomationControlled"]
    channels = [args.channel] if args.channel else BROWSER_CHANNELS
    last_err: Exception | None = None
    for ch in channels:
        try:
            kw: dict[str, Any] = dict(headless=args.headless, args=launch_args)
            if ch:
                kw["channel"] = ch
            browser = pw.chromium.launch(**kw)
            print(f"브라우저 채널: {ch or 'chromium(기본)'}")
            return browser
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  채널 {ch or 'chromium'} 실패: {e}")
    raise RuntimeError(f"브라우저 실행 실패: {last_err}")


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
def build_row(page: Page, src: dict, naver: dict, match_status: str) -> dict:
    cn = naver.get("complexNumber")
    detail = get_complex_detail(page, cn) or {}
    time.sleep(random.uniform(0.2, 0.5))

    addr = detail.get("address") or {}
    ld_no = addr.get("legalDivisionNumber") or ""
    parking = detail.get("parkingInfo") or {}
    ratio = detail.get("buildingRatioInfo") or {}

    schools = get_schools(page, cn)
    time.sleep(random.uniform(0.2, 0.4))
    dev = get_development(page, cn)
    time.sleep(random.uniform(0.2, 0.4))
    presales = get_surrounding_presale(page, ld_no)
    time.sleep(random.uniform(0.2, 0.4))
    subways = get_subways(page, cn)

    return {
        "source_complex_no": src.get("complex_no", ""),
        "naver_complex_no": cn,
        "complex_name": detail.get("name") or naver.get("complexName") or src.get("complex_name", ""),
        "sigungu": src.get("sigungu", ""),
        "legal_division": naver.get("legalDivisionName", ""),
        "address_jibun": addr.get("jibun", ""),
        "road_name": addr.get("roadName", ""),
        "use_approval_date": fmt_use_approval(detail.get("useApprovalDate")),
        "approval_elapsed_year": detail.get("approvalElapsedYear", ""),
        "total_household": detail.get("totalHouseholdNumber", ""),
        "lease_household": detail.get("leaseHouseholdNumber", ""),
        "dong_count": detail.get("dongCount", ""),
        "lowest_dong_floor": detail.get("lowestDongFloor", ""),
        "highest_dong_floor": detail.get("highestDongFloor", ""),
        "total_parking": parking.get("totalParkingCount", ""),
        "parking_per_household": parking.get("parkingCountPerHousehold", ""),
        "floor_area_ratio": ratio.get("floorAreaRatio", ""),
        "building_coverage_ratio": ratio.get("buildingCoverageRatio", ""),
        "construction_company": detail.get("constructionCompany", ""),
        "elementary_schools": serialize_schools(schools),
        "developments": serialize_developments(dev),
        "surrounding_presales": serialize_presales(presales),
        "subways": serialize_subways(subways),
        "match_status": match_status,
    }


def crawl(args: argparse.Namespace) -> None:
    rows = read_index(args.limit)
    ensure_header()
    done = load_done()
    print(f"단지 {len(rows)}개 처리 시작 (이미 완료 {len(done)}개)")

    stats = {"ok": 0, "skipped": 0, "no_match": 0, "not_seoul_src": 0}

    with sync_playwright() as pw:
        browser = launch_browser(pw, args)
        ctx = browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        ctx.add_init_script(_STEALTH_JS)
        page = ctx.new_page()
        page.goto(SESSION_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        for idx, src in enumerate(rows, 1):
            src_no = src.get("complex_no", "")
            name = src.get("complex_name", "")
            addr = src.get("addr", "")

            if src_no in done:
                stats["skipped"] += 1
                continue
            # 원본 자체가 서울이 아니면 무시
            if not addr.startswith("서울"):
                stats["not_seoul_src"] += 1
                continue

            print(f"\n[{idx}/{len(rows)}] {name} ({src.get('sigungu','')})")
            results = search_complex(page, name)
            naver, status = pick_complex(results, src.get("sigungu", ""), addr)

            # 원본 명으로 서울 매칭 실패 시 접미사 제거 키워드로 재시도
            if not naver:
                cleaned = clean_keyword(name)
                if cleaned != name:
                    time.sleep(random.uniform(0.2, 0.5))
                    results = search_complex(page, cleaned)
                    naver, status = pick_complex(results, src.get("sigungu", ""), addr)
                    if naver:
                        status += "_cleaned"

            if not naver:
                stats["no_match"] += 1
                append_row({
                    **{k: "" for k in FIELDS},
                    "source_complex_no": src_no, "complex_name": name,
                    "sigungu": src.get("sigungu", ""), "match_status": status,
                })
                print(f"  매칭 실패: {status}")
                done.add(src_no)
                time.sleep(random.uniform(args.delay_min, args.delay_max))
                continue

            row = build_row(page, src, naver, status)
            append_row(row)
            done.add(src_no)
            stats["ok"] += 1
            print(
                f"  -> 네이버단지 {row['naver_complex_no']} | {row['legal_division']} | "
                f"세대 {row['total_household']} | 초교 {row['elementary_schools'][:30]}"
            )
            time.sleep(random.uniform(args.delay_min, args.delay_max))

        browser.close()

    print(
        f"\n완료. 수집 {stats['ok']} / 건너뜀 {stats['skipped']} / "
        f"매칭실패 {stats['no_match']} / 서울외제외 {stats['not_seoul_src']}"
    )
    print(f"결과: {OUT_FILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description="네이버 부동산 단지정보 수집기")
    ap.add_argument("--limit", type=int, default=None, help="처리할 단지 수 제한(테스트)")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 숨김")
    ap.add_argument("--channel", default=None,
                    help="브라우저 채널 고정(msedge/chrome). 기본은 자동 탐색")
    ap.add_argument("--delay-min", type=float, default=0.5, help="단지 간 최소 지연(초)")
    ap.add_argument("--delay-max", type=float, default=1.2, help="단지 간 최대 지연(초)")
    args = ap.parse_args()
    crawl(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
