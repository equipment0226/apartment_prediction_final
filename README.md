# 서울 아파트 실거래 단지 추출 + KB 과거시세 수집

`data/sample/2010.csv ~ 2025.csv` (서울시 아파트 실거래가, EUC-KR) 를 처리하여
1. 서울시 전체 아파트 **단지명 목록(중복 제거)** 을 만들고
2. 각 단지를 **kbland.kr** 에서 검색해 모든 평형의 **KB 과거시세 파일** 을 내려받습니다.

## 폴더 구조
```
data/sample/            원본 실거래가 CSV (2010~2025)
src/
  extract_apartments.py 1단계: 단지명 추출
  crawl_kbland.py       2단계: kbland 과거시세 크롤러
output/
  apartments.csv        1단계 결과 (고유 단지명 7,947개)
  downloads/            2단계 결과
    _manifest.csv       다운로드 이력 (재개용)
    _complex_index.csv  검색으로 발견한 단지 목록
    <시군구>/<단지번호>_<단지명>/<면적일련번호>_<전용면적>.xlsx
```

## 설치
```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 1단계 — 단지명 목록 만들기
```powershell
python src\extract_apartments.py
```
- 16개 CSV의 **F열(단지명)** 을 읽어 중복 제거 후 `output/apartments.csv` 생성.
- EUC-KR 인코딩과 안내문 헤더를 자동 처리합니다.

## 2단계 — KB 과거시세 수집
```powershell
# 테스트: 키워드 1개만 (창 보임)
python src\crawl_kbland.py --keyword "헬리오시티"

# 전체 실행 (창 숨김)
python src\crawl_kbland.py --headless
```
주요 옵션:
| 옵션 | 설명 |
|------|------|
| `--keyword "이름"` | apartments.csv 대신 직접 키워드 지정(반복 가능) |
| `--limit N` | 앞에서 N개 키워드만 처리(테스트) |
| `--headless` | 브라우저 창 숨김 |
| `--all-regions` | 서울 외 지역도 포함(기본은 서울만) |
| `--all-types` | 아파트 외 유형도 포함 |
| `--delay-min/--delay-max` | 요청 간 지연(초). 차단 방지용 |

동작:
1. 단지명으로 `intgraSerch` 통합검색 → 단지 목록
2. **서울 + 아파트** 만 필터 (옵션으로 해제)
3. 단지별 `mpriByType` → 평형(면적일련번호) 목록
4. 평형별 `perMnPastPriceExcelDownload` → 과거시세 파일 저장

## 동작 원리 / 주의사항
- **파일 형식은 XLSX 입니다.** kbland "과거 시세 다운로드"가 제공하는 실제 파일이
  엑셀(xlsx)이라 CSV가 아닌 xlsx로 저장됩니다. (매매·전세 주간 KB시세 history 포함)
- **차단 우회**: kbland API는 일반 HTTP 클라이언트(`requests`)를 WAF로 차단합니다.
  그래서 실제 Chromium(Playwright) 안에서 `fetch` 로 호출/다운로드합니다.
- **중복 제거**: 서로 다른 단지명 검색이 같은 단지를 반환해도 `_complex_index.csv`/`_manifest.csv`
  기준으로 이미 받은 단지·평형은 건너뜁니다. → 중단 후 재실행하면 이어서 진행됩니다.
- **규모**: 단지명 약 7,947개를 검색하며, 단지별 평형 수만큼 파일이 생성됩니다(단지당 수~수십 개).
  전체 실행은 장시간 걸리며, 과도한 속도는 차단 위험이 있으니 기본 지연을 유지하세요.
- `output/apartments.csv` 에는 단지명이 비어 번지로만 표기된 항목(예 `(780-29)`) 314개가 포함됩니다.
  이들은 검색 결과가 없어 자연히 건너뜁니다.
