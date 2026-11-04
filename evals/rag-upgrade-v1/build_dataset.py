"""產生自製評估文件和 gold；不連 DB、不連網、不修改服務。

build 只適用首次建立資料集，拒絕覆寫凍結資料。Python 與文件套件使用
Codex bundled runtime；Gold 是可供人工核對的草案，不冒充人工已驗證。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
INPUT_ROOT = ROOT
FONT_PATH = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
TYPES = (
    "fact",
    "list",
    "count",
    "compare",
    "procedure",
    "followup",
    "no_answer",
    "ambiguity",
)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_documents(s: dict) -> list[dict]:
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    folder = ROOT / "corpus" / s["split"] / s["id"]
    folder.mkdir(parents=True, exist_ok=False)
    title = f"U0 SYNTHETIC {s['id']} {s['topic']}"
    rules = (
        f"# {title} 規章\n\n## 適用範圍\n\n本文件是 2027 至 2028 年測試規章 v2，"
        f"只供測試，沒有真實員工或公司資料。\n\n## 日期\n\n"
        f"[E-{s['id']}-DATE] {s['topic']} v2 的{s['event']}"
        f"截止日為{s['deadline']}。\n\n"
        "## 清單範圍\n\n目錄的 v2 清單共有三列；只計狀態為開放的項目。"
        f"計數單位為{s['unit']}，唯一鍵為項目代碼，不以名稱或頁數計數。\n"
    )
    (folder / "rules.md").write_text(rules, encoding="utf-8")
    rows = [["項目代碼", "名稱", "資格", "費用", "狀態"]]
    for index, name in enumerate(s["names"]):
        rows.append(
            [
                f"R-{s['id']}-{index + 1}",
                name,
                s["qualifications"][index],
                f"{s['fees'][index]} 元",
                "開放" if index < 2 else "暫停",
            ]
        )
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.left_margin = sec.right_margin = Inches(0.7)
    for style_name in ("Normal", "Title", "Heading 1"):
        style = doc.styles[style_name]
        style.font.name = "Arial Unicode MS"
        style.font.size = Pt(11 if style_name == "Normal" else 16)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Arial Unicode MS")
        properties = style.element.find(qn("w:pPr"))
        if properties is not None:
            for border in list(properties.findall(qn("w:pBdr"))):
                properties.remove(border)
    doc.add_paragraph(title + " 目錄", "Title")
    doc.add_paragraph("本目錄說明 v2 適用項目、資格、費用與開放狀態。依項目代碼計數。")
    doc.add_heading("適用項目", level=1)
    doc.add_paragraph(
        f"[E-{s['id']}-CATALOG] v2 完整清單如下。所有列都在本表，沒有其他附件。"
    )
    table = doc.add_table(rows=0, cols=5)
    table.style = "Table Grid"
    for values in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, values, strict=True):
            cell.text = value
            borders = OxmlElement("w:tcBorders")
            for edge in ("top", "left", "bottom", "right"):
                border = OxmlElement(f"w:{edge}")
                border.set(qn("w:val"), "single")
                border.set(qn("w:sz"), "4")
                border.set(qn("w:color"), "D9D9D9")
                borders.append(border)
            cell._tc.get_or_add_tcPr().append(borders)
    header = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(header)
    doc.core_properties.created = doc.core_properties.modified = datetime(2026, 10, 1)
    doc.core_properties.author = "U0 Synthetic Fixture"
    doc.save(folder / "catalog.docx")
    # 固定 ZIP entry 時間，讓相同 fixture 重建時有可重現 bytes hash。
    with ZipFile(folder / "catalog.docx") as z:
        entries = [(name, z.read(name)) for name in z.namelist()]
    with ZipFile(folder / "catalog.docx", "w", ZIP_DEFLATED) as z:
        for name, content in entries:
            info = ZipInfo(name, (2026, 10, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            z.writestr(info, content)
    # 嵌入 TrueType 字型，避免 reader 主機缺少 Adobe CJK CMap。
    pdfmetrics.registerFont(TTFont("FixtureCJK", str(FONT_PATH)))
    ps = ParagraphStyle("fixture", fontName="FixtureCJK", fontSize=11, leading=17)
    story = [
        Paragraph(title + " 操作流程", ps),
        Spacer(1, 14),
        Paragraph("只供隔離測試使用。步驟必須依序完成，不操作正式環境。", ps),
    ]
    for page in (1, 2):
        if page == 2:
            story.append(PageBreak())
            story.append(Paragraph(title + " 操作流程 續頁", ps))
        story.append(Paragraph("操作步驟", ps))
        for idx in range((page - 1) * 2, page * 2):
            story.append(
                Paragraph(
                    f"[E-{s['id']}-STEP{idx + 1}] {idx + 1}. {s['steps'][idx]}。", ps
                )
            )
        story.append(Spacer(1, 18))
        story.append(Paragraph("跨頁目錄對照 附錄", ps))
        selected = rows[1:3] if page == 1 else rows[3:]
        rendered = [[Paragraph(v, ps) for v in row] for row in [rows[0], *selected]]
        t = Table(rendered, colWidths=[90, 95, 120, 65, 65])
        t.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.lightgrey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ]
            )
        )
        story.append(t)
    SimpleDocTemplate(
        str(folder / "procedure.pdf"),
        invariant=1,
        title=title,
        author="U0 Synthetic Fixture",
    ).build(story)
    notes = (
        f"{title} 版本說明\n\n[E-{s['id']}-EDITION] "
        f"{s['topic']}另有 v1 與 v2 兩個適用批次，各自獨立，"
        "不能只靠版本號推定申請人批次。"
        f"v1 截止日為2026-12-10；v2 截止日為{s['deadline']}。"
        "未提供適用批次時須先詢問，不得猜測。\n\n"
        f"[E-{s['id']}-MISSING] 本測試文件未記載「{s['missing']}」。\n"
    )
    (folder / "notes.txt").write_text(notes, encoding="utf-8")
    docs = []
    for name in ("rules.md", "catalog.docx", "procedure.pdf", "notes.txt"):
        path = folder / name
        doc_id = f"{s['id']}/{name}"
        docs.append(
            {
                "id": doc_id,
                "group": s["id"],
                "split": s["split"],
                "domain": s["domain"],
                "path": path.relative_to(ROOT).as_posix(),
                "version": "v2",
                "title": title + " " + name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "expected_pages": 2 if name.endswith(".pdf") else None,
            }
        )
        # 不進入既有來源資料夾；ACL 僅允許此次測試群組，外送預設禁止。
        sidecar = {
            "title": docs[-1]["title"],
            "document_type": s["domain"],
            "owner_department": "Synthetic Evaluation",
            "document_owner": "u0-fixture-owner",
            "version_number": "v2",
            "confidentiality": "public",
            "status": "published",
            "language": "zh-TW",
            "external_llm_allowed": False,
            "acl": [
                {
                    "principal_type": "group",
                    "principal_id": "rag-u0-fixtures",
                    "effect": "allow",
                }
            ],
            "custom_metadata": {"synthetic": True, "u0_group": s["id"]},
        }
        # JSON 是合法 YAML；避免另引入 YAML writer 依賴。
        write_json(path.with_name(name + ".metadata.yaml"), sidecar)
    return docs


def build_cases(s: dict) -> list[dict]:
    group, topic, names = s["id"], s["topic"], s["names"]
    prompts = {
        "fact": f"{topic} v2 的{s['event']}截止日期是什麼時候？",
        "list": f"請列出{topic} v2 全部開放的{s['unit']}，不包含暫停項目。",
        "count": f"以項目代碼去重，{topic} v2 共有幾個開放的{s['unit']}？",
        "compare": f"比較{topic} v2 的{names[0]}與{names[1]}，資格與費用各有何不同？",
        "procedure": f"根據{topic} v2 操作流程，請按順序說明四個步驟。",
        "followup": "那第二個的資格與費用呢？",
        "no_answer": f"根據{topic}文件，{s['missing']}是多少？",
        "ambiguity": f"{topic}這次的截止日是哪一天？",
    }
    cases = []
    for kind in TYPES:
        doc_name = {
            "fact": "rules.md",
            "procedure": "procedure.pdf",
            "ambiguity": "notes.txt",
            "no_answer": "notes.txt",
        }.get(kind, "catalog.docx")
        anchors = (
            [f"E-{group}-DATE"]
            if kind == "fact"
            else [f"E-{group}-STEP{i}" for i in range(1, 5)]
            if kind == "procedure"
            else [f"E-{group}-EDITION"]
            if kind == "ambiguity"
            else [f"E-{group}-MISSING"]
            if kind == "no_answer"
            else [f"R-{group}-1", f"R-{group}-2"]
        )
        claims = {
            "fact": [s["deadline"]],
            "list": names[:2],
            "count": ["2"],
            "compare": [
                names[0],
                names[1],
                *s["qualifications"][:2],
                *[str(v) for v in s["fees"][:2]],
            ],
            "procedure": s["steps"],
            "followup": [names[1], s["qualifications"][1], str(s["fees"][1])],
            "no_answer": [],
            "ambiguity": [],
        }[kind]
        prior = (
            []
            if kind != "followup"
            else [
                {
                    "role": "user",
                    "text": f"{topic} v2 開放的第一個與第二個項目名稱是什麼？",
                },
                {
                    "role": "assistant",
                    "text": f"第一個是{names[0]}，第二個是{names[1]}。",
                },
            ]
        )
        cases.append(
            {
                "id": f"{group}-{kind}",
                "type": kind,
                "split": s["split"],
                "domain": s["domain"],
                "source_group": group,
                "question": prompts[kind],
                "history": prior,
                "scope": {
                    "topic": topic,
                    "edition": None if kind == "ambiguity" else "v2",
                },
                "required_conditions": ["source_group", "edition", "authorized_source"],
                "gold": {
                    "expected": "clarify"
                    if kind == "ambiguity"
                    else "refuse"
                    if kind == "no_answer"
                    else "answer",
                    "claims": claims,
                    "reason": "missing_edition"
                    if kind == "ambiguity"
                    else "not_documented"
                    if kind == "no_answer"
                    else None,
                    "evidence": [
                        {
                            "document": f"{group}/{doc_name}",
                            "version": "v2",
                            "anchors": anchors,
                            "section": "操作步驟"
                            if kind == "procedure"
                            else "日期"
                            if kind == "fact"
                            else "適用項目",
                            "pages": [1, 2] if kind == "procedure" else None,
                        }
                    ],
                    "row_ids": [f"R-{group}-1", f"R-{group}-2"]
                    if kind in {"list", "count", "compare"}
                    else [],
                    "complete_required": kind in {"list", "count"},
                    "count_unit": s["unit"] if kind == "count" else None,
                    "count_unique_key": "item_code" if kind == "count" else None,
                    "human_review_status": "pending",
                },
            }
        )
    return cases


def main() -> None:
    global ROOT, FONT_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--font", type=Path, default=FONT_PATH)
    args = parser.parse_args()
    ROOT, FONT_PATH = args.output_dir.resolve(), args.font.resolve()
    if (ROOT / "manifest.json").exists() or (ROOT / "corpus").exists():
        raise SystemExit(
            "Refusing to overwrite dataset; "
            "use a separately reviewed new dataset version"
        )
    if not FONT_PATH.is_file():
        raise SystemExit(
            "A local CJK TrueType font is required; no downloads attempted"
        )
    ROOT.mkdir(parents=True, exist_ok=True)
    if ROOT != INPUT_ROOT:
        for name in ("scenarios.json", "safety-cases.json"):
            shutil.copyfile(INPUT_ROOT / name, ROOT / name)
    scenarios = json.loads((ROOT / "scenarios.json").read_text(encoding="utf-8"))
    docs, cases = [], []
    for scenario in scenarios:
        docs.extend(build_documents(scenario))
        cases.extend(build_cases(scenario))
    for split in ("tuning", "holdout"):
        write_json(
            ROOT / "datasets" / f"{split}.json",
            [c for c in cases if c["split"] == split],
        )
    write_json(ROOT / "documents.json", docs)
    files = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in (ROOT / "corpus", ROOT / "datasets")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }
    for name in ("documents.json", "scenarios.json", "safety-cases.json"):
        files[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    write_json(
        ROOT / "manifest.json",
        {
            "version": "u0-synthetic-v1",
            "created": "2026-10-01",
            "gold_status": "pending_human_review",
            "files": files,
        },
    )
    print(
        "Dataset created: 80 business cases, 40 source documents; "
        "gold human review pending"
    )


if __name__ == "__main__":
    main()
