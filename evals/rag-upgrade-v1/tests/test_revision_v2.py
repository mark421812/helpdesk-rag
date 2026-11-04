"""v2 Gold 計數／拒答設計 guards；不是模型、ACL 或人工品質驗收。"""

import unittest

from audit_revision_v2 import row_cells_present
from build_revision_v2 import build_cases, physical_rows, scenarios, unique_open_rows


class RevisionTests(unittest.TestCase):
    def test_linear_cells_cannot_be_borrowed_from_next_row(self):
        s = scenarios()[0]
        first, second = physical_rows(s)[:2]
        text = f"{first['anchor']} 甲單位 K001\n{second['anchor']} "
        text += f"{first['name']} {first['qualification']} {first['fee']} 暫停"
        self.assertFalse(row_cells_present(text, first))

    def test_linear_cells_are_not_typed_structure(self):
        row = physical_rows(scenarios()[0])[0]
        text = " ".join(
            str(row[k])
            for k in (
                "anchor",
                "unit_id",
                "item_code",
                "name",
                "qualification",
                "fee",
                "status",
            )
        )
        self.assertTrue(row_cells_present(text, row))
        self.assertFalse(
            row_cells_present(text.replace(row["qualification"], "缺欄"), row)
        )

    def test_complete_counts_have_zero_small_and_large_values(self):
        seen = set()
        for s in scenarios():
            rows = physical_rows(s)
            if s["completeness"] == "complete":
                self.assertEqual(len(unique_open_rows(rows)), s["open_count"])
                case = next(c for c in build_cases(s, rows) if c["type"] == "count")
                self.assertEqual(case["gold"]["claims"], [str(s["open_count"])])
                seen.add(s["open_count"])
        self.assertEqual(seen, {0, 1, 2, 3, 5, 33, 36})

    def test_same_name_code_different_units_stay_distinct(self):
        s = next(s for s in scenarios() if s["id"] == "T03")
        rows = physical_rows(s)
        self.assertEqual(rows[0]["name"], rows[1]["name"])
        self.assertEqual(rows[0]["item_code"], rows[1]["item_code"])
        self.assertNotEqual(rows[0]["unit_id"], rows[1]["unit_id"])
        self.assertEqual(len(unique_open_rows(rows)), 3)

    def test_reprinted_row_does_not_increase_count(self):
        s = next(s for s in scenarios() if s["id"] == "T05")
        rows = physical_rows(s)
        self.assertEqual(rows[0]["item_code"], rows[8]["item_code"])
        self.assertEqual(rows[0]["unit_id"], rows[8]["unit_id"])
        self.assertNotEqual(rows[0]["anchor"], rows[8]["anchor"])
        self.assertNotEqual(rows[0]["page"], rows[8]["page"])
        self.assertEqual(len(unique_open_rows(rows)), 36)

    def test_conflicting_duplicate_rejected_even_when_paused(self):
        row = physical_rows(scenarios()[0])[0]
        with self.assertRaises(ValueError):
            unique_open_rows([row, {**row, "fee": row["fee"] + 1}])
        with self.assertRaises(ValueError):
            unique_open_rows([row, {**row, "status": "開放"}])

    def test_partial_source_has_no_exact_total_or_list_claim(self):
        for s in scenarios():
            if s["completeness"] != "complete":
                cases = build_cases(s, physical_rows(s))
                for c in cases:
                    if c["type"] in {"list", "count"}:
                        self.assertEqual(c["gold"]["expected"], "refuse")
                        self.assertEqual(c["gold"]["claims"], [])
                        self.assertFalse(c["gold"]["source_complete"])
                        self.assertEqual(c["gold"]["reason"], s["completeness"])

    def test_gold_is_pending_and_pages_follow_source_rows(self):
        for s in scenarios():
            rows = physical_rows(s)
            cases = build_cases(s, rows)
            self.assertEqual(len(cases), 8)
            for c in cases:
                self.assertEqual(c["gold"]["human_review_status"], "pending")
                if c["type"] in {"list", "count"}:
                    self.assertEqual(c["gold"]["row_ids"], [r["anchor"] for r in rows])
                    self.assertEqual(
                        c["gold"]["evidence"][0]["pages"][-1], max(2, rows[-1]["page"])
                    )

    def test_followup_identifies_unit_not_ambiguous_name(self):
        s = scenarios()[0]
        case = next(
            c for c in build_cases(s, physical_rows(s)) if c["type"] == "followup"
        )
        self.assertIn("乙單位 K001", case["history"][-1]["text"])
        self.assertIn("乙單位", case["gold"]["claims"][0])

    def test_plain_text_gold_does_not_invent_section_or_page(self):
        s = scenarios()[0]
        for c in build_cases(s, physical_rows(s)):
            if c["type"] in {"ambiguity", "no_answer"}:
                self.assertIsNone(c["gold"]["evidence"][0]["section"])
                self.assertIsNone(c["gold"]["evidence"][0]["pages"])


if __name__ == "__main__":
    unittest.main()
