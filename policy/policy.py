import pandas as pd
import numpy as np

# 1. 서울 25개 구 정의 및 권역 그룹화
gu_list = [
    '강남구', '서초구', '송파구', '용산구', '성동구', '노원구', '마포구', '양천구', '영등포구', '강서구', '강동구',
    '종로구', '중구', '동대문구', '동작구',
    '광진구', '중랑구', '성북구', '강북구', '도봉구', '은평구', '서대문구', '구로구', '금천구', '관악구'
]

gangnam3 = ['강남구', '서초구', '송파구']
gangnam4 = ['강남구', '서초구', '송파구', '용산구']
tugi_11 = gangnam4 + ['강동구', '성동구', '노원구', '마포구', '양천구', '영등포구', '강서구']
tugi_15 = tugi_11 + ['종로구', '중구', '동대문구', '동작구']

# 2. 월별 타임스탬프 (2010.01 ~ 2026.06)
dates = pd.date_range(start='2010-01-01', end='2026-06-01', freq='MS')

# 3. 데이터프레임 초기화
records = []
for d in dates:
    for gu in gu_list:
        records.append({
            'timestamp': d,
            'gu': gu,
            'policy__is_speculative': 0, # 투기지역
            'policy__is_overheated': 0,  # 투기과열지구
            'policy__is_regulated': 0    # 조정대상지역
        })

df = pd.DataFrame(records)

# 4. 규제 이력(마일스톤) 하드코딩 매핑
# (1) 2010.01 ~ 2012.05 : 강남 3구 투기 및 투기과열 유지
m1 = (df['timestamp'] <= '2012-05-01') & (df['gu'].isin(gangnam3))
df.loc[m1, ['policy__is_speculative', 'policy__is_overheated']] = 1

# (2) 2012.06 ~ 2016.10 : 전면 해제기 (기본값 0 유지)

# (3) 2016.11 ~ 2017.07 : 11·3 대책 (전역 조정대상지역 신설)
m3 = (df['timestamp'] >= '2016-11-01') & (df['timestamp'] < '2017-08-01')
df.loc[m3, 'policy__is_regulated'] = 1

# (4) 2017.08 ~ 2018.07 : 8·2 대책 (전역 조정+투기과열, 11개구 투기지역)
m4 = (df['timestamp'] >= '2017-08-01') & (df['timestamp'] < '2018-08-01')
df.loc[m4, ['policy__is_regulated', 'policy__is_overheated']] = 1
df.loc[m4 & df['gu'].isin(tugi_11), 'policy__is_speculative'] = 1

# (5) 2018.08 ~ 2022.12 : 8·27 대책 (투기지역 4곳 추가 -> 15개구)
m5 = (df['timestamp'] >= '2018-08-01') & (df['timestamp'] < '2023-01-01')
df.loc[m5, ['policy__is_regulated', 'policy__is_overheated']] = 1
df.loc[m5 & df['gu'].isin(tugi_15), 'policy__is_speculative'] = 1

# (6) 2023.01 ~ 2025.09 : 1·3 대책 (강남3구+용산만 3중 규제 유지, 나머지 해제)
m6 = (df['timestamp'] >= '2023-01-01') & (df['timestamp'] < '2025-10-01')
df.loc[m6 & df['gu'].isin(gangnam4), ['policy__is_regulated', 'policy__is_overheated', 'policy__is_speculative']] = 1

# (7) 2025.10 ~ 2026.06 : 10·15 대책 (전역 투기과열+조정대상 재지정, 투기지역은 강남4구 유지)
m7 = (df['timestamp'] >= '2025-10-01')
df.loc[m7, ['policy__is_regulated', 'policy__is_overheated']] = 1
df.loc[m7 & df['gu'].isin(gangnam4), 'policy__is_speculative'] = 1

# 5. Excel 파일로 출력
output_path = "seoul_policy_history_2010_2026.xlsx"
df.to_excel(output_path, index=False)
print(f"✅ Excel 파일 생성 완료: {output_path} (총 {len(df)}행)")