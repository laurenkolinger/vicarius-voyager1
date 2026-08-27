"""Tests for src/registry_client.py (Task 8): no-op when non-TCRMP, thin
wrapper over the shared vicarius/_METADATA/3d registry when TCRMP is on.
"""
import os, shutil, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

LIB_DIR = os.environ.get("VICARIUS_ROOT", "/mnt/rip/vicarius_drive/vicarius") + "/_METADATA/3d"
sys.path.insert(0, LIB_DIR)
import registry as _shared_registry  # noqa: E402  (used only to reach EVENTS_CSV etc. for assertions)


class RegistryClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["VICARIUS_3D_REGISTRY_ROOT"] = self.tmp
        import importlib
        import registry_client
        importlib.reload(registry_client)
        self.rc = registry_client

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("VICARIUS_3D_REGISTRY_ROOT", None)

    def test_disabled_until_configured(self):
        self.assertFalse(self.rc.enabled())
        self.assertIsNone(self.rc.stage("MRS_T1_2023ann", 1, "aligning"))
        self.assertIsNone(self.rc.update("MRS_T1_2023ann", notes="x"))
        self.assertIsNone(self.rc.row("MRS_T1_2023ann"))
        self.assertIsNone(self.rc.rows_for())

    def test_disabled_when_tcrmp_false(self):
        self.rc.configure({"processing": {"tcrmp": False}})
        self.assertFalse(self.rc.enabled())
        self.assertIsNone(self.rc.update("MRS_T1_2023ann", notes="x"))

    def test_enabled_when_tcrmp_true(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        self.assertTrue(self.rc.enabled())

    def test_actor_constant(self):
        self.assertEqual(self.rc.ACTOR, "3D_phase_1")

    def test_update_and_row_roundtrip(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        self.rc.update("MRS_T1_2023ann", site="MRS", transect="T1", year="2023",
                        season_token="ann", process="true", original_videos="TCRMP20231015_3D_MRS_T1.MOV")
        row = self.rc.row("MRS_T1_2023ann")
        self.assertEqual(row["site"], "MRS")
        self.assertEqual(row["last_edited_by"], self.rc.ACTOR)

    def test_update_writes_event_through_actor(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        self.rc.update("MRS_T1_2023ann", site="MRS", transect="T1", year="2023", season_token="ann")
        import importlib
        importlib.reload(_shared_registry)
        import csv
        with open(_shared_registry.EVENTS_CSV) as fh:
            events = list(csv.DictReader(fh))
        self.assertTrue(any(e["actor"] == "3D_phase_1" for e in events))

    def test_stage_sets_step_and_stage(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        self.rc.update("MRS_T1_2023ann", site="MRS", transect="T1", year="2023", season_token="ann")
        self.rc.stage("MRS_T1_2023ann", 1, "aligning")
        row = self.rc.row("MRS_T1_2023ann")
        self.assertEqual(row["step"], "1")
        self.assertEqual(row["stage"], "aligning")

    def test_snapshot_delegates_to_registry(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        self.rc.update("MRS_T1_2023ann", site="MRS", transect="T1", year="2023", season_token="ann")
        folder = os.path.join(self.tmp, "MRS_T1_2023ann_3dprocessing")
        os.makedirs(folder)
        d = self.rc.snapshot("MRS_T1_2023ann", folder, {"scale_error_mm": 1.4}, report_pdf=None, params_yaml=None)
        self.assertTrue(os.path.isdir(d))
        self.assertTrue(os.path.isfile(os.path.join(d, "snapshot.yaml")))

    def test_rows_for_filters_process_true_and_sorts(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        for rid, site, t, y, tok, proc in [
            ("MRS_T1_2024ann", "MRS", "T1", "2024", "ann", "true"),
            ("BID_T1_2023ann", "BID", "T1", "2023", "ann", "true"),
            ("MRS_T1_2024_pbl", "MRS", "T1", "2024", "_pbl", "true"),
            ("MRS_T1_2022ann", "MRS", "T1", "2022", "ann", "false"),
        ]:
            self.rc.update(rid, site=site, transect=t, year=y, season_token=tok, process=proc)
        ids = [r["readable_id"] for r in self.rc.rows_for()]
        self.assertEqual(ids, ["BID_T1_2023ann", "MRS_T1_2024_pbl", "MRS_T1_2024ann"])

    def test_rows_for_filters_by_site_and_transect(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        for rid, site, t in [("MRS_T1_2024ann", "MRS", "T1"), ("MRS_T2_2024ann", "MRS", "T2"),
                              ("BID_T1_2024ann", "BID", "T1")]:
            self.rc.update(rid, site=site, transect=t, year="2024", season_token="ann", process="true")
        ids = [r["readable_id"] for r in self.rc.rows_for(site="MRS")]
        self.assertEqual(ids, ["MRS_T1_2024ann", "MRS_T2_2024ann"])
        ids = [r["readable_id"] for r in self.rc.rows_for(site="MRS", transect="T1")]
        self.assertEqual(ids, ["MRS_T1_2024ann"])

    def test_rows_for_ids_preserves_requested_order(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        for rid, site in [("MRS_T1_2024ann", "MRS"), ("BID_T1_2023ann", "BID")]:
            self.rc.update(rid, site=site, transect="T1", year="2024", season_token="ann", process="true")
        rows = self.rc.rows_for(ids=["MRS_T1_2024ann", "BID_T1_2023ann"])
        self.assertEqual([r["readable_id"] for r in rows], ["MRS_T1_2024ann", "BID_T1_2023ann"])

    def test_rows_for_ids_raises_keyerror_listing_missing(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        self.rc.update("MRS_T1_2024ann", site="MRS", transect="T1", year="2024", season_token="ann", process="true")
        with self.assertRaises(KeyError) as ctx:
            self.rc.rows_for(ids=["MRS_T1_2024ann", "NOPE_T9_1999ann"])
        self.assertIn("NOPE_T9_1999ann", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
