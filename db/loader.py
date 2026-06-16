"""ipynb / 스크립트에서 meta_ml · meta2 데이터를 DB 로부터 읽어오는 API.

기존 파이프라인의 `load_global_panel(cutoff_end)` 와 동일한 컬럼·행 형태로 반환해
ipynb 코드를 한 줄만 바꿔도 동작하도록 설계.

사용 예 (ipynb):

    from db.loader import load_global_panel
    panel = load_global_panel(cutoff_end="2024-05")
"""
from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd
from sqlalchemy import text

from .config import get_engine
from .schema import META2, META_ML


# ---- meta_ml (ML 학습 패널) -------------------------------------------------

def load_global_panel(
    cutoff_end: Optional[str] = None,
    cutoff_start: Optional[str] = None,
    gu: Optional[Iterable[str]] = None,
    dong: Optional[Iterable[str]] = None,
    danji: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """meta_ml 전체 패널을 단일 DataFrame 으로 반환 (ipynb 의 load_global_panel 대체).

    Parameters
    ----------
    cutoff_end : 'YYYY-MM' or 'YYYY-MM-DD'
        Header_Timestamp <= cutoff_end 만 반환 (누수 차단).
    cutoff_start : 옵션. >= cutoff_start.
    gu, dong, danji : 부분집합 필터.
    """
    where = []
    params: dict = {}
    if cutoff_end:
        where.append("`Header_Timestamp` <= :ce")
        params["ce"] = _to_eom(cutoff_end)
    if cutoff_start:
        where.append("`Header_Timestamp` >= :cs")
        params["cs"] = _to_eom(cutoff_start, eom=False)
    if gu:
        where.append("`Header_구` IN :gu")
        params["gu"] = tuple(gu)
    if dong:
        where.append("`Header_동` IN :dong")
        params["dong"] = tuple(dong)
    if danji:
        where.append("`Header_단지명` IN :danji")
        params["danji"] = tuple(danji)

    cols = ", ".join(f"`{c.name}`" for c in META_ML.columns if c.name != "ingested_at")
    sql = f"SELECT {cols} FROM `meta_ml`"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY `Header_구`, `Header_동`, `Header_단지명`, `Header_평형`, `Header_Timestamp`"

    eng = get_engine()
    df = pd.read_sql(text(sql), eng, params=params)
    if "Header_Timestamp" in df.columns:
        df["Header_Timestamp"] = pd.to_datetime(df["Header_Timestamp"])
    return df


# ---- meta2 (메타데이터/시세 원본) -------------------------------------------

def load_meta2_panel(
    cutoff_end: Optional[str] = None,
    cutoff_start: Optional[str] = None,
    gu: Optional[Iterable[str]] = None,
    dong: Optional[Iterable[str]] = None,
    danji: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    where = []
    params: dict = {}
    if cutoff_end:
        where.append("`Timestamp` <= :ce")
        params["ce"] = _to_eom(cutoff_end)
    if cutoff_start:
        where.append("`Timestamp` >= :cs")
        params["cs"] = _to_eom(cutoff_start, eom=False)
    if gu:
        where.append("`구` IN :gu"); params["gu"] = tuple(gu)
    if dong:
        where.append("`동` IN :dong"); params["dong"] = tuple(dong)
    if danji:
        where.append("`아파트명` IN :danji"); params["danji"] = tuple(danji)

    cols = ", ".join(f"`{c.name}`" for c in META2.columns if c.name != "ingested_at")
    sql = f"SELECT {cols} FROM `meta2`"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY `구`, `동`, `아파트명`, `전용면적`, `Timestamp`"

    eng = get_engine()
    df = pd.read_sql(text(sql), eng, params=params)
    if "Timestamp" in df.columns:
        df["Timestamp"] = pd.to_datetime(df["Timestamp"])
    return df


# ---- 단지 단위 로딩 (ipynb 의 read_csv(per-단지) 자리에 사용) ----------------

def load_danji(
    table: str,
    danji_name: str,
    pyeong: Optional[str] = None,
    cutoff_end: Optional[str] = None,
) -> pd.DataFrame:
    """단지명(+평형) 기준 슬라이스.

    table: 'meta_ml' | 'meta2'
    """
    if table == "meta_ml":
        return load_global_panel(
            cutoff_end=cutoff_end,
            danji=[danji_name],
        ).pipe(lambda d: d[d["Header_평형"] == pyeong] if pyeong else d)
    elif table == "meta2":
        return load_meta2_panel(
            cutoff_end=cutoff_end,
            danji=[danji_name],
        ).pipe(lambda d: d[d["전용면적"] == pyeong] if pyeong else d)
    else:
        raise ValueError(f"unknown table: {table}")


# ---- helpers ---------------------------------------------------------------

def _to_eom(s: str, eom: bool = True) -> str:
    """'YYYY-MM' → 'YYYY-MM-(말일)' / 'YYYY-MM-DD' 는 통과."""
    s = s.strip()
    if len(s) == 7 and s[4] == "-":  # YYYY-MM
        ts = pd.Timestamp(s + "-01")
        return (ts + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d") if eom else ts.strftime("%Y-%m-%d")
    return s
