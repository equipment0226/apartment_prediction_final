"""ECOS API 설정.

API 키는 환경변수 `ECOS_API_KEY` 를 우선 사용하고, 없으면 기본값을 사용한다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class EcosAPISettings:
    api_key: str
    timeout_seconds: int = 20


def load_settings() -> EcosAPISettings:
    api_key = os.environ.get("ECOS_API_KEY", "NIUNC86E0LFBPW0MLKN8").strip()
    return EcosAPISettings(api_key=api_key)
