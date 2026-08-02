"""
세무·구매 Phase 1 MVP — 로컬 웹 뷰어
----------------------------------------------------------------------
mvp_tax_judgment.py / mvp_purchase_docai.py 의 로직을 그대로 재사용해
브라우저에서 확인할 수 있는 화면으로 보여줍니다.

외부 라이브러리(Flask 등) 없이 파이썬 표준 라이브러리(http.server)만
사용합니다 — 인터넷 연결 없이도 그대로 실행됩니다.

실행:
    python3 mvp_web_app.py
브라우저에서:
    http://localhost:8000

주의: 이건 "로컬"에서 여는 웹페이지입니다. 본인 컴퓨터에서 서버를 켜둔
동안에만, 본인 브라우저에서만 보입니다. 다른 사람과 공유하려면 별도
호스팅 서비스(Render, Railway 등)에 배포해야 합니다.
"""
import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mvp_tax_judgment import judge_new_expense_data, df as TAX_CASES
from mvp_purchase_docai import (
    RAW_DOCS, PO_REFERENCE_KRW, parse_document, review_flags, condition_adjusted_compare,
    three_way_match, check_approval_limit,
)

# 클라우드 호스팅 서비스(Render, Railway 등)는 PORT 환경변수로 포트를 지정함 — 없으면 로컬 기본값 8000 사용
PORT = int(os.environ.get("PORT", 8000))

NAVY = "#1F3864"
GOLD = "#C9974C"
GREEN = "#2E7D32"
RED = "#9C3B2E"
GRAY = "#595959"
LGRAY = "#F2F2F2"

STYLE = f"""
<style>
body {{ font-family: -apple-system, "Malgun Gothic", sans-serif; background:#fff; color:#262626;
       max-width: 980px; margin: 0 auto; padding: 24px 20px 60px; }}
h1 {{ color:{NAVY}; font-size: 22px; margin-bottom:4px; }}
.sub {{ color:{GRAY}; font-size: 13px; margin-bottom: 28px; font-style: italic; }}
h2 {{ background:{NAVY}; color:#fff; padding:8px 14px; border-radius:6px; font-size:15px; margin:34px 0 14px; }}
.card {{ border:1px solid #e2e2e2; border-radius:8px; padding:16px 18px; margin-bottom:14px; background:#fafbfd; }}
.card.review {{ border-color:{RED}; background:#fdf3f1; }}
.card.ok {{ border-color:{GREEN}; background:#f2f8f2; }}
.tag {{ display:inline-block; font-size:11px; font-weight:bold; padding:2px 9px; border-radius:10px; margin-left:6px; }}
.tag.auto {{ background:{GOLD}; color:#fff; }}
.tag.review {{ background:{RED}; color:#fff; }}
table {{ border-collapse: collapse; width:100%; font-size:13px; margin-top:8px; }}
td, th {{ border:1px solid #e2e2e2; padding:6px 10px; text-align:left; }}
th {{ background:{LGRAY}; }}
form.inline {{ margin: 10px 0 18px; }}
input[type=text], textarea {{ width:100%; box-sizing:border-box; padding:8px 10px; font-size:14px;
       border:1px solid #ccc; border-radius:6px; font-family:inherit; }}
textarea {{ height: 110px; }}
button {{ background:{NAVY}; color:#fff; border:none; padding:8px 18px; border-radius:6px;
       font-size:13px; margin-top:8px; cursor:pointer; }}
.muted {{ color:{GRAY}; font-size:12px; }}
.field {{ font-size:13px; margin: 2px 0; }}
.field b {{ display:inline-block; width:76px; color:{GRAY}; }}
.subcard {{ border-top:1px dashed #ddd; margin-top:10px; padding-top:8px; font-size:13px; }}
code {{ background:{LGRAY}; padding:1px 5px; border-radius:4px; }}
</style>
"""


def esc(s):
    return html.escape(str(s), quote=True)


def render_tax_section(query_text):
    result_html = ""
    if query_text.strip():
        r = judge_new_expense_data(query_text.strip())
        if r["routed_to_expert"]:
            result_html = f"""
            <div class="card review">
              <b>⚠ 신규/예외 패턴</b> <span class="tag review">전문가 검토 라우팅</span><br>
              최고 유사도 {r['best_similarity']:.2f} (임계값 {r['threshold']} 미만) — 과거 사례와 충분히 유사하지 않아
              자동 처리안을 제시하지 않습니다.
            </div>"""
        elif r["auto_approved"]:
            s = r["suggestion"]
            result_html = f"""
            <div class="card ok">
              <b>✅✅ 동일·거의 동일 거래 → 자동 확정</b> <span class="tag auto">유사도 {r['best_similarity']:.2f} ≥ {r['auto_approve_threshold']} · 근거 {esc(s['근거사례'])}</span>
              <div class="field"><b>계정과목</b>{esc(s['계정과목'])}</div>
              <div class="field"><b>법인세</b>{esc(s['손금인정(법인세)'])}</div>
              <div class="field"><b>부가세</b>{esc(s['매입세액공제(부가세)'])}</div>
              <div class="field"><b>판단근거</b>{esc(s['판단근거'])}</div>
              <div class="muted">※ 담당자 확인 없이 자동 확정 — 사후 샘플링 검토 대상으로 로그 기록</div>
            </div>"""
        else:
            s = r["suggestion"]
            result_html = f"""
            <div class="card ok">
              <b>✅ 자동 처리안 제시</b> <span class="tag auto">유사도 {r['best_similarity']:.2f} · 근거 {esc(s['근거사례'])}</span>
              <div class="field"><b>계정과목</b>{esc(s['계정과목'])}</div>
              <div class="field"><b>법인세</b>{esc(s['손금인정(법인세)'])}</div>
              <div class="field"><b>부가세</b>{esc(s['매입세액공제(부가세)'])}</div>
              <div class="field"><b>판단근거</b>{esc(s['판단근거'])}</div>
              <div class="muted">※ 담당자 최종 확인 후 반영 (자동 확정 아님)</div>
            </div>"""

        rows = "".join(
            f"<tr><td>{esc(c['id'])}</td><td>{c['similarity']:.2f}</td>"
            f"<td>{esc(c['계정과목'])}</td><td>{esc(c['지출내역'])}</td></tr>"
            for c in r["similar_cases"]
        )
        result_html += f"""
        <table><tr><th>사례ID</th><th>유사도</th><th>계정과목</th><th>지출내역</th></tr>{rows}</table>
        """

    case_rows = "".join(
        f"<tr><td>{esc(c['id'])}</td><td>{esc(c['계정과목'])}</td><td>{esc(c['지출내역'])}</td></tr>"
        for c in TAX_CASES.to_dict("records")
    )

    return f"""
    <h2>① 세무 판단이력 DB + 사례기반 자동매칭</h2>
    <form class="inline" method="get" action="/">
      <label class="muted">새 비용전표 지출내역을 입력해보세요</label>
      <input type="text" name="new_expense" value="{esc(query_text)}"
             placeholder="예: 거래처 부장님과 저녁 접대 자리 비용">
      <button type="submit">판단 실행</button>
    </form>
    {result_html}
    <details><summary class="muted">과거 사례 DB 보기 ({len(TAX_CASES)}건)</summary>
      <table><tr><th>ID</th><th>계정과목</th><th>지출내역</th></tr>{case_rows}</table>
    </details>
    """


def render_three_way_block(doc_name, extracted):
    """[확장] 3-Way 대사 + 승인한도 검증 결과를 카드 안에 이어붙이는 서브블록."""
    tw = three_way_match(doc_name, extracted)
    appr = check_approval_limit(doc_name, extracted)

    parts = []
    if tw:
        tag = "review" if tw["needs_review"] else "auto"
        tag_label = "사람 검토 필요" if tw["needs_review"] else "3-Way 일치"
        parts.append(f"""
        <div class="subcard">
          <b>3-Way 대사</b> <span class="tag {tag}">{tag_label}</span><br>
          PO수량 {tw['po_qty']} / 인보이스수량 {tw['invoice_qty'] if tw['invoice_qty'] is not None else '—'} /
          검수수량 {tw['received_qty']}<br>
          <span class="muted">{esc(tw['overall'])}</span>
        </div>""")

    if appr:
        if appr["exceeded"]:
            parts.append(f"""
            <div class="subcard">
              <b>승인한도 검증</b> <span class="tag review">에스컬레이션 필요</span><br>
              {appr['amount_krw']:,.0f}원 &gt; {esc(appr['approver'])} 전결한도 {appr['approval_limit_krw']:,.0f}원<br>
              <span class="muted">→ 상위 승인자({esc(appr['escalate_to'])})에게 재상신 필요</span>
            </div>""")
        else:
            parts.append(f"""
            <div class="subcard">
              <b>승인한도 검증</b> <span class="tag auto">승인 가능</span><br>
              {appr['amount_krw']:,.0f}원 ≤ {esc(appr['approver'])} 전결한도 {appr['approval_limit_krw']:,.0f}원
            </div>""")

    return "".join(parts)


def render_purchase_section(custom_raw):
    blocks = []
    for name, raw in RAW_DOCS.items():
        extracted = parse_document(raw)
        flags = review_flags(extracted)
        cmp = condition_adjusted_compare(name, extracted)
        tw = three_way_match(name, extracted)
        card_cls = "review" if (flags or (tw and tw["needs_review"])) else "ok"

        field_rows = "".join(
            f"<div class='field'><b>{f}</b>{esc(extracted.get(f, '—'))}</div>"
            for f in ["거래처", "품목", "수량", "단가", "통화", "총액", "인도조건"]
        )
        flag_html = (f"<div><span class='tag review'>사람 검토 필요</span> 누락 필드: {esc(', '.join(flags))}</div>"
                     if flags else "<div><span class='tag auto'>필드 전량 자동 추출</span></div>")

        cmp_html = ""
        if cmp:
            status = "적정" if cmp["허용오차이내"] else "⚠ 오차 초과"
            cmp_html = (f"<div class='muted' style='margin-top:6px'>규칙기반 조건반영 비교: "
                        f"원화환산 {cmp['원화환산액']:,.0f}원 vs PO기준 {cmp['PO기준액']:,.0f}원 "
                        f"(차이 {cmp['차이율']*100:+.1f}%) → {status}</div>")

        control_html = render_three_way_block(name, extracted)

        blocks.append(f"""
        <div class="card {card_cls}">
          <b>{esc(name)}</b>
          {field_rows}
          {flag_html}
          {cmp_html}
          {control_html}
        </div>""")

    custom_html = ""
    if custom_raw.strip():
        extracted = parse_document(custom_raw)
        flags = review_flags(extracted)
        field_rows = "".join(
            f"<div class='field'><b>{f}</b>{esc(extracted.get(f, '—'))}</div>"
            for f in ["거래처", "품목", "수량", "단가", "통화", "총액", "인도조건"]
        )
        flag_html = (f"<div><span class='tag review'>사람 검토 필요</span> 누락 필드: {esc(', '.join(flags))}</div>"
                     if flags else "<div><span class='tag auto'>필드 전량 자동 추출</span></div>")
        custom_html = f"""
        <h3 style="font-size:14px; margin-top:22px;">직접 입력한 문서 추출 결과</h3>
        <div class="card {'review' if flags else 'ok'}">{field_rows}{flag_html}
          <div class="muted" style="margin-top:6px;">※ 3-Way 대사·승인한도 검증은 사전 등록된 PO/검수 마스터가 있는 위 3개 데모 문서에서만 확인할 수 있습니다.</div>
        </div>
        """

    return f"""
    <h2>② 구매 다국어 서류 표준화 + 3-Way 대사·승인한도 검증</h2>
    {''.join(blocks)}
    <details open><summary class="muted">직접 문서 텍스트를 입력해 테스트해보기 (Label: Value 형식)</summary>
      <form class="inline" method="get" action="/">
        <textarea name="custom_doc" placeholder="예:
Supplier: ABC Trading
Item: Widget A
Qty: 100
Unit Price: USD 5.00
Currency: USD
Total Amount: USD 500.00">{esc(custom_raw)}</textarea>
        <button type="submit">추출 실행</button>
      </form>
      {custom_html}
    </details>
    """


PAGE_TEMPLATE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>AX Phase 1 MVP — 세무·구매</title>{style}</head><body>
<h1>세무·구매 Phase 1 MVP 데모</h1>
<div class="sub">전사 AX 제안서 — 세무 판단DB / 구매 문서AI·3-Way 대사·승인한도 검증 로컬 프로토타입 (외부 API 미사용)</div>
{tax_section}
{purchase_section}
<p class="muted" style="margin-top:40px;">전사 AX 제안서(세무·구매 1단계) 참고용 개념검증(PoC) 데모입니다. 실제 사내 데이터가 아닌 시연용 샘플 데이터를 사용합니다.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/":
            self.send_response(404)
            self.end_headers()
            return

        params = urllib.parse.parse_qs(parsed.query)
        query_text = params.get("new_expense", [""])[0]
        custom_doc = params.get("custom_doc", [""])[0]

        body = PAGE_TEMPLATE.format(
            style=STYLE,
            tax_section=render_tax_section(query_text),
            purchase_section=render_purchase_section(custom_doc),
        )
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, fmt, *args):
        pass  # 콘솔 로그 억제(필요 시 이 메서드를 지우면 접속 로그가 출력됨)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"서버 실행 중 (포트 {PORT}) → 로컬이면 http://localhost:{PORT} 접속하세요")
    print("종료하려면 Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
