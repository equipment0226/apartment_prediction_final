"""백테스트 아파트 선정 헬퍼.

조건:
 1. 구(區) 단위로 1개씩 선정
 2. 준공년도 < 2015 (2015년보다 오래된 단지)
 3. meta_ml 데이터에 존재하는 단지만
"""
import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
META2 = ROOT / "meta2" / "output"
METAML = ROOT / "meta_ml" / "output"

# 사용자가 준 후보 리스트 (시 구 동 단지명)
CANDIDATES_RAW = """송파구 가락동 헬리오시티
강남구 개포동 디에이치퍼스티어아이파크
송파구 신천동 파크리오
송파구 잠실동 잠실엘스
송파구 방이동 올림픽선수기자촌
송파구 잠실동 리센츠
서초구 반포동 래미안원베일리
서초구 반포동 반포자이
송파구 잠실동 잠실주공(5단지)
강남구 대치동 은마
강남구 압구정동 현대(신현대)
서초구 반포동 래미안퍼스티지
송파구 문정동 올림픽훼밀리타운
강남구 대치동 한보미도맨션
송파구 잠실동 트리지움
강남구 도곡동 도곡렉슬
강남구 개포동 개포자이프레지던스
강남구 압구정동 현대(6,7차)
강동구 고덕동 고덕그라시움
서초구 서초동 삼풍
송파구 잠실동 레이크팰리스
서초구 반포동 아크로리버파크
강동구 상일동 고덕아르테온
강남구 압구정동 현대(1,2차)
서초구 잠원동 신반포(한신2차)
마포구 아현동 마포래미안푸르지오
양천구 신정동 목동신시가지(14단지)
송파구 신천동 장미(1차)
강동구 고덕동 래미안힐스테이트고덕
송파구 잠실동 아시아선수촌
양천구 목동 목동신시가지(7단지)
강남구 개포동 개포래미안포레스트
강남구 일원동 디에이치자이개포
서초구 잠원동 신반포(한신4차)
용산구 서빙고동 신동아
송파구 잠실동 우성1,2,3차
강남구 도곡동 타워팰리스(1차)
강남구 개포동 래미안블레스티지
강동구 암사동 강동롯데캐슬퍼스트
강남구 대치동 래미안대치팰리스1단지
양천구 신정동 목동신시가지(13단지)
용산구 이촌동 한가람
양천구 목동 목동신시가지(5단지)
서대문구 남가좌동 DMC파크뷰자이
양천구 신정동 목동신시가지(9단지)
마포구 성산동 성산시영
중구 신당동 남산타운
양천구 신정동 목동신시가지(10단지)
강남구 대치동 선경(1,2차)
양천구 목동 목동신시가지(1단지)"""


def norm(s: str) -> str:
    """비교용 정규화: 괄호/특수문자/공백 제거, 소문자."""
    s = re.sub(r"[\s()（）.,·\-]", "", s or "")
    return s.lower()


def main() -> None:
    # meta2: (구 정규화 단지명) -> (동, 준공년도, 원본단지명)
    m2 = {}
    for src in META2.rglob("*.csv"):
        with src.open(encoding="utf-8-sig") as f:
            r = next(csv.DictReader(f), None)
        if not r:
            continue
        gu = r.get("구") or ""
        apt = r.get("아파트명") or ""
        m2[(gu, norm(apt))] = (r.get("동") or "", r.get("준공년도") or "", apt)

    # meta_ml 존재 단지 (구, 정규화 단지명)
    ml = set()
    ml_name = {}
    for src in METAML.rglob("*.csv"):
        gu = src.parts[-3]
        apt = src.stem.rsplit("_", 1)[0]
        ml.add((gu, norm(apt)))
        ml_name[(gu, norm(apt))] = apt

    rows = []
    for line in CANDIDATES_RAW.strip().splitlines():
        parts = line.split(maxsplit=2)
        gu, dong, apt = parts[0], parts[1], parts[2]
        nk = (gu, norm(apt))
        in_ml = nk in ml
        rec = m2.get(nk)
        year = rec[1] if rec else ""
        try:
            yr = int(year[:4]) if year else None
        except ValueError:
            yr = None
        rows.append({
            "구": gu, "동": dong, "단지명": apt,
            "준공년도": year or "?", "준공연": yr,
            "meta_ml존재": in_ml,
            "조건충족": in_ml and (yr is not None and yr < 2015),
        })

    # 전체 진단표
    print("=== 후보 진단 (구 / 단지 / 준공 / meta_ml존재 / 조건충족) ===")
    for x in rows:
        flag = "O" if x["조건충족"] else " "
        ml_f = "ML" if x["meta_ml존재"] else "--"
        print(f"[{flag}] {x['구']:5} {x['단지명']:24} 준공 {x['준공년도']:>6}  {ml_f}")

    # 구별 1개 선정 (조건충족 중, 준공 오래된 순 우선 → 가장 오래된 단지)
    print("\n=== 구별 최종 선정 (조건충족 단지 중 가장 오래된 1개) ===")
    by_gu = {}
    for x in rows:
        if not x["조건충족"]:
            continue
        g = x["구"]
        if g not in by_gu or x["준공연"] < by_gu[g]["준공연"]:
            by_gu[g] = x
    for g in sorted(by_gu):
        x = by_gu[g]
        print(f"  {g:5} → {x['단지명']:24} (준공 {x['준공년도']}, {x['동']})")
    print(f"\n선정된 구 수: {len(by_gu)}")

    # 조건 미충족 구(후보는 있으나 선정 불가) 안내
    cand_gus = {x["구"] for x in rows}
    miss = sorted(cand_gus - set(by_gu))
    if miss:
        print(f"선정 불가 구: {miss} (meta_ml 없음 또는 준공 2015년 이후)")


if __name__ == "__main__":
    main()
