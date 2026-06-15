from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesConfig:
    name: str
    statbl_id: str
    dtacycle_cd: str
    itm_id: str | None = None


# ---------------------------------------------------------------------------
# KB부동산(KBREB) 로 이관된 컬럼
# ---------------------------------------------------------------------------
# ``apt_sale_index`` / ``apt_jeonse_index`` 두 항목(아파트 매매/전세 가격지수)은 한국부동산(REB)
# API 가 서울을 5개 권역 단위로만 제공해 구 단위 해상도가 없었다. 이 두 지수는
# 이제 KB부동산 데이터허브(구 단위·월 단위)에서 수집한다(house_price_agent.kbreb_api
# 의 collect_kbreb_data / DEFAULT_KBREB_SERIES 참조). 따라서 아래 REB 카탈로그에는
# 더 이상 포함하지 않으며, 메타테이블 컬럼명(reb__apt_sale_index / reb__apt_jeonse_index)은 동일하게 유지된다.

# Default target/features selected from the provided REB table catalog.
# REB ᗀ apt_sale_index 는 KB 구 단위 지수로 대체되어 더 이상 수집하지 않으므로,
# REB 수집 타겟은 매매수급동향(apt_sale_supply_demand)으로 둔다.
DEFAULT_TARGET_SERIES = SeriesConfig(
    name="apt_sale_supply_demand",
    statbl_id="A_2024_00076",  # (월) 매매수급동향_아파트
    dtacycle_cd="MM",
)

DEFAULT_FEATURE_SERIES: list[SeriesConfig] = [
    SeriesConfig(
        name="apt_sale_supply_demand",
        statbl_id="A_2024_00076",  # (월) 매매수급동향_아파트
        dtacycle_cd="MM",
    ),
    SeriesConfig(
        name="apt_jeonse_supply_demand",
        statbl_id="A_2024_00077",  # (월) 전세수급동향_아파트
        dtacycle_cd="MM",
    ),
    SeriesConfig(
        name="apt_monthly_rent_supply_demand",
        statbl_id="A_2024_00078",  # (월) 월세수급동향_아파트
        dtacycle_cd="MM",
    ),
]


AVAILABLE_FEATURE_SERIES: dict[str, SeriesConfig] = {
    series.name: series
    for series in [
        *DEFAULT_FEATURE_SERIES,
        # apt_jeonse_index (A_2024_00182) 는 KB 구 단위 지수로 이관되어 REB 카탈로그에서 제거됨.
        SeriesConfig(
            name="apt_sale_price_index_house_total",
            statbl_id="A_2024_00016",  # (월) 매매가격지수_주택종합
            dtacycle_cd="MM",
        ),
        SeriesConfig(
            name="land_price_index_monthly",
            statbl_id="A_2024_00901",  # (월) 지역별 지가지수
            dtacycle_cd="MM",
        ),
    ]
}


def resolve_feature_series(selected_names: list[str] | None) -> list[SeriesConfig]:
    if not selected_names:
        return DEFAULT_FEATURE_SERIES

    resolved: list[SeriesConfig] = []
    for name in selected_names:
        series = AVAILABLE_FEATURE_SERIES.get(name)
        if series is not None:
            resolved.append(series)

    if not resolved:
        return DEFAULT_FEATURE_SERIES

    return resolved


# ---------------------------------------------------------------------------
# 서울 5개 권역(생활권) 정의
# ---------------------------------------------------------------------------
# 한국부동산원 통계는 '서울 전체' 외에 서울을 5개 권역으로 세분한 행을 제공한다.
# 권역의 CLS_ID(지역코드)는 statbl_id마다 다르지만 CLS_NM(권역명)은 항상 동일하므로
# 수집/매핑은 권역명을 기준으로 수행한다.
#
# 참고 (statbl_id별 CLS_ID 예시):
#   A_2024_00188  도심권=510005 동북권=510006 서북권=510007 서남권=510008 동남권=510009
#   A_2024_00076  도심권=520010 동북권=520011 서북권=520012 서남권=520014 동남권=520015
#   A_2024_00178  도심권=510008 동북권=510009 서북권=510010 서남권=510011 동남권=510012
SEOUL_KWONYEOK_TO_GU: dict[str, list[str]] = {
    "도심권": ["종로구", "중구", "용산구"],
    "동북권": ["성동구", "광진구", "동대문구", "중랑구", "성북구", "강북구", "도봉구", "노원구"],
    "서북권": ["은평구", "서대문구", "마포구"],
    "서남권": ["양천구", "강서구", "구로구", "금천구", "영등포구", "동작구", "관악구"],
    "동남권": ["서초구", "강남구", "송파구", "강동구"],
}

# 권역명 집합 (빠른 멤버십 체크용)
SEOUL_KWONYEOK_NAMES: frozenset[str] = frozenset(SEOUL_KWONYEOK_TO_GU.keys())

# 구 → 권역명 역매핑
SEOUL_GU_TO_KWONYEOK: dict[str, str] = {
    gu: kwon
    for kwon, gu_list in SEOUL_KWONYEOK_TO_GU.items()
    for gu in gu_list
}
