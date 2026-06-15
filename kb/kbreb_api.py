"""KB부동산(KBREB) 월간 지역별 가격지수 수집 파이프라인.

한국부동산원(REB) API는 서울을 5개 권역(도심권/동북권/...) 단위로만 제공하여
구 단위 해상도가 없다. KB부동산 데이터허브(data-api.kbland.kr)는 동일 지표를
**구 단위·월 단위**로 제공하므로, ``apt_sale_index`` /
``apt_jeonse_index`` 두 컬럼(아파트 매매/전세 가격지수)을 KB 구 단위 데이터로
수집하기 위한 전용 클라이언트를 둔다. KB priceIndex 엔드포인트가 반환하는 값은
가격(만원)이 아니라 기준 2026.1=100 의 가격지수이므로 컬럼명도 ``*_index`` 로 둔다.

엔드포인트
----------
``GET https://data-api.kbland.kr/bfmstat/weekMnthlyHuseTrnd/priceIndex``

주요 파라미터(한글 키)
    매매전세코드        : 01=매매, 02=전세
    매물종별구분        : 01=아파트
    월간주간구분코드    : 01=월간 (필수)
    apiFlag             : priceIndex
    메뉴코드            : 1
    기간                : 17
    지역코드            : 1A0000(한강 이북 14개 구) / 1B0000(한강 이남 11개 구)

응답 구조
---------
``dataBody.data`` 아래에
    날짜리스트   : ["200905", "200906", ... "202605"]  (YYYYMM, 길이 N)
    데이터리스트 : [{지역코드, 지역명, dataList:[...]}, ...]

``dataList`` 는 날짜리스트보다 길며(끝에 MoM/YoY 등 요약값이 덧붙음) 앞 N개만
날짜축과 1:1 정렬된다. 지수는 기준 2026.1 = 100.0 의 무단위 값이다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger(__name__)


class KBRebApiError(RuntimeError):
    """KB부동산 API가 예상치 못한 응답을 반환할 때 발생."""


# 매매전세코드 ↔ 대상 컬럼명. KB priceIndex 는 가격지수(기준 2026.1=100)이므로
# 메타테이블 컬럼은 reb__apt_sale_index / reb__apt_jeonse_index 가 된다.
SALE_SERIES_NAME = "apt_sale_index"
JEONSE_SERIES_NAME = "apt_jeonse_index"

# 서울 전역을 덮는 KB 지역코드(한강 이북/이남). 두 코드를 합치면 25개 구 전부.
SEOUL_REGION_CODES: tuple[str, ...] = ("1A0000", "1B0000")


@dataclass(frozen=True)
class KBRebSeriesConfig:
    """KB부동산 priceIndex 수집 단위 정의."""

    name: str               # 대상 컬럼명 (예: apt_sale_index)
    maemae_jeonse_code: str  # 01=매매, 02=전세
    region_codes: tuple[str, ...] = SEOUL_REGION_CODES


# stat_catalog 에서 참조하는 기본 KB 수집 항목.
DEFAULT_KBREB_SERIES: list[KBRebSeriesConfig] = [
    KBRebSeriesConfig(name=SALE_SERIES_NAME, maemae_jeonse_code="01"),
    KBRebSeriesConfig(name=JEONSE_SERIES_NAME, maemae_jeonse_code="02"),
]


class KBRebApiClient:
    BASE_URL = "https://data-api.kbland.kr/bfmstat/weekMnthlyHuseTrnd/priceIndex"

    def __init__(self, timeout_seconds: int = 30) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def fetch_price_index(self, maemae_jeonse_code: str, region_code: str) -> dict[str, Any]:
        """단일 (매매/전세, 지역코드) 조합의 priceIndex 응답(JSON)을 반환한다."""
        params = {
            "매매전세코드": maemae_jeonse_code,
            "매물종별구분": "01",        # 아파트
            "월간주간구분코드": "01",     # 월간 (필수)
            "기간": "17",
            "type": "true",
            "apiFlag": "priceIndex",
            "메뉴코드": "1",
            "지역코드": region_code,
        }
        connect_timeout = min(8, max(2, int(self.timeout_seconds / 3)))
        read_timeout = max(5, int(self.timeout_seconds))
        started = time.perf_counter()
        try:
            response = self.session.get(
                self.BASE_URL,
                params=params,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.RequestException as exc:
            elapsed = time.perf_counter() - started
            raise KBRebApiError(
                f"KB API 요청 실패 region={region_code} code={maemae_jeonse_code} "
                f"elapsed={elapsed:.2f}s err={exc}"
            ) from exc

        elapsed = time.perf_counter() - started
        logger.info(
            "[kbreb_api] region=%s maemae_jeonse=%s status=%s elapsed=%.2fs",
            region_code,
            maemae_jeonse_code,
            response.status_code,
            elapsed,
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KBRebApiError(
                f"KB API HTTP 오류: {exc} | body={response.text[:300]}"
            ) from exc

        payload = response.json()
        header = payload.get("dataHeader", {})
        if str(header.get("resultCode")) != "10000":
            raise KBRebApiError(
                f"KB API 비정상 응답 region={region_code} code={maemae_jeonse_code} "
                f"header={header} body={str(payload)[:300]}"
            )
        return payload


def _parse_price_index_payload(
    payload: dict[str, Any],
    series_name: str,
    maemae_jeonse_code: str,
) -> list[dict[str, Any]]:
    """priceIndex 응답을 REB long-format 스키마와 동일한 행 리스트로 변환한다."""
    data = (payload.get("dataBody") or {}).get("data") or {}
    dates: list[Any] = data.get("날짜리스트") or []
    regions: list[dict[str, Any]] = data.get("데이터리스트") or []
    if not dates or not regions:
        return []

    n = len(dates)
    rows: list[dict[str, Any]] = []
    for region in regions:
        gu = str(region.get("지역명") or "").strip()
        region_code = str(region.get("지역코드") or "").strip()
        values = region.get("dataList") or []
        if not gu or not values:
            continue
        # dataList 끝의 요약값(MoM/YoY 등)을 제외하고 날짜축과 1:1 정렬.
        for ym, raw_val in zip(dates, values[:n]):
            ym_str = str(ym).strip()
            if len(ym_str) != 6 or not ym_str.isdigit():
                continue
            try:
                value = float(raw_val)
            except (TypeError, ValueError):
                continue
            rows.append(
                {
                    "date": pd.Timestamp(int(ym_str[:4]), int(ym_str[4:6]), 1),
                    "wrttime": ym_str,
                    "grp_id": "",
                    "grp_nm": "",
                    "cls_id": region_code,
                    "cls_nm": gu,                 # 구명 → build 단계에서 gu 수준으로 분류
                    "series_name": series_name,
                    "series_code": f"KB_priceIndex_{maemae_jeonse_code}",
                    "value": value,
                }
            )
    return rows


def collect_kbreb_data(
    output_csv_path: str | None = None,
    series_list: list[KBRebSeriesConfig] | None = None,
    start_wrttime: str | None = None,
    end_wrttime: str | None = None,
    timeout_seconds: int = 30,
) -> pd.DataFrame:
    """KB부동산 구 단위 월별 가격지수를 수집해 long-format DataFrame으로 반환한다.

    반환 컬럼: date, wrttime, grp_id, grp_nm, cls_id, cls_nm,
              series_name, series_code, value (REB 수집 결과와 동일 스키마)

    start_wrttime / end_wrttime : 'YYYYMM' 형식 월 필터 (None → 전체 범위)
    """
    series = series_list or DEFAULT_KBREB_SERIES
    client = KBRebApiClient(timeout_seconds=timeout_seconds)

    all_rows: list[dict[str, Any]] = []
    for cfg in series:
        for region_code in cfg.region_codes:
            payload = client.fetch_price_index(cfg.maemae_jeonse_code, region_code)
            rows = _parse_price_index_payload(payload, cfg.name, cfg.maemae_jeonse_code)
            logger.info(
                "[kbreb] series=%s region=%s rows=%s",
                cfg.name,
                region_code,
                len(rows),
            )
            all_rows.extend(rows)

    if not all_rows:
        raise KBRebApiError("KB부동산 API에서 수집된 데이터가 없습니다.")

    df = pd.DataFrame(all_rows)
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["date", "value"])

    if start_wrttime:
        df = df[df["wrttime"] >= str(start_wrttime)]
    if end_wrttime:
        df = df[df["wrttime"] <= str(end_wrttime)]

    df = df.sort_values(["series_name", "cls_nm", "date"]).reset_index(drop=True)

    if output_csv_path:
        out_path = Path(output_csv_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
        logger.info("[kbreb] saved rows=%s path=%s", len(df), out_path)

    return df
