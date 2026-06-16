# db/ — MySQL 데이터 레이어

`meta_ml` / `meta2` CSV 데이터를 MySQL(Railway)로 적재하고,
ipynb / 스크립트에서 동일 컬럼으로 읽기 위한 모듈.

## 폴더 구조

```
db/
├── .env              # 자격증명 (gitignore)
├── .env.example      # 템플릿
├── config.py         # 환경변수 로드 + SQLAlchemy 엔진
├── schema.py         # 테이블 DDL (meta_ml, meta2)
├── load_csv_to_db.py # CSV → DB 일괄 적재 CLI
├── loader.py         # DB → DataFrame 조회 API
└── __init__.py
```

## 설계 결정

- **테이블 2개만 사용**: `meta_ml` (5,973 CSV → 1 테이블), `meta2` (12,518 CSV → 1 테이블).
  - ipynb 의 `load_global_panel()` 이 이미 `glob+concat` 으로 모든 CSV 를 1개 패널로 합치므로,
    DB 도 동일 구조(통합 테이블)일 때 코드 변경이 최소화됨.
  - 폴더 구조(`시/구/동`)는 행 데이터에 컬럼으로 보존되며, `(구, 동, 단지명, 평형, Timestamp)` 인덱스로 빠르게 슬라이스됨.
  - 원본 파일 경로는 `src_path` 컬럼으로 추적.

## 사전 준비

```powershell
pip install -r requirements.txt
```

`db/.env` 가 이미 생성되어 있습니다. 키 회전 시 `.env.example` 을 참고하세요.

## 적재

```powershell
# 첫 실행: 두 테이블 모두 drop → create → insert
python -m db.load_csv_to_db

# 단일 테이블
python -m db.load_csv_to_db --only meta_ml
python -m db.load_csv_to_db --only meta2

# 기존 테이블에 추가 (append 모드)
python -m db.load_csv_to_db --mode append
```

진행 상황(5초 간격 files/s · 누적 rows) 이 표시됩니다.

## 조회 (ipynb 에서)

```python
from db.loader import load_global_panel, load_meta2_panel, load_danji

# ipynb 의 기존 load_global_panel(cutoff_end) 와 동일 시그니처
panel = load_global_panel(cutoff_end="2024-05")

# 부분집합
gangnam = load_global_panel(cutoff_end="2024-05", gu=["강남구"])

# 단지 단위
df = load_danji("meta_ml", "디에이치퍼스티어아이파크", pyeong="84.82", cutoff_end="2024-05")
```

반환 DataFrame 의 컬럼명은 CSV 와 동일하므로 기존 파이프라인 코드를 그대로 사용 가능.

## ipynb 마이그레이션 (1줄)

`backtest_hybrid_pipeline.ipynb` 의 `load_global_panel` 함수 본문을 통째로 다음으로 교체:

```python
def load_global_panel(cutoff_end: str) -> pd.DataFrame:
    from db.loader import load_global_panel as _db_load
    return _db_load(cutoff_end=cutoff_end)
```

`PANEL_ROOT` / `glob` 의존이 사라지고, 컬럼·정렬은 동일하게 유지됩니다.
