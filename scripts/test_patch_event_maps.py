#!/usr/bin/env python3
"""Unit tests for patch_event_maps.py: registering event maps that the game
ships but Funcom's world-template.yaml leaves out.

Pure text in, text out. PyYAML is only used to check the result still parses
(it is not needed at runtime).

Run: python3 scripts/test_patch_event_maps.py
Optional: DUNE_WORLD_TEMPLATE=/path/to/world-template.yaml also patches a real
depot template and checks it.
"""
import importlib.util
import os
import pathlib
import sys
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - CI images ship PyYAML
    yaml = None

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("patch_event_maps", _HERE / "patch_event_maps.py")
assert _spec is not None and _spec.loader is not None
pem = importlib.util.module_from_spec(_spec)
sys.modules["patch_event_maps"] = pem  # dataclasses resolve their module
_spec.loader.exec_module(pem)

HALLOWEEN = "CB_Halloween_Event_Shipwreck_01"

# A cut-down world-template.yaml with Funcom's real indentation: the
# worldPartitions list and the serverGroup sets are both at depth 12/10.
FIXTURE = """\
apiVersion: igw.funcom.com/v1
kind: BattleGroup
spec:
  database:
    template:
      spec:
        sets:
          - spec:
            version: 17.4-alpine
            worldPartitions:
            - map: Survival_1
              partitions:
              - dimension: 0
                disable: false
                id: 1
                maxX: 1
                maxY: 1
                minX: 0
                minY: 0
            - map: CB_Story_Hephaestus
              partitions:
              - dimension: 0
                disable: false
                id: 5
                maxX: 1
                maxY: 1
                minX: 0
                minY: 0
  restartScalers:
  - map: ^(.*_)Story_.*
    size: 1
  serverGroup:
    template:
      spec:
        sets:
        - arguments:
          - -FarmRegion={WORLD_REGION}
          connectDirector: false
          image: registry.funcom.com/funcom/self-hosting/seabass-server:{WORLD_IMAGE_TAG}
          map: Survival_1
          replicas: 1
          restartMode: Individual
        - arguments:
          - -FarmRegion={WORLD_REGION}
          connectDirector: false
          dedicatedScaling: true
          image: registry.funcom.com/funcom/self-hosting/seabass-server:{WORLD_IMAGE_TAG}
          map: CB_Story_Hephaestus
          messageQueues:
          - argumentNames:
              hostname: --RMQGameHostname
            mode: Internal
          replicas: 0
          restartMode: Individual
          terminationGracePeriodSeconds: 120
        - arguments:
          - -FarmRegion={WORLD_REGION}
          map: Overmap
          replicas: 1
"""


def world_partitions(doc):
    return doc["spec"]["database"]["template"]["spec"]["sets"][0]["worldPartitions"]


def server_sets(doc):
    return doc["spec"]["serverGroup"]["template"]["spec"]["sets"]


@unittest.skipIf(yaml is None, "PyYAML not installed")
class PatchFixture(unittest.TestCase):
    def setUp(self):
        assert yaml is not None
        self.out, self.added = pem.patch_template(FIXTURE)
        self.doc = yaml.safe_load(self.out)

    def test_reports_added_map(self):
        self.assertEqual(self.added, [HALLOWEEN])

    def test_adds_world_partition_with_reserved_id(self):
        entry = [e for e in world_partitions(self.doc) if e["map"] == HALLOWEEN]
        self.assertEqual(len(entry), 1)
        self.assertEqual(
            entry[0]["partitions"],
            [{"dimension": 0, "disable": False, "id": pem.EVENT_MAPS[0].partition_id,
              "maxX": 1, "maxY": 1, "minX": 0, "minY": 0}],
        )

    def test_keeps_existing_world_partitions(self):
        names = [e["map"] for e in world_partitions(self.doc)]
        self.assertEqual(names, ["Survival_1", "CB_Story_Hephaestus", HALLOWEEN])

    def test_clones_server_set_from_template_map(self):
        sets = server_sets(self.doc)
        names = [s["map"] for s in sets]
        self.assertEqual(names, ["Survival_1", "CB_Story_Hephaestus", HALLOWEEN, "Overmap"])
        heph = sets[1]
        clone = sets[2]
        self.assertEqual({**heph, "map": HALLOWEEN}, clone)

    def test_leaves_restart_scaler_patterns_alone(self):
        self.assertEqual(self.doc["spec"]["restartScalers"], [{"map": "^(.*_)Story_.*", "size": 1}])

    def test_idempotent(self):
        again, added = pem.patch_template(self.out)
        self.assertEqual(again, self.out)
        self.assertEqual(added, [])


class Refusals(unittest.TestCase):
    def test_skips_map_funcom_already_lists(self):
        listed = FIXTURE.replace("- map: CB_Story_Hephaestus", f"- map: {HALLOWEEN}", 1)
        out, added = pem.patch_template(listed)
        self.assertEqual(added, [])
        self.assertEqual(out, listed)

    def test_untouched_without_world_partitions(self):
        text = "kind: BattleGroup\nspec: {}\n"
        with self.assertRaises(pem.TemplateShapeError):
            pem.patch_template(text)

    def test_untouched_without_source_server_set(self):
        text = FIXTURE.replace("map: CB_Story_Hephaestus\n          messageQueues", "map: Other\n          messageQueues")
        with self.assertRaises(pem.TemplateShapeError):
            pem.patch_template(text)

    def test_reserved_ids_stay_clear_of_funcom_and_dimension_ranges(self):
        for m in pem.EVENT_MAPS:
            # Funcom's own ids are 1..35 today and grow by one per update;
            # 101+ belongs to multi-sietch-config.sh's dimension ranges.
            self.assertTrue(36 < m.partition_id < 101, m)

    def test_prestart_seeds_the_same_ids(self):
        # The template entry and the dune.world_partition row must agree, or
        # the instance claims a partition the Director never routes to.
        seed = (_HERE / "prestart.sh").read_text()
        for m in pem.EVENT_MAPS:
            self.assertIn(f"  ({m.partition_id}, '{m.name}', ", seed, m)


@unittest.skipUnless(yaml is not None and os.environ.get("DUNE_WORLD_TEMPLATE"), "no real template given")
class RealTemplate(unittest.TestCase):
    def test_patches_real_depot_template(self):
        text = pathlib.Path(os.environ["DUNE_WORLD_TEMPLATE"]).read_text()
        out, added = pem.patch_template(text)
        self.assertEqual(added, [HALLOWEEN])
        assert yaml is not None
        doc = yaml.safe_load(out)
        maps = []

        def walk(v):
            if isinstance(v, dict):
                if v.get("map") == HALLOWEEN:
                    maps.append(v)
                for c in v.values():
                    walk(c)
            elif isinstance(v, list):
                for c in v:
                    walk(c)

        walk(doc)
        self.assertEqual(len(maps), 2)  # one worldPartitions entry + one server set
        self.assertEqual(pem.patch_template(out), (out, []))


if __name__ == "__main__":
    unittest.main()
