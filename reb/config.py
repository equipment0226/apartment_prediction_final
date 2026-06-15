"""REB(한국부동산원 R-ONE) API 설정.

API 키는 환경변수 `REB_API_KEY` 를 우선 사용하고, 없으면 기본값을 사용한다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class APISettings:
    api_key: str
    output_type: str = "json"
    page_size: int = 1000
    timeout_seconds: int = 20


def load_settings() -> APISettings:
    api_key = os.environ.get("REB_API_KEY", "06474c7842c44d819a3d8841f7af5e15").strip()
    return APISettings(api_key=api_key)
