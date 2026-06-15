"""
2단계: kbland.kr 에서 아파트별 KB 과거시세(평형별) 엑셀(xlsx)을 수집한다.

동작 개요 (검증된 내부 API 사용):
  1) output/apartments.csv 의 단지명마다 통합검색 API(intgraSerch)로 단지 목록 조회
  2) 서울특별시 + 아파트(SLND_PERTY_NM='아파트', COLLECTION='CRW_HSCM')만 필터 (옵션으로 해제 가능)
  3) 단지별 mpriByType API로 평형(면적일련번호) 목록 조회
  4) 평형별 perMnPastPriceExcelDownload API로 과거시세 xlsx 다운로드

중요:
  - kbland API는 일반 HTTP 클라이언트(requests)를 WAF로 차단하므로,
    실제 Chromium 브라우저(Playwright) 컨텍스트 안에서 fetch 를 실행해 호출한다.
  - "과거 시세 다운로드" 결과 파일은 CSV가 아니라 XLSX 형식이다.

산출물:
  output/downloads/<시군구>/<단지번호>_<단지명>/<면적일련번호>_<전용면적>.xlsx
  output/downloads/_manifest.csv     (다운로드 이력/재개용)
  output/downloads/_complex_index.csv (검색으로 발견한 단지 목록)

재개(resume):
  _manifest.csv 에 기록된 (단지번호, 면적일련번호) 는 건너뛴다.

사용 예:
  python src/crawl_kbland.py --limit 1            # 키워드 1개만 (테스트)
  python src/crawl_kbland.py                       # 전체
  python src/crawl_kbland.py --all-regions         # 서울 외 지역도 포함
  python src/crawl_kbland.py --headless            # 창 숨김
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "output" / "apartments.csv"
DOWNLOAD_DIR = ROOT / "output" / "downloads"
MANIFEST_FILE = DOWNLOAD_DIR / "_manifest.csv"
COMPLEX_INDEX_FILE = DOWNLOAD_DIR / "_complex_index.csv"
KEYWORDS_DONE_FILE = DOWNLOAD_DIR / "_keywords_done.csv"

API = "https://api.kbland.kr"
HOME_URL = "https://kbland.kr/"

MANIFEST_FIELDS = [
    "apt_keyword", "complex_no", "complex_name", "sigungu", "addr",
    "area_no", "exclusive_area", "supply_area", "house_type", "file",
]
COMPLEX_FIELDS = ["complex_no", "complex_name", "sigungu", "addr", "property_type", "collection"]


# ---------------------------------------------------------------------------
# 브라우저 컨텍스트 내 fetch 헬퍼
# ---------------------------------------------------------------------------
def fetch_json(page: Page, url: str, retries: int = 3) -> dict[str, Any] | None:
    """브라우저 fetch 로 JSON 호출. 실패 시 None."""
    js = """async (url) => {
        try {
            const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
            const t = await r.text();
            return { status: r.status, body: t };
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
        except PWTimeout:
            pass
        except Exception:
            pass
        time.sleep(1.0 + attempt)
    return None


def fetch_binary_b64(page: Page, url: str, retries: int = 3) -> bytes | None:
    """브라우저 fetch 로 바이너리 다운로드. base64 로 받아 bytes 반환."""
    js = """async (url) => {
        try {
            const r = await fetch(url);
            if (r.status !== 200) return { status: r.status, b64: null };
            const buf = await r.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let bin = '';
            const chunk = 0x8000;
            for (let i = 0; i < bytes.length; i += chunk) {
                bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
            }
            return { status: 200, b64: btoa(bin) };
        } catch (e) { return { status: -1, b64: null }; }
    }"""
    for attempt in range(retries):
        try:
            res = page.evaluate(js, url)
            if res and res.get("status") == 200 and res.get("b64"):
                data = base64.b64decode(res["b64"])
                # XLSX(zip) 시그니처 확인
                if data[:2] == b"PK":
                    return data
                return None  # 200 이지만 xlsx 아님 -> 시세 없음
        except Exception:
            pass
        time.sleep(1.0 + attempt)
    return None


# ---------------------------------------------------------------------------
# API 래퍼
# ---------------------------------------------------------------------------
def q(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


def search_complexes(page: Page, keyword: str, page_size: int = 50, max_pages: int = 20) -> list[dict]:
    """통합검색으로 단지 목록 조회 (HSCM). 페이지네이션 처리."""
    results: list[dict] = []
    for p in range(1, max_pages + 1):
        url = (
            f"{API}/land-complex/serch/intgraSerch?"
            f"{q('검색설정명')}=SRC_NTOTAL&"
            f"{q('검색키워드')}={q(keyword)}&"
            f"{q('출력갯수')}={page_size}&"
            f"{q('페이지설정값')}={p}"
        )
        data = fetch_json(page, url)
        if not data:
            break
        try:
            hscm = data["dataBody"]["data"]["data"]["HSCM"]
        except (KeyError, TypeError):
            break
        rows = hscm.get("data") or []
        results.extend(rows)
        totcnt = int(hscm.get("totcnt") or 0)
        if len(results) >= totcnt or not rows:
            break
        time.sleep(random.uniform(0.3, 0.7))
    return results


def get_area_list(page: Page, complex_no: str) -> list[dict]:
    """단지의 평형(면적일련번호) 목록 조회."""
    url = f"{API}/land-complex/complex/mpriByType?{q('단지기본일련번호')}={complex_no}"
    data = fetch_json(page, url)
    if not data:
        return []
    try:
        return data["dataBody"]["data"] or []
    except (KeyError, TypeError):
        return []


def download_area_excel(page: Page, complex_no: str, area_no: str) -> bytes | None:
    """평형별 과거시세 xlsx 다운로드."""
    url = (
        f"{API}/land-price/price/perMnPastPriceExcelDownload?"
        f"{q('단지기본일련번호')}={complex_no}&"
        f"{q('면적일련번호')}={area_no}&"
        f"{q('연결구분명')}={q('일반')}"
    )
    return fetch_binary_b64(page, url)


# ---------------------------------------------------------------------------
# 유틸
# ---------------------------------------------------------------------------
def sanitize(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "")
    name = name.strip().rstrip(".")
    return name[:80] or "_"


def parse_sigungu(bubaddr: str) -> str:
    """'서울특별시 송파구 가락동' -> '송파구'."""
    parts = (bubaddr or "").split()
    return parts[1] if len(parts) >= 2 else "기타"


def load_done_keys() -> set[tuple[str, str]]:
    """이미 다운로드한 (complex_no, area_no) 집합."""
    done: set[tuple[str, str]] = set()
    if MANIFEST_FILE.exists():
        with MANIFEST_FILE.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                done.add((row["complex_no"], row["area_no"]))
    return done


def load_done_complexes() -> set[str]:
    done: set[str] = set()
    if COMPLEX_INDEX_FILE.exists():
        with COMPLEX_INDEX_FILE.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                done.add(row["complex_no"])
    return done


def load_done_keywords() -> set[str]:
    """이미 검색까지 끝낸 키워드 집합(키워드 단위 재개용).

    진행 로그(_keywords_done.csv)가 있으면 그대로 사용한다.
    없으면 기존 manifest 의 apt_keyword 로 부트스트랩하되,
    중단 직전 마지막 키워드는 미완일 수 있으므로 제외하고 로그로 옮겨 적는다.
    """
    if KEYWORDS_DONE_FILE.exists():
        done: set[str] = set()
        with KEYWORDS_DONE_FILE.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if row and row[0] and row[0] != "apt_keyword":
                    done.add(row[0])
        return done

    # 부트스트랩: manifest 에서 키워드 추출(마지막 키워드 제외)
    ordered: list[str] = []
    seen: set[str] = set()
    last_kw = ""
    if MANIFEST_FILE.exists():
        with MANIFEST_FILE.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                kw = row.get("apt_keyword") or ""
                last_kw = kw
                if kw and kw not in seen:
                    seen.add(kw)
                    ordered.append(kw)
    done = {kw for kw in ordered if kw != last_kw}
    if done:
        ensure_csv_header(KEYWORDS_DONE_FILE, ["apt_keyword"])
        for kw in ordered:
            if kw != last_kw:
                append_row(KEYWORDS_DONE_FILE, ["apt_keyword"], {"apt_keyword": kw})
    return done


def ensure_csv_header(path: Path, fields: list[str]) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(fields)


def append_row(path: Path, fields: list[str], row: dict) -> None:
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writerow(row)


def read_keywords(limit: int | None, override: list[str] | None = None) -> list[str]:
    if override:
        return override
    if not INPUT_FILE.exists():
        print(f"[오류] {INPUT_FILE} 가 없습니다. 먼저 extract_apartments.py 를 실행하세요.", file=sys.stderr)
        sys.exit(1)
    kws: list[str] = []
    with INPUT_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.reader(f)):
            if i == 0 or not row:
                continue
            name = row[0].strip()
            if name:
                kws.append(name)
    if limit:
        kws = kws[:limit]
    return kws


# ---------------------------------------------------------------------------
# 메인 크롤링
# ---------------------------------------------------------------------------
def crawl(args: argparse.Namespace) -> None:
    keywords = read_keywords(args.limit, override=args.keyword)
    print(f"키워드 {len(keywords)}개 처리 시작")

    ensure_csv_header(MANIFEST_FILE, MANIFEST_FIELDS)
    ensure_csv_header(COMPLEX_INDEX_FILE, COMPLEX_FIELDS)
    ensure_csv_header(KEYWORDS_DONE_FILE, ["apt_keyword"])
    done_keys = load_done_keys()
    seen_complexes = load_done_complexes()
    done_keywords = set() if (args.no_resume or args.keyword) else load_done_keywords()
    if done_keywords:
        print(f"키워드 재개: 이미 끝낸 {len(done_keywords)}개는 검색 없이 즉시 건너뜁니다. (나머지만 처리)")

    stats = {"complexes": 0, "files": 0, "skipped": 0, "skipped_kw": 0, "errors": 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        for idx, kw in enumerate(keywords, 1):
            if kw in done_keywords:
                stats["skipped_kw"] += 1
                continue
            print(f"\n[{idx}/{len(keywords)}] 검색: {kw}")
            complexes = search_complexes(page, kw, page_size=args.page_size)

            for c in complexes:
                complex_no = str(c.get("COMPLEX_NO") or "").strip()
                name = c.get("HSCM_NM") or ""
                bubaddr = c.get("BUBADDR") or ""
                prop_type = c.get("SLND_PERTY_NM") or ""
                collection = c.get("COLLECTION") or ""

                if not complex_no:
                    continue
                # 필터: 서울 + 아파트(실단지)
                if not args.all_regions and not bubaddr.startswith("서울"):
                    continue
                if not args.all_types:
                    if prop_type != "아파트" or collection != "CRW_HSCM":
                        continue

                if complex_no in seen_complexes:
                    # 이미 평형까지 처리한 단지면 통째로 스킵
                    continue
                seen_complexes.add(complex_no)

                sigungu = parse_sigungu(bubaddr)
                append_row(COMPLEX_INDEX_FILE, COMPLEX_FIELDS, {
                    "complex_no": complex_no, "complex_name": name,
                    "sigungu": sigungu, "addr": bubaddr,
                    "property_type": prop_type, "collection": collection,
                })
                stats["complexes"] += 1
                print(f"  - 단지 {complex_no} {name} ({sigungu})")

                areas = get_area_list(page, complex_no)
                time.sleep(random.uniform(0.3, 0.6))

                for a in areas:
                    area_no = str(a.get("면적일련번호") or "").strip()
                    if not area_no:
                        continue
                    if str(a.get("시세제공여부") or "") != "1":
                        continue
                    if (complex_no, area_no) in done_keys:
                        stats["skipped"] += 1
                        continue

                    exclusive = str(a.get("전용면적") or "")
                    supply = str(a.get("공급면적") or "")
                    htype = str(a.get("주택형타입내용") or "")

                    blob = download_area_excel(page, complex_no, area_no)
                    if not blob:
                        stats["errors"] += 1
                        continue

                    out_dir = DOWNLOAD_DIR / sanitize(sigungu) / sanitize(f"{complex_no}_{name}")
                    out_dir.mkdir(parents=True, exist_ok=True)
                    fname = sanitize(f"{area_no}_{exclusive}") + ".xlsx"
                    fpath = out_dir / fname
                    fpath.write_bytes(blob)

                    rel = fpath.relative_to(ROOT).as_posix()
                    append_row(MANIFEST_FILE, MANIFEST_FIELDS, {
                        "apt_keyword": kw, "complex_no": complex_no, "complex_name": name,
                        "sigungu": sigungu, "addr": bubaddr, "area_no": area_no,
                        "exclusive_area": exclusive, "supply_area": supply,
                        "house_type": htype, "file": rel,
                    })
                    done_keys.add((complex_no, area_no))
                    stats["files"] += 1
                    print(f"      평형 {area_no} 전용{exclusive} -> {fname}")
                    time.sleep(random.uniform(args.delay_min, args.delay_max))

            # 키워드 처리 완료 기록(다음 실행 시 검색 없이 즉시 건너뜀)
            append_row(KEYWORDS_DONE_FILE, ["apt_keyword"], {"apt_keyword": kw})
            done_keywords.add(kw)
            time.sleep(random.uniform(args.delay_min, args.delay_max))

        browser.close()

    print(
        f"\n완료. 단지 {stats['complexes']}개 / 다운로드 {stats['files']}개 / "
        f"건너뜀(평형) {stats['skipped']} / 건너뜀(키워드) {stats['skipped_kw']} / 오류 {stats['errors']}"
    )
    print(f"이력: {MANIFEST_FILE}")


def main() -> int:
    ap = argparse.ArgumentParser(description="kbland 과거시세 크롤러")
    ap.add_argument("--limit", type=int, default=None, help="처리할 키워드 수 제한(테스트용)")
    ap.add_argument("--keyword", action="append", help="apartments.csv 대신 직접 검색할 키워드(반복 지정 가능)")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 숨김")
    ap.add_argument("--all-regions", action="store_true", help="서울 외 지역도 포함")
    ap.add_argument("--all-types", action="store_true", help="아파트 외 유형도 포함")
    ap.add_argument("--page-size", type=int, default=50, help="검색 페이지당 결과 수")
    ap.add_argument("--delay-min", type=float, default=0.4, help="요청 간 최소 지연(초)")
    ap.add_argument("--delay-max", type=float, default=1.0, help="요청 간 최대 지연(초)")
    ap.add_argument("--no-resume", action="store_true", help="키워드 단위 건너뛰기 끄기(처음부터 전부 재검색)")
    args = ap.parse_args()
    crawl(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
