"""Offline checks for what 1.0.0 added: @tool decided the way Godot decides it (BOM, annotations on one line, the
extends chain for hooks, an inherited scene's root), the side effects extracted from the upstream source in the plan
and its fingerprint, overwrite naming every dropped value before approval, the second approval (confirm_risky_plan),
read-back after each step, GameForge pixel art, dual-grid maps, relinked .tres files, the playtest report, pending
buses and score watching in a new scene."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bridge as T  # noqa: E402
import test_v09 as V  # noqa: E402

B = T.B


def gameforge_dual_grid(rows: list[str], variants: bool) -> list[tuple[int, int, int, int]]:
    """GameForge's _build_ground (engines/templates/godot_topdown/main.gd), written again from its GDScript."""
    mh, mw = len(rows), len(rows[0])

    def cell(x, y):
        if x < 0 or y < 0 or x >= mw or y >= mh:
            return "~"
        return rows[y][x]

    def land(x, y):
        return 0 if cell(x, y) == "~" else 1

    out = []
    for vy in range(mh + 1):
        for vx in range(mw + 1):
            idx = land(vx - 1, vy - 1) * 8 + land(vx, vy - 1) * 4 + land(vx - 1, vy) * 2 + land(vx, vy)
            atlas = (idx % 4, idx // 4)
            h = (vx * 73856093) ^ (vy * 19349663)
            if variants and (idx == 15 or idx == 0) and h % 3 == 0:
                atlas = (abs(h >> 4) % 4, 4 if idx == 15 else 5)
            out.append((vx, vy, atlas[0], atlas[1]))
    return out


class HeaderTest(unittest.TestCase):
    def test_tool_is_read_the_way_godot_reads_it(self):
        cases = {
            "@tool\nextends Node2D\n": True,
            B.BOM + "@tool\nextends Node2D\n": True,
            '@icon("res://i.png") @tool\nextends Node2D\n': True,
            "@static_unload @tool\nextends Node2D\n": True,
            "@tool # helper\nextends Node2D\n": True,
            '"""doc"""\n@tool\nextends Node2D\n': True,
            "@tool\r\nextends Node2D\r\n": True,
            "## doc comment\n\n@tool\nextends Node2D\n": True,
            "extends Node2D\n@tool\n": True,  # a parse error in Godot; still counted (more careful)
            "extends Node2D\nvar s = \"@tool\"\n# @tool\n": False,
            'extends Node2D\nvar s = """\n@tool\n"""\n': False,
            "extends Node2D\n": False,
        }
        for text, tool in cases.items():
            self.assertEqual(B.gdscript_header(text)["tool"], tool, repr(text))
        self.assertIsNone(B.gdscript_header('extends Node\nvar s = "open\n'))
        self.assertEqual(B.gdscript_header('extends "res://a.gd"\n')["extends"], "res://a.gd")
        self.assertEqual(B.gdscript_header("class_name Foo extends Bar.Baz\n")["extends"], "Bar.Baz")
        self.assertIn("@getter", B.gdscript_header("@tool\nextends Node\nvar hp := 1:\n\tget:\n\t\treturn 2\n")["funcs"])

    def test_csharp_tool_attribute(self):
        self.assertTrue(B.csharp_is_tool("[GlobalClass, Tool]\npublic partial class A : Node {}"))
        self.assertTrue(B.csharp_is_tool("[Tool]\npublic partial class A : Node {}"))
        self.assertFalse(B.csharp_is_tool("// [Tool]\npublic partial class A : Node {}"))
        self.assertFalse(B.csharp_is_tool('var s = "[Tool]";'))


class V10Test(unittest.TestCase):
    """The 0.9 fixtures (level.tscn, the fake upstream that remembers writes)."""

    def setUp(self):
        self.p = T.Project()

    def tearDown(self):
        self.p.close()

    def fake(self, **kw):
        kw.setdefault("project", self.p.root)
        kw.setdefault("scene", "res://scenes/level.tscn")
        kw.setdefault("nodes", V.LEVEL_NODES)
        return T.FakeChild(**kw)

    def plan(self, fake, steps):
        prepared = T.prepare_save(fake.bridge, steps)
        if prepared is not None:
            self.fail(prepared["content"][0]["text"])
        res = B.Bridge.call(fake.bridge, "plan_change", {"steps": steps})
        self.assertFalse(res["isError"], res["content"][0]["text"])
        return res["content"][0]["text"], T._plan_id_of(res)

    def apply(self, fake, plan_id):
        return B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": plan_id})

    def _write(self, rel: str, text: str) -> None:
        path = self.p.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _variants_scene(self) -> None:
        self._write("t/base.gd", "@tool\nextends Node2D\nclass_name GetBase\n\nfunc _get(p):\n\treturn null\n")
        self._write("t/bom.gd", B.BOM + "@tool\nextends Node2D\n\nfunc _get(p):\n\treturn null\n")
        self._write("t/icon.gd", '@icon("res://x.png") @tool\nextends Node2D\n\nfunc _get(p):\n\treturn null\n')
        self._write("t/child.gd", '@tool\nextends "res://t/base.gd"\n')
        self._write("t/by_class.gd", "@tool\nextends GetBase\n")
        self._write("t/plain_child.gd", 'extends "res://t/base.gd"\n')
        self._write("t/lost.gd", "@tool\nextends NoSuchClassAnywhere\n")
        nodes = ["Bom", "Icon", "Child", "ByClass", "PlainChild", "Lost"]
        scripts = ["bom", "icon", "child", "by_class", "plain_child", "lost"]
        head = "[gd_scene format=3]\n\n" + "".join(
            f'[ext_resource type="Script" path="res://t/{s}.gd" id="{i}"]\n' for i, s in enumerate(scripts, 1))
        body = '\n[node name="V" type="Node2D"]\n' + "".join(
            f'\n[node name="{n}" type="Node2D" parent="."]\nscript = ExtResource("{i}")\n' for i, n in enumerate(nodes, 1))
        self._write("scenes/v.tscn", head + body)

    def test_variants_the_auditors_found_are_refused(self):
        self._variants_scene()
        fake = self.fake(scene="res://scenes/v.tscn", nodes=["/V"] + [f"/V/{n}" for n in
                                                                      ("Bom", "Icon", "Child", "ByClass", "PlainChild", "Lost")])
        try:
            for name in ("Bom", "Icon", "Child", "ByClass", "Lost", "PlainChild"):
                res = fake.bridge.call("scene_look", {"op": "properties", "path": f"/V/{name}", "fields": ["position"]})
                self.assertFalse(res["isError"], (name, res["content"][0]["text"]))
                self.assertIn("editor was not asked", res["content"][0]["text"])
            tree = fake.bridge.call("scene_look", {"op": "scene_tree"})
            self.assertFalse(tree["isError"], tree["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_get_hierarchy" for row in fake.sent()))
            self._write("scenes/bom.tscn", B.BOM + '[gd_scene format=3]\n\n[node name="Bom" type="Node2D"]\n')
            bom = self.fake(scene="res://scenes/bom.tscn", nodes=["/Bom"])
            try:
                refused = bom.bridge.call("scene_look", {"op": "scene_tree"})
                self.assertTrue(refused["isError"])
                self.assertIn("byte-order mark", refused["content"][0]["text"])
                self.assertIn("Nothing was read", refused["content"][0]["text"])
            finally:
                bom.close()
        finally:
            fake.close()

    def test_an_inherited_scene_root_and_an_instance_root(self):
        self._write("t/base_root.gd", "@tool\nextends Node2D\n\nfunc _get(p):\n\treturn null\n")
        self._write("scenes/base.tscn", '[gd_scene format=3]\n\n[ext_resource type="Script" path="res://t/base_root.gd" '
                                        'id="1"]\n\n[node name="EnemyBase" type="Node2D"]\nscript = ExtResource("1")\n\n'
                                        '[node name="Body" type="Node2D" parent="."]\n')
        self._write("scenes/inh.tscn", '[gd_scene format=3]\n\n[ext_resource type="PackedScene" path="res://scenes/base.tscn" '
                                       'id="1"]\n\n[node name="Slime" instance=ExtResource("1")]\n\n'
                                       '[node name="Eye" type="Node2D" parent="."]\n')
        self._write("scenes/holder.tscn", '[gd_scene format=3]\n\n[ext_resource type="PackedScene" '
                                          'path="res://scenes/base.tscn" id="1"]\n\n[node name="Holder" type="Node2D"]\n\n'
                                          '[node name="Foe" parent="." instance=ExtResource("1")]\n')
        fake = self.fake(scene="res://scenes/inh.tscn", nodes=["/Slime", "/Slime/Body", "/Slime/Eye"])
        try:
            for op, args in (("scene_tree", {}), ("find_nodes", {"name": "Eye"}),
                             ("properties", {"path": "/Slime", "fields": ["position"]})):
                res = fake.bridge.call("scene_look", {"op": op, **args})
                self.assertTrue(res["isError"], op)
                self.assertIn("res://t/base_root.gd", res["content"][0]["text"])
        finally:
            fake.close()
        holder = self.fake(scene="res://scenes/holder.tscn", nodes=["/Holder", "/Holder/Foe"])
        try:
            res = holder.bridge.call("scene_look", {"op": "properties", "path": "/Holder/Foe", "fields": ["position"]})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("editor was not asked", res["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_get_hierarchy" for row in holder.sent()))
        finally:
            holder.close()

    def test_a_write_in_a_scene_with_a_get_hook_says_the_script_runs(self):
        self._variants_scene()
        fake = self.fake(scene="res://scenes/v.tscn", nodes=["/V"])
        try:
            text, _ = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "New",
                                        "parent_path": "/V"}])
            self.assertIn("when a step changes the open scene, the editor and Godot AI read its nodes again", text)
            self.assertNotIn("Two approvals", text)  # project code, like playing: one approval
        finally:
            fake.close()

    def test_autoload_of_a_tool_script_and_a_write_outside_the_project(self):
        self._write("scripts/auto.gd", "@tool\nextends Node\n")
        fake = self.fake()
        try:
            text, _ = self.plan(fake, [{"tool": "project_do", "op": "autoload_add", "name": "Auto",
                                        "path": "res://scripts/auto.gd"}])
            self.assertIn("as an autoload Godot runs it in the editor from the next time the project opens", text)
            out = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "file_do", "op": "write_file", "path": "/tmp/outside.txt", "content": "x"}]})
            self.assertTrue(out["isError"])
            self.assertIn("outside the project", out["content"][0]["text"])
        finally:
            fake.close()

    def test_overwrite_names_every_value_before_any_approval_and_in_the_reply(self):
        fake = self.fake(nodes=V.LEVEL_NODES, props={"/Level/Enemies/Enemy3": {"position": {"x": 9, "y": 9}}})
        try:
            plain = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "scene_do", "op": "save"}]})
            self.assertTrue(plain["isError"])
            self.assertIn("nobody was asked", plain["content"][0]["text"])
            text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "save", "overwrite": True}])
            self.assertIn("drops the disk side of res://scenes/level.tscn: /Level/Enemies/Enemy3 position: disk "
                          "Vector2(1, 2)", text)
            self.assertIn("Two approvals", text)
            first = self.apply(fake, plan_id)
            self.assertIn("needs a second approval", first["content"][0]["text"])
            self.assertIn("/Level/Enemies/Enemy3 position: disk Vector2(1, 2)", first["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_save" for row in fake.sent()))
            done = B.Bridge.call(fake.bridge, "confirm_risky_plan", {"plan_id": plan_id})
            self.assertFalse(done["isError"], done["content"][0]["text"])
            said = "\n".join(item.get("text", "") for item in done["content"])
            self.assertIn("drops the disk side of values", said)
            self.assertIn("/Level/Enemies/Enemy3 position: disk Vector2(1, 2)", said)
        finally:
            fake.close()

    def test_overwrite_refuses_when_the_disk_changed_after_the_plan(self):
        fake = self.fake(nodes=V.LEVEL_NODES, props={"/Level/Enemies/Enemy3": {"position": {"x": 9, "y": 9}}})
        try:
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "save", "overwrite": True}])
            level = self.p.root / "scenes" / "level.tscn"
            level.write_text(level.read_text().replace("Vector2(1, 2)", "Vector2(3, 4)"))
            res = self.apply(fake, plan_id)
            self.assertTrue(res["isError"])
            self.assertIn("changed after this plan", res["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_save" for row in fake.sent()))
        finally:
            fake.close()

    def test_an_overwrite_with_nothing_to_drop_asks_once(self):
        fake = self.fake(nodes=V.LEVEL_NODES)
        try:
            text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "save", "overwrite": True}])
            self.assertIn("the editor and the file agree now", text)
            self.assertNotIn("Two approvals", text)
            self.assertFalse(self.apply(fake, plan_id)["isError"])
        finally:
            fake.close()

    def test_the_second_approval_text_is_short_and_escaped(self):
        fake = self.fake(nodes=V.LEVEL_NODES + ["/Level/Odd Name"])
        try:
            _text, plan_id = self.plan(fake, [{"tool": "game_do", "op": "run_game_code",
                                               "code": "print('a" + chr(0x202E) + "b')"}])
            first = self.apply(fake, plan_id)
            summary = first["content"][0]["text"]
            self.assertLessEqual(len(summary), B.CONFIRM_LIMIT)
            self.assertNotIn(chr(0x202E), summary)
            self.assertTrue(summary.splitlines()[1].startswith("- step 1 runs code written in this plan"))
        finally:
            fake.close()

    def test_control_offsets_are_read_at_apply_and_named_only_when_they_differ(self):
        project = T.Project()
        (project.root / "scenes" / "premise.tscn").write_text(T.PREMISE)
        layout = {"/Main/Ground": {"offset_left": -1000.0, "offset_top": -20.0, "offset_right": 1000.0,
                                   "offset_bottom": 20.0}}
        fake = T.FakeChild(project=project.root, scene="res://scenes/premise.tscn", nodes=T.PREMISE_NODES,
                           layout=layout)
        try:
            text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "save", "overwrite": True}])
            self.assertIn("agree now", text)
            self.assertNotIn("Two approvals", text)
            res = self.apply(fake, plan_id)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertNotIn("needs a second approval", res["content"][0]["text"])  # they agree: one approval
        finally:
            fake.close()
        moved = {"/Main/Ground": dict(layout["/Main/Ground"], offset_right=900.0)}
        fake = T.FakeChild(project=project.root, scene="res://scenes/premise.tscn", nodes=T.PREMISE_NODES,
                           layout=moved)
        try:
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "save", "overwrite": True}])
            res = self.apply(fake, plan_id)
            self.assertIn("needs a second approval", res["content"][0]["text"])
            self.assertIn("/Main/Ground offset_right: disk 1000.0, editor 900.0", res["content"][0]["text"])
        finally:
            fake.close()
            project.close()

    def test_a_list_too_long_for_the_second_approval_makes_no_plan(self):
        names = [f"/Level/Enemies/Enemy{i}" for i in range(3, 7)]
        fake = self.fake(nodes=V.LEVEL_NODES)
        try:
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "game_do", "op": "run_game_code", "code": "print('" + "x" * 1800 + "')"}]})
            self.assertTrue(res["isError"])
            self.assertIn("more than the 1700", res["content"][0]["text"])
            self.assertEqual(len(names), 4)
        finally:
            fake.close()

    def test_review_holes_are_closed(self):
        # A property getter is a hook (it runs on a read), also through a base script.
        self._write("t/getter.gd", "@tool\nextends Node2D\nvar hp := 1:\n\tget:\n\t\treturn 2\n")
        self._write("t/getter_child.gd", '@tool\nextends "getter.gd"\n')
        root = str(self.p.root)
        fake = self.fake()
        try:
            self.assertIn("@getter", fake.bridge._script_tool_hooks("res://t/getter.gd", root))
            self.assertIn("@getter", fake.bridge._script_tool_hooks("res://t/getter_child.gd", root))
            # A relative extends at the project root resolves to res://, not res:/.
            self._write("base_at_root.gd", "extends Node\n")
            self._write("child_at_root.gd", '@tool\nextends "base_at_root.gd"\n')
            self.assertEqual(fake.bridge._script_tool_hooks("res://child_at_root.gd", root), set())
            # write_file of a @tool script is code written in the plan: two approvals.
            text, _ = self.plan(fake, [{"tool": "file_do", "op": "write_file", "path": "res://t/new_tool.gd",
                                        "content": "@tool\nextends Node\nfunc _ready():\n\tpass\n"}])
            self.assertIn("runs code written in this plan: res://t/new_tool.gd is a @tool script", text)
            self.assertIn("Two approvals", text)
            # Rewriting an [ext_resource] line that points at a real file is a replacement (two approvals).
            self._write("t/scene_a.tscn", '[gd_scene format=3]\n\n[ext_resource type="Script" path="res://t/getter.gd" '
                                          'id="1"]\n\n[node name="A" type="Node"]\nscript = ExtResource("1")\n')
            swapped = (self.p.root / "t/scene_a.tscn").read_text().replace("res://t/getter.gd", "res://t/new_tool.gd")
            text, _ = self.plan(fake, [{"tool": "file_do", "op": "write_file", "path": "res://t/scene_a.tscn",
                                        "content": swapped, "overwrite": True}])
            self.assertIn("replaces the existing file res://t/scene_a.tscn", text)
        finally:
            fake.close()

    def test_an_overwrite_of_a_scene_the_plan_opens_keeps_its_second_approval(self):
        fake = self.fake(nodes=V.LEVEL_NODES)
        try:
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "open_scene", "path": "res://actors/room.tscn"},
                {"tool": "scene_do", "op": "save", "overwrite": True}]})
            self.assertTrue(res["isError"], res["content"][0]["text"])
            self.assertIn("scene open now", res["content"][0]["text"])
            self.assertIn("compare", res["content"][0]["text"])
            self.assertNotIn("plan_id:", res["content"][0]["text"])
        finally:
            fake.close()

    def test_an_overwrite_that_could_not_be_compared_asks_twice_and_then_runs(self):
        fake = self.fake(nodes=V.LEVEL_NODES)
        try:
            real = fake.bridge.compare
            fake.bridge.compare = lambda scene, root, focus=None: (False, [], "a value could not be read", ["unparsed x"]) \
                if focus is None else real(scene, root, focus)
            prepared = T.prepare_save(fake.bridge, [{"tool": "scene_do", "op": "save", "overwrite": True}])
            self.assertIsNotNone(prepared)
            self.assertTrue(prepared["isError"])
            self.assertIn("could not", prepared["content"][0]["text"])
            self.assertNotIn("plan_id:", prepared["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_save" for row in fake.sent()))
        finally:
            fake.close()

    def test_an_instance_whose_scene_cannot_be_read_counts_as_tool(self):
        level = self.p.root / "scenes" / "level.tscn"
        level.write_text(level.read_text().replace("res://actors/enemy.tscn", "res://actors/moved_away.tscn"))
        fake = self.fake()
        try:
            res = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/Enemies/Enemy3",
                                                  "fields": ["position"]})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("editor was not asked", res["content"][0]["text"])
            self.assertFalse(any(row["name"] == "node_get_properties" for row in fake.sent()))
        finally:
            fake.close()

    def test_one_plan_runs_once_even_when_two_calls_race(self):
        fake = self.fake()
        try:
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Once",
                                               "parent_path": "/Level"}])
            fake.bridge._plans[plan_id]["busy"] = True  # as if another call were running it
            res = self.apply(fake, plan_id)
            self.assertIn("already working on this plan_id", res["content"][0]["text"])
            self.assertTrue(fake.bridge._plans[plan_id]["busy"])  # the refused call does not release it
            fake.bridge._plans[plan_id]["busy"] = False
            self.assertFalse(self.apply(fake, plan_id)["isError"])
            self.assertEqual(sum(1 for row in fake.sent() if row["name"] == "node_create"), 1)
        finally:
            fake.close()

    def test_side_effects_come_from_the_upstream_source(self):
        row = B._EFFECTS_BY_COMMAND["create_node"]
        self.assertIn("adds_to_tree", row["side_effects"])
        self.assertIn("saves_project_settings", B._EFFECTS_BY_COMMAND["set_main_scene"]["side_effects"])
        self.assertIn("rescans", B._EFFECTS_BY_COMMAND["write_file"]["side_effects"])
        fake = self.fake()
        try:
            text, plan_id = self.plan(fake, [{"tool": "project_do", "op": "set", "key": "application/config/name",
                                              "value": "New"}])
            self.assertIn("Godot AI's handler for this step can also", text)
            self.assertIn("now in project.godot: application/config/name", text)
            (self.p.root / "project.godot").write_text('[application]\nconfig/name="Other"\n')
            res = self.apply(fake, plan_id)
            self.assertTrue(res["isError"])  # project.godot is in the fingerprint as a side-effect file
            self.assertIn("a file a step changes as a side effect", res["content"][0]["text"])
        finally:
            fake.close()

    def test_a_value_godot_did_not_keep_is_an_error(self):
        fake = self.fake()
        try:
            original = fake.bridge.upstream

            def upstream(name, arguments, timeout=None, mid=None):
                if name == "node_get_properties" and arguments.get("fields") == ["visible"]:
                    return {"content": [{"type": "text", "text": json.dumps(
                        {"properties": [{"name": "visible", "value": True}]})}], "isError": False}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "set_node_property", "path": "/Level/Enemies",
                                               "property": "visible", "value": False}])
            res = self.apply(fake, plan_id)
            self.assertTrue(res["isError"])
            self.assertIn("Godot did not keep the value", res["content"][0]["text"])
        finally:
            fake.close()

    def test_music_keeps_the_users_loop_points(self):
        V._gameforge(self.p.root)
        imp = self.p.root / "assets" / "music" / "theme.wav.import"
        imp.write_text(imp.read_text().replace("edit/loop_mode=0\nedit/loop_begin=0\nedit/loop_end=-1",
                                               "edit/loop_mode=0\nedit/loop_begin=11025\nedit/loop_end=33075"))
        fake = self.fake()
        try:
            text, _ = self.plan(fake, [{"tool": "place_assets", "op": "music", "stream": "res://assets/music/theme.wav"}])
            self.assertIn("params/edit/loop_mode 0 -> 2", text)
            self.assertIn("edit/loop_begin=11025", text)
            self.assertIn("edit/loop_end=33075", text)
            imp.write_text(imp.read_text().replace("edit/loop_mode=0", "edit/loop_mode=2"))
            text, _ = self.plan(fake, [{"tool": "place_assets", "op": "music", "stream": "res://assets/music/theme.wav"}])
            self.assertIn("already loops (loop mode 2, loop_begin 11025, loop_end 33075); kept as it is", text)
            self.assertNotIn("path: res://assets/music/theme.wav.import", text)
        finally:
            fake.close()

    def test_dual_grid_is_gameforges_formula(self):
        rows = ["~~~~~~", "~@..c~", "~.TT.~", "~....~", "~~~~~~"]
        self.assertEqual(B.dual_grid_cells(rows, True), gameforge_dual_grid(rows, True))
        self.assertEqual(B.dual_grid_cells(rows, False), gameforge_dual_grid(rows, False))
        big = ["".join("~" if (x * 7 + y * 3) % 5 == 0 else "." for x in range(30)) for y in range(20)]
        self.assertEqual(B.dual_grid_cells(big, True), gameforge_dual_grid(big, True))

    def _gameforge_project(self, nearest: bool = False) -> None:
        V._gameforge(self.p.root)
        atlas = json.loads((self.p.root / "assets/sprites/hero.json").read_text())
        atlas["meta"]["app"] = "hermes-gameforge"
        (self.p.root / "assets/sprites/hero.json").write_text(json.dumps(atlas))
        layout = json.loads((self.p.root / "assets/tiles/water_grass.json").read_text())
        layout.update({"wang_order": [list(c) for c in B.WANG_ORDER], "corner_bits": "tl,tr,bl,br; 1 = upper terrain"})
        (self.p.root / "assets/tiles/water_grass.json").write_text(json.dumps(layout))
        (self.p.root / "assets/tiles/water_grass.tres").write_text(
            B.tileset_from_layout("res://assets/tiles/water_grass.png", layout))
        if nearest:
            (self.p.root / "project.godot").write_text(
                "[rendering]\ntextures/canvas_textures/default_texture_filter=0\n")

    def test_gameforge_pixel_art_gets_nearest_and_a_map_gets_the_dual_grid(self):
        self._gameforge_project()
        fake = self.fake()
        try:
            text, plan_id = self.plan(fake, [
                {"tool": "place_assets", "op": "sprite_sheet", "sheet": "res://assets/sprites/hero.png"},
                {"tool": "place_assets", "op": "tileset", "tileset": "res://assets/tiles/water_grass.tres",
                 "map": ["~~~~", "~..~", "~~~~"]}])
            self.assertIn("texture_filter Nearest on /Level/Hero (GameForge pixel art, 16 px", text)
            self.assertIn("texture_filter Nearest on /Level/Ground", text)
            self.assertIn("GameForge's dual grid over a 4x3 map: 20 cells", text)
            self.assertIn("20 tilemap_set_cell on /Level/Ground", text)
            self.assertIn("property: position", text)
            res = self.apply(fake, plan_id)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            batch = [row for row in fake.sent() if row["name"] == "batch_execute"]
            cells = [(c["params"]["map_x"], c["params"]["map_y"], c["params"]["atlas_col"], c["params"]["atlas_row"])
                     for row in batch for c in row["arguments"]["commands"]]
            self.assertEqual(cells, gameforge_dual_grid(["~~~~", "~..~", "~~~~"], True))
        finally:
            fake.close()
        self.p.close()
        self.p = T.Project()
        self._gameforge_project(nearest=True)
        fake = self.fake()
        try:
            text, _ = self.plan(fake, [{"tool": "place_assets", "op": "sprite_sheet", "sheet": "res://assets/sprites/hero.png"}])
            self.assertIn("the project already draws textures Nearest", text)
            self.assertNotIn("property: texture_filter", text)
        finally:
            fake.close()

    def test_a_tres_from_another_project_is_relinked(self):
        self._gameforge_project()
        art = self.p.root / "art" / "gf"
        art.mkdir(parents=True)
        (art / "hero.png").write_bytes((self.p.root / "assets/sprites/hero.png").read_bytes())
        text, _anims = B.spriteframes_from_atlas("res://assets/sprites/hero.png", json.loads(
            (self.p.root / "assets/sprites/hero.json").read_text()))
        (art / "hero_frames.tres").write_text(text.replace('path="res://assets/sprites/hero.png"',
                                                           'uid="uid://nothere" path="res://assets/sprites/hero.png"'))
        (self.p.root / "assets/sprites/hero.png").unlink()
        fake = self.fake()
        try:
            plan, plan_id = self.plan(fake, [{"tool": "place_assets", "op": "sprite_sheet",
                                              "frames": "res://art/gf/hero_frames.tres"}])
            self.assertIn("points at res://assets/sprites/hero.png, which is not in this project", plan)
            self.assertIn("res://art/gf/hero.png", plan)
            self.assertNotIn("Two approvals", plan)  # only the broken path line changes
            res = self.apply(fake, plan_id)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            fixed = (art / "hero_frames.tres").read_text()
            self.assertIn('path="res://art/gf/hero.png"', fixed)
            self.assertNotIn("uid://nothere", fixed)
        finally:
            fake.close()

    def test_gameforge_playtest_report_becomes_a_fix_plan(self):
        self._write("main.gd", "extends Node2D\n\nvar score := 0\n\nfunc _physics_process(d):\n\tvar x = cfg[\"nope\"]\n")
        report = {"verdict": "BROKEN", "engine": "godot", "events": {"coin": 2}, "issues": [
            {"severity": "critical", "title": "Engine/script error",
             "detail": "SCRIPT ERROR: Invalid access to property or key 'nope' | at: _physics_process (res://main.gd:6)"},
            {"severity": "major", "title": "Player got stuck", "detail": "no movement for 4s at (40, 72) holding right",
             "t": 12.0}]}
        self._write("playtests/godot_20261021T1200/report.json", json.dumps(report))
        fake = self.fake()
        try:
            res = fake.bridge.call("file_look", {"op": "gameforge_report"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            body = json.loads(res["content"][1]["text"])
            self.assertIn("GameForge found it; this plugin fixes it", res["content"][0]["text"])
            self.assertEqual(body["findings"][0]["file"], "res://main.gd")
            self.assertEqual(body["findings"][0]["line"], 6)
            self.assertEqual(body["findings"][0]["code"], '\tvar x = cfg["nope"]')
            self.assertEqual(body["findings"][1]["cell"], {"x": 2, "y": 4, "tile_size": 16})
            steps = body["fix_plan"]["steps"]
            self.assertEqual(steps[0]["old_text"], '\tvar x = cfg["nope"]')
            self.assertEqual(steps[-1]["op"], "play_session")
            defs = {d["name"]: d for d in B.tool_definitions()}
            self.assertIn("gameforge_report", defs["file_look"]["inputSchema"]["properties"]["op"]["enum"])
        finally:
            fake.close()

    def test_a_bus_the_editor_had_not_loaded_waits_and_finishes_after_reopening(self):
        V._gameforge(self.p.root)
        data = tempfile.TemporaryDirectory()
        os.environ["GAME_EDITOR_DATA_DIR"] = data.name
        fake = self.fake(props={"/Level/CoinSound": {"bus": "Master"}})
        try:
            original = fake.bridge.upstream
            kept = {"bus": "Master"}

            def upstream(name, arguments, timeout=None, mid=None):
                if name == "node_get_properties" and arguments.get("path") == "/Level/CoinSound" and \
                        arguments.get("fields") == ["bus"]:
                    return {"content": [{"type": "text", "text": json.dumps(
                        {"properties": [{"name": "bus", "value": kept["bus"]}]})}], "isError": False}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            _text, plan_id = self.plan(fake, [{"tool": "place_assets", "op": "sfx", "stream": "res://assets/sfx/coin.wav"}])
            res = self.apply(fake, plan_id)
            self.assertIn("waiting: /Level/CoinSound stays on Master", res["content"][0]["text"])
            ledger = json.loads((Path(data.name) / "pending_buses.json").read_text())
            self.assertEqual(ledger[str(self.p.root)][0]["bus"], "SFX")
            direct = fake.bridge.call("editor_look", {"op": "state"})
            self.assertIn("not called directly", direct["content"][0]["text"])
            state = fake.bridge.call("project_look", {"op": "get", "key": "application/config/name"})
            self.assertFalse(state["isError"], state["content"][0]["text"])
            self.assertIn("place_assets op buses", state["content"][0]["text"])
            kept["bus"] = "SFX"  # the project was opened again
            text, plan_id = self.plan(fake, [{"tool": "place_assets", "op": "buses"}])
            self.assertIn("audio_set_playback", text)
            self.assertIn("scene_do op save", text)
            done = self.apply(fake, plan_id)
            self.assertFalse(done["isError"], done["content"][0]["text"])
            self.assertIn("/Level/CoinSound is on bus SFX now", done["content"][0]["text"])
            self.assertFalse((Path(data.name) / "pending_buses.json").exists())
        finally:
            os.environ.pop("GAME_EDITOR_DATA_DIR", None)
            fake.close()
            data.cleanup()

    def test_a_new_scene_still_gets_its_score_holders_watched(self):
        self._write("scripts/main.gd", "extends Node2D\n\nvar score := 0\n")
        fake = self.fake()
        try:
            text, _ = self.plan(fake, [
                {"tool": "scene_do", "op": "new_scene", "path": "res://scenes/fresh.tscn", "root_type": "Node2D",
                 "root_name": "Fresh"},
                {"tool": "scene_do", "op": "attach_script", "path": "/Fresh", "script_path": "res://scripts/main.gd"},
                {"tool": "scene_do", "op": "add_node", "type": "Label", "name": "Score", "parent_path": "/Fresh"},
                {"tool": "scene_do", "op": "set_node_property", "path": "/Fresh/Score", "property": "text",
                 "value": "score: 0"},
                {"tool": "scene_do", "op": "save"},
                {"tool": "game_do", "op": "play_session", "mode": "custom", "scene": "res://scenes/fresh.tscn",
                 "goal": "score >= 1", "steps": [{"op": "wait", "seconds": 1}]}])
            self.assertIn("/root/Fresh (res://scripts/main.gd has var score)", text)
            self.assertIn("/root/Fresh/Score (this plan sets its text to score: 0)", text)
        finally:
            fake.close()

    def test_the_skill_search_words_reach_these_tools(self):
        words = set()
        for d in B.tool_definitions():
            words |= set(re.findall(r"[a-z0-9]+", (d["name"].replace("_", " ") + " " + d["description"]).lower()))
        for word in ("tilemap", "playtest", "ui", "label", "godot"):
            self.assertIn(word, words)


class ObserveN2Test(unittest.TestCase):
    """Rows for the n2 groove: an empty agreement does not name a discard, a node only in the editor
    is not an agreement, and a met goal with frames advanced is ok."""

    def setUp(self):
        self.p = T.Project()

    def tearDown(self):
        self.p.close()

    def fake(self, **kw):
        kw.setdefault("project", self.p.root)
        kw.setdefault("scene", "res://scenes/level.tscn")
        kw.setdefault("nodes", V.LEVEL_NODES)
        return T.FakeChild(**kw)

    def test_an_empty_agreement_does_not_plan_quit_revert_or_reload(self):
        # A stored empty snapshot is not the list. The plan names nothing from it. apply_plan compares.
        fake = self.fake(nodes=list(V.LEVEL_NODES) + ["/Level/OnlyHere"],
                         props={"/Level/Enemies/Enemy3": {"position": {"x": 11, "y": 12}}})
        try:
            scene = "res://scenes/level.tscn"
            sha = B.file_sha(fake.bridge.disk_path(scene, str(self.p.root)))
            fake.bridge.compare_snapshots[scene] = {
                "sha": sha, "diffs": [], "compared": True, "why": "", "skipped": [], "at": 0,
                "bytes_moved": False}
            steps = [
                {"tool": "editor_do", "op": "quit"},
                {"tool": "scene_do", "op": "revert_scene"},
                {"tool": "scene_do", "op": "open_scene", "path": scene, "force_reload": True},
            ]
            for step in steps:
                res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [step]})
                text = res["content"][0]["text"]
                self.assertFalse(res["isError"], text)
                self.assertIn("plan_id:", text)
                self.assertIn("discards unsaved work", text)
                self.assertNotIn("discards no unsaved value", text)
                self.assertNotIn("position:", text)
                self.assertNotIn("/Level/OnlyHere", text)
                applied = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": T._plan_id_of(res)})
                listed = applied["content"][0]["text"]
                self.assertFalse(applied["isError"], listed)
                self.assertIn("needs a second approval", listed)
                self.assertIn('position: disk Vector2(1, 2), editor {"x": 11, "y": 12}', listed)
                self.assertIn("/Level/OnlyHere", listed)
                self.assertFalse(any(row["name"] in ("editor_manage", "scene_open") for row in fake.sent()))
        finally:
            fake.close()

    def test_new_scene_does_not_store_an_empty_agreement(self):
        fake = self.fake()
        try:
            scene = "res://scenes/level.tscn"
            root = str(self.p.root)
            sha = B.file_sha(fake.bridge.disk_path(scene, root))
            fake.bridge.compare_snapshots[scene] = {
                "sha": sha, "diffs": [], "compared": True, "why": "", "skipped": []}
            fake.bridge.in_step(scene, root)
            kept = fake.bridge.compare_snapshots.get(scene) or {}
            self.assertFalse(kept.get("compared"))
            self.assertEqual(kept.get("diffs"), [])
            self.assertEqual(kept.get("sha"), sha)
            record = fake.bridge.scenes.get(scene) or {}
            self.assertFalse(record.get("compared"))
            self.assertNotIn("compared", record)
            self.assertFalse(record.get("certain"))
            self.assertFalse(record.get("full"))
            self.assertEqual(record.get("sha"), sha)
            made = fake.bridge.call("scene_do", {"op": "new_scene", "path": "res://scenes/bare.tscn",
                                                 "root_type": "Node2D", "root_name": "Bare"})
            bare = fake.bridge.compare_snapshots.get("res://scenes/bare.tscn") or {}
            self.assertFalse(bare.get("compared"))
            self.assertFalse(bare.get("diffs"))
            self.assertNotIn("discards no unsaved value", made["content"][0]["text"])
            fake.bridge.in_step(scene, root)
            fake.bridge.observe(scene, root, set())
            looked = fake.bridge.scenes.get(scene) or {}
            self.assertTrue(looked.get("compared"), looked)
            self.assertTrue(looked.get("certain"), looked)
            self.assertFalse((fake.bridge.compare_snapshots.get(scene) or {}).get("compared"))
            self.assertEqual(fake.bridge.check_save("scene_do", scene, root, False), "")
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "editor_do", "op": "quit"}]})
            text = res["content"][0]["text"]
            self.assertFalse(res["isError"], text)
            self.assertIn("plan_id:", text)
            self.assertIn("discards unsaved work", text)
            self.assertNotIn("discards no unsaved value", text)
        finally:
            fake.close()

    def test_compare_names_a_node_that_is_only_in_the_editor(self):
        fake = self.fake()
        try:
            added = fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Hook",
                                                  "parent_path": "/Level"})
            self.assertFalse(added["isError"], added["content"][0]["text"])
            compared = fake.bridge.call("scene_do", {"op": "compare"})
            text = compared["content"][0]["text"]
            self.assertFalse(compared["isError"], text)
            self.assertIn("in editor only: /Level/Hook", text)
            self.assertNotIn("script null", text)
            self.assertNotIn("in editor only: /Level/Hook script", text)
            self.assertNotIn("the editor and the file agree", text)
        finally:
            fake.close()
        fake = self.fake(props={"/Level/Hook": {"script": "res://actors/enemy.gd"}})
        try:
            added = fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Hook",
                                                  "parent_path": "/Level"})
            self.assertFalse(added["isError"], added["content"][0]["text"])
            compared = fake.bridge.call("scene_do", {"op": "compare"})
            text = compared["content"][0]["text"]
            self.assertFalse(compared["isError"], text)
            self.assertIn('in editor only: /Level/Hook script "res://actors/enemy.gd"', text)
            self.assertNotIn("script null", text)
        finally:
            fake.close()

    def test_play_ok_matches_frames_advanced_when_the_goal_is_met(self):
        steps = [
            {"op": "wait", "seconds": 0.5, "pressed": True},
            {"op": "action", "action": "ui_right", "pressed": True, "seconds": 0.4},
        ]
        samples = [
            {"step": 0, "seconds": 0.5, "timed_out": False, "process_frames": 114,
             "errors": ['get_system_ca_certificates: Condition "ret != noErr" is true. Returning: ""'],
             "nodes": {"/root/Game/Score": {"found": True, "props": {"text": "score: 132"}},
                       "/root/Game/Player": {"found": True, "props": {"position": {"x": 10, "y": 20}}}}},
            {"step": 1, "seconds": 0.4, "timed_out": False, "process_frames": 2921,
             "errors": ['WARNING: Steam is not initialized'],
             "nodes": {"/root/Game/Score": {"found": True, "props": {"text": "score: 1125"}},
                       "/root/Game/Player": {"found": True, "props": {"position": {"x": 50.75, "y": 20}}}}},
        ]
        report = B.build_play_report("score >= 3", steps, samples, {}, True, 1.2, 120, True, None,
                                     frames_before=114, require_frames=False, session_live=True)
        self.assertTrue(report["oracle"]["advanced"])
        self.assertTrue(report["goal_met"])
        self.assertTrue(report["ok"])
        self.assertIsNone(report["failure"])
        kinds = {item["kind"] for item in report["findings"]}
        self.assertNotIn("error", kinds)
        self.assertIn("log", kinds)
        missed = B.build_play_report("score >= 3", steps, [
            {"step": 0, "seconds": 0.5, "timed_out": False, "process_frames": 10, "errors": [],
             "nodes": {"/root/Game/Score": {"found": True, "props": {"text": "score: 0"}}}},
            {"step": 1, "seconds": 0.4, "timed_out": False, "process_frames": 80, "errors": [],
             "nodes": {"/root/Game/Score": {"found": True, "props": {"text": "score: 1"}}}},
        ], {}, True, 1.2, 120, True, None, frames_before=10, require_frames=False, session_live=True)
        self.assertTrue(missed["oracle"]["advanced"])
        self.assertFalse(missed["ok"])
        self.assertEqual(missed["failure"], "goal_missed")

    def test_a_runtime_error_names_the_file_line_and_code(self):
        scripts = self.p.root / "scripts"
        scripts.mkdir()
        (scripts / "level.gd").write_text(
            "extends Node\n\nfunc f():\n\tvar x = 1\n\tvar y = 2\n\tvar z = 3\n\tprint(missing)\n")
        runtime = 'level.gd:7 @ func(): Attempt to call function "print" on a null instance'
        report = B.build_play_report("score >= 1", [{"op": "wait", "seconds": 0.5}], [
            {"step": 0, "seconds": 0.5, "timed_out": False, "process_frames": 40,
             "errors": [runtime, "Attempt to call function 'foo' on a null instance.", "Node not found: Player"],
             "nodes": {"/root/Game/Score": {"found": True, "props": {"text": "score: 5"}}}},
        ], {}, True, 1.2, 120, True, None, frames_before=10, require_frames=False, session_live=True)
        self.assertTrue(report["goal_met"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["failure"], "error")
        kinds = [item["kind"] for item in report["findings"] if item["kind"] in ("error", "log")]
        self.assertEqual(kinds, ["error", "error", "error"])
        fake = self.fake()
        try:
            fake.bridge.locate_errors(report, str(self.p.root))
        finally:
            fake.close()
        named = report["findings"][0]
        self.assertEqual(named["file"], "res://scripts/level.gd")
        self.assertEqual(named["line"], 7)
        self.assertEqual(named["code"], "\tprint(missing)")
        self.assertIn("level.gd", named["next"])
        self.assertNotIn("file", report["findings"][1])
        self.assertNotIn("file", report["findings"][2])

    def test_saving_a_scripted_prefab_is_not_a_false_difference(self):
        scripts = self.p.root / "scripts"
        scripts.mkdir()
        (scripts / "gem.gd").write_text("extends Node2D\n")
        (self.p.root / "actors" / "gem.tscn").write_text(
            '[gd_scene load_steps=2 format=3]\n\n'
            '[ext_resource type="Script" path="res://scripts/gem.gd" id="1"]\n\n'
            '[node name="Gem" type="Node2D"]\nscript = ExtResource("1")\n', encoding="utf-8")
        (self.p.root / "scenes" / "board.tscn").write_text(
            '[gd_scene load_steps=2 format=3]\n\n'
            '[ext_resource type="PackedScene" path="res://actors/gem.tscn" id="1"]\n\n'
            '[node name="Board" type="Node2D"]\n\n'
            '[node name="Gem" parent="." instance=ExtResource("1")]\n', encoding="utf-8")
        fake = T.FakeChild(project=self.p.root, scene="res://scenes/board.tscn",
                           nodes=["/Board", "/Board/Gem"],
                           props={"/Board/Gem": {"script": "res://scripts/gem.gd"}})
        try:
            compared = fake.bridge.call("scene_do", {"op": "compare"})
            text = compared["content"][0]["text"]
            self.assertFalse(compared["isError"], text)
            self.assertNotIn("script:", text)
            self.assertNotIn("disk (none)", text)
            saved = fake.bridge.call("scene_do", {"op": "save"})
            blob = json.dumps(saved["content"])
            self.assertFalse(saved["isError"], blob)
            self.assertNotIn("disk (none)", blob)
            self.assertNotIn("gem.gd", " ".join(fake.bridge.second_approvals))
        finally:
            fake.close()

    def test_an_existing_resource_file_is_replaced_for_any_write(self):
        folder = self.p.root / "themes"
        folder.mkdir()
        body = '[gd_resource type="Theme" format=3]\n\n[resource]\n'
        for name in ("ui.tres", "mat.tres", "env.tres", "made.tres"):
            (folder / name).write_text(body, encoding="utf-8")
        fake = self.fake()
        try:
            refused = fake.bridge.call("asset_do", {"op": "theme_create", "path": "res://themes/ui.tres"})
            self.assertTrue(refused["isError"], refused["content"][0]["text"])
            self.assertIn("already exists", refused["content"][0]["text"])
            self.assertEqual((folder / "ui.tres").read_text(encoding="utf-8"), body)
            self.assertFalse(any(row["name"] == "theme_manage" for row in fake.sent()))
            steps = [
                ("theme_create", {"op": "theme_create", "path": "res://themes/ui.tres", "overwrite": True},
                 "res://themes/ui.tres"),
                ("material_create", {"op": "material_create", "path": "res://themes/mat.tres", "overwrite": True},
                 "res://themes/mat.tres"),
                ("environment_create", {"op": "environment_create", "resource_path": "res://themes/env.tres",
                                        "overwrite": True}, "res://themes/env.tres"),
                ("edit_resources_create", {"op": "edit_resources_create", "type": "Theme",
                                           "resource_path": "res://themes/made.tres", "overwrite": True},
                 "res://themes/made.tres"),
            ]
            for op, arguments, path in steps:
                fake.bridge.second_approvals.clear()
                done = fake.bridge.call("asset_do", arguments)
                self.assertFalse(done["isError"], (op, done["content"][0]["text"]))
                listed = " ".join(fake.bridge.second_approvals)
                self.assertIn("replaces the existing file " + path, listed, op)
                self.assertIn("gd_resource", listed, op)
                self.assertGreaterEqual(len(fake.bridge.second_approvals), 1, op)
        finally:
            fake.close()

    def test_a_move_names_the_uid_and_import_beside_it(self):
        scripts = self.p.root / "scripts"
        scripts.mkdir()
        (scripts / "gem.gd").write_text("extends Node\n", encoding="utf-8")
        (scripts / "gem.gd.uid").write_text("uid://gem\n", encoding="utf-8")
        (scripts / "taken.gd").write_text("extends Node\n", encoding="utf-8")
        fake = self.fake()
        try:
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "file_do", "op": "move", "path": "res://scripts/gem.gd",
                 "new_path": "res://scripts/taken.gd"}]})
            text = planned["content"][0]["text"]
            self.assertFalse(planned["isError"], text)
            self.assertIn("also moves", text)
            self.assertIn("res://scripts/gem.gd.uid", text)
            self.assertIn("res://scripts/gem.gd.import", text)
            self.assertIn("res://scripts/taken.gd.uid", text)
            self.assertIn("res://scripts/taken.gd.import", text)
            self.assertIn("replaces the existing file res://scripts/taken.gd", text)
        finally:
            fake.close()

    def test_a_failed_later_step_still_records_a_waiting_bus(self):
        data = tempfile.TemporaryDirectory()
        os.environ["GAME_EDITOR_DATA_DIR"] = data.name
        fake = self.fake()
        try:
            original = fake.bridge.upstream

            def upstream(name, arguments, timeout=None, mid=None):
                if name == "node_get_properties" and arguments.get("path") == "/Level/CoinSound":
                    return {"content": [{"type": "text", "text": json.dumps(
                        {"properties": [{"name": "bus", "value": "Master"}]})}], "isError": False}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            fake.bridge.ensure_child()
            lines = fake.bridge.note_pending_buses(
                {"checks": [{"kind": "audio", "bus": "SFX", "path": "/Level/CoinSound",
                             "scene": "res://scenes/level.tscn"}]},
                [{"isError": True}], str(self.p.root))
            self.assertTrue(any("waiting: /Level/CoinSound" in line for line in lines), lines)
            ledger = json.loads((Path(data.name) / "pending_buses.json").read_text(encoding="utf-8"))
            self.assertEqual(ledger[str(self.p.root)][0]["bus"], "SFX")
            fake.bridge.upstream = lambda name, arguments, timeout=None, mid=None: {
                "content": [{"type": "text", "text": "no"}], "isError": True}
            silent = fake.bridge.note_pending_buses(
                {"checks": [{"kind": "audio", "bus": "Music", "path": "/Level/Tune",
                             "scene": "res://scenes/level.tscn"}]},
                [{"isError": True}], str(self.p.root))
            self.assertEqual(silent, [])
            again = json.loads((Path(data.name) / "pending_buses.json").read_text(encoding="utf-8"))
            self.assertEqual(len(again[str(self.p.root)]), 1)
        finally:
            os.environ.pop("GAME_EDITOR_DATA_DIR", None)
            fake.close()
            data.cleanup()

    def test_every_effect_row_has_one_of_the_four_families(self):
        data = json.loads((Path(B.__file__).resolve().parent / "effects.json").read_text(encoding="utf-8"))
        families = {"disk_read", "editor_contact", "mutation", "envelope"}
        seen = set()
        for table in ("by_command", "by_tool"):
            self.assertGreater(len(data[table]), 0)
            for name, row in data[table].items():
                self.assertIn(row.get("family"), families, name)
                seen.add(row["family"])
        self.assertEqual(seen, families)
        self.assertEqual(data["by_command"]["open_scene"]["family"], "editor_contact")
        self.assertEqual(data["by_command"]["save_scene"]["family"], "mutation")
        self.assertEqual(data["by_tool"]["@local.play_session"]["family"], "envelope")
        self.assertEqual(data["by_tool"]["@local.scene_look"]["family"], "disk_read")
        self.assertEqual(data["by_tool"]["@local.export_game"]["family"], "envelope")


if __name__ == "__main__":
    unittest.main()
