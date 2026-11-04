"""建立獨立 v2 草案；不覆寫 v1、不連 DB／網路、不核准人工 Gold。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from build_dataset import FONT_PATH, TYPES, write_json

ROOT = Path(__file__).resolve().parent
REVISION = ROOT / "revisions" / "u0-synthetic-v2"
VERSION = "u0-synthetic-v2-draft"
HEADERS = ["來源列", "單位", "代碼", "名稱", "資格", "費用", "狀態"]


def scenarios() -> list[dict]:
    """沿用題材，不沿用固定答案 2；來源群組仍不跨 split。"""
    values = json.loads((ROOT / "scenarios.json").read_text(encoding="utf-8"))
    counts = {"tuning": [0, 1, 3, 12, 36], "holdout": [0, 2, 5, 14, 33]}
    for value in values:
        offset = int(value["id"][1:]) - 1
        value["open_count"] = counts[value["split"]][offset]
        value["completeness"] = (
            "missing_page"
            if value["id"] == "T04"
            else "partial_visibility"
            if value["id"] == "H04"
            else "complete"
        )
    return values


def physical_rows(s: dict) -> list[dict]:
    """來源列 ID 與業務唯一鍵分離；同名、同代碼不同單位不合併。"""
    rows = []
    for index in range(max(3, s["open_count"] + 4)):
        # 前兩列同名、同代碼，但屬於不同單位，必須算兩個項目。
        rows.append(
            {
                "unit_id": "甲單位" if index % 2 == 0 else "乙單位",
                "item_code": f"K{index // 2 + 1:03d}",
                "name": s["names"][0]
                if index < 2
                else f"{s['topic']}項目{index + 1:02d}",
                "qualification": s["qualifications"][index % 3],
                "fee": s["fees"][index % 3],
                "status": "開放" if index < s["open_count"] else "暫停",
            }
        )
    # 同一業務列再次出現，只有來源 anchor 不同，不增加唯一項目數。
    rows.insert(min(8, len(rows)), dict(rows[0]))
    for index, row in enumerate(rows, 1):
        row["anchor"] = f"R-{s['id']}-{index:03d}"
        row["page"] = (index - 1) // 8 + 1
    if s["completeness"] != "complete":
        rows = rows[:8]
    return rows


def unique_open_rows(rows: list[dict]) -> list[dict]:
    result = {}
    for row in rows:
        key = (row["unit_id"], row["item_code"])
        if key in result and any(
            result[key][field] != row[field]
            for field in ("name", "qualification", "fee", "status")
        ):
            raise ValueError("Conflicting duplicate source row")
        result.setdefault(key, row)
    return [row for row in result.values() if row["status"] == "開放"]


def values(row: dict) -> list[str]:
    return [
        row["anchor"],
        row["unit_id"],
        row["item_code"],
        row["name"],
        row["qualification"],
        str(row["fee"]),
        row["status"],
    ]


def scope_text(s: dict, rows: list[dict]) -> str:
    common = (
        f"{s['topic']} v2 以單位與項目代碼的組合作唯一鍵。"
        "同名或相同代碼但不同單位是不同項目；重複刊載同一鍵只計一次。"
        "只計狀態為開放的項目，不計表頭或暫停項目。"
    )
    if s["completeness"] == "complete":
        return common + f"本來源共有 {len(rows)} 個實體資料列，末頁有 END 標記。"
    reason = (
        "後續目錄頁未附上"
        if s["completeness"] == "missing_page"
        else "只提供部分可見範圍"
    )
    return common + reason + "；不得由可見列推定完整清單或確切總數。"


def author_documents(out: Path, s: dict, rows: list[dict], font: Path) -> list[dict]:
    from docx import Document
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
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

    folder = out / "corpus" / s["split"] / s["id"]
    folder.mkdir(parents=True, exist_ok=False)
    title = f"{s['id']} {s['topic']}"
    intro = scope_text(s, rows)
    marker = (
        f"E-{s['id']}-END"
        if s["completeness"] == "complete"
        else f"E-{s['id']}-INCOMPLETE"
    )
    ending = (
        "此為完整來源終點，沒有其他附件。"
        if s["completeness"] == "complete"
        else ("來源不完整，必須取得缺頁或完整授權範圍，不能報確切總數。")
    )
    (folder / "rules.md").write_text(
        f"# {title} 規章\n\n## 適用範圍\n\n{intro}\n\n## 日期\n\n"
        f"[E-{s['id']}-DATE] v2 的{s['event']}截止日為{s['deadline']}。\n",
        encoding="utf-8",
    )
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Inches(8.5), Inches(11)
    sec.left_margin = sec.right_margin = Inches(0.65)
    for name in ("Normal", "Title", "Heading 1"):
        style = doc.styles[name]
        style.font.name = "Arial Unicode MS"
        style.font.size = Pt(11 if name == "Normal" else 16)
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "Arial Unicode MS")
        style.paragraph_format.space_after = Pt(7)
        properties = style.element.find(qn("w:pPr"))
        if properties is not None:
            for border in list(properties.findall(qn("w:pBdr"))):
                properties.remove(border)
    doc.add_paragraph(title + " 目錄", "Title")
    doc.add_paragraph(intro)
    doc.add_heading("適用項目", 1)
    doc.add_paragraph(f"[E-{s['id']}-CATALOG] 以下各列屬同一 v2 範圍。費用單位為元。")
    table = doc.add_table(rows=0, cols=7)
    table.autofit = False
    widths = [0.95, 0.62, 0.55, 1.2, 1.45, 0.6, 0.55]
    for column, width in zip(table.columns, widths, strict=True):
        column.width = Inches(width)
    for index, contents in enumerate([HEADERS, *[values(r) for r in rows]]):
        tr = table.add_row()
        for col, (cell, text) in enumerate(zip(tr.cells, contents, strict=True)):
            cell.width = Inches(widths[col])
            cell.text = text
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for p in cell.paragraphs:
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.space_before = Pt(4)
                p.alignment = (
                    WD_ALIGN_PARAGRAPH.CENTER
                    if col in {1, 2, 5, 6}
                    else WD_ALIGN_PARAGRAPH.LEFT
                )
                for run in p.runs:
                    run.font.size = Pt(10)
            props = cell._tc.get_or_add_tcPr()
            borders = OxmlElement("w:tcBorders")
            for edge in ("top", "left", "bottom", "right"):
                item = OxmlElement(f"w:{edge}")
                for key, value in {
                    "val": "single",
                    "sz": "4",
                    "color": "D9D9D9",
                }.items():
                    item.set(qn(f"w:{key}"), value)
                borders.append(item)
            props.append(borders)
            margins = OxmlElement("w:tcMar")
            for edge in ("top", "left", "bottom", "right"):
                item = OxmlElement(f"w:{edge}")
                item.set(qn("w:w"), "70")
                item.set(qn("w:type"), "dxa")
                margins.append(item)
            props.append(margins)
            if index == 0:
                shade = OxmlElement("w:shd")
                shade.set(qn("w:fill"), "E8EEF3")
                props.append(shade)
        no_split = OxmlElement("w:cantSplit")
        tr._tr.get_or_add_trPr().append(no_split)
    header = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(header)
    doc.add_paragraph(f"[{marker}] {ending}")
    doc.core_properties.created = doc.core_properties.modified = datetime(2026, 10, 4)
    doc.core_properties.author = "U0 Synthetic Fixture"
    doc.save(folder / "catalog.docx")
    with ZipFile(folder / "catalog.docx") as archive:
        entries = [(name, archive.read(name)) for name in archive.namelist()]
    with ZipFile(folder / "catalog.docx", "w", ZIP_DEFLATED) as archive:
        for name, content in entries:
            info = ZipInfo(name, (2026, 10, 4, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)

    pdfmetrics.registerFont(TTFont("U0V2CJK", str(font)))
    ps = ParagraphStyle("body", fontName="U0V2CJK", fontSize=10, leading=15)
    pages = max(2, (len(rows) + 7) // 8)
    story = []
    for page in range(1, pages + 1):
        if page > 1:
            story.append(PageBreak())
        story.extend(
            [Paragraph(f"{title} 操作流程與目錄 第 {page} 頁", ps), Spacer(1, 10)]
        )
        if page == 1:
            story.extend(
                [Paragraph(f"[E-{s['id']}-CATALOG] {intro}", ps), Spacer(1, 8)]
            )
        if page <= 2:
            story.append(Paragraph("操作步驟", ps))
            for index in range((page - 1) * 2, page * 2):
                story.append(
                    Paragraph(
                        f"[E-{s['id']}-STEP{index + 1}] "
                        f"{index + 1}. {s['steps'][index]}。",
                        ps,
                    )
                )
        story.extend([Spacer(1, 10), Paragraph("適用項目 費用單位為元", ps)])
        part = rows[(page - 1) * 8 : page * 8]
        rendered = [
            [Paragraph(v, ps) for v in row]
            for row in [HEADERS, *[values(r) for r in part]]
        ]
        table_pdf = Table(rendered, colWidths=[70, 45, 40, 85, 105, 45, 45])
        table_pdf.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9D9D9")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF3")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(table_pdf)
        if page == pages:
            story.extend([Spacer(1, 8), Paragraph(f"[{marker}] {ending}", ps)])
    SimpleDocTemplate(
        str(folder / "procedure.pdf"),
        invariant=1,
        title=title,
        author="U0 Synthetic Fixture",
    ).build(story)
    (folder / "notes.txt").write_text(
        f"{title} 版本說明\n\n[E-{s['id']}-EDITION] v1 與 v2 是不同適用批次，"
        f"v1 截止日 2026-12-10，v2 截止日 {s['deadline']}。"
        "未指定批次應追問，不猜測。\n\n"
        f"[E-{s['id']}-MISSING] 未記載{s['missing']}。\n",
        encoding="utf-8",
    )
    docs = []
    for name in ("rules.md", "catalog.docx", "procedure.pdf", "notes.txt"):
        path = folder / name
        docs.append(
            {
                "id": f"{s['id']}/{name}",
                "group": s["id"],
                "split": s["split"],
                "domain": s["domain"],
                "path": path.relative_to(out).as_posix(),
                "version": "v2",
                "title": title + " " + name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "expected_pages": pages if name.endswith(".pdf") else None,
            }
        )
        write_json(
            path.with_name(name + ".metadata.yaml"),
            {
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
                "custom_metadata": {
                    "synthetic": True,
                    "u0_group": s["id"],
                    "dataset_version": VERSION,
                },
            },
        )
    return docs


def build_cases(s: dict, rows: list[dict]) -> list[dict]:
    group, topic = s["id"], s["topic"]
    opens = unique_open_rows(rows)
    complete = s["completeness"] == "complete"
    pairs = rows[:2]
    prompts = {
        "fact": f"{topic} v2 的{s['event']}截止日期？",
        "list": f"根據{topic} v2 操作流程 PDF，列出全部開放項目，附單位及代碼並去重。",
        "count": f"根據{topic} v2 操作流程 PDF，以單位與項目代碼的組合去重，"
        "共有幾個開放項目？",
        "compare": f"比較{topic} v2 目錄中甲單位 K001 與乙單位 K001 的資格及費用。",
        "procedure": f"{topic} v2 操作流程的四個步驟，請依序說明。",
        "followup": "那第二個的資格和費用呢？",
        "no_answer": f"{topic}文件中的{s['missing']}是多少？",
        "ambiguity": f"{topic}這次的截止日是哪一天？",
    }
    result = []
    for kind in TYPES:
        refusal = kind == "no_answer" or (kind in {"list", "count"} and not complete)
        doc = (
            "rules.md"
            if kind == "fact"
            else "notes.txt"
            if kind in {"no_answer", "ambiguity"}
            else "procedure.pdf"
            if kind in {"list", "count", "procedure"}
            else "catalog.docx"
        )
        marker = f"E-{group}-END" if complete else f"E-{group}-INCOMPLETE"
        anchors = {
            "fact": [f"E-{group}-DATE"],
            "list": [f"E-{group}-CATALOG", marker, *[r["anchor"] for r in rows]],
            "count": [f"E-{group}-CATALOG", marker, *[r["anchor"] for r in rows]],
            "compare": [r["anchor"] for r in pairs],
            "followup": [pairs[1]["anchor"]],
            "procedure": [f"E-{group}-STEP{i}" for i in range(1, 5)],
            "no_answer": [f"E-{group}-MISSING"],
            "ambiguity": [f"E-{group}-EDITION"],
        }[kind]
        claims = {
            "fact": [s["deadline"]],
            "list": [f"{r['unit_id']} {r['item_code']} {r['name']}" for r in opens],
            "count": [str(len(opens))],
            "compare": [
                f"{r['unit_id']} {r['qualification']} {r['fee']} 元" for r in pairs
            ],
            "followup": [
                f"{pairs[1]['unit_id']} {pairs[1]['qualification']} "
                f"{pairs[1]['fee']} 元"
            ],
            "procedure": s["steps"],
            "no_answer": [],
            "ambiguity": [],
        }[kind]
        if refusal:
            claims = []
        pages = (
            list(range(1, max(2, (len(rows) + 7) // 8) + 1))
            if kind in {"list", "count"}
            else [1, 2]
            if kind == "procedure"
            else None
        )
        section = (
            "日期"
            if kind == "fact"
            else "操作步驟"
            if kind == "procedure"
            else "適用項目"
            if doc != "notes.txt"
            else None
        )
        history = (
            []
            if kind != "followup"
            else [
                {
                    "role": "user",
                    "text": f"請在{topic} v2 目錄比較甲單位 K001 與乙單位 K001。",
                },
                {
                    "role": "assistant",
                    "text": "第一個是甲單位 K001，第二個是乙單位 K001。",
                },
            ]
        )
        result.append(
            {
                "id": f"{group}-{kind}",
                "type": kind,
                "split": s["split"],
                "domain": s["domain"],
                "source_group": group,
                "question": prompts[kind],
                "history": history,
                "scope": {
                    "topic": topic,
                    "edition": None if kind == "ambiguity" else "v2",
                },
                "required_conditions": ["source_group", "edition", "authorized_source"],
                "gold": {
                    "expected": "refuse"
                    if refusal
                    else "clarify"
                    if kind == "ambiguity"
                    else "answer",
                    "claims": claims,
                    "reason": "not_documented"
                    if kind == "no_answer"
                    else s["completeness"]
                    if refusal
                    else "missing_edition"
                    if kind == "ambiguity"
                    else None,
                    "evidence": [
                        {
                            "document": f"{group}/{doc}",
                            "version": "v2",
                            "anchors": anchors,
                            "section": section,
                            "pages": pages,
                        }
                    ],
                    "row_ids": [r["anchor"] for r in rows]
                    if kind in {"list", "count"}
                    else [r["anchor"] for r in pairs]
                    if kind == "compare"
                    else [],
                    "complete_required": kind in {"list", "count"},
                    "source_complete": complete,
                    "count_unit": s["unit"] if kind == "count" else None,
                    "count_unique_key": ["unit_id", "item_code"]
                    if kind == "count"
                    else None,
                    "human_review_status": "pending",
                },
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REVISION)
    args = parser.parse_args()
    out = args.output_dir.resolve()
    if out.exists():
        raise SystemExit("Refusing to overwrite an existing revision directory")
    if not FONT_PATH.is_file():
        raise SystemExit("Local CJK font required; no downloads attempted")
    groups, docs, cases, lineage = scenarios(), [], [], []
    for s in groups:
        rows = physical_rows(s)
        docs.extend(author_documents(out, s, rows, FONT_PATH))
        cases.extend(build_cases(s, rows))
        lineage.append(
            {"group": s["id"], "completeness": s["completeness"], "rows": rows}
        )
    write_json(out / "scenarios.json", groups)
    write_json(out / "lineage.json", lineage)
    write_json(out / "documents.json", docs)
    write_json(
        out / "safety-cases.json", json.loads((ROOT / "safety-cases.json").read_text())
    )
    for split in ("tuning", "holdout"):
        write_json(
            out / "datasets" / f"{split}.json",
            [c for c in cases if c["split"] == split],
        )
    files = {
        p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }
    write_json(
        out / "manifest.json",
        {
            "version": VERSION,
            "created": "2026-10-04",
            "gold_status": "pending_human_review",
            "files": files,
            "parent_manifest_sha256": hashlib.sha256(
                (ROOT / "manifest.json").read_bytes()
            ).hexdigest(),
            "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )
    print(
        "v2 draft created: 80 cases / 40 documents; "
        "human review pending; no DB or model calls"
    )


if __name__ == "__main__":
    main()
