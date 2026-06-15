"""한국은행 ECOS Open API 클라이언트."""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import EcosAPISettings

logger = logging.getLogger(__name__)


class EcosApiError(RuntimeError):
    """ECOS API 오류."""


class EcosApiClient:
    BASE_URL = "https://ecos.bok.or.kr/api"

    CYCLE_MAP = {
        "MM": "M",
        "M": "M",
        "QQ": "Q",
        "Q": "Q",
        "YY": "A",
        "A": "A",
        "DD": "D",
        "D": "D",
    }

    def __init__(self, settings: EcosAPISettings) -> None:
        self.settings = settings
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

    def fetch_series(
        self,
        stat_code: str,
        cycle_type: str,
        start_date: str,
        end_date: str,
        item_code1: str = "?",
        item_code2: str = "?",
        item_code3: str = "?",
        item_code4: str = "?",
        start_count: int = 1,
        end_count: int = 10000,
    ) -> list[dict[str, Any]]:
        """StatisticSearch 엔드포인트로 시계열 데이터를 조회한다.

        Returns
        -------
        list of row dicts (keys: STAT_CODE, ITEM_CODE1..4, ITEM_NAME1..4, DATA_VALUE, TIME)
        """
        normalized_cycle = self.CYCLE_MAP.get(cycle_type.upper(), cycle_type)
        key = quote(self.settings.api_key, safe="")

        url = (
            f"{self.BASE_URL}/StatisticSearch"
            f"/{key}/json/kr"
            f"/{start_count}/{end_count}"
            f"/{quote(stat_code, safe='')}/{quote(normalized_cycle, safe='')}"
            f"/{quote(start_date, safe='')}/{quote(end_date, safe='')}"
            f"/{quote(item_code1, safe='')}/{quote(item_code2, safe='')}"
            f"/{quote(item_code3, safe='')}/{quote(item_code4, safe='')}"
        )

        started = time.perf_counter()
        connect_timeout = min(8, max(2, self.settings.timeout_seconds // 3))
        read_timeout = max(5, self.settings.timeout_seconds)

        try:
            resp = self.session.get(url, timeout=(connect_timeout, read_timeout))
        except requests.Timeout as exc:
            elapsed = time.perf_counter() - started
            raise EcosApiError(
                f"Timeout stat_code={stat_code} elapsed={elapsed:.2f}s"
            ) from exc
        except requests.RequestException as exc:
            raise EcosApiError(f"Request error stat_code={stat_code}: {exc}") from exc

        elapsed = time.perf_counter() - started
        logger.info(
            "[ecos] stat_code=%s cycle=%s %s~%s status=%s elapsed=%.2fs",
            stat_code, normalized_cycle, start_date, end_date,
            resp.status_code, elapsed,
        )

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            raise EcosApiError(f"HTTP error: {exc} | body={resp.text[:300]}") from exc

        payload = resp.json()

        # ECOS 에러 응답: {"RESULT": {"CODE": "INFO-200", ...}}
        if isinstance(payload, dict) and "RESULT" in payload:
            code = payload["RESULT"].get("CODE", "")
            if code == "INFO-200":
                return []
            if not code.startswith("INFO-000"):
                raise EcosApiError(
                    f"ECOS API error code={code} message={payload['RESULT'].get('MESSAGE', '')}"
                )

        rows = _extract_rows(payload)
        if rows is None:
            raise EcosApiError(f"Cannot parse ECOS response: {str(payload)[:400]}")

        return rows

    def fetch_all_pages(
        self,
        stat_code: str,
        cycle_type: str,
        start_date: str,
        end_date: str,
        item_code1: str = "?",
        item_code2: str = "?",
        item_code3: str = "?",
        item_code4: str = "?",
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        """전체 페이지를 순회하며 데이터를 수집한다."""
        all_rows: list[dict[str, Any]] = []
        start = 1

        while True:
            end = start + page_size - 1
            rows = self.fetch_series(
                stat_code=stat_code,
                cycle_type=cycle_type,
                start_date=start_date,
                end_date=end_date,
                item_code1=item_code1,
                item_code2=item_code2,
                item_code3=item_code3,
                item_code4=item_code4,
                start_count=start,
                end_count=end,
            )
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
            start += page_size

        return all_rows


def _extract_rows(payload: Any) -> list[dict[str, Any]] | None:
    if not isinstance(payload, dict):
        return None
    # 최상위 키가 endpoint 이름 ("StatisticSearch")인 경우
    for key, val in payload.items():
        if key in ("RESULT",):
            continue
        if isinstance(val, dict) and "row" in val:
            return val["row"] or []
        if isinstance(val, list):
            return val
    return None
