"""Files produced when a deal closes: the certificate, each side's private annex, and the order form PDF."""

from __future__ import annotations

import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .terms import Terms


def write_closing_files(out_dir: Path, cert: dict, annexes: dict[str, dict], base_url: str = "") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    deal = cert["deal"]["id"]
    files = {
        "certificate": out_dir / f"{deal}-certificate.json",
        "order_form": out_dir / f"{deal}-order-form.pdf",
    }
    files["certificate"].write_text(json.dumps(cert, indent=2))
    order_form_pdf(files["order_form"], cert)
    for company, annex in annexes.items():
        path = out_dir / f"{deal}-annex-{company}.json"
        path.write_text(json.dumps(annex, indent=2))
        path.chmod(0o600)
        files[f"annex_{company}"] = path

    def link(key: str, path: Path) -> str:
        if base_url and not key.startswith("annex"):
            return f"{base_url}/files/{path.name}"
        return str(path)

    return {k: link(k, v) for k, v in files.items()}


def order_form_pdf(path: Path, cert: dict) -> None:
    styles = getSampleStyleSheet()
    ink = colors.HexColor("#14161a")
    muted = colors.HexColor("#6b7280")
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, textColor=ink, alignment=0, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontSize=9, textColor=muted)
    body = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14, textColor=ink)
    small = ParagraphStyle("small", parent=styles["Normal"], fontName="Courier", fontSize=7.5, leading=10, textColor=muted)

    deal = cert["deal"]
    terms = Terms.from_dict(cert["final_terms"])
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f"Order form {deal['id']}", author="Mandate referee")
    story = [
        Paragraph("Order Form", h1),
        Paragraph(f"Deal {deal['id']} · {deal['product']} · issued {cert['issued_at']}", sub),
        Spacer(1, 8 * mm),
        Paragraph(f"<b>Customer:</b> {deal['buyer']['name']} &nbsp;&nbsp; <b>Vendor:</b> {deal['seller']['name']}", body),
        Spacer(1, 5 * mm),
    ]
    rows = [["Term", "Agreed value"],
            ["Subscription price", f"${terms.price_per_year:,} per year"],
            ["Contract term", f"{terms.term_months} months"],
            ["Payment terms", f"Net {terms.payment_days} days"],
            ["Seats", f"{terms.seats}"],
            ["Total contract value", f"${terms.total_contract_value:,}"]]
    table = Table(rows, colWidths=[60 * mm, 100 * mm])
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9), ("TEXTCOLOR", (0, 0), (-1, 0), muted),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 10), ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e7eb")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 10),
    ]))
    story += [table, Spacer(1, 8 * mm), Paragraph("<b>Authority record</b>", body), Spacer(1, 2 * mm)]
    for company, m in cert["mandates_in_force"].items():
        story.append(Paragraph(f"• {company}: negotiated under mandate {m['label']} (commitment {m['commitment'][:20]}…)", body))
    for a in cert["approvals"]:
        story.append(Paragraph(f"• Approval {a['id']}: {a['company']} principal <b>{a['principal']}</b> approved {a['offer']} "
                               f"for terms hash {a['terms_hash'][:16]}… ({a['at']})", body))
    for c in cert["countersigns"]:
        story.append(Paragraph(f"• Countersigned for {c['company']} by <b>{c['principal']}</b> at {c['at']}", body))
    story += [Spacer(1, 8 * mm), Paragraph("Verification", body),
              Paragraph(f"Ledger root {cert['root_hash']}", small),
              Paragraph(f"Referee key {cert['referee_public_key']}", small),
              Paragraph(f"Signature {cert['signature'][:64]}…", small),
              Spacer(1, 3 * mm),
              Paragraph("Run <font name='Courier'>mandate verify certificate.json</font> to check the signature, the hash chain and both "
                        "countersigns offline. Each party can add its private annex to prove it stayed within its own mandate.", sub)]
    doc.build(story)
