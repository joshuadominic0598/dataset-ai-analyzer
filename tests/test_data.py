import tempfile
import unittest
from pathlib import Path

from reviso.data import Store, canonical, parse_csv, profile

class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite")
        self.dataset = self.store.create_dataset("Test")
        self.id = self.dataset["id"]

    def ingest(self, content=b"id,value\n1,10\n2,20\n"):
        return self.store.ingest(self.id, "Original", content)

    def test_utf8_bom_and_multiline_csv(self):
        columns, rows = parse_csv('\ufeffid,notes\n1,"a line\nwith a comma, too"\n'.encode())
        self.assertEqual(columns, ["id", "notes"])
        self.assertEqual(rows[0]["notes"], "a line\nwith a comma, too")

    def test_duplicate_headers_rejected_positionally(self):
        for source in (b"id,id\n1,2\n", b"id,ID\n1,2\n"):
            with self.assertRaisesRegex(ValueError, "Repeated"):
                parse_csv(source)

    def test_malformed_width_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "record 2"):
            parse_csv(b"id,value\n1,2,3\n")

    def test_invalid_encoding_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            parse_csv(b"col\n\xff\n")

    def test_missing_values_preserve_semantic_categories(self):
        header, rows = parse_csv(b"x,y\nNA,\nNone,0\n ,foo\n")
        facts = profile(header, rows)
        self.assertEqual(facts["x"]["missing"], 1)
        self.assertEqual(rows[0]["x"], "NA")
        self.assertEqual(rows[1]["x"], "None")
        self.assertEqual(rows[1]["y"], "0")

    def test_normalization_keeps_leading_zero_codes(self):
        self.assertEqual(canonical("20.0"), canonical("20"))
        self.assertNotEqual(canonical("001"), canonical("1"))
        self.assertIsNone(canonical(" "))

    def test_sql_values_are_typed_and_large_integers_preserved(self):
        v = self.ingest(b"id,value\n001,9007199254740993\n002,20\n")
        result = self.store.query(self.id, f'SELECT id, value FROM "{v["table"]}" ORDER BY id')
        self.assertEqual(result["rows"][0], ["001", 9007199254740993])

    def test_original_bytes_are_immutable(self):
        content = b"id,value\r\n1,10.0\r\n"
        v = self.ingest(content)
        self.assertEqual(self.store.source(self.id, v["id"]), content)

    def test_five_version_limit_preserves_existing(self):
        for i in range(5):
            self.store.ingest(self.id, str(i), b"x\n1\n")
        with self.assertRaisesRegex(ValueError, "five"):
            self.ingest()
        self.assertEqual(len(self.store.versions(self.id)), 5)

    def test_failed_ingestion_does_not_consume_slot(self):
        with self.assertRaises(ValueError):
            self.ingest(b"x,y\n1\n")
        self.assertEqual(len(self.store.versions(self.id)), 0)

    def test_safe_aggregate_query(self):
        v = self.ingest()
        result = self.store.query(self.id, f'SELECT count(*), sum(value) FROM "{v["table"]}"')
        self.assertEqual(result["rows"], [[2, 30]])

    def test_mutations_and_multiple_statements_blocked(self):
        v = self.ingest()
        for sql in (
            f'DELETE FROM "{v["table"]}"',
            f'SELECT * FROM "{v["table"]}"; DROP TABLE datasets',
            'ATTACH DATABASE ":memory:" AS other',
            "PRAGMA database_list",
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.store.query(self.id, sql)
        self.assertEqual(
            self.store.query(self.id, f'SELECT count(*) FROM "{v["table"]}"')["rows"], [[2]]
        )

    def test_metadata_and_functions_blocked(self):
        self.ingest()
        for sql in (
            "SELECT * FROM sqlite_master",
            "SELECT * FROM datasets",
            'SELECT * FROM pragma_table_info("datasets")',
            "SELECT randomblob(999999999)",
            'SELECT load_extension("x")',
        ):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.store.query(self.id, sql)

    def test_other_datasets_and_future_snapshots_blocked(self):
        first = self.ingest()
        second = self.ingest(b"id,value\n3,30\n")
        other = self.store.create_dataset("Other")
        foreign = self.store.ingest(other["id"], "Secret", b"secret\nprivate\n")
        for table, ids in ((foreign["table"], None), (second["table"], [first["id"]])):
            with self.assertRaises(ValueError):
                self.store.query(self.id, f'SELECT * FROM "{table}"', ids)

    def test_sql_result_cap(self):
        v = self.ingest(("x\n" + "\n".join(str(i) for i in range(205)) + "\n").encode())
        result = self.store.query(self.id, f'SELECT * FROM "{v["table"]}"')
        self.assertEqual(len(result["rows"]), 200)
        self.assertTrue(result["truncated"])

    def test_recursive_query_has_time_budget(self):
        self.ingest()
        with self.assertRaisesRegex(ValueError, "interrupted"):
            self.store.query(
                self.id,
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n",
            )

    def test_quoted_column_names_are_safe(self):
        v = self.ingest(b'"odd""name",value\nhello,5\n')
        result = self.store.query(self.id, f'SELECT "odd""name" FROM "{v["table"]}"')
        self.assertEqual(result["rows"], [["hello"]])

    def test_empty_csv_and_empty_cells(self):
        v = self.ingest(b"id,value\n")
        self.assertEqual(v["row_count"], 0)
        self.assertEqual(v["profile"]["id"]["kind"], "empty")

    def test_configuration_validates_columns_and_thresholds(self):
        self.ingest()
        for config in (
            {"identity": ["missing"]},
            {"growth_threshold": float("nan")},
            {"cloud_allowed": "false"},
            {"semantic": "value"},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.store.configure(self.id, config)

if __name__ == "__main__":
    unittest.main()
