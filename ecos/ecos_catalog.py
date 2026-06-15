"""한국은행 ECOS 수집 지표 목록.

부동산 시장과 연관성이 높은 거시지표를 중심으로 구성.
stat_code / item_code1 등은 ECOS 공식 포털(ecos.bok.or.kr)의
'통계표 목록'에서 확인할 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EcosSeriesConfig:
    name: str           # 내부 식별 이름 (영문 스네이크케이스)
    stat_code: str      # ECOS 통계표 코드
    cycle_type: str     # MM(월), QQ(분기), YY(연)
    item_code1: str = "?"  # 항목코드1 ("?" = 전체)
    item_code2: str = "?"
    item_code3: str = "?"
    item_code4: str = "?"
    description: str = ""  # 한글 설명 (참고용)


# ---------------------------------------------------------------------------
# 기본 수집 지표 (DEFAULT_ECOS_SERIES)
# 부동산 가격에 영향을 주는 주요 거시지표
# ---------------------------------------------------------------------------

DEFAULT_ECOS_SERIES: list[EcosSeriesConfig] = [
    EcosSeriesConfig(
        name="base_rate",
        stat_code="722Y001",
        cycle_type="MM",
        item_code1="0101000",
        description="한국은행 기준금리 (월말)",
    ),
    EcosSeriesConfig(
        name="cd_91d_rate",
        stat_code="721Y001",
        cycle_type="MM",
        item_code1="2010000",
        description="CD 91일 금리 (월평균)",
    ),
    EcosSeriesConfig(
        name="mortgage_rate_new",
        stat_code="121Y006",
        cycle_type="MM",
        item_code1="BECBLA0302",
        description="예금은행 주택담보대출 금리 (신규취급액 기준)",
    ),
    EcosSeriesConfig(
        name="cpi_housing",
        stat_code="901Y009",
        cycle_type="MM",
        item_code1="D",
        description="소비자물가지수 (주택 관련 품목)",
    ),
    EcosSeriesConfig(
        name="m2_avg",
        stat_code="161Y006",
        cycle_type="MM",
        item_code1="BBHA00",
        description="통화량 M2 (평잔, 원계열)",
    ),
    EcosSeriesConfig(
        name="unemployment_rate",
        stat_code="901Y027",
        cycle_type="MM",
        item_code1="I61BC",
        item_code2="I28A",
        description="실업률 (원계열)",
    ),
]
