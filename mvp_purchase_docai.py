"""
MVP: 구매 다국어 서류 표준화 파일럿 + 조건반영 총액비교 (Phase 1 - 구매팀)
----------------------------------------------------------------------
컨셉: 상위 거래처(영어/중국어/일본어 서식)에서 오는 견적서·인보이스를
      표준 스키마(거래처/품목/수량/단가/통화/총액/인도조건)로 자동 변환하고,
      누락 필드는 "사람 검토 필요"로 플래그한다.
      이후 환율을 반영해 원화 환산 총액을 계산하고, 사내 발주금액(PO)과
      비교해 조건반영 비교(규칙기반)까지 한 번에 보여준다.

실제 파일럿에서는 라벨 사전 매칭 대신 사내 문서AI(OCR+LLM) 모델이
이 역할을 하지만, 이 MVP는 "표준 스키마로 변환 → 사람 확인" 이라는
설계 원칙 자체를 외부 API 없이 규칙기반으로 재현한 것입니다.
"""

import re

# ----------------------------------------------------------------
# 1) 표준 스키마 & 다국어 라벨 사전 (실제로는 문서AI 모델이 이 역할)
# ----------------------------------------------------------------
FIELD_ALIASES = {
    "거래처": ["supplier", "vendor", "거래처", "供应商", "取引先"],
    "품목": ["item", "product", "품목", "项目", "品目"],
    "수량": ["qty", "quantity", "수량", "数量"],
    "단가": ["unit price", "단가", "单价"],
    "통화": ["currency", "통화", "币种"],
    "총액": ["total amount", "total", "총액", "합계금액", "합계", "合计", "合計金額"],
    "인도조건": ["incoterm", "인도조건", "贸易条款"],
}

REQUIRED_FIELDS = ["거래처", "품목", "수량", "단가", "통화", "총액"]

# 사내 보관 원본 문서 (실제로는 사내 파일서버/ERP 첨부파일에서 로드 — 외부 반출 없음)
RAW_DOCS = {
    "DOC-001 (미국 거래처, 영어)": """
INVOICE
Supplier: Global Components Inc.
Item: Connector Module X200
Qty: 500
Unit Price: USD 12.50
Currency: USD
Total Amount: USD 6250.00
Incoterm: FOB
""",
    "DOC-002 (중국 거래처, 중국어)": """
发票
供应商: 深圳华南电子有限公司
项目: 连接器模块 X200
数量: 500
单价: CNY 88.00
币种: CNY
合计: CNY 44000.00
贸易条款: FOB
""",
    "DOC-003 (일본 거래처, 일본어 · 일부 항목 누락)": """
見積書
取引先: 東京電子部品株式会社
品目: コネクタモジュール X200
数量: 480
単価: JPY 1850
合計金額: JPY 888000
""",
}

# 사내 발주(PO) 기준금액 — 원화 환산 후 이 값과 비교(조건반영 비교, 규칙기반)
PO_REFERENCE_KRW = {
    "DOC-001 (미국 거래처, 영어)": 8_400_000,
    "DOC-002 (중국 거래처, 중국어)": 8_300_000,
    "DOC-003 (일본 거래처, 일본어 · 일부 항목 누락)": 8_200_000,
}

# 환율 (원/단위, 데모용 고정값 — 실제로는 매일 환율 API/사내 기준환율 연동)
FX_TO_KRW = {"USD": 1340.0, "CNY": 190.0, "JPY": 9.0, "KRW": 1.0}
TOLERANCE = 0.05  # 조건반영 비교 허용오차 ±5%


# ----------------------------------------------------------------
# 2) 다국어 라벨 → 표준 필드 매핑 (문서 AI 표준화 로직 단순화 버전)
# ----------------------------------------------------------------
def parse_document(raw_text):
    extracted = {}
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line or ":" not in line and "：" not in line:
            continue
        sep = ":" if ":" in line else "："
        label, value = line.split(sep, 1)
        label_norm = label.strip().lower()
        value = value.strip()

        for std_field, aliases in FIELD_ALIASES.items():
            if any(alias.lower() in label_norm or label_norm in alias.lower() for alias in aliases):
                extracted[std_field] = value
                break

    # 통화 보정: "USD 12.50" 처럼 값 안에 통화기호가 섞인 경우 분리
    for f in ["단가", "총액"]:
        if f in extracted:
            m = re.match(r"([A-Za-z]{3})?\s*([\d,\.]+)", extracted[f])
            if m:
                if m.group(1) and "통화" not in extracted:
                    extracted["통화"] = m.group(1)
                extracted[f] = m.group(2).replace(",", "")
    return extracted


def review_flags(extracted):
    return [f for f in REQUIRED_FIELDS if f not in extracted or not extracted[f]]


def condition_adjusted_compare(doc_name, extracted):
    """규칙기반 조건반영 비교: 통화 환산 후 사내 PO 기준금액과 비교"""
    if "총액" not in extracted or "통화" not in extracted:
        return None
    try:
        amount = float(extracted["총액"])
    except ValueError:
        return None
    currency = extracted["통화"].upper()
    rate = FX_TO_KRW.get(currency)
    if rate is None:
        return None
    krw_amount = amount * rate
    ref = PO_REFERENCE_KRW.get(doc_name)
    if ref is None:
        return None
    diff_ratio = (krw_amount - ref) / ref
    within_tolerance = abs(diff_ratio) <= TOLERANCE
    return {
        "원화환산액": krw_amount,
        "PO기준액": ref,
        "차이율": diff_ratio,
        "허용오차이내": within_tolerance,
    }


# ----------------------------------------------------------------
# 3) 파일럿 실행 데모
# ----------------------------------------------------------------
if __name__ == "__main__":
    print("구매 다국어 서류 표준화 파일럿 + 조건반영 총액비교 — MVP 데모")
    print(f"파일럿 대상 문서 {len(RAW_DOCS)}건 (상위 거래처 한정)\n")

    for doc_name, raw in RAW_DOCS.items():
        print("=" * 78)
        print(f"[원본 문서] {doc_name}")
        extracted = parse_document(raw)

        print("[AI 추출 → 표준 스키마 변환 결과]")
        for f in ["거래처", "품목", "수량", "단가", "통화", "총액", "인도조건"]:
            print(f"    {f:6s}: {extracted.get(f, '—')}")

        flags = review_flags(extracted)
        if flags:
            print(f"  ⚠ 사람 검토 필요 (누락 필드): {', '.join(flags)}")
        else:
            print("  ✅ 필수 필드 전량 추출 — 담당자 최종 확인 대기")

        cmp = condition_adjusted_compare(doc_name, extracted)
        if cmp:
            status = "적정" if cmp["허용오차이내"] else "⚠ 확인 필요(오차 초과)"
            print(f"  [규칙기반 조건반영 비교] 원화환산 {cmp['원화환산액']:,.0f}원 vs "
                  f"PO기준 {cmp['PO기준액']:,.0f}원 (차이 {cmp['차이율']*100:+.1f}%) → {status}")
        print()

    print("=" * 78)
    print("요약: 3건 중 2건은 필드 전량 자동 추출, 1건(DOC-003)은 통화·인도조건 "
          "누락으로 사람 검토 라우팅")
    print("→ 실제 파일럿에서는 이 라우팅 로그 자체가 '2단계 확장 여부 판단'을 위한 성공지표(커버율·수정률) 데이터가 됨")
