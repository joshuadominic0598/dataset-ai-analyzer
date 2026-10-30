import tempfile
import unittest
from pathlib import Path

from reviso.analysis import build_report, discover
from reviso.data import Store
from reviso.demo import evaluate, fixtures, load_demo

class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite")
        self.id = self.store.create_dataset("Test")["id"]

    def prepare(self, before, after, identity=None):
        self.store.ingest(self.id, "Before", before)
        self.store.ingest(self.id, "After", after)
        self.store.configure(self.id, {"identity": identity or [], "setup_complete": True})
        return build_report(self.store, self.id)

    def test_known_housing_changes(self):
        manifest = load_demo(self.store, self.id)
        report = build_report(self.store, self.id)
        result = evaluate(report, manifest)
        self.assertEqual(
            result["passed"], result["total"], [c for c in result["checks"] if not c["passed"]]
        )

    def test_portable_synthetic_demo(self):
        snapshots, manifest = fixtures(None)
        for label, content in snapshots:
            self.store.ingest(self.id, label, content)
        self.store.configure(self.id, {"identity": ["Id"], "setup_complete": True})
        result = evaluate(build_report(self.store, self.id), manifest)
        self.assertEqual(
            result["passed"], result["total"], [c for c in result["checks"] if not c["passed"]]
        )

    def test_no_setup_or_question_is_required_and_one_file_is_enough(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            build_report(self.store, self.id)
        self.store.ingest(self.id, "One", b"x\n1\n")
        report = build_report(self.store, self.id)
        self.assertEqual(report["mode"], "exploration")
        self.assertFalse(report["draft"])
        self.assertEqual(report["comparisons"], [])
        self.assertIsNone(report["baseline"])
        self.assertEqual(report["exploration"]["records"], 1)

    def test_automatic_analysis_selects_useful_fields_without_changing_user_settings(self):
        content = "id,price,sale_date,region,rating\n" + "".join(
            f"{i},{i * 10},2026-01-{i:02d},{'North' if i % 2 else 'South'},{'Low' if i % 2 else 'High'}\n"
            for i in range(1, 13)
        )
        self.store.ingest(self.id, "Only file", content.encode())
        report = build_report(self.store, self.id)
        self.assertEqual(report["analysis_config"]["identity"], ["id"])
        self.assertEqual(report["analysis_config"]["target_column"], "price")
        self.assertEqual(report["analysis_config"]["date_column"], "sale_date")
        self.assertEqual(report["exploration"]["fields"]["rating"]["role"], "ordinal")
        self.assertEqual(report["exploration"]["time_summary"]["valid_dates"], 12)
        self.assertTrue(report["exploration"]["group_summaries"])
        self.assertTrue(report["analysis_assumptions"])
        self.assertFalse(report["draft"])
        self.assertEqual(report["config"]["identity"], [])
        self.assertEqual(self.store.dataset(self.id)["config"], report["config"])
        self.assertFalse(report["config"]["cloud_allowed"])

    def test_automatic_identity_keeps_later_missing_and_repeated_records_visible(self):
        self.store.ingest(self.id, "Before", b"id,price\n1,10\n2,20\n3,30\n")
        self.store.ingest(self.id, "After", b"id,price\n1,10\n2,20\n2,99\n,30\n")
        report = build_report(self.store, self.id)
        movement = report["comparisons"][0]["movement"]
        self.assertEqual(movement["mode"], "record_identity")
        self.assertEqual(movement["missing_identity_after"], 1)
        self.assertEqual(movement["ambiguous_identities"], 1)
        self.assertFalse(movement["exact"])

    def test_automatic_analysis_respects_overrides_and_leaves_ambiguous_dates_unselected(self):
        self.store.ingest(
            self.id, "Only file", b"id,cost,price,date\n1,10,20,01/02/2026\n2,15,30,02/03/2026\n"
        )
        self.store.configure(
            self.id, {"target_column": "cost", "column_roles": {"price": "nominal"}}
        )
        report = build_report(self.store, self.id)
        self.assertEqual(report["analysis_config"]["target_column"], "cost")
        self.assertEqual(report["exploration"]["fields"]["price"]["role"], "nominal")
        self.assertIsNone(report["exploration"]["time_summary"])

    def test_without_identity_does_not_invent_edits(self):
        report = self.prepare(b"a,b\nx,1\nx,1\ny,2\n", b"a,b\nx,1\nx,1\ny,3\n")
        movement = report["comparisons"][0]["movement"]
        self.assertEqual(movement["mode"], "row_content")
        self.assertEqual(movement["matched"], 2)
        self.assertNotIn("changed", movement)

    def test_composite_identity(self):
        r = self.prepare(
            b"customer,site,value\na,x,1\na,y,2\n",
            b"customer,site,value\na,x,3\na,y,2\n",
            ["customer", "site"],
        )
        self.assertEqual(r["comparisons"][0]["movement"]["changed"], 1)
        self.assertTrue(r["comparisons"][0]["movement"]["exact"])

    def test_numeric_representation_is_not_record_edit(self):
        r = self.prepare(b"id,value\n1,10\n2,20\n", b"id,value\n1,10.0\n2,20.0\n", ["id"])
        self.assertEqual(r["comparisons"][0]["movement"]["changed"], 0)
        self.assertIn("number_format", [f["kind"] for f in r["findings"]])

    def test_missing_and_repeated_identifiers_are_excluded(self):
        r = self.prepare(
            b"id,value\n1,10\n2,20\n,30\n", b"id,value\n1,10\n2,20\n2,99\n,30\n", ["id"]
        )
        m = r["comparisons"][0]["movement"]
        self.assertFalse(m["exact"])
        self.assertEqual(m["unchanged"], 1)
        self.assertEqual(m["ambiguous_identities"], 1)
        self.assertEqual(m["missing_identity_after"], 1)

    def test_same_size_replacement_detected(self):
        r = self.prepare(b"id,value\n1,10\n2,20\n", b"id,value\n1,10\n3,30\n", ["id"])
        m = r["comparisons"][0]["movement"]
        self.assertEqual((m["added"], m["removed"]), (1, 1))

    def test_ambiguous_renames_are_not_aligned(self):
        r = self.prepare(b"id,old\n1,10\n2,20\n", b"id,new_a,new_b\n1,10,10\n2,20,20\n", ["id"])
        self.assertEqual(r["comparisons"][0]["provisional_mappings"], {})
        self.assertEqual(len([f for f in r["findings"] if f["kind"] == "possible_rename"]), 2)

    def test_growing_missing_share_versus_count(self):
        r = self.prepare(b"id,value\n1,\n2,20\n", b"id,value\n1,\n2,20\n3,\n4,40\n", ["id"])
        self.assertNotIn("missing_values", [f["kind"] for f in r["findings"]])

    def test_rejected_rename_keeps_fields_separate(self):
        self.prepare(b"id,old\n1,10\n2,20\n", b"id,new\n1,10\n2,20\n", ["id"])
        self.store.configure(
            self.id, {"rename_decisions": [{"old": "old", "new": "new", "decision": "reject"}]}
        )
        report = build_report(self.store, self.id)
        self.assertEqual(report["comparisons"][0]["provisional_mappings"], {})
        signal = next(f for f in report["findings"] if f["kind"] == "possible_rename")
        self.assertEqual(signal["assessment"], "user_rejected")

    def test_confirmed_rename_can_match_changed_values(self):
        self.prepare(b"id,old\n1,10\n2,20\n", b"id,new\n1,30\n2,40\n", ["id"])
        self.store.configure(
            self.id, {"rename_decisions": [{"old": "old", "new": "new", "decision": "confirm"}]}
        )
        report = build_report(self.store, self.id)
        self.assertEqual(report["comparisons"][0]["movement"]["changed"], 2)
        signal = next(f for f in report["findings"] if f["kind"] == "possible_rename")
        self.assertEqual(signal["assessment"], "user_confirmed")

    def test_rename_survives_large_append_in_baseline(self):
        load_demo(self.store, self.id)
        report = build_report(self.store, self.id)
        self.assertEqual(report["baseline"]["provisional_mappings"], {"SalePrice": "SaleAmount"})
        self.assertEqual(report["baseline"]["movement"]["changed"], 0)

    def test_text_suggestion_needs_actual_prose(self):
        prose = "A detailed description of a quiet family house near parks and public transport."
        content = f"id,notes,code\n1,{prose},abc\n2,{prose},def\n".encode()
        self.store.ingest(self.id, "One", content)
        d = discover(self.store, self.id)
        self.assertIn("notes", d["semantic_candidates"])
        self.assertNotIn("code", d["semantic_candidates"])

    def test_first_to_latest_is_separate_from_neighboring_steps(self):
        self.store.ingest(self.id, "One", b"id,x\n1,1\n")
        self.store.ingest(self.id, "Two", b"id,x\n1,2\n")
        self.store.ingest(self.id, "Three", b"id,x\n1,1\n")
        self.store.configure(self.id, {"identity": ["id"], "setup_complete": True})
        r = build_report(self.store, self.id)
        self.assertEqual([p["movement"]["changed"] for p in r["comparisons"]], [1, 1])
        self.assertEqual(r["baseline"]["movement"]["changed"], 0)

    def test_identifiers_are_not_screened_as_numeric_extremes(self):
        before = "id,value\n" + "\n".join(f"{i},{i % 5}" for i in range(20)) + "\n"
        after = before + "\n".join(f"{10000 + i},{i % 5}" for i in range(20)) + "\n"
        r = self.prepare(before.encode(), after.encode(), ["id"])
        self.assertFalse(
            any(f["kind"] == "unusual_values" and f["column"] == "id" for f in r["findings"])
        )

    def test_larger_load_with_same_extreme_share_is_not_new_drift(self):
        before = (
            "id,value\n"
            + "\n".join(f"{i},{1000 if i % 20 == 0 else i % 20}" for i in range(20))
            + "\n"
        )
        after = (
            "id,value\n"
            + "\n".join(f"{i},{1000 if i % 20 == 0 else i % 20}" for i in range(120))
            + "\n"
        )
        r = self.prepare(before.encode(), after.encode(), ["id"])
        self.assertFalse(any(f["kind"] == "unusual_values" for f in r["findings"]))

    def test_zero_heavy_field_preserves_previous_valid_range(self):
        values = [0] * 16 + [10, 20, 30, 40]
        before = "id,value\n" + "\n".join(f"{i},{v}" for i, v in enumerate(values)) + "\n"
        after = (
            "id,value\n"
            + "\n".join(f"{i},{v}" for i, v in enumerate(values + [10, 20, 30, 40] * 2))
            + "\n"
        )
        r = self.prepare(before.encode(), after.encode(), ["id"])
        self.assertFalse(any(f["kind"] == "unusual_values" for f in r["findings"]))

if __name__ == "__main__":
    unittest.main()
