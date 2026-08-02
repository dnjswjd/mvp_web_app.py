"""
MVP: 세무 판단이력 DB + 사례기반 자동매칭 (Phase 1 - 세무팀)
----------------------------------------------------------------
컨셉: 과거 세무처리 사례를 축적해두고, 새로운 비용전표가 들어오면
      문자 n-gram TF-IDF 유사도로 가장 비슷한 과거 사례를 찾아
      처리안을 자동 제시한다. 유사도가 낮으면(=신규/예외 패턴)
      자동 제시하지 않고 "전문가 검토 필요"로 라우팅한다.

외부 라이브러리(scikit-learn 등) 없이 표준 라이브러리만으로 구현한
경량 MVP입니다. 실제 구축 시에는 ERP 전표·증빙 데이터와 연동하고,
임베딩 모델 등으로 유사도 품질을 높이는 것을 권장합니다.
"""

import math
import re
from collections import Counter

import pandas as pd

# ----------------------------------------------------------------
# 1) 과거 세무처리 사례 DB (실제로는 ERP/전표 시스템에서 축적)
# ----------------------------------------------------------------
CASES = [
    {
        "id": "C001", "계정과목": "접대비",
        "지출내역": "거래처 임원과 저녁 식사 접대비 지출",
        "손금인정(법인세)": "한도 내 손금 인정 (한도 초과분은 손금불산입)",
        "매입세액공제(부가세)": "공제 불가 (접대비 관련 매입세액)",
        "판단근거": "접대비는 법인세법상 한도 내에서만 손금 인정, 부가세법상 매입세액 공제는 원천 배제",
    },
    {
        "id": "C002", "계정과목": "접대비",
        "지출내역": "거래처 대표와 골프 접대 라운딩 비용",
        "손금인정(법인세)": "한도 내 손금 인정 (한도 초과분은 손금불산입)",
        "매입세액공제(부가세)": "공제 불가 (접대비 관련 매입세액)",
        "판단근거": "접대 성격의 유흥·골프 비용은 접대비로 분류, C001과 동일 처리",
    },
    {
        "id": "C003", "계정과목": "회의비",
        "지출내역": "팀 내부 회의용 다과 및 음료 구입",
        "손금인정(법인세)": "전액 손금 인정",
        "매입세액공제(부가세)": "공제 가능",
        "판단근거": "사내 업무 회의 목적의 통상적 비용으로 접대비 아닌 회의비로 판단",
    },
    {
        "id": "C004", "계정과목": "여비교통비",
        "지출내역": "해외 출장 항공권 및 숙박비",
        "손금인정(법인세)": "전액 손금 인정",
        "매입세액공제(부가세)": "공제 가능 (세금계산서 수취분)",
        "판단근거": "업무 관련성이 명확한 출장비로 통상경비 인정",
    },
    {
        "id": "C005", "계정과목": "광고선전비",
        "지출내역": "신제품 홍보 SNS 온라인 광고비",
        "손금인정(법인세)": "전액 손금 인정",
        "매입세액공제(부가세)": "공제 가능",
        "판단근거": "불특정 다수 대상 광고로 접대비가 아닌 광고선전비로 분류",
    },
    {
        "id": "C006", "계정과목": "복리후생비",
        "지출내역": "직원 명절 선물 세트 구입",
        "손금인정(법인세)": "전액 손금 인정 (사회통념상 인정 범위 내)",
        "매입세액공제(부가세)": "공제 가능",
        "판단근거": "임직원 전체 대상 복리후생 목적, 사회통념상 타당한 범위",
    },
    {
        "id": "C007", "계정과목": "지급수수료",
        "지출내역": "계약 검토 관련 법률 자문 수수료",
        "손금인정(법인세)": "전액 손금 인정",
        "매입세액공제(부가세)": "공제 가능",
        "판단근거": "업무 수행에 직접 관련된 용역 대가로 통상경비 인정",
    },
    {
        "id": "C008", "계정과목": "소모품비",
        "지출내역": "사무용품(문구류) 구매",
        "손금인정(법인세)": "전액 손금 인정",
        "매입세액공제(부가세)": "공제 가능",
        "판단근거": "일상적 업무용 소모품으로 통상경비 인정",
    },
    {
        "id": "C009", "계정과목": "접대비",
        "지출내역": "신규 거래처 계약 체결 축하 식사 및 선물",
        "손금인정(법인세)": "한도 내 손금 인정 (한도 초과분은 손금불산입)",
        "매입세액공제(부가세)": "공제 불가 (접대비 관련 매입세액)",
        "판단근거": "특정 거래처 대상 접대 목적 지출로 접대비 분류",
    },
    {
        "id": "C010", "계정과목": "차량유지비",
        "지출내역": "비영업용 승용차 유류비",
        "손금인정(법인세)": "전액 손금 인정 (업무용 사용 한도 내)",
        "매입세액공제(부가세)": "공제 불가 (비영업용 소형승용차 관련 매입세액)",
        "판단근거": "비영업용 소형승용차 관련 매입세액은 부가세법상 공제 배제",
    },
]

df = pd.DataFrame(CASES)


# ----------------------------------------------------------------
# 2) 경량 TF-IDF(문자 n-gram) + 코사인 유사도  — 외부 라이브러리 불필요
# ----------------------------------------------------------------
def char_ngrams(text, n_range=(2, 3)):
    text = re.sub(r"\s+", " ", text.strip())
    grams = []
    for n in range(n_range[0], n_range[1] + 1):
        grams += [text[i:i + n] for i in range(len(text) - n + 1)]
    return grams


class TinyTfidf:
    def __init__(self, documents):
        self.docs_tokens = [char_ngrams(d) for d in documents]
        self.vocab = sorted(set(t for toks in self.docs_tokens for t in toks))
        self.idf = self._compute_idf()
        self.doc_vecs = [self._vectorize(toks) for toks in self.docs_tokens]

    def _compute_idf(self):
        n_docs = len(self.docs_tokens)
        df_count = Counter()
        for toks in self.docs_tokens:
            for t in set(toks):
                df_count[t] += 1
        return {t: math.log((1 + n_docs) / (1 + df_count[t])) + 1 for t in self.vocab}

    def _vectorize(self, tokens):
        tf = Counter(tokens)
        total = sum(tf.values()) or 1
        vec = {t: (c / total) * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def transform_query(self, text):
        return self._vectorize(char_ngrams(text))

    @staticmethod
    def cosine(vec_a, vec_b):
        common = set(vec_a) & set(vec_b)
        return sum(vec_a[t] * vec_b[t] for t in common)

    def most_similar(self, query_text, top_k=3):
        qvec = self.transform_query(query_text)
        sims = [self.cosine(qvec, dv) for dv in self.doc_vecs]
        order = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)[:top_k]
        return [(i, sims[i]) for i in order]


tfidf = TinyTfidf(df["지출내역"].tolist())

CONFIDENCE_THRESHOLD = 0.30  # 이 값 미만이면 "신규/예외 → 전문가 검토"로 라우팅


# ----------------------------------------------------------------
# 3) 신규 비용전표 판단 함수
# ----------------------------------------------------------------
def judge_new_expense_data(new_text, top_k=3):
    """구조화된 결과(dict)를 반환 — CLI 출력과 웹 UI가 이 함수를 공유"""
    ranked = tfidf.most_similar(new_text, top_k=top_k)
    best_idx, best_sim = ranked[0]
    routed_to_expert = best_sim < CONFIDENCE_THRESHOLD

    similar_cases = [
        {
            "id": df.iloc[idx]["id"], "similarity": sim,
            "계정과목": df.iloc[idx]["계정과목"], "지출내역": df.iloc[idx]["지출내역"],
        }
        for idx, sim in ranked
    ]

    result = {
        "query": new_text,
        "routed_to_expert": routed_to_expert,
        "best_similarity": best_sim,
        "similar_cases": similar_cases,
        "threshold": CONFIDENCE_THRESHOLD,
    }
    if not routed_to_expert:
        best = df.iloc[best_idx]
        result["suggestion"] = {
            "계정과목": best["계정과목"],
            "손금인정(법인세)": best["손금인정(법인세)"],
            "매입세액공제(부가세)": best["매입세액공제(부가세)"],
            "판단근거": best["판단근거"],
            "근거사례": best["id"],
        }
    return result


def judge_new_expense(new_text, top_k=3):
    result = judge_new_expense_data(new_text, top_k=top_k)

    print(f"\n{'='*78}")
    print(f"[신규 비용전표] {new_text}")
    print(f"{'-'*78}")
    if result["routed_to_expert"]:
        print(f"판정: ⚠ 신규/예외 패턴 (최고 유사도 {result['best_similarity']:.2f} < "
              f"임계값 {CONFIDENCE_THRESHOLD}) → 전문가 검토로 라우팅")
    else:
        s = result["suggestion"]
        print(f"판정: ✅ 자동 처리안 제시 (유사도 {result['best_similarity']:.2f}, 근거사례 {s['근거사례']})")
        print(f"  - 추정 계정과목        : {s['계정과목']}")
        print(f"  - 손금인정(법인세)     : {s['손금인정(법인세)']}")
        print(f"  - 매입세액공제(부가세) : {s['매입세액공제(부가세)']}")
        print(f"  - 판단근거(과거사례)   : {s['판단근거']}")
        print(f"  - ※ 담당자 최종 확인 후 반영 (자동 확정 아님)")

    print(f"  참고 유사사례 Top-{top_k}:")
    for c in result["similar_cases"]:
        print(f"    · {c['id']} (유사도 {c['similarity']:.2f}) [{c['계정과목']}] {c['지출내역']}")
    return {"routed_to_expert": result["routed_to_expert"], "similarity": result["best_similarity"]}


# ----------------------------------------------------------------
# 4) 데모 실행
# ----------------------------------------------------------------
if __name__ == "__main__":
    print("세무 판단이력 DB + 사례기반 자동매칭 — MVP 데모")
    print(f"과거 사례 {len(df)}건 로드 완료\n")

    demo_queries = [
        "거래처 부장님과 저녁 접대 자리 비용",          # C001/C002/C009와 유사 → 자동 처리안
        "부서 워크숍 저녁 회식비",                       # 애매 - 회의비/접대비 경계 케이스
        "해외 바이어 출장 항공권 구매",                  # C004와 유사 → 자동 처리안
        "신규 직원 재택근무용 모니터 구매",              # 과거 사례와 유사도 낮음 → 신규/예외
    ]
    results = [judge_new_expense(q) for q in demo_queries]

    n_auto = sum(1 for r in results if not r["routed_to_expert"])
    print(f"\n{'='*78}")
    print(f"요약: {len(results)}건 중 {n_auto}건 자동 처리안 제시, "
          f"{len(results)-n_auto}건 전문가 검토 라우팅")
    print("(실제 도입 시: 과거 사례 수가 늘어날수록 자동 처리안 비율이 상승하는 구조)")
