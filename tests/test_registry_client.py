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


class RegistryClientFactsTests(unittest.TestCase):
    """registry_client.facts: the row_facts.csv sidecar through the shared
    set_facts, signed with the module actor, a no-op outside TCRMP mode."""

    READABLE_ID = "MRS_T1_2023ann"

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

    def _enable_with_row(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        self.rc.update(self.READABLE_ID, site="MRS", transect="T1", year="2023",
                       season_token="ann", process="true")

    def _shared(self):
        import importlib
        return importlib.reload(_shared_registry)

    def _lines(self, section="voyager1"):
        return {line["key"]: line for line in self._shared().facts(self.READABLE_ID, section)}

    def test_section_constant_is_a_known_registry_section(self):
        self.assertEqual(self.rc.VOYAGER1_SECTION, "voyager1")
        self.assertIn(self.rc.VOYAGER1_SECTION, self._shared().FACT_SECTIONS)

    def test_disabled_until_configured_writes_nothing(self):
        self.assertIsNone(self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "x"}))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "row_facts.csv")))

    def test_disabled_when_tcrmp_false_writes_nothing(self):
        self.rc.configure({"processing": {"tcrmp": False}})
        self.assertIsNone(self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "x"}))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "row_facts.csv")))

    def test_writes_the_sidecar_signed_by_the_module_actor(self):
        self._enable_with_row()
        changed = self.rc.facts(
            self.READABLE_ID, "voyager1",
            {"run_id": "TCRMP_3sep26_LO_MRS3_23ann-25pbl", "step0_seconds": 12.5,
             "frames_extracted": 1000, "console_log_step0": "/x/console/step0_1.log"},
            links={"console_log_step0": "/x/console/step0_1.log"},
            units={"step0_seconds": "s", "frames_extracted": "count"},
        )
        self.assertIs(changed, True)
        lines = self._lines()
        self.assertEqual(lines["run_id"]["value"], "TCRMP_3sep26_LO_MRS3_23ann-25pbl")
        self.assertEqual(lines["step0_seconds"]["value"], "12.5")
        self.assertEqual(lines["step0_seconds"]["unit"], "s")
        self.assertEqual(lines["frames_extracted"]["value"], "1000")
        self.assertEqual(lines["frames_extracted"]["unit"], "count")
        self.assertEqual(lines["console_log_step0"]["link"], "/x/console/step0_1.log")
        for line in lines.values():
            self.assertEqual(line["recorded_by"], self.rc.ACTOR)
            self.assertTrue(line["recorded_at"].endswith("-04:00"), line["recorded_at"])

    def test_rerun_with_the_same_facts_returns_false_and_keeps_one_line_per_key(self):
        self._enable_with_row()
        self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "R1"})
        self.assertIs(self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "R1"}), False)
        all_lines = self._shared().facts(self.READABLE_ID, "voyager1")
        self.assertEqual([line["key"] for line in all_lines], ["run_id"])

    def test_replaces_only_the_keys_given(self):
        self._enable_with_row()
        self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "R1", "params_version": "v1.0.0"})
        self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "R2"})
        lines = self._lines()
        self.assertEqual(lines["run_id"]["value"], "R2")
        self.assertEqual(lines["params_version"]["value"], "v1.0.0")

    def test_event_carries_the_actor(self):
        self._enable_with_row()
        self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "R1"})
        import csv
        with open(self._shared().EVENTS_CSV) as fh:
            events = [e for e in csv.DictReader(fh) if e["field"] == "row_facts"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], self.rc.ACTOR)
        self.assertIn("run_id=R1", events[0]["new"])

    def test_unknown_section_is_refused_before_any_write(self):
        self._enable_with_row()
        with self.assertRaises(ValueError) as ctx:
            self.rc.facts(self.READABLE_ID, "phase9", {"run_id": "x"})
        self.assertIn("phase9", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "row_facts.csv")))

    def test_missing_row_is_refused(self):
        self.rc.configure({"processing": {"tcrmp": True}})
        with self.assertRaises(KeyError):
            self.rc.facts("NOPE_T9_1999ann", "voyager1", {"run_id": "x"})

    def test_hostile_values_are_refused_or_round_trip(self):
        self._enable_with_row()
        with self.assertRaises(ValueError):
            self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "a\x00b"})
        with self.assertRaises(ValueError):
            self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": True})
        with self.assertRaises(ValueError):
            self.rc.facts(self.READABLE_ID, "voyager1", {"bad key": "x"})
        with self.assertRaises(ValueError):
            self.rc.facts(self.READABLE_ID, "voyager1", {})
        nasty = 'line one\nline "two", with, commas; and </script>'
        self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": nasty})
        self.assertEqual(self._lines()["run_id"]["value"], nasty)

    def test_wrong_types_are_refused(self):
        self._enable_with_row()
        with self.assertRaises(TypeError):
            self.rc.facts(self.READABLE_ID, "voyager1", ["run_id", "x"])
        with self.assertRaises(TypeError):
            self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "x"}, links=["x"])
        with self.assertRaises(ValueError):
            self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "x"}, units={"run_id": "hours"})
        with self.assertRaises(KeyError):
            self.rc.facts(self.READABLE_ID, "voyager1", {"run_id": "x"}, links={"other": "/p"})

    def test_many_facts_in_one_call(self):
        self._enable_with_row()
        many = {f"key_{i}": i for i in range(500)}
        self.assertIs(self.rc.facts(self.READABLE_ID, "voyager1", many), True)
        self.assertEqual(len(self._lines()), 500)

    def test_forwards_actor_links_and_units_to_set_facts(self):
        self._enable_with_row()
        from unittest import mock
        with mock.patch.object(self.rc, "_registry") as fake:
            self.rc.facts("X_T1_2020ann", "voyager1", {"run_id": "R"},
                          links={"run_id": "/p"}, units={"run_id": ""})
            fake.set_facts.assert_called_once_with(
                "X_T1_2020ann", "voyager1", {"run_id": "R"}, actor=self.rc.ACTOR,
                links={"run_id": "/p"}, units={"run_id": ""})
            fake.reset_mock()
            self.rc.facts("X_T1_2020ann", "voyager1", {"run_id": "R"})
            fake.set_facts.assert_called_once_with(
                "X_T1_2020ann", "voyager1", {"run_id": "R"}, actor=self.rc.ACTOR,
                links=None, units=None)


if __name__ == "__main__":
    unittest.main()
