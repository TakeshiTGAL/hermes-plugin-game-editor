"""Offline checks for what 0.9.0 added: the newest plan only, the second approval (elicitation) for a plan that
loses or runs something, batch lines, plan steps that see earlier steps, the comparison before the first step,
@tool-aware reads, place_assets, play_session help, progress notifications and the remembered PORT_OCCUPIED."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import time
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bridge as T  # noqa: E402  (also installs the test hooks on Bridge)

B = T.B
ROOT = Path(__file__).resolve().parents[1]

LEVEL_NODES = ["/Level", "/Level/Enemies", "/Level/Enemies/Enemy3", "/Level/Enemies/Enemy3/Sprite",
               "/Level/Enemies/Enemy3/Waypoint", "/Level/Enemies/Enemy6", "/Level/Enemies/Enemy6/Sprite",
               "/Level/RoomA", "/Level/RoomA/Guard", "/Level/RoomA/Guard/Sprite", "/Level/RoomB",
               "/Level/RoomB/Guard", "/Level/RoomB/Guard/Sprite"]


def _png(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")
                     + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)


def _wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(44100)
        out.writeframes(struct.pack("<100h", *([0] * 100)))


def _gameforge(root: Path) -> None:
    """Files in the formats GameForge writes (made here; GameForge is not run)."""
    sprites = root / "assets" / "sprites"
    _png(sprites / "hero.png", 96, 32)
    atlas = {"frames": {f"hero_{i}": {"frame": {"x": (i % 6) * 16, "y": (i // 6) * 16, "w": 16, "h": 16},
                                      "duration": 125} for i in range(8)},
             "meta": {"image": "hero.png", "frameTags": [{"name": "idle", "from": 0, "to": 1},
                                                          {"name": "walk_right", "from": 2, "to": 5}]}}
    (sprites / "hero.json").write_text(json.dumps(atlas))
    tiles = root / "assets" / "tiles"
    _png(tiles / "water_grass.png", 64, 96)
    (tiles / "water_grass.json").write_text(json.dumps({"tile_size": 16, "columns": 4, "terrains": ["water", "grass"],
                                                        "fill_variants": {"upper_row": 4, "lower_row": 5}}))
    (tiles / "water_grass.tres").write_text(B.tileset_from_layout("res://assets/tiles/water_grass.png", json.loads(
        (tiles / "water_grass.json").read_text())))
    _wav(root / "assets" / "sfx" / "coin.wav")
    _wav(root / "assets" / "music" / "theme.wav")
    (root / "assets" / "music" / "theme.wav.import").write_text(
        '[remap]\n\nimporter="wav"\ntype="AudioStreamWAV"\nuid="uid://x"\npath="res://.godot/imported/t.sample"\n\n'
        '[params]\n\nforce/8_bit=false\nedit/loop_mode=0\nedit/loop_begin=0\nedit/loop_end=-1\ncompress/mode=2\n')


class V09Test(unittest.TestCase):
    def setUp(self):
        self.p = T.Project()

    def tearDown(self):
        self.p.close()

    def fake(self, **kw):
        kw.setdefault("project", self.p.root)
        kw.setdefault("scene", "res://scenes/level.tscn")
        kw.setdefault("nodes", LEVEL_NODES)
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

    def test_only_the_newest_plan_runs_and_only_once(self):
        fake = self.fake()
        try:
            _text, first = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "A",
                                             "parent_path": "/Level"}])
            _text, second = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "B",
                                              "parent_path": "/Level"}])
            old = self.apply(fake, first)
            self.assertTrue(old["isError"])
            self.assertIn("a newer plan replaced this plan_id", old["content"][0]["text"])
            self.assertFalse(any(row["name"] == "node_create" for row in fake.sent()))
            new = self.apply(fake, second)
            self.assertFalse(new["isError"], new["content"][0]["text"])
            again = self.apply(fake, second)
            self.assertIn("already used", again["content"][0]["text"])
        finally:
            fake.close()

    def test_a_plan_expires(self):
        fake = self.fake()
        try:
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "save"}])
            fake.bridge._plans[plan_id]["made"] -= B.PLAN_TTL + 1
            res = self.apply(fake, plan_id)
            self.assertIn("older than 10 minutes", res["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_save" for row in fake.sent()))
        finally:
            fake.close()

    def test_a_delete_needs_the_second_approval_and_apply_runs_nothing(self):
        fake = self.fake()
        try:
            text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "delete_node", "path": "/Level/Enemies/Enemy3"},
                                             {"tool": "scene_do", "op": "save"}])
            self.assertIn("! deletes node /Level/Enemies/Enemy3", text)
            self.assertIn("Two approvals", text)
            res = self.apply(fake, plan_id)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            summary = res["content"][0]["text"]
            self.assertIn("needs a second approval", summary)
            self.assertTrue(summary.splitlines()[1].startswith("- step 1 deletes node /Level/Enemies/Enemy3"))
            self.assertLessEqual(len(summary), B.CONFIRM_LIMIT)
            self.assertFalse(any(row["name"] in ("node_manage", "scene_save") and
                                 (row.get("arguments") or {}).get("op") in (None, "delete") for row in fake.sent()))
            again = self.apply(fake, plan_id)
            self.assertEqual(again["content"][0]["text"], summary)  # the same list, still nothing ran
        finally:
            fake.close()

    def test_confirm_runs_only_after_apply_and_only_that_plan(self):
        fake = self.fake()
        try:
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "delete_node", "path": "/Level/Enemies/Enemy3"}])
            early = B.Bridge.call(fake.bridge, "confirm_risky_plan", {"plan_id": plan_id})
            self.assertTrue(early["isError"])
            self.assertIn("only after apply_plan was approved", early["content"][0]["text"])
            self.assertFalse(self.apply(fake, plan_id)["isError"])
            done = B.Bridge.call(fake.bridge, "confirm_risky_plan", {"plan_id": plan_id})
            self.assertFalse(done["isError"], done["content"][0]["text"])
            self.assertTrue(any(row["name"] == "node_manage" and (row.get("arguments") or {}).get("op") == "delete"
                                for row in fake.sent()))
            twice = B.Bridge.call(fake.bridge, "confirm_risky_plan", {"plan_id": plan_id})
            self.assertIn("already used", twice["content"][0]["text"])
        finally:
            fake.close()

    def test_an_ordinary_edit_asks_only_once(self):
        fake = self.fake()
        try:
            text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Coin",
                                              "parent_path": "/Level"}, {"tool": "scene_do", "op": "save"}])
            self.assertNotIn("This plan deletes", text)
            self.assertNotIn("Two approvals", text)
            res = self.apply(fake, plan_id)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertNotIn("needs a second approval", res["content"][0]["text"])
            self.assertTrue(any(row["name"] == "scene_save" for row in T.FakeChild.sent(fake)))
            self.assertIn("game-editor checked: res://scenes/level.tscn on disk matches the editor",
                          res["content"][0]["text"])
        finally:
            fake.close()

    def test_play_and_game_code_differ(self):
        fake = self.fake()
        try:
            play, _ = self.plan(fake, [{"tool": "game_do", "op": "play_session", "goal": "score >= 1",
                                        "steps": [{"op": "wait", "seconds": 1}]}])
            self.assertNotIn("Two approvals", play)
            self.assertIn("runs project code: plays the game", play)
            code, _ = self.plan(fake, [{"tool": "game_do", "op": "run_game_code", "code": "print(1)"}])
            self.assertIn("Two approvals", code)
            self.assertIn("runs code written in this plan", code)
        finally:
            fake.close()

    def test_batch_commands_get_their_own_lines(self):
        fake = self.fake()
        try:
            text, _plan_id = self.plan(fake, [{"tool": "batch", "commands": [
                {"command": "delete_node", "params": {"path": "/Level/Enemies/Enemy3"}},
                {"command": "set_property", "params": {"path": "/Level/Enemies", "property": "visible", "value": False}}]}])
            self.assertIn("deletes node /Level/Enemies/Enemy3 (batch)", text)
            self.assertIn("This plan deletes", text)
        finally:
            fake.close()

    def test_a_new_scene_and_its_prefab_are_one_plan(self):
        fake = self.fake()
        try:
            text, _plan_id = self.plan(fake, [
                {"tool": "scene_do", "op": "new_scene", "path": "res://scenes/main.tscn", "root_type": "Node2D",
                 "root_name": "Main"},
                {"tool": "scene_do", "op": "add_node", "scene_path": "res://actors/enemy.tscn", "name": "Enemy",
                 "parent_path": "/Main"},
                {"tool": "scene_do", "op": "set_node_property", "path": "/Main/Enemy", "property": "position",
                 "value": {"x": 1, "y": 2}},
                {"tool": "scene_do", "op": "save"}])
            self.assertIn("scene: res://scenes/main.tscn", text)
            cycle = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "new_scene", "path": "res://scenes/loop.tscn", "root_type": "Node2D"},
                {"tool": "scene_do", "op": "add_node", "scene_path": "res://scenes/loop.tscn", "parent_path": "/loop"}]})
            self.assertTrue(cycle["isError"])
            self.assertIn("a cycle", cycle["content"][0]["text"])
            again = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "script_do", "op": "create_script", "path": "res://actors/a.gd", "content": "extends Node\n"},
                {"tool": "script_do", "op": "create_script", "path": "res://actors/a.gd", "content": "extends Node\n"}]})
            self.assertTrue(again["isError"])
            self.assertIn("already exists", again["content"][0]["text"])
            named = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "script_do", "op": "create_script", "path": "res://actors/fresh.gd",
                 "content": "extends Node\n"}]})
            self.assertFalse(named["isError"], named["content"][0]["text"])
            self.assertIn("also writes res://actors/fresh.gd.uid beside the script", named["content"][0]["text"])
        finally:
            fake.close()

    def test_a_scene_open_before_the_session_takes_an_add_and_a_save(self):
        fake = self.fake()
        try:
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Coin",
                                               "parent_path": "/Level"},
                                              {"tool": "scene_do", "op": "set_node_property", "path": "/Level/Coin",
                                               "property": "position", "value": {"x": 3, "y": 4}},
                                              {"tool": "scene_do", "op": "save"}])
            res = self.apply(fake, plan_id)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("apply_plan ran all 3 steps", res["content"][0]["text"])
        finally:
            fake.close()

    def test_reads_refuse_a_tool_script_by_what_the_handler_reads(self):
        tool = self.p.root / "actors" / "spark.gd"
        tool.write_text("@tool\nextends Node\n\nfunc _get(p):\n\treturn null\n")
        quiet = self.p.root / "actors" / "quiet.gd"
        quiet.write_text("@tool\nextends Node\n\nfunc _process(_d):\n\tpass\n")
        level = self.p.root / "scenes" / "level.tscn"
        text = level.read_text()
        text = text.replace('[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]',
                            '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]\n'
                            '[ext_resource type="Script" path="res://actors/quiet.gd" id="3"]')
        text = text.replace('[node name="Enemies" type="Node2D" parent="."]',
                            '[node name="Enemies" type="Node2D" parent="."]\nscript = ExtResource("3")')
        level.write_text(text)
        fake = self.fake()
        try:
            tree = fake.bridge.call("scene_look", {"op": "scene_tree"})
            self.assertFalse(tree["isError"], tree["content"][0]["text"])
            self.assertIn("editor was not asked", tree["content"][0]["text"])
            props = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/Enemies"})
            self.assertFalse(props["isError"], props["content"][0]["text"])
            text = text.replace("res://actors/quiet.gd", "res://actors/spark.gd")
            level.write_text(text)
            tree = fake.bridge.call("scene_look", {"op": "scene_tree"})
            self.assertFalse(tree["isError"], tree["content"][0]["text"])
            self.assertIn("editor was not asked", tree["content"][0]["text"])
            plain = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/Enemies/Enemy3"})
            self.assertFalse(plain["isError"], plain["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_get_hierarchy" for row in fake.sent()))
        finally:
            fake.close()

    def test_node_access_comes_from_the_upstream_source(self):
        self.assertEqual(B.op_node_access(B.TOOLS["scene_look"]["ops"]["scene_tree"]), {"reads": ["name"], "walks": True})
        self.assertIn("values", B.op_node_access(B.TOOLS["scene_look"]["ops"]["properties"])["reads"])
        self.assertIn("values", B.op_node_access(B.TOOLS["asset_look"]["ops"]["camera"])["reads"])
        self.assertIn("values", B.op_node_access(B.TOOLS["asset_look"]["ops"]["particles"])["reads"])
        self.assertIn("values", B.op_node_access(B.TOOLS["asset_look"]["ops"]["animation_check"])["reads"])
        self.assertIsNone(B.op_node_access(B.TOOLS["editor_look"]["ops"]["state"]))

    def test_place_assets_becomes_godot_ai_steps(self):
        _gameforge(self.p.root)
        fake = self.fake()
        try:
            text, plan_id = self.plan(fake, [
                {"tool": "place_assets", "op": "sprite_sheet", "sheet": "res://assets/sprites/hero.png",
                 "animation": "walk_right"},
                {"tool": "place_assets", "op": "tileset", "tileset": "res://assets/tiles/water_grass.tres"},
                {"tool": "place_assets", "op": "sfx", "stream": "res://assets/sfx/coin.wav"},
                {"tool": "place_assets", "op": "music", "stream": "res://assets/music/theme.wav"},
                {"tool": "scene_do", "op": "save"}])
            self.assertIn("place_assets op sprite_sheet (your step 1)", text)
            self.assertIn("res://assets/sprites/hero_frames.tres (written from res://assets/sprites/hero.json)", text)
            self.assertIn("40" if False else "10x6 cells of tile (0, 4)", text)
            self.assertIn("edit/loop_mode=2", text)
            self.assertIn("bus SFX is added to res://default_bus_layout.tres", text)
            self.assertNotIn("This plan deletes", text)  # import settings and a new bus layout lose nothing
            res = self.apply(fake, plan_id)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            names = [row["name"] for row in fake.sent()]
            for name in ("filesystem_manage", "node_create", "node_set_property", "tilemap_manage", "audio_manage"):
                self.assertIn(name, names)
            written = (self.p.root / "assets" / "sprites" / "hero_frames.tres")
            self.assertTrue(written.exists())  # written by the step and read back
            self.assertIn("game-editor placed:", res["content"][0]["text"])
        finally:
            fake.close()

    def test_place_assets_checks_the_files(self):
        _gameforge(self.p.root)
        (self.p.root / "assets" / "sfx" / "bad.wav").write_text("not audio")
        fake = self.fake()
        try:
            bad = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "place_assets", "op": "sfx", "stream": "res://assets/sfx/bad.wav"}]})
            self.assertIn("is not a wav file", bad["content"][0]["text"])
            missing = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "place_assets", "op": "sprite_sheet", "sheet": "res://assets/sprites/hero.png",
                 "animation": "jump"}]})
            self.assertIn("has no animation 'jump'", missing["content"][0]["text"])
            guide = B.change_guide({"tool": "place_assets", "op": "music"})["content"][0]["text"]
            self.assertIn("example step", guide)
        finally:
            fake.close()

    def test_gameforge_formats(self):
        text, anims = B.spriteframes_from_atlas("res://a.png", {"frames": {"a": {"frame": {"x": 0, "y": 0, "w": 8, "h": 8},
                                                                             "duration": 100}},
                                                                "meta": {"frameTags": [{"name": "idle", "from": 0, "to": 0}]}})
        self.assertEqual(anims, ["idle"])
        self.assertIn('"name": &"idle", "speed": 10.0', text)
        merged = B.bus_layout_with(B.bus_layout_with("", "SFX"), "Music")
        self.assertIn('bus/1/name = &"SFX"', merged)
        self.assertIn('bus/2/name = &"Music"', merged)
        looped = B.import_with_loop("[remap]\n\n[params]\n\nedit/loop_mode=0\n", "wav")
        self.assertIn("edit/loop_mode=2", looped)
        self.assertTrue(B.only_params_differ("[remap]\na=1\n[params]\nx=0\n", "[remap]\na=1\n[params]\nx=1\n"))
        self.assertEqual(B.audio_kind(b"OggS\x00"), "ogg")

    def test_play_session_finds_the_score_and_names_the_error_line(self):
        scripts = self.p.root / "scripts"
        scripts.mkdir()
        (scripts / "main.gd").write_text("extends Node2D\n\nvar score := 0\n\nfunc f():\n\tpass\n\t$HUD/Missing.text = 'x'\n")
        (self.p.root / "scenes" / "main.tscn").write_text(
            '[gd_scene format=3]\n\n[ext_resource type="Script" path="res://scripts/main.gd" id="1"]\n\n'
            '[node name="Main" type="Node2D"]\nscript = ExtResource("1")\n\n[node name="HUD" type="CanvasLayer" parent="."]\n\n'
            '[node name="Score" type="Label" parent="HUD"]\ntext = "score: 0"\n')
        (self.p.root / "project.godot").write_text('[application]\nrun/main_scene="res://scenes/main.tscn"\n')
        fake = self.fake()
        try:
            text, _plan_id = self.plan(fake, [{"tool": "game_do", "op": "play_session", "goal": "score >= 3",
                                               "watch": ["/root/Main/Player"], "steps": [{"op": "wait", "seconds": 1}]}])
            self.assertIn('"/root/Main", "/root/Main/HUD/Score"', text)
            self.assertIn("added to watch because the goal compares score", text)
            report = {"findings": [{"kind": "error", "detail": "main.gd:7 @ f(): Node not found"}]}
            fake.bridge.locate_errors(report, str(self.p.root))
            found = report["findings"][0]
            self.assertEqual((found["file"], found["line"]), ("res://scripts/main.gd", 7))
            self.assertEqual(found["code"], "\t$HUD/Missing.text = 'x'")
        finally:
            fake.close()

    def test_first_step_idle_is_judged(self):
        steps = [{"op": "action", "action": "ui_right", "seconds": 1.5, "pressed": True}]
        sample = {"step": 0, "seconds": 1.5, "process_frames": 90, "errors": [],
                  "nodes": {"/root/Main/Player": {"found": True, "props": {"position": {"x": 100, "y": 200}}}}}
        start = {"/root/Main/Player": {"found": True, "props": {"position": {"x": 100, "y": 200}}}}
        report = B.build_play_report("score >= 0", steps, [sample], {}, True, 1.2, 120, True, start)
        self.assertIn("idle", {item["kind"] for item in report["findings"]})
        report = B.build_play_report("score >= 0", steps, [sample], {}, True, 1.2, 120, True, None)
        self.assertNotIn("idle", {item["kind"] for item in report["findings"]})

    def test_tests_are_checked_when_the_plan_is_made(self):
        fake = self.fake()
        try:
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "checks", "op": "gut"}]})
            self.assertTrue(res["isError"])
            text = res["content"][0]["text"]
            self.assertTrue("GUT is not installed" in text or "Godot binary" in text, text)
            self.assertIn("No plan was saved", text)
        finally:
            fake.close()

    def test_progress_only_with_a_token(self):
        fake = self.fake()
        try:
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "P",
                                               "parent_path": "/Level"}, {"tool": "scene_do", "op": "save"}])
            sent = []
            fake.bridge.write = sent.append
            fake.bridge._call_and_reply(7, {"name": "apply_plan", "arguments": {"plan_id": plan_id},
                                            "_meta": {"progressToken": "tok"}})
            notes = [m for m in sent if m.get("method") == "notifications/progress"]
            self.assertGreaterEqual(len(notes), 3)
            self.assertTrue(all(m["params"]["progressToken"] == "tok" for m in notes))
            self.assertEqual([m["params"]["progress"] for m in notes], sorted(m["params"]["progress"] for m in notes))
            _text, plan_id = self.plan(fake, [{"tool": "scene_do", "op": "save"}])
            sent.clear()
            fake.bridge._call_and_reply(8, {"name": "apply_plan", "arguments": {"plan_id": plan_id}})
            self.assertEqual([m for m in sent if m.get("method") == "notifications/progress"], [])
            self.assertEqual(sent[-1]["id"], 8)
        finally:
            fake.close()

    def test_port_occupied_is_remembered_while_the_port_is_taken(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        original = socket.create_connection

        def connect(address, timeout=None, *args, **kwargs):
            host = address[0] if isinstance(address, tuple) else None
            want = address[1] if isinstance(address, tuple) else None
            if host == "127.0.0.1" and want == port and listener.fileno() != -1:
                left, right = socket.socketpair()
                right.close()
                return left
            raise OSError("port is free")

        socket.create_connection = connect
        try:
            bridge = B.Bridge(argv=[sys.executable, "-c", "raise SystemExit(9)"])
            bridge._port_block = (port, "godot-ai attach [PORT_OCCUPIED]: Port %d is occupied" % port, time.time())
            started = time.time()
            first = bridge.ensure_child()
            self.assertIn("is still taken", first)
            self.assertIsNone(bridge.child)
            self.assertLess(time.time() - started, 2)
            listener.close()
            second = bridge.ensure_child()
            self.assertNotIn("is still taken", second)
            self.assertIsNone(bridge._port_block)
        finally:
            socket.create_connection = original
            if listener.fileno() != -1:
                listener.close()

    def test_extractor_runs_from_this_repository(self):
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "extract_effects.py")], capture_output=True,
                             text=True, env={k: v for k, v in os.environ.items() if k != "GODOT_AI_SRC"})
        self.assertEqual(out.returncode, 2)
        self.assertIn("git clone --depth 1 --branch v4.3.0", out.stderr)
        src = os.environ.get("GODOT_AI_SRC")
        if not src:
            self.skipTest("GODOT_AI_SRC is not set; the --check run needs the upstream source")
        check = subprocess.run([sys.executable, str(ROOT / "tools" / "extract_effects.py"), "--upstream", src,
                                "--check"], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_a_script_changed_behind_the_plan_is_named(self):
        script = self.p.root / "actors" / "enemy.gd"
        bridge = B.Bridge(argv=["true"])
        bridge.written_scripts["res://actors/enemy.gd"] = B.file_sha(script)
        self.assertEqual(bridge.scripts_changed_behind([], str(self.p.root)), [])
        script.write_text("extends Node\n# older copy\n")
        lines = bridge.scripts_changed_behind([], str(self.p.root))
        self.assertEqual(len(lines), 1)
        self.assertIn("res://actors/enemy.gd changed while this plan ran", lines[0])
        self.assertEqual(bridge.scripts_changed_behind([], str(self.p.root)), [])  # said once
        script.write_text("extends Node\n")
        self.assertEqual(bridge.scripts_changed_behind([{"tool": "script_do", "path": "res://actors/enemy.gd"}],
                                                       str(self.p.root)), [])


    def test_each_finding_carries_its_evidence(self):
        steps = [{"op": "action", "action": "ui_right", "seconds": 1.0, "pressed": True},
                 {"op": "wait", "seconds": 1.0, "pressed": True}]
        samples = [{"step": 0, "seconds": 1.0, "process_frames": 60,
                    "errors": ["SCRIPT ERROR: main.gd:7 @ f(): Node not found"],
                    "nodes": {"/root/Main/Player": {"found": True, "props": {"position": {"x": 1, "y": 2}}}}},
                   {"step": 1, "seconds": 1.0, "process_frames": None, "timed_out": True, "answered": False,
                    "errors": [], "nodes": {}}]
        report = B.build_play_report("score >= 1", steps, samples, {}, True, 1.2, 120, True, None)
        error = next(item for item in report["findings"] if item["kind"] == "error")
        self.assertEqual(error["evidence"]["frame"], 60)
        self.assertEqual(error["evidence"]["after_step"], 1)
        self.assertEqual(error["evidence"]["log"], ["SCRIPT ERROR: main.gd:7 @ f(): Node not found"])
        self.assertIn("/root/Main/Player", error["evidence"]["nodes"])
        stall = next(item for item in report["findings"] if item["kind"] == "stall")
        self.assertIsNone(stall["evidence"]["frame"])
        self.assertEqual(stall["evidence"]["last_answered_frame"], 60)


    EDITOR = ["/Level", "/Level/Enemies", "/Level/Enemies/Enemy3", "/Level/Enemies/Enemy6",
              "/Level/Enemies/Enemy6/Sprite", "/Level/Enemies/Enemy3/Waypoint", "/Level/RoomA", "/Level/RoomB"]

    def test_review_two_plans_still_compare_before_the_save(self):
        fake = self.fake(nodes=self.EDITOR, props={"/Level/Enemies/Enemy3": {"position": {"x": 9, "y": 9}}})
        try:
            _t, first = self.plan(fake, [{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Fresh",
                                          "parent_path": "/Level"}])
            self.assertFalse(self.apply(fake, first)["isError"])
            prepared = T.prepare_save(fake.bridge, [{"tool": "scene_do", "op": "save"}])
            self.assertIsNone(prepared)
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "scene_do", "op": "save"}]})
            self.assertTrue(res["isError"], res["content"][0]["text"])
            self.assertIn("/Level/Enemies/Enemy3 position", res["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_save" for row in fake.sent()))
        finally:
            fake.close()

    def test_review_a_tool_script_without_get_hooks_is_compared(self):
        quiet = self.p.root / "actors" / "quiet.gd"
        quiet.write_text("@tool\nextends Node\n\nfunc _process(_d):\n\tpass\n")
        level = self.p.root / "scenes" / "level.tscn"
        text = level.read_text().replace('[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]',
                                         '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]\n'
                                         '[ext_resource type="Script" path="res://actors/quiet.gd" id="3"]')
        text = text.replace('[node name="Enemy3" parent="Enemies" instance=ExtResource("1")]\nposition = Vector2(1, 2)',
                            '[node name="Enemy3" parent="Enemies" instance=ExtResource("1")]\nposition = Vector2(1, 2)\n'
                            'script = ExtResource("3")')
        level.write_text(text)
        self.assertIn('script = ExtResource("3")', level.read_text())
        fake = self.fake(nodes=self.EDITOR, props={"/Level/Enemies/Enemy3": {"position": {"x": 9, "y": 9}}})
        try:
            prepared = T.prepare_save(fake.bridge, [{"tool": "scene_do", "op": "save"}])
            self.assertIsNone(prepared)
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "scene_do", "op": "save"}]})
            self.assertTrue(res["isError"], res["content"][0]["text"])
            self.assertIn("Enemy3 position", res["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_save" for row in fake.sent()))
        finally:
            fake.close()

    def test_review_a_name_that_is_taken_is_refused(self):
        fake = self.fake(nodes=self.EDITOR)
        try:
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Enemies", "parent_path": "/Level"}]})
            self.assertTrue(res["isError"])
            self.assertIn("already there", res["content"][0]["text"])
            twice = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Twin", "parent_path": "/Level"},
                {"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Twin", "parent_path": "/Level"}]})
            self.assertTrue(twice["isError"])
        finally:
            fake.close()

    def test_review_odd_node_paths_and_session_ids_are_refused(self):
        fake = self.fake(nodes=self.EDITOR)
        try:
            for odd in ("/Level/Enemies/.", "%Enemies", "/Level//Enemies", "/Level/Enemies/../Enemies"):
                res = fake.bridge.call("scene_look", {"op": "properties", "path": odd})
                self.assertTrue(res["isError"], odd)
                self.assertIn("plain path", res["content"][0]["text"], odd)
            res = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "scene_do", "op": "save",
                                                                       "session_id": "other"}]})
            self.assertIn("has session_id", res["content"][0]["text"])
            unknown = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/NotInFile"})
            self.assertTrue(unknown["isError"])
            self.assertIn("not in res://scenes/level.tscn on disk", unknown["content"][0]["text"])
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main()
