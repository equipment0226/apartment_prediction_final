from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import APISettings


class REBApiError(RuntimeError):
    """Raised when the REB API returns an unexpected response."""


logger = logging.getLogger(__name__)


class REBApiClient:
    BASE_URL = "https://www.reb.or.kr/r-one/openapi"

    def __init__(self, settings: APISettings) -> None:
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

    def list_tables(self, statbl_id: str | None = None, page_index: int = 1) -> list[dict[str, Any]]:
        params = self._base_params(page_index)
        if statbl_id:
            params["STATBL_ID"] = statbl_id
        return self._get_rows("SttsApiTbl.do", params)

    def list_items(
        self,
        statbl_id: str,
        itm_tag: str | None = None,
        page_index: int = 1,
    ) -> list[dict[str, Any]]:
        params = self._base_params(page_index)
        params["STATBL_ID"] = statbl_id
        if itm_tag:
            params["ITM_TAG"] = itm_tag
        return self._get_rows("SttsApiTblItm.do", params)

    def fetch_table_data(
        self,
        statbl_id: str,
        dtacycle_cd: str,
        page_index: int = 1,
        wrttime_idtfr_id: str | None = None,
        grp_id: str | None = None,
        cls_id: str | None = None,
        itm_id: str | None = None,
        start_wrttime: str | None = None,
        end_wrttime: str | None = None,
    ) -> list[dict[str, Any]]:
        params = self._base_params(page_index)
        params["STATBL_ID"] = statbl_id
        params["DTACYCLE_CD"] = dtacycle_cd

        optional_params = {
            "WRTTIME_IDTFR_ID": wrttime_idtfr_id,
            "GRP_ID": grp_id,
            "CLS_ID": cls_id,
            "ITM_ID": itm_id,
            "START_WRTTIME": start_wrttime,
            "END_WRTTIME": end_wrttime,
        }
        for key, value in optional_params.items():
            if value is not None:
                params[key] = value

        return self._get_rows("SttsApiTblData.do", params)

    def _base_params(self, page_index: int) -> dict[str, str | int]:
        return {
            "KEY": self.settings.api_key,
            "Type": self.settings.output_type,
            "pIndex": page_index,
            "pSize": self.settings.page_size,
        }

    def _get_rows(self, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        url = f"{self.BASE_URL}/{endpoint}"
        started = time.perf_counter()
        connect_timeout = min(8, max(2, int(self.settings.timeout_seconds / 3)))
        read_timeout = max(5, int(self.settings.timeout_seconds))

        try:
            response = self.session.get(
                url,
                params=params,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.Timeout as exc:
            elapsed = time.perf_counter() - started
            raise REBApiError(
                "Timeout from REB API "
                f"endpoint={endpoint} elapsed={elapsed:.2f}s "
                f"params={{STATBL_ID:{params.get('STATBL_ID')},DTACYCLE_CD:{params.get('DTACYCLE_CD')},pIndex:{params.get('pIndex')}}}"
            ) from exc
        except requests.RequestException as exc:
            elapsed = time.perf_counter() - started
            raise REBApiError(
                "Request error from REB API "
                f"endpoint={endpoint} elapsed={elapsed:.2f}s err={exc}"
            ) from exc

        elapsed = time.perf_counter() - started
        logger.info(
            "[reb_api] endpoint=%s statbl_id=%s cycle=%s page=%s status=%s elapsed=%.2fs",
            endpoint,
            params.get("STATBL_ID"),
            params.get("DTACYCLE_CD"),
            params.get("pIndex"),
            response.status_code,
            elapsed,
        )

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise REBApiError(f"HTTP error from REB API: {exc} | body={response.text[:300]}") from exc

        payload: Any
        if self.settings.output_type.lower() == "json":
            payload = response.json()
        else:
            raise REBApiError("Only json output is supported in this tool. Set REB_API_TYPE=json.")

        if isinstance(payload, dict):
            result = payload.get("RESULT")
            if isinstance(result, dict) and result.get("CODE") == "INFO-200":
                return []

        rows = _extract_rows(payload)
        if rows is None:
            message = str(payload)[:400]
            raise REBApiError(f"Could not parse row list from REB API payload: {message}")

        return rows


def _extract_rows(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list):
        if not payload:
            return []
        if all(isinstance(item, dict) for item in payload):
            return payload  # direct row list

    if isinstance(payload, dict):
        if "row" in payload and isinstance(payload["row"], list):
            return payload["row"]

        for value in payload.values():
            if isinstance(value, dict) and "row" in value and isinstance(value["row"], list):
                return value["row"]
            if isinstance(value, list):
                for sub_value in value:
                    if isinstance(sub_value, dict) and "row" in sub_value and isinstance(sub_value["row"], list):
                        return sub_value["row"]

    return None
