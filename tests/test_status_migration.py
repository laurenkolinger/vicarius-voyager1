"""status.csv schema migration (final review C1).

`config.initialize_tracking` used to truncate any existing status.csv whose
header differed from the current schema, discarding every data row. This
build's own header change (identity columns plus the two scale columns) made
that reachable from a 3D_init or 3D_phase1 workspace, so the truncation is
replaced by a backup plus a column-name migration. These tests pin both the
pure mapping and the on-disk behaviour, using the archived 3D_init header
verbatim (_archive/3D_init/github_repo/src/config.py:207).
"""

import csv
import importlib
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "src"))

# The archived 3D_init status.csv header, copied literally from
# _archive/3D_init/github_repo/src/config.py:207. Model ID / Status first, no
# identity columns, Notes before the scale columns, and no "Scale Error (ppm)"
# or "Scale Bars" at all.
LEGACY_HEADER = [
    "Model ID", "Status", "Step 0 complete", "Video Length (s)", "Total Video Frames",
    "Frames Extracted", "Video Source", "Extraction Timestamp", "Step 0 start time",
    "Step 0 end time", "Step 0 processing time (s)", "Frames directory", "Step 0 error time",
    "Step 1 complete", "Step 1 start time", "Step 1 end time", "Step 1 processing time (s)",
    "Aligned cameras", "Total cameras", "PSX file", "Report file", "Step 1 error time",
    "Step 2 complete", "Step 2 site", "Step 2 consolidation time", "Step 3 complete",
    "Step 3 scale method", "Step 3 scale applied", "Step 3 ortho exported",
    "Step 3 model exported", "Step 3 processing time", "Step 4 complete",
    "Step 4 web published", "Sketchfab URL", "Step 4 high-res exported",
    "Step 4 processing time", "Notes", "Scale", "Scale Error (m)", "Cameras Removed",
]


def _legacy_row(model_id, **cells):
    row = [""] * len(LEGACY_HEADER)
    row[LEGACY_HEADER.index("Model ID")] = model_id
    for name, value in cells.items():
        row[LEGACY_HEADER.index(name)] = value
    return row


class MigrateRowsTests(unittest.TestCase):
    """The pure mapping, with no project directory in play."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sys.argv = ["x", self.tmp]
        open(os.path.join(self.tmp, "analysis_params.yaml"), "w").write(
            open(os.path.join(REPO, "analysis_params.yaml")).read())
        import config
        self.c = importlib.reload(config)

    def test_shared_columns_are_carried_and_new_columns_start_blank(self):
        rows = self.c.migrate_rows(
            LEGACY_HEADER,
            [_legacy_row("MRS_T1", Status="Complete")],
            self.c.headers,
        )
        got = dict(zip(self.c.headers, rows[0]))
        self.assertEqual(got["Model ID"], "MRS_T1")
        self.assertEqual(got["Status"], "Complete")
        self.assertEqual(got["original_videos"], "")
        self.assertEqual(got["readable_id"], "")
        self.assertEqual(got["Scale Error (ppm)"], "")
        self.assertEqual(got["Scale Bars"], "")

    def test_columns_only_the_old_header_had_are_appended_to_notes(self):
        old_header = LEGACY_HEADER + ["Legacy Only"]
        row = _legacy_row("MRS_T1", Notes="operator note") + ["kept value"]
        rows = self.c.migrate_rows(old_header, [row], self.c.headers)
        notes = dict(zip(self.c.headers, rows[0]))["Notes"]
        self.assertIn("operator note", notes)
        self.assertIn("migrated: Legacy Only=kept value", notes)

    def test_empty_old_only_columns_are_not_written_into_notes(self):
        old_header = LEGACY_HEADER + ["Legacy Only"]
        row = _legacy_row("MRS_T1") + [""]
        rows = self.c.migrate_rows(old_header, [row], self.c.headers)
        self.assertEqual(dict(zip(self.c.headers, rows[0]))["Notes"], "")

    def test_short_rows_do_not_raise(self):
        rows = self.c.migrate_rows(LEGACY_HEADER, [["MRS_T1", "Complete"]], self.c.headers)
        self.assertEqual(dict(zip(self.c.headers, rows[0]))["Model ID"], "MRS_T1")


class InitializeTrackingMigrationTests(unittest.TestCase):
    """The on-disk behaviour: backup taken, rows kept, warning logged."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        sys.argv = ["x", self.tmp]
        open(os.path.join(self.tmp, "analysis_params.yaml"), "w").write(
            open(os.path.join(REPO, "analysis_params.yaml")).read())
        import config
        self.c = importlib.reload(config)
        self.tracking = os.path.join(self.tmp, "status.csv")
        with open(self.tracking, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(LEGACY_HEADER)
            writer.writerow(_legacy_row("MRS_T1", Status="Complete",
                                        **{"Step 1 complete": "True"}))
            writer.writerow(_legacy_row("MRS_T2", Status="Error in frame extraction"))

    def _backups(self):
        return [n for n in os.listdir(self.tmp)
                if n.startswith("status.csv.pre_") and n.endswith(".bak")]

    def test_existing_rows_survive_a_header_change(self):
        with self.assertLogs(level="WARNING") as captured:
            self.c.initialize_tracking("MRS_T3")
        with open(self.tracking, newline="") as fh:
            rows = list(csv.DictReader(fh))
        by_id = {r["Model ID"]: r for r in rows}
        self.assertIn("MRS_T1", by_id)
        self.assertIn("MRS_T2", by_id)
        self.assertEqual(by_id["MRS_T1"]["Status"], "Complete")
        self.assertEqual(by_id["MRS_T1"]["Step 1 complete"], "True")
        self.assertEqual(by_id["MRS_T2"]["Status"], "Error in frame extraction")
        # New columns exist and are blank on the migrated rows.
        self.assertEqual(by_id["MRS_T1"]["readable_id"], "")
        self.assertEqual(by_id["MRS_T1"]["Scale Bars"], "")
        # The model this call was made for was still appended.
        self.assertIn("MRS_T3", by_id)
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertIn(backups[0], "\n".join(captured.output))

    def test_backup_is_a_byte_for_byte_copy_of_the_original(self):
        original = open(self.tracking, newline="").read()
        self.c.initialize_tracking("MRS_T3")
        backup = os.path.join(self.tmp, self._backups()[0])
        self.assertEqual(open(backup, newline="").read(), original)

    def test_header_is_rewritten_to_the_current_schema(self):
        self.c.initialize_tracking("MRS_T3")
        with open(self.tracking, newline="") as fh:
            self.assertEqual(next(csv.reader(fh)), self.c.headers)

    def test_matching_header_is_left_alone_and_no_backup_is_taken(self):
        os.remove(self.tracking)
        self.c.initialize_tracking("MRS_T1")
        self.c.update_tracking("MRS_T1", {"Status": "Complete"})
        self.c.initialize_tracking("MRS_T1")
        with open(self.tracking, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["Model ID"] for r in rows], ["MRS_T1"])
        self.assertEqual(rows[0]["Status"], "Complete")
        self.assertEqual(self._backups(), [])


if __name__ == "__main__":
    unittest.main()
