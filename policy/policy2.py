import pandas as pd
import numpy as np

# 1. 서울 25개 구 및 강남 4구 정의
gu_list = [
    '강남구', '서초구', '송파구', '용산구', '성동구', '노원구', '마포구', '양천구', '영등포구', '강서구', '강동구',
    '종로구', '중구', '동대문구', '동작구', '광진구', '중랑구', '성북구', '강북구', '도봉구', '은평구', '서대문구', '구로구', '금천구', '관악구'
]
gangnam4 = ['강남구', '서초구', '송파구', '용산구']

# 2. 월별 타임스탬프 (2010.01 ~ 2026.06)
dates = pd.date_range(start='2010-01-01', end='2026-06-01', freq='MS')

# 3. 데이터프레임 초기화
records = []
for d in dates:
    for gu in gu_list:
        records.append({
            'timestamp': d,
            'gu': gu,
            'policy__ltv_tightness': 0,  # -1(완화), 0(중립), 1(강화)
            'policy__dsr_severity': 0    # -1(완화), 0(DTI 등 부분규제), 1(개인별 DSR 강화)
        })

df = pd.DataFrame(records)

# 4. 규제 이력 하드코딩 매핑
# (1) 2010.01 ~ 2014.07 : LTV/DTI 기본 적용 (중립 0)
# (이미 초기값이 0이므로 패스)

# (2) 2014.08 ~ 2017.07 : 최경환 초이노믹스 (LTV 70%, DTI 60% 일괄 완화)
m2 = (df['timestamp'] >= '2014-08-01') & (df['timestamp'] < '2017-08-01')
df.loc[m2, ['policy__ltv_tightness', 'policy__dsr_severity']] = -1

# (3) 2017.08 ~ 2021.06 : 8·2 및 12·16 대책 (LTV 대폭 축소 및 15억 금지, DSR은 아직 국지적)
m3 = (df['timestamp'] >= '2017-08-01') & (df['timestamp'] < '2021-07-01')
df.loc[m3, 'policy__ltv_tightness'] = 1
df.loc[m3, 'policy__dsr_severity'] = 0  # DSR 전면도입 전, DTI 중심

# (4) 2021.07 ~ 2022.12 : 개인별 DSR 1~3단계 도입기 (돈줄 완전 차단)
m4 = (df['timestamp'] >= '2021-07-01') & (df['timestamp'] < '2023-01-01')
df.loc[m4, 'policy__ltv_tightness'] = 1
df.loc[m4, 'policy__dsr_severity'] = 1

# (5) 2023.01 ~ 2025.09 : 1·3 대책 (규제 해제 및 LTV 완화, but DSR은 유지)
m5 = (df['timestamp'] >= '2023-01-01') & (df['timestamp'] < '2025-10-01')
# 강남 4구는 규제지역 유지 (LTV 50% 수준이라 중립(0)으로 매핑)
df.loc[m5 & df['gu'].isin(gangnam4), 'policy__ltv_tightness'] = 0
# 비규제지역 21개 구는 LTV 최대 70% 완화 (-1)
df.loc[m5 & ~df['gu'].isin(gangnam4), 'policy__ltv_tightness'] = -1
# 하지만 DSR 40%와 스트레스 DSR은 서울 전역에 바인딩 (1)
df.loc[m5, 'policy__dsr_severity'] = 1

# (6) 2025.10 ~ 2026.06 : 10·15 대책 (서울 전역 3중 규제 재지정, 스트레스 DSR 3단계)
m6 = (df['timestamp'] >= '2025-10-01')
df.loc[m6, ['policy__ltv_tightness', 'policy__dsr_severity']] = 1

# 5. Excel 파일로 출력
output_path = "seoul_finance_policy_2010_2026.xlsx"
df.to_excel(output_path, index=False)
print(f"✅ Excel 파일 생성 완료: {output_path} (총 {len(df)}행)")