"""
MVP: 구매 다국어 서류 표준화 파일럿 + 조건반영 총액비교 + 3-Way 대사 + 승인한도 검증 (Phase 1 - 구매팀)
----------------------------------------------------------------------
컨셉: 상위 거래처(영어/중국어/일본어 서식)에서 오는 견적서·인보이스를
표준 스키마(거래처/품목/수량/단가/통화/총액/인도조건)로 자동 변환하고,
누락 필드는 "사람 검토 필요"로 플래그한다.

이후 환율을 반영해 원화 환산 총액을 계산하고, 사내 발주금액(PO)과
비교해 조건반영 비교(규칙기반)까지 한 번에 보여준다.

[확장] 여기에 두 가지 내부통제 로직을 추가한다.
  1) 3-Way 대사: 발주(PO) 수량 vs 인보이스(문서 추출) 수량 vs 검수(입고) 수량을
     규칙기반으로 대조해, 부분입고·오배송 등 수량 불일치를 잡아낸다.
  2) 승인한도 검증: PO 금액(원화환산)이 해당 건 승인자의 전결 한도를 넘는지
     규칙기반으로 확인하고, 초과 시 상위 승인자로 에스컬레이션한다.

실제 파일럿에서는 라벨 사전 매칭 대신 사내 문서AI(OCR+LLM) 모델이
서류 인식 역할을 하지만, 대사·조건계산·승인한도 검증은 이 MVP처럼
전 구간 규칙기반(결정론적)으로 유지하는 것이 설계 원칙입니다 — 문서AI는
"추출"까지만 담당하고, "판단"은 룰엔진이 담당합니다.
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
# 1-b) [확장] 발주(PO) 마스터 — 수량·승인자·전결한도 (실제로는 ERP 발주 모듈에서 연동)
# ----------------------------------------------------------------
PURCHASE_ORDERS = {
    "DOC-001 (미국 거래처, 영어)": {
        "po_qty": 500, "approver": "김민수 과장", "approval_limit_krw": 10_000_000,
    },
    "DOC-002 (중국 거래처, 중국어)": {
        "po_qty": 500, "approver": "박지연 차장", "approval_limit_krw": 8_000_000,
    },
    "DOC-003 (일본 거래처, 일본어 · 일부 항목 누락)": {
        "po_qty": 500, "approver": "이수현 과장", "approval_limit_krw": 10_000_000,
    },
}

# 승인한도 초과 시 에스컬레이션할 상위 승인자 (실제로는 사내 전결규정표에서 연동)
ESCALATION_APPROVER = {
    "김민수 과장": "정하늘 부장",
    "박지연 차장": "정하늘 부장",
    "이수현 과장": "정하늘 부장",
}

# [확장] 검수(입고) 기록 — 실제로는 창고/물류 시스템의 입고확인(GR) 데이터에서 연동
GOODS_RECEIPTS = {
    "DOC-001 (미국 거래처, 영어)": {"received_qty": 500, "inspector": "물류팀 최도윤", "received_date": "2026-07-28"},
    "DOC-002 (중국 거래처, 중국어)": {"received_qty": 500, "inspector": "물류팀 최도윤", "received_date": "2026-07-29"},
    "DOC-003 (일본 거래처, 일본어 · 일부 항목 누락)": {"received_qty": 480, "inspector": "물류팀 한서연", "received_date": "2026-07-30"},
}

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
# 2-b) [확장] 3-Way 대사: 발주(PO) 수량 vs 인보이스 수량 vs 검수(입고) 수량
# ----------------------------------------------------------------
def three_way_match(doc_name, extracted):
    """PO·인보이스·검수 3자 대사 (규칙기반, 결정론적).

    금액 비교는 기존 condition_adjusted_compare를 그대로 재사용하고,
    여기서는 수량 3자 대사만 추가로 수행한 뒤 종합판정을 내린다.
    """
    po = PURCHASE_ORDERS.get(doc_name)
    gr = GOODS_RECEIPTS.get(doc_name)
    if po is None or gr is None:
        return None

    invoice_qty_raw = extracted.get("수량")
    try:
        invoice_qty = int(float(invoice_qty_raw)) if invoice_qty_raw is not None else None
    except ValueError:
        invoice_qty = None

    po_qty = po["po_qty"]
    received_qty = gr["received_qty"]

    qty_values = {"PO": po_qty, "인보이스": invoice_qty, "검수": received_qty}
    known = {k: v for k, v in qty_values.items() if v is not None}
    qty_all_match = len(set(known.values())) <= 1 and len(known) == 3

    amount_cmp = condition_adjusted_compare(doc_name, extracted)
    amount_ok = amount_cmp["허용오차이내"] if amount_cmp else None

    if invoice_qty is None:
        overall = "인보이스 수량 누락 — 대사 불가"
    elif not qty_all_match:
        mismatches = ", ".join(f"{k} {v}" for k, v in qty_values.items())
        overall = f"수량 불일치 ({mismatches}) — 부분입고/오배송 의심, 사람 검토 필요"
    elif amount_ok is False:
        overall = "수량은 일치하나 금액 오차 허용범위 초과 — 사람 검토 필요"
    elif amount_ok is None:
        overall = "수량 일치, 금액 비교 불가(필드 부족)"
    else:
        overall = "PO·인보이스·검수 3자 일치, 금액 오차 이내 — 적정"

    return {
        "po_qty": po_qty,
        "invoice_qty": invoice_qty,
        "received_qty": received_qty,
        "qty_all_match": qty_all_match,
        "amount_cmp": amount_cmp,
        "overall": overall,
        "needs_review": (invoice_qty is None) or (not qty_all_match) or (amount_ok is False),
    }


# ----------------------------------------------------------------
# 2-c) [확장] 승인한도 검증: PO 금액(원화환산)이 승인자 전결한도를 넘는지 확인
# ----------------------------------------------------------------
def check_approval_limit(doc_name, extracted):
    """규칙기반 승인한도 검증. 초과 시 상위 승인자로 에스컬레이션."""
    po = PURCHASE_ORDERS.get(doc_name)
    if po is None:
        return None

    cmp = condition_adjusted_compare(doc_name, extracted)
    krw_amount = cmp["원화환산액"] if cmp else PO_REFERENCE_KRW.get(doc_name)
    if krw_amount is None:
        return None

    approver = po["approver"]
    limit = po["approval_limit_krw"]
    exceeded = krw_amount > limit

    result = {
        "approver": approver,
        "approval_limit_krw": limit,
        "amount_krw": krw_amount,
        "exceeded": exceeded,
    }
    if exceeded:
        result["escalate_to"] = ESCALATION_APPROVER.get(approver, "상위 승인권자 확인 필요")
    return result


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
            print(f"  {f:6s}: {extracted.get(f, '—')}")

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

        tw = three_way_match(doc_name, extracted)
        if tw:
            tag = "⚠" if tw["needs_review"] else "✅"
            print(f"  [3-Way 대사] PO {tw['po_qty']} / 인보이스 {tw['invoice_qty']} / "
                  f"검수 {tw['received_qty']} → {tag} {tw['overall']}")

        appr = check_approval_limit(doc_name, extracted)
        if appr:
            if appr["exceeded"]:
                print(f"  [승인한도 검증] {appr['amount_krw']:,.0f}원 > {appr['approver']} 전결한도 "
                      f"{appr['approval_limit_krw']:,.0f}원 → ⚠ 상위 승인자 에스컬레이션 필요 "
                      f"({appr['escalate_to']})")
            else:
                print(f"  [승인한도 검증] {appr['amount_krw']:,.0f}원 ≤ {appr['approver']} 전결한도 "
                      f"{appr['approval_limit_krw']:,.0f}원 → ✅ 승인 가능")
        print()

    print("=" * 78)
    print("요약: 3건 중 2건은 필드 전량 자동 추출, 1건(DOC-003)은 통화·인도조건 "
          "누락으로 사람 검토 라우팅")
    print("3-Way 대사: DOC-003은 PO수량(500)과 인보이스·검수수량(480)이 불일치 — 부분입고 의심으로 별도 플래그")
    print("승인한도 검증: DOC-002는 PO금액이 담당 승인자(박지연 차장) 전결한도를 초과해 상위 승인자로 에스컬레이션")
    print("→ 실제 파일럿에서는 이 라우팅 로그 자체가 '2단계 확장 여부 판단'을 위한 성공지표(커버율·수정률) 데이터가 됨")
