"""Offline checks for the bridge: no network, no Godot. Standard library only."""

from __future__ import annotations

import importlib.util
import json
import ntpath
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).resolve().parent / "fake_upstream.py"
BRIDGE_PATH = ROOT / "bridge" / "game_editor_bridge.py"


def load_bridge():
    spec = importlib.util.spec_from_file_location("game_editor_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_bridge()


def _plan_id_of(result: dict) -> str:
    text = result["content"][0]["text"]
    for line in text.splitlines():
        if line.startswith("plan_id: "):
            return line[len("plan_id: "):].strip()
    return ""


_bridge_call = B.Bridge.call


def _testing_call(self, name, arguments=None, mid=None):
    """Unit tests still call write tools. The server only runs them through plan_change and apply_plan."""
    tool = B.TOOLS.get(name) if isinstance(name, str) else None
    if tool is None or tool["tier"] == "read" or name in ("plan_change", "apply_plan", "confirm_risky_plan"):
        return _bridge_call(self, name, arguments, mid)
    step = {"tool": name}
    if isinstance(arguments, dict):
        step.update(arguments)
    else:
        return _bridge_call(self, name, arguments, mid)
    planned = _bridge_call(self, "plan_change", {"steps": [step]}, mid)
    if planned.get("isError"):
        first_text = (planned.get("content") or [{}])[0].get("text", "") if isinstance(planned, dict) else ""
        if (name == "scene_do" and step.get("op") == "save" and "op compare" in first_text
                and "made no plan" in first_text):
            compared = _bridge_call(self, "plan_change", {"steps": [{"tool": "scene_do", "op": "compare"}]}, mid)
            if compared.get("isError"):
                return compared
            compare_id = _plan_id_of(compared)
            applied_compare = _bridge_call(self, "apply_plan", {"plan_id": compare_id}, mid)
            compare_text = (applied_compare.get("content") or [{}])[0].get("text", "") if isinstance(applied_compare, dict) else ""
            if not applied_compare.get("isError") and "needs a second approval" in compare_text:
                self.second_approvals.append(compare_text)
                applied_compare = _bridge_call(self, "confirm_risky_plan", {"plan_id": compare_id}, mid)
            if applied_compare.get("isError"):
                return applied_compare
            planned = _bridge_call(self, "plan_change", {"steps": [step]}, mid)
            if planned.get("isError"):
                return planned
        else:
            return planned
    text = planned["content"][0]["text"]
    confirmer = getattr(self, "confirmer", None)
    op = step.get("op")
    if confirmer is None or not confirmer(text):
        label = f" op {op}" if op else ""
        return B.error_result(
            f"game-editor: {name}{label} was not approved. Nothing was sent to the Godot editor.")
    plan_id = _plan_id_of(planned)
    applied = _bridge_call(self, "apply_plan", {"plan_id": plan_id}, mid)
    first = (applied.get("content") or [{}])[0].get("text", "") if isinstance(applied, dict) else ""
    if not applied.get("isError") and "needs a second approval" in first:
        self.second_approvals.append(first)
        return _bridge_call(self, "confirm_risky_plan", {"plan_id": plan_id}, mid)
    return applied


B.Bridge.call = _testing_call

_bridge_init = B.Bridge.__init__


def prepare_save(bridge, steps):
    """Run scene_do op compare when a plan saves the scene that is open now. Returns an error reply, or None."""
    for step in steps:
        if step.get("tool") == "scene_do" and step.get("op") in ("open_scene", "new_scene", "revert_scene"):
            return None
        if step.get("tool") == "scene_do" and step.get("op") == "save":
            break
        if step.get("op") == "play_session" and step.get("autosave") is True:
            break
    else:
        return None
    compared = B.Bridge.call(bridge, "plan_change", {"steps": [{"tool": "scene_do", "op": "compare"}]})
    if compared.get("isError"):
        return compared
    compare_id = _plan_id_of(compared)
    applied = B.Bridge.call(bridge, "apply_plan", {"plan_id": compare_id})
    text = (applied.get("content") or [{}])[0].get("text", "") if isinstance(applied, dict) else ""
    if not applied.get("isError") and "needs a second approval" in text:
        bridge.second_approvals.append(text)
        applied = B.Bridge.call(bridge, "confirm_risky_plan", {"plan_id": compare_id})
    return applied if applied.get("isError") else None


def apply_one(bridge, step):
    """plan_change one step and apply it. A second approval is confirmed, as Bridge.call does for a write."""
    planned = _bridge_call(bridge, "plan_change", {"steps": [step]})
    if planned.get("isError"):
        return planned
    plan_id = _plan_id_of(planned)
    applied = _bridge_call(bridge, "apply_plan", {"plan_id": plan_id})
    text = (applied.get("content") or [{}])[0].get("text", "") if isinstance(applied, dict) else ""
    if not applied.get("isError") and "needs a second approval" in text:
        bridge.second_approvals.append(text)
        return _bridge_call(bridge, "confirm_risky_plan", {"plan_id": plan_id})
    return applied


def _testing_init(self, *args, **kwargs):
    """Records the second-approval texts apply_plan returned (the helper then calls confirm_risky_plan)."""
    _bridge_init(self, *args, **kwargs)
    self.second_approvals = []


B.Bridge.__init__ = _testing_init

# This plugin does not read HERMES_HOME. A shell value must not change a test.
os.environ.pop("HERMES_HOME", None)

LEVEL = """[gd_scene load_steps=3 format=3]

[ext_resource type="PackedScene" path="res://actors/enemy.tscn" id="1"]
[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]

[node name="Level" type="Node2D"]

[node name="Enemies" type="Node2D" parent="."]

[node name="Enemy3" parent="Enemies" instance=ExtResource("1")]
position = Vector2(1, 2)

[node name="Enemy6" parent="Enemies" instance=ExtResource("1")]

[node name="Sprite" parent="Enemies/Enemy6" index="0"]
position = Vector2(7, 7)

[node name="Waypoint" type="Node2D" parent="Enemies/Enemy3"]

[node name="RoomA" parent="." instance=ExtResource("2")]

[node name="RoomB" parent="." instance=ExtResource("2")]

[editable path="Enemies/Enemy6"]

[editable path="RoomA"]

[editable path="RoomB"]

[editable path="RoomB/Guard"]
"""
# The editor tree that matches LEVEL. A shorter list is a real difference, not an empty scene.
LEVEL_NODES = ["/Level", "/Level/Enemies", "/Level/Enemies/Enemy3", "/Level/Enemies/Enemy3/Sprite",
               "/Level/Enemies/Enemy3/Waypoint", "/Level/Enemies/Enemy6", "/Level/Enemies/Enemy6/Sprite",
               "/Level/RoomA", "/Level/RoomA/Guard", "/Level/RoomA/Guard/Sprite", "/Level/RoomB",
               "/Level/RoomB/Guard", "/Level/RoomB/Guard/Sprite"]
ROOM = """[gd_scene load_steps=2 format=3]

[ext_resource type="PackedScene" path="res://actors/enemy.tscn" id="1"]

[node name="Room" type="Node2D"]

[node name="Guard" parent="." instance=ExtResource("1")]
"""
PREMISE = """[gd_scene load_steps=3 format=3]

[ext_resource type="PackedScene" path="res://actors/room.tscn" id="1"]

[sub_resource type="RectangleShape2D" id="shape1"]
size = Vector2(99, 99)

[node name="Main" type="Node2D" groups=["fromdisk"]]

[node name="Label" type="Label" parent="."]
theme_override_colors/font_color = Color(1, 0, 0, 1)
theme_override_font_sizes/font_size = 32
metadata/_tags = PackedStringArray("red")

[node name="Shape" type="CollisionShape2D" parent="."]
shape = SubResource("shape1")

[node name="Keeper" type="Node" parent="."]

[node name="Ground" type="ColorRect" parent="."]
offset_left = -1000.0
offset_top = -20.0
offset_right = 1000.0
offset_bottom = 20.0

[node name="Room" parent="." instance=ExtResource("1")]

[connection signal="ready" from="." to="Keeper" method="_on_ready"]
"""
ENEMY = """[gd_scene format=3]

[node name="Enemy" type="CharacterBody2D"]

[node name="Sprite" type="Sprite2D" parent="."]
"""


def first_sentence(text: str) -> str:
    text = " ".join(text.split())
    match = re.search(r"(?<!\be\.g)(?<!\bi\.e)(?<!\betc)[.!?](?=\s|$)", text)
    return text[: match.end()] if match else text


class Project:
    """A temporary project folder with a level and an enemy scene."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        (self.root / "scenes").mkdir(parents=True)
        (self.root / "actors").mkdir()
        (self.root / ".godot").mkdir()
        (self.root / "scenes" / "level.tscn").write_text(LEVEL)
        (self.root / "actors" / "enemy.tscn").write_text(ENEMY)
        (self.root / "actors" / "room.tscn").write_text(ROOM)
        (self.root / "actors" / "enemy.gd").write_text("extends Node\n")
        (self.root / "actors" / "enemy.gd.uid").write_text("uid://cenemygd\n")
        (self.root / ".godot" / "export_credentials.cfg").write_text("x")

    def close(self):
        self.tmp.cleanup()


def _disk_editor(project: Path, scene: str) -> tuple[dict, dict, dict, dict]:
    """Editor values that match the .tscn, so a scene that agrees with disk still saves."""
    rel = scene[len("res://"):] if scene.startswith("res://") else ""
    path = project.joinpath(*rel.split("/")) if rel else None
    if path is None or not path.is_file():
        return {}, {}, {}, {}
    parsed = B.parse_tscn(path.read_text(encoding="utf-8"))
    root_name = parsed["root"] or ""
    props: dict[str, dict] = {}
    resources: dict[str, dict] = {}

    def add(node_path: str, raw_props: dict) -> None:
        for key, raw in raw_props.items():
            value = B.parse_value(raw, parsed["ext"])
            if isinstance(value, tuple) and len(value) == 2 and value[0] == "sub":
                sub = (parsed.get("subs") or {}).get(value[1]) or {}
                body = {}
                for sub_key, sub_raw in (sub.get("props") or {}).items():
                    sub_value = B.parse_value(sub_raw, parsed["ext"])
                    if sub_value is not None:
                        body[sub_key] = sub_value
                resources[f"{node_path} {key}"] = {"type": sub.get("type", ""), "properties": body}
            elif value is not None:
                props.setdefault(node_path, {})[key] = value

    if parsed["root_props"]:
        add(f"/{root_name}", parsed["root_props"])
    for rel_path, info in parsed["nodes"].items():
        if info["props"]:
            add(f"/{root_name}/{rel_path}", info["props"])
    groups = {f"/{root_name}": list(parsed.get("root_groups") or [])}
    for rel_path, names in (parsed.get("groups") or {}).items():
        groups[f"/{root_name}/{rel_path}"] = list(names)
    signals: dict[str, list] = {}
    for item in parsed.get("connections") or []:
        signals.setdefault(item["from"], []).append(
            {"signal": item["signal"], "target": item["to"], "method": item["method"]})
    return props, groups, signals, resources


class FakeChild:
    def __init__(self, mode: str = "normal", project: Path | None = None, scene: str = "",
                 nodes: list[str] | None = None, props: dict | None = None, groups: dict | None = None,
                 signals: dict | None = None, resources: dict | None = None, game_status: str | None = None,
                 screenshot: str | None = None, screenshot_2d: str | None = None, layout: dict | None = None,
                 node_types: dict | None = None, clobber: dict | None = None,
                 open_scenes: list | None = None, play_touch: list | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = Path(self.tmp.name) / "calls.jsonl"
        env = dict(os.environ)
        for key in ("FAKE_PROJECT_PATH", "FAKE_SCENE", "FAKE_NODES", "FAKE_PROPS", "FAKE_GROUPS",
                    "FAKE_SIGNALS", "FAKE_RESOURCES", "FAKE_GAME_STATUS", "FAKE_SCREENSHOT", "FAKE_SCREENSHOT_2D",
                    "FAKE_LAYOUT", "FAKE_NODE_TYPES", "FAKE_SAVE_CLOBBER", "FAKE_OPEN_SCENES", "FAKE_PLAY_TOUCH"):
            env.pop(key, None)
        disk_props, disk_groups, disk_signals, disk_resources = ({}, {}, {}, {})
        if project and scene:
            disk_props, disk_groups, disk_signals, disk_resources = _disk_editor(project, scene)
        for path, fields in (props or {}).items():
            disk_props.setdefault(path, {}).update(fields)
        if groups:
            disk_groups.update(groups)
        if signals is not None:
            disk_signals.update(signals)
        if resources:
            disk_resources.update(resources)
        if layout is not None:
            env["FAKE_LAYOUT"] = json.dumps(layout)
            for path, fields in layout.items():
                bucket = disk_props.get(path)
                if isinstance(bucket, dict):
                    for key in fields:
                        bucket.pop(key, None)
        env["FAKE_PROPS"] = json.dumps(disk_props)
        env["FAKE_GROUPS"] = json.dumps(disk_groups)
        env["FAKE_SIGNALS"] = json.dumps(disk_signals)
        env["FAKE_RESOURCES"] = json.dumps(disk_resources)
        if project:
            env["FAKE_PROJECT_PATH"] = str(project)
        env["FAKE_SCENE"] = scene
        env["FAKE_NODES"] = json.dumps(nodes or [])
        if game_status:
            env["FAKE_GAME_STATUS"] = game_status
        if screenshot:
            env["FAKE_SCREENSHOT"] = screenshot
        if screenshot_2d:
            env["FAKE_SCREENSHOT_2D"] = screenshot_2d
        if node_types:
            env["FAKE_NODE_TYPES"] = json.dumps(node_types)
        if clobber:
            env["FAKE_SAVE_CLOBBER"] = json.dumps(clobber)
        if open_scenes:
            env["FAKE_OPEN_SCENES"] = json.dumps(open_scenes)
        if play_touch:
            env["FAKE_PLAY_TOUCH"] = json.dumps(play_touch)
        self.bridge = B.Bridge(argv=[sys.executable, "-I", str(FAKE), str(self.calls), mode], env=env)
        self.bridge.confirmer = lambda _message: True

    def sent(self) -> list[dict]:
        if not self.calls.exists():
            return []
        rows = [json.loads(line) for line in self.calls.read_text().splitlines() if line.strip()]
        lookups = {("session_manage", "list"), ("editor_state", None), ("scene_get_hierarchy", None),
                   ("scene_manage", "get_roots")}
        rows = [r for r in rows if (r["name"], (r.get("arguments") or {}).get("op")) not in lookups]

        def hidden(row: dict) -> bool:
            args = row.get("arguments") or {}
            if row["name"] == "node_get_properties" and "fields" in args and str(args.get("path", "")).startswith("/Level"):
                return True
            params = args.get("params") or {}
            path = str(params.get("path") or params.get("node_path") or "")
            if not path.startswith("/Level"):
                return False
            if row["name"] == "node_manage" and args.get("op") == "get_groups":
                return True
            if row["name"] == "signal_manage" and args.get("op") == "list":
                return True
            return row["name"] == "resource_manage" and args.get("op") == "inspect"

        return [r for r in rows if not hidden(r)]

    def close(self):
        self.bridge.shutdown()
        self.tmp.cleanup()


class TableTest(unittest.TestCase):
    def test_every_upstream_operation_is_reachable_or_explained(self):
        counts = B.TABLE["counts"]
        self.assertEqual(counts["upstream_ops"], 161)
        self.assertEqual(counts["upstream_single_call_tools"], 19)
        self.assertEqual(counts["reachable"], 178)
        self.assertEqual(len(B.TABLE["left_out"]), 5)
        self.assertEqual(counts["reachable"] + len(B.TABLE["left_out"]), 183)
        offered = {(spec["upstream"], spec["op"]) for tool in B.TABLE["tools"] for spec in tool["ops"].values()}
        self.assertEqual(len(offered), 178)
        self.assertNotIn(("custom_manage", "invoke"), offered)
        for item in B.TABLE["left_out"]:
            self.assertTrue(item["why"])

    def test_client_status_and_permanent_delete(self):
        offered = {(spec["upstream"], spec["op"]) for tool in B.TABLE["tools"] for spec in tool["ops"].values()}
        self.assertNotIn(("client_manage", "status"), offered)
        self.assertNotIn(("client_manage", "configure"), offered)
        self.assertNotIn("permanent", B.TOOLS["file_do"]["ops"]["delete_file"]["keys"])
        self.assertEqual(B.TOOLS["file_do"]["ops"]["delete_file_permanently"]["fixed"], {"permanent": True})
        self.assertEqual(B.TOOLS["file_do"]["tier"], "destructive")

    def test_tool_describe_carries_godot_ai_notes(self):
        for tool in B.TABLE["tools"]:
            for spec in tool["ops"].values():
                self.assertTrue(spec["doc"], (tool["name"], spec["sig"]))
        visuals = " ".join(spec["doc"] for spec in B.TOOLS["asset_do"]["ops"].values()
                           if spec.get("guard") == "edit_material")
        self.assertIn('"standard" | "orm" | "canvas_item" | "shader"', visuals)
        self.assertNotIn("edit_visuals", B.TOOLS)
        self.assertIn("create_node", B.TOOLS["batch"]["properties"]["commands"]["description"])

    def test_tiers_and_annotations(self):
        defs = {d["name"]: d for d in B.tool_definitions()}
        published = {"scene_look", "script_look", "file_look", "project_look",
                     "plan_change", "apply_plan", "confirm_risky_plan", "change_guide"}
        self.assertEqual(set(defs), published)
        self.assertIs(defs["confirm_risky_plan"]["annotations"]["readOnlyHint"], False)
        self.assertIs(defs["change_guide"]["annotations"]["readOnlyHint"], True)
        self.assertNotIn("scene_do", defs)
        self.assertNotIn("editor_look", defs)
        self.assertNotIn("asset_look", defs)
        # Disk reads list stored values. A live property read, including a held resource, is a plan step.
        self.assertIn("properties", defs["scene_look"]["inputSchema"]["properties"]["op"]["enum"])
        self.assertNotIn("resource", defs["scene_look"]["inputSchema"]["properties"]["op"]["enum"])
        self.assertIs(defs["plan_change"]["annotations"]["readOnlyHint"], True)
        self.assertNotIn("destructiveHint", defs["plan_change"]["annotations"])
        self.assertIs(defs["apply_plan"]["annotations"]["readOnlyHint"], False)
        self.assertIs(defs["apply_plan"]["annotations"]["destructiveHint"], True)
        for name in ("scene_look", "script_look", "file_look", "project_look", "change_guide", "plan_change"):
            self.assertIs(defs[name]["annotations"]["readOnlyHint"], True, name)
        danger_flags = {"run_code", "write", "delete", "send", "unresolved"}
        for tool in B.TABLE["tools"]:
            found = set()
            for spec in tool["ops"].values():
                row = B._EFFECTS_BY_TOOL.get(spec.get("effect_key")) or {}
                found |= set(row.get("effects") or [])
            if "delete" in found or tool["name"] == "batch":
                self.assertEqual(tool["tier"], "destructive", tool["name"])
            elif found & danger_flags or any(spec.get("elicit") for spec in tool["ops"].values()):
                self.assertEqual(tool["tier"], "change", tool["name"])
            else:
                self.assertEqual(tool["tier"], "read", tool["name"])
        self.assertGreaterEqual(len(B.TABLE["tools"]), 15)
        self.assertLessEqual(len(B.TABLE["tools"]), 25)

    def test_read_tools_only_read(self):
        for tool in B.TABLE["tools"]:
            if tool["tier"] != "read":
                continue
            for spec in tool["ops"].values():
                row = B._EFFECTS_BY_TOOL[spec["effect_key"]]
                self.assertEqual(set(row["effects"]), {"read"}, (tool["name"], spec["effect_key"]))
                self.assertFalse(spec.get("elicit"), (tool["name"], spec["effect_key"]))
                self.assertNotIn("unresolved", row["effects"])
        for pair in (("asset_look", "preset_fade"), ("asset_look", "preset_slide"),
                     ("asset_look", "preset_shake"), ("asset_look", "preset_pulse"),
                     ("asset_look", "particle_restart")):
            self.assertNotIn(pair, B.READ_ALLOW)
        fake = FakeChild()
        try:
            res = fake.bridge.call("asset_look", {"op": "preset_fade"})
            self.assertTrue(res["isError"])
            self.assertIn("Nothing was sent", res["content"][0]["text"])
            self.assertIn("plan_change", res["content"][0]["text"])
            self.assertEqual(fake.sent(), [])
        finally:
            fake.close()

    def test_descriptions_fit_tool_search(self):
        for d in B.tool_definitions():
            self.assertLessEqual(len(d["description"]), 1024, d["name"])
            sentence = first_sentence(d["description"])
            self.assertLessEqual(len(sentence), 60, d["name"])
            self.assertIn("Godot", sentence, d["name"])
            self.assertFalse(sentence.lower().startswith("read-only"), d["name"])
            ask = "Asks the user for approval every time." in d["description"]
            self.assertEqual(ask, not d["annotations"]["readOnlyHint"], d["name"])
            for prop in d["inputSchema"]["properties"].values():
                desc = prop.get("description")
                if isinstance(desc, str):
                    self.assertLessEqual(len(desc), 1024, d["name"])
        listed = json.dumps(B.tool_definitions()).encode("utf-8")
        self.assertLess(len(listed), 80000)
        for name in ("file_look", "scene_look", "script_look"):
            blob = json.dumps(B.TOOLS[name]["properties"])
            self.assertNotIn("write_text", blob)
            self.assertNotIn("remove(", blob)

    def test_schemas_are_closed(self):
        for d in B.tool_definitions():
            schema = d["inputSchema"]
            self.assertIs(schema["additionalProperties"], False)
            if d["name"] not in B.TOOLS or d["name"] in B.DISK_READS:
                self.assertNotIn("session_id", schema["properties"], d["name"])
                self.assertTrue(set(schema["required"]) <= set(schema["properties"]), d["name"])
                continue
            tool = B.TOOLS[d["name"]]
            talks = any(spec["upstream"] not in ("@cli", "@local") for spec in tool["ops"].values())
            if talks:
                self.assertIn("session_id", schema["properties"], d["name"])
            else:
                self.assertNotIn("session_id", schema["properties"], d["name"])
            self.assertTrue(set(schema["required"]) <= set(schema["properties"]), d["name"])


class PathRuleTest(unittest.TestCase):
    def test_names(self):
        refused = ["res://.godot/export_credentials.cfg", "res://export_credentials.cfg", "res://.env",
                   "res://prod.env", "res://keys/release.keystore", "res://a.jks", "res://cert.p12", "res://k.pem",
                   "res://private.key", "res://AuthKey.p8", "res://secrets.json", "res://.git/config",
                   "res://android/key.properties", "res://gradle.properties", "res://Key.Properties",
                   "res://android/build/google-services.json", "res://ios/GoogleService-Info.plist",
                   "res://addons/x/api_keys.cfg", "res://config/db_password.txt", "res://a/../b.gd",
                   "res://release.keystore ", "res://release.keystore.", "res://secrets/config.json",
                   "res://auth_token.txt", "res://keys/access_token.json", "res://a/.../b", "RES://.godot/x"]
        allowed = ["res://player.gd", "res://ui/credentials_screen.tscn", "res://levels/secret_room.tscn",
                   "res://token.gd", "res://notes.txt", "res://main.tscn", "res://art/password_icon.png",
                   "res://data/tokens.json", "res://ui/token_counter.tscn"]
        for path in refused:
            self.assertTrue(B.static_refusal("read_file", path), path)
        for path in allowed:
            self.assertEqual(B.static_refusal("read_file", path), "", path)

    def test_windows_paths(self):
        root = "C:\\Games\\Proj"
        self.assertEqual(B.resolved_refusal("read_file", "res://Scenes/Main.tscn", root, pm=ntpath), "")
        self.assertIn("does not read", B.resolved_refusal("read_file", "res://.GODOT/x.cfg", root, pm=ntpath))
        self.assertIn("does not read", B.resolved_refusal("read_file", "res://Release.KEYSTORE.", root, pm=ntpath))
        self.assertIn("does not read", B.resolved_refusal("read_file", "res://keys\\a.pem ", root, pm=ntpath))
        self.assertTrue(B.static_refusal("read_file", "res://sub\\..\\..\\x"))

    def test_values_at_any_depth_and_with_spaces(self):
        found = B.scheme_strings({"value": [" res://a.tres", {"x": "uid://abc"}], "content": "res://not/a/path",
                                  "n": 3, "texture": "user://save.tres"})
        self.assertEqual(sorted(found), ["res://a.tres", "uid://abc", "user://save.tres"])

    def test_symbolic_links(self):
        p = Project()
        try:
            outside = Path(p.tmp.name) / "outside"
            outside.mkdir()
            (outside / "plain.txt").write_text("x")
            os.symlink(outside / "plain.txt", p.root / "out_link.txt")
            os.symlink(".godot/export_credentials.cfg", p.root / "in_link.txt")
            self.assertIn("outside the project", B.resolved_refusal("read_file", "res://out_link.txt", str(p.root)))
            self.assertIn("resolves to a file", B.resolved_refusal("read_file", "res://in_link.txt", str(p.root)))
            self.assertEqual(B.resolved_refusal("read_file", "res://actors/enemy.gd", str(p.root)), "")
        finally:
            p.close()

    def test_uid_index(self):
        p = Project()
        try:
            self.assertEqual(B.uid_index(str(p.root)).get("uid://cenemygd"), "res://actors/enemy.gd")
        finally:
            p.close()


class SceneRuleTest(unittest.TestCase):
    def test_parse(self):
        scene = B.parse_tscn(LEVEL)
        self.assertEqual(scene["root"], "Level")
        self.assertTrue(scene["nodes"]["Enemies/Enemy3"]["instance"])
        self.assertEqual(scene["nodes"]["RoomA"]["scene"], "res://actors/room.tscn")
        self.assertFalse(scene["nodes"]["Enemies/Enemy6/Sprite"]["local"])
        self.assertEqual(scene["nodes"]["Enemies/Enemy3"]["props"], {"position": "Vector2(1, 2)"})
        premise = B.parse_tscn(PREMISE)
        self.assertEqual(premise["root_groups"], ["fromdisk"])
        self.assertEqual(premise["connections"][0]["to"], "/Main/Keeper")
        self.assertEqual(premise["subs"]["shape1"]["props"]["size"], "Vector2(99, 99)")
        self.assertEqual(B.parse_value('PackedStringArray("red")', {}), ["red"])
        self.assertEqual(B.parse_value("SubResource(\"shape1\")", {}), ("sub", "shape1"))
        self.assertFalse(B._under_instance("Room", premise["nodes"]))
        self.assertTrue(B._under_instance("Room/Guard", premise["nodes"]))
        self.assertEqual(B.parse_value("Vector2(1, 2)", {}), {"x": 1.0, "y": 2.0})
        self.assertTrue(B.same_value({"x": 1.0, "y": 2.0}, {"x": 1.0, "y": 2.0}))
        self.assertFalse(B.same_value({"x": 1.0, "y": 2.0}, {"x": 1.0, "y": 3.0}))
        transform = B.parse_value("Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 4, 5, 6)", {})
        self.assertEqual(transform["origin"], {"x": 4.0, "y": 5.0, "z": 6.0})
        self.assertEqual(transform["basis"]["x"], {"x": 1.0, "y": 0.0, "z": 0.0})
        self.assertEqual(B.parse_value("Rect2(1, 2, 32, 48)", {}),
                         {"position": {"x": 1.0, "y": 2.0}, "size": {"x": 32.0, "y": 48.0}})
        self.assertEqual(B.parse_value("Transform2D(1, 0, 0, 1, 3, 4)", {})["origin"], {"x": 3.0, "y": 4.0})
        self.assertIn("x", B.parse_value("Basis(1, 0, 0, 0, 1, 0, 0, 0, 1)", {}))
        self.assertEqual(B.parse_value("AABB(0, 1, 2, 3, 4, 5)", {})["size"]["z"], 5.0)
        self.assertEqual(B.parse_value("Quaternion(0, 0, 0, 1)", {}),
                         {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0})
        self.assertEqual(B.parse_value("Plane(0, 1, 0, 2)", {})["d"], 2.0)
        keys = B.parse_value('{\n"times": PackedFloat32Array(0, 1),\n"update": 0,\n'
                             '"values": [Vector2(0, 0), Vector2(10, 0)]\n}', {})
        self.assertEqual(keys["values"][1], {"x": 10.0, "y": 0.0})
        self.assertEqual(B.parse_value('{\n&"walk": SubResource("a")\n}', {}), {"walk": ("sub", "a")})
        self.assertTrue(B.same_value(transform, transform))

    def test_test_counts(self):
        gut = "---- Totals ----\nScripts 1\nTests             3\nPassing tests     2\nFailing tests     1\n"
        self.assertEqual(B.read_test_counts("gut", gut), (3, 1))
        self.assertEqual(B.read_test_counts("gdunit4", "Overall Summary: 4 test cases | 0 errors | 0 failures |"),
                         (4, 0))
        self.assertEqual(B.read_test_counts("gut", "no summary"), (None, None))

    def test_cycle(self):
        self.assertEqual(B.instanced_scenes(LEVEL), [("res://actors/enemy.tscn", ""), ("res://actors/room.tscn", "")])


class ArgumentTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeChild()

    def tearDown(self):
        self.fake.close()

    def test_undeclared_arguments_are_refused(self):
        for tool, args in [("editor_look", {"nope": 1}), ("file_look", {"path": "res://a.txt", "x": 1}),
                           ("scene_do", {"op": "delete_node", "path": "/Main/A", "overwrite": True}),
                           ("scene_do", {"op": "rename", "path": "/Main/A", "new_name": "B", "force": 1}),
                           ("asset_do", {}), ("scene_do", {"op": "explode"})]:
            res = self.fake.bridge.call(tool, args)
            self.assertTrue(res["isError"], tool)
            self.assertIn("Nothing was sent", res["content"][0]["text"])
        self.assertEqual(self.fake.sent(), [])

    def test_op_tools_reach_the_declared_operation(self):
        self.fake.bridge.call("scene_do", {"op": "rename", "path": "/Main/A", "new_name": "B"})
        self.fake.bridge.call("scene_do", {"op": "properties", "path": "/Main/A", "fields": ["position"]})
        looked = self.fake.bridge.call("scene_look", {"op": "signals", "path": "/Main/A"})
        sent = self.fake.sent()
        self.assertEqual(sent[0], {"name": "node_manage", "arguments": {"op": "rename",
                                                                        "params": {"path": "/Main/A", "new_name": "B"}}})
        self.assertEqual(sent[1], {"name": "node_get_properties", "arguments": {"path": "/Main/A", "fields": ["position"]}})
        # scene_look signals reads the .tscn. It does not ask the editor.
        self.assertFalse(any(row["name"] == "signal_manage" for row in sent))
        self.assertTrue(looked["isError"])  # this fake has no project folder

    def test_upstream_error_passes_through(self):
        res = self.fake.bridge.call("scene_do", {"op": "properties", "path": "/Main/NoSuchNode"})
        self.assertTrue(res["isError"])
        self.assertTrue(res["content"][0]["text"].startswith("NODE_NOT_FOUND: Node not found: /Main/NoSuchNode."))

    def test_run_game_does_not_save_by_default(self):
        fake = FakeChild(game_status="live")
        try:
            res = fake.bridge.call("project_do", {"op": "run_game", "mode": "main"})
            self.assertEqual(fake.sent()[-1]["arguments"]["autosave"], False)
            self.assertIn("autosave was false", res["content"][-1]["text"])
            self.assertTrue(res["content"][0]["text"].startswith("game-editor did: run_game"))
            self.assertFalse(res["isError"])
        finally:
            fake.close()

    def test_batch_only_scene_edits(self):
        res = self.fake.bridge.call("batch", {"op": "batch_execute", "commands": [{"command": "write_file", "params": {}}]})
        self.assertTrue(res["isError"])
        self.assertIn("does not run write_file", res["content"][0]["text"])

    def test_decline_shows_the_change_and_does_not_run_it(self):
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn",
                         nodes=["/Level", "/Level/Enemies", "/Level/Enemies/Enemy6"])
        seen = []
        fake.bridge.confirmer = lambda message: seen.append(message) or False
        try:
            res = fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Level/Enemies/Enemy6"})
            self.assertTrue(res["isError"])
            self.assertIn("not approved", res["content"][0]["text"])
            self.assertEqual(len(seen), 1)
            self.assertIn("delete_node", seen[0])
            self.assertIn("/Level/Enemies/Enemy6", seen[0])
            self.assertNotIn("...", seen[0])
            self.assertFalse(any(row["name"] == "node_manage" and (row.get("arguments") or {}).get("op") == "delete"
                                 for row in fake.sent()))
        finally:
            fake.close()
            project.close()

    def test_long_script_is_planned_whole_and_then_sent(self):
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn")
        seen = []
        fake.bridge.confirmer = lambda message: seen.append(message) or True
        body = "x" * 4000
        try:
            res = fake.bridge.call("script_do", {"op": "create_script", "path": "res://new_long.gd",
                                                 "content": body})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertEqual(len(seen), 1)
            self.assertIn(body, seen[0])
            self.assertNotIn("was not shown cut", seen[0])
            created = [row for row in fake.sent() if row["name"] == "script_create"]
            self.assertEqual(len(created), 1)
            self.assertIn(body, json.dumps(created[0]))
        finally:
            fake.close()
            project.close()

    def test_percent_encoded_and_quoted_paths_are_refused(self):
        encoded = self.fake.bridge.call("file_look", {"op": "text", "path": "res://a%2e%2e/secret.gd"})
        self.assertTrue(encoded["isError"])
        self.assertIn("percent-encoding", encoded["content"][0]["text"])
        quoted = self.fake.bridge.call("file_look", {"op": "text", "path": '"res://player.gd"'})
        self.assertTrue(quoted["isError"])
        self.assertIn("quoted path", quoted["content"][0]["text"])
        self.assertEqual(self.fake.sent(), [])

    def test_mcp_decline_does_not_forward(self):
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn",
                         nodes=["/Level", "/Level/Enemies", "/Level/Enemies/Enemy6"])
        fake.bridge.confirmer = None
        messages = []
        fake.bridge.write = lambda msg: messages.append(msg)
        try:
            res = fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Level/Enemies/Enemy6"})
            self.assertTrue(res["isError"])
            self.assertIn("not approved", res["content"][0]["text"])
            self.assertFalse(any(msg.get("method") == "elicitation/create" for msg in messages))
            self.assertFalse(any(row["name"] == "node_manage" and (row.get("arguments") or {}).get("op") == "delete"
                                 for row in fake.sent()))
        finally:
            fake.close()
            project.close()

    def test_zero_result_note(self):
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn")
        try:
            res = fake.bridge.call("scene_look", {"op": "find_nodes", "type": "camera2d"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("Camera2D", res["content"][-1]["text"])
            res = fake.bridge.call("scene_look", {"op": "find_nodes", "name": "Nope"})
            note = res["content"][-1]["text"]
            self.assertTrue(note.startswith("game-editor note: 0 results. "))
            self.assertNotIn(". .", note)
            self.assertEqual(fake.sent(), [])
        finally:
            fake.close()
            project.close()


class ProjectCallTest(unittest.TestCase):
    def setUp(self):
        self.p = Project()

    def tearDown(self):
        self.p.close()

    def fake(self, **kw):
        return FakeChild(project=self.p.root, scene="res://scenes/level.tscn", **kw)

    def test_paths_are_checked_before_any_call(self):
        fake = self.fake()
        try:
            for tool, args in [("file_look", {"path": "res://.godot/export_credentials.cfg"}),
                               ("scene_do", {"op": "set_node_property", "path": "/Level/Enemies", "property": "texture",
                                             "value": [" res://.godot/x.tres"]}),
                               ("scene_do", {"op": "set_node_property", "path": "/Level/Enemies", "property": "texture",
                                             "value": "user://save.tres"}),
                               ("scene_do", {"op": "open_scene", "path": "uid://notinproject"})]:
                res = fake.bridge.call(tool, args)
                self.assertTrue(res["isError"], (tool, args))
            res = fake.bridge.call("script_look", {"op": "source", "path": "uid://cenemygd"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertEqual(fake.sent(), [])
            self.assertIn("extends Node", res["content"][1]["text"])
        finally:
            fake.close()

    def test_instanced_children_are_refused(self):
        fake = self.fake()
        try:
            refused = ["/Level/Enemies/Enemy3/Sprite", "/Level/RoomA/Guard/Sprite", "/Level/Enemies/Enemy3/Waypoint/../Sprite"]
            for path in refused[:2]:
                res = fake.bridge.call("scene_do", {"op": "set_node_property", "path": path, "property": "position",
                                                             "value": {"x": 1, "y": 1}})
                self.assertTrue(res["isError"], path)
                self.assertIn("Editable Children", res["content"][0]["text"])
            for tool, args in [("scene_do", {"op": "delete_node", "path": "/Level/Enemies/Enemy3/Sprite"}),
                               ("scene_do", {"op": "reparent", "path": "/Level/Enemies/Enemy3/Sprite",
                                             "new_parent": "/Level"}),
                               ("batch", {"op": "batch_execute", "commands": [{"command": "set_property", "params": {
                                   "path": "/Level/RoomA/Guard/Sprite", "property": "position", "value": 1}}]})]:
                self.assertTrue(fake.bridge.call(tool, args)["isError"], tool)
            for path in ["/Level/Enemies/Enemy6/Sprite", "/Level/RoomA/Guard", "/Level/RoomB/Guard/Sprite",
                         "/Level/Enemies/Enemy3", "/Level/Enemies/Enemy3/Waypoint"]:
                ok = fake.bridge.call("scene_do", {"op": "set_node_property", "path": path, "property": "position",
                                                            "value": {"x": 1, "y": 1}})
                self.assertFalse(ok["isError"], path)
            self.assertEqual(len([c for c in fake.sent() if c["name"] == "node_set_property"]), 5)
        finally:
            fake.close()

    def test_prefab_structure_and_stored_properties(self):
        fake = self.fake()
        try:
            deleted = fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Level/Enemies/Enemy6/Sprite"})
            self.assertTrue(deleted["isError"])
            self.assertIn("comes back", deleted["content"][0]["text"])
            self.assertIn("open_scene res://actors/enemy.tscn", deleted["content"][0]["text"])
            self.assertFalse(fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Level/Enemies/Enemy3/Waypoint"})["isError"])
            moved = fake.bridge.call("scene_do", {"op": "reparent", "path": "/Level/Enemies/Enemy6/Sprite",
                                                   "new_parent": "/Level"})
            self.assertTrue(moved["isError"])
            self.assertIn("second copy", moved["content"][0]["text"])
            kept = fake.bridge.call("scene_do", {"op": "reparent", "path": "/Level/Enemies/Enemy6/Sprite",
                                                  "new_parent": "/Level/Enemies/Enemy6"})
            self.assertTrue(kept["isError"])
            self.assertIn("second copy", kept["content"][0]["text"])
            self.assertIn("inside the instance", kept["content"][0]["text"])
            renamed = fake.bridge.call("scene_do", {"op": "rename", "path": "/Level/Enemies/Enemy6/Sprite",
                                                     "new_name": "Torso"})
            self.assertTrue(renamed["isError"])
            self.assertIn("old name comes back", renamed["content"][0]["text"])
            plain = fake.bridge.call("scene_do", {"op": "rename", "path": "/Level/Enemies/Enemy3/Waypoint",
                                                   "new_name": "Mark"})
            self.assertFalse(plain["isError"], plain["content"][0]["text"])
            off = fake.bridge.call("scene_do", {"op": "connect", "path": "/Level/Enemies/Enemy3/Sprite", "signal": "ready",
                                                      "target": "/Level", "method": "_on_ready"})
            self.assertTrue(off["isError"])
            self.assertIn("does not save a connection", off["content"][0]["text"])
            self.assertNotIn("part of the instanced scene", off["content"][0]["text"])
            on = fake.bridge.call("scene_do", {"op": "connect", "path": "/Level/Enemies/Enemy6/Sprite", "signal": "ready",
                                                     "target": "/Level", "method": "_on_ready"})
            self.assertFalse(on["isError"], on["content"][0]["text"])
            root_sig = fake.bridge.call("scene_do", {"op": "connect", "path": "/Level/Enemies/Enemy3", "signal": "ready",
                                                           "target": "/Level", "method": "_on_ready"})
            self.assertFalse(root_sig["isError"], root_sig["content"][0]["text"])
            under = fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Extra",
                                                  "parent_path": "/Level/Enemies/Enemy3/Sprite"})
            self.assertTrue(under["isError"])
            self.assertIn("Editable Children", under["content"][0]["text"])
            root_add = fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Extra",
                                                     "parent_path": "/Level/Enemies/Enemy3"})
            self.assertFalse(root_add["isError"])
            child = fake.bridge.call("batch", {"op": "batch_execute", "commands": [{"command": "delete_node",
                                                                     "params": {"path": "/Level/Enemies/Enemy6/Sprite"}}]})
            self.assertTrue(child["isError"])
            self.assertIn("comes back", child["content"][0]["text"])
            detached = fake.bridge.call("batch", {"op": "batch_execute", "commands": [{"command": "detach_script",
                                                                        "params": {"path": "/Level/Enemies"}}]})
            self.assertFalse(detached["isError"], detached["content"][0]["text"])
            moved = fake.bridge.call("scene_do", {"op": "move", "path": "/Level/Enemies/Enemy6/Sprite", "index": 1})
            self.assertTrue(moved["isError"])
            self.assertIn("order", moved["content"][0]["text"])
            self.assertIn("prefab scene", moved["content"][0]["text"])
            grouped = fake.bridge.call("scene_do", {"op": "add_to_group", "path": "/Level/Enemies/Enemy6/Sprite",
                                                     "group": "mobs"})
            self.assertFalse(grouped["isError"], grouped["content"][0]["text"])
            ungrouped = fake.bridge.call("scene_do", {"op": "remove_from_group",
                                                       "path": "/Level/Enemies/Enemy6/Sprite", "group": "mobs"})
            self.assertFalse(ungrouped["isError"], ungrouped["content"][0]["text"])
            off_group = fake.bridge.call("scene_do", {"op": "add_to_group",
                                                       "path": "/Level/Enemies/Enemy3/Sprite", "group": "mobs"})
            self.assertTrue(off_group["isError"])
            self.assertIn("Editable Children", off_group["content"][0]["text"])
            self.assertFalse(any(row["name"] == "node_manage" and (row.get("arguments") or {}).get("op") == "move"
                                 for row in fake.sent()))
            root_delete = fake.bridge.call("batch", {"op": "batch_execute", "commands": [{"command": "delete_node",
                                                                           "params": {"path": "/Level/Enemies/Enemy6"}}]})
            self.assertFalse(root_delete["isError"], root_delete["content"][0]["text"])
            alone = fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Level/Enemies/Enemy6"})
            self.assertFalse(alone["isError"], alone["content"][0]["text"])
            sized = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite", "property": "size",
                                                           "value": {"x": 180, "y": 36}})
            self.assertFalse(sized["isError"], sized["content"][0]["text"])
        finally:
            fake.close()

    def test_nested_editable_inside_the_prefab_is_allowed(self):
        room = self.p.root / "actors" / "room.tscn"
        room.write_text(ROOM + '\n[editable path="Guard"]\n')
        level = self.p.root / "scenes" / "level.tscn"
        text = LEVEL.replace('[editable path="RoomA"]\n', "").replace('[editable path="RoomB/Guard"]\n', "")
        level.write_text(text)
        fake = self.fake()
        try:
            ok = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/RoomB/Guard/Sprite", "property": "position",
                                                        "value": {"x": 6, "y": 6}})
            self.assertFalse(ok["isError"], ok["content"][0]["text"])
            refused = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/RoomA/Guard/Sprite",
                                                             "property": "position", "value": {"x": 1, "y": 1}})
            self.assertTrue(refused["isError"])
        finally:
            fake.close()

    def test_non_stored_instance_property_is_refused(self):
        fake = self.fake()
        try:
            original = fake.bridge.upstream

            def upstream(name, arguments, timeout=None, mid=None):
                fields = arguments.get("fields") if name == "node_get_properties" else None
                table = {
                    "playing": {"node_type": "AudioStreamPlayer2D",
                                "properties": [{"name": "playing", "value": False, "usage": 2}]},
                    "current_animation": {"node_type": "AnimationPlayer",
                                          "properties": [{"name": "current_animation", "value": ""}]},
                    "frame_coords": {"node_type": "Sprite2D",
                                     "properties": [{"name": "frame_coords", "value": {"x": 0, "y": 0}}]},
                    "position": {"node_type": "MeshInstance3D",
                                 "properties": [{"name": "position", "value": {"x": 0, "y": 0, "z": 0}}]},
                    "text": {"node_type": "Label", "properties": [{"name": "text", "value": "hi"}]},
                    "hidden": {"node_type": "Sprite2D", "properties": [], "unknown_fields": ["hidden"]},
                    "scratch": {"node_type": "Sprite2D", "properties": [{"name": "texture", "value": ""}]},
                }
                if isinstance(fields, list) and len(fields) == 1 and fields[0] in table:
                    return {"content": [{"type": "text", "text": json.dumps(table[fields[0]])}], "isError": False}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            for prop in ("playing", "current_animation"):
                refused = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                                 "property": prop, "value": 1})
                self.assertTrue(refused["isError"], prop)
                self.assertIn("does not store", refused["content"][0]["text"])
                self.assertIn("ClassDB", refused["content"][0]["text"])
            frame = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                           "property": "frame_coords", "value": {"x": 1, "y": 0}})
            # Allowed and sent; the stubbed read keeps (0, 0), so the read-back says Godot did not keep it.
            self.assertTrue(any((row.get("arguments") or {}).get("property") == "frame_coords" for row in fake.sent()))
            if frame["isError"]:
                self.assertIn("read back after the step", frame["content"][0]["text"])
            moved = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                           "property": "position", "value": {"x": 1, "y": 2, "z": 3}})
            # Allowed and sent; the stubbed read keeps (0, 0, 0), so the read-back reports it.
            self.assertTrue(any((row.get("arguments") or {}).get("property") == "position" for row in fake.sent()))
            if moved["isError"]:
                self.assertIn("read back after the step", moved["content"][0]["text"])
            hidden = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                            "property": "hidden", "value": 1})
            self.assertTrue(hidden["isError"])
            self.assertIn("not an editor-visible", hidden["content"][0]["text"])
            kept = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                          "property": "text", "value": "hi"})
            self.assertFalse(kept["isError"], kept["content"][0]["text"])
            missing = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                             "property": "scratch", "value": 1})
            self.assertTrue(missing["isError"])
            self.assertIn("not an editor-visible", missing["content"][0]["text"])
            sent_props = [row["arguments"].get("property") for row in fake.sent() if row["name"] == "node_set_property"]
            self.assertEqual(sent_props, ["frame_coords", "position", "text"])
        finally:
            fake.close()

    def test_set_property_does_not_compare_the_whole_scene_first(self):
        fake = self.fake(nodes=self.EDITOR)
        try:
            fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies", "property": "position",
                                                   "value": {"x": 3, "y": 4}})
            raw = [json.loads(line) for line in fake.calls.read_text().splitlines() if line.strip()]
            names = [row["name"] for row in raw]
            # The plan fingerprint walks the tree shape only. No other node's values are read.
            read_paths = {row["arguments"].get("path") for row in raw if row["name"] == "node_get_properties"}
            self.assertLessEqual(read_paths, {"/Level/Enemies"})
            self.assertIn("node_set_property", names)
        finally:
            fake.close()

    def test_read_file_hides_an_old_script_encryption_key(self):
        cfg = self.p.root / "export_presets.cfg"
        cfg.write_text('[preset.0]\n\nscript_encryption_key="abcd1234"\nname="Web"\n'
                       'keystore/release_password="hunter2"\npassword="hunter2"\n')
        fake = self.fake()
        try:
            original = fake.bridge.upstream

            def upstream(name, arguments, timeout=None, mid=None):
                if name == "filesystem_manage":
                    return {"content": [{"type": "text", "text": cfg.read_text()}], "isError": False}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            res = fake.bridge.call("file_look", {"op": "text", "path": "res://export_presets.cfg"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("editor was not asked", res["content"][0]["text"])
            body = json.loads(res["content"][1]["text"])["text"]
            self.assertNotIn("abcd1234", body)
            self.assertNotIn("hunter2", body)
            self.assertIn('script_encryption_key=""', body)
            self.assertIn('keystore/release_password=""', body)
            self.assertIn('password=""', body)
            self.assertIn("abcd1234", cfg.read_text())
            self.assertIn("hunter2", cfg.read_text())
        finally:
            fake.close()

    def test_instances_added_in_this_session(self):
        fake = self.fake()
        try:
            fake.bridge.call("scene_do", {"op": "add_node", "scene_path": "res://actors/enemy.tscn", "name": "New", "parent_path": "/Level"})
            self.assertTrue(fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/New/Sprite", "property": "x",
                                                                   "value": 1})["isError"])
            self.assertFalse(fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/New", "property": "x",
                                                                    "value": 1})["isError"])
            fake.bridge.call("batch", {"op": "batch_execute", "commands": [{"command": "create_node", "params": {
                "scene_path": "res://actors/enemy.tscn", "name": "B", "parent_path": "/Level"}}]})
            self.assertTrue(fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/B/Sprite", "property": "x",
                                                                   "value": 1})["isError"])
            fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Plain", "parent_path": "/Level/New"})
            self.assertFalse(fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/New/Plain", "property": "x",
                                                                    "value": 1})["isError"])
        finally:
            fake.close()

    def test_inherited_disconnect_comes_back(self):
        (self.p.root / "actors" / "enemy.tscn").write_text(
            ENEMY + '\n[connection signal="ready" from="." to="." method="_on_ready"]\n')
        fake = self.fake()
        try:
            res = fake.bridge.call("scene_do", {"op": "disconnect", "path": "/Level/Enemies/Enemy6",
                                                      "signal": "ready", "target": "/Level/Enemies/Enemy6",
                                                      "method": "_on_ready"})
            self.assertTrue(res["isError"], res["content"][0]["text"])
            self.assertIn("brings it back", res["content"][0]["text"])
            self.assertFalse(any(row["name"] == "signal_manage" and (row.get("arguments") or {}).get("op") == "disconnect"
                                 for row in fake.sent()))
        finally:
            fake.close()

    def test_export_storage_on_the_defining_script_is_allowed(self):
        (self.p.root / "actors" / "enemy.gd").write_text("extends Sprite2D\n@export_storage var hidden\n")
        (self.p.root / "actors" / "enemy.tscn").write_text(
            '[gd_scene load_steps=2 format=3]\n\n'
            '[ext_resource type="Script" path="res://actors/enemy.gd" id="1"]\n\n'
            '[node name="Enemy" type="CharacterBody2D"]\n\n'
            '[node name="Sprite" type="Sprite2D" parent="."]\n'
            'script = ExtResource("1")\n')
        fake = self.fake()
        try:
            original = fake.bridge.upstream

            def upstream(name, arguments, timeout=None, mid=None):
                if name == "node_get_properties" and arguments.get("fields") == ["hidden"]:
                    return {"content": [{"type": "text", "text": json.dumps(
                        {"properties": [], "unknown_fields": ["hidden"], "node_type": "Sprite2D"})}],
                        "isError": False}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            ok = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                        "property": "hidden", "value": 1})
            self.assertFalse(ok["isError"], ok["content"][0]["text"])
            self.assertIn("hidden", [row["arguments"].get("property") for row in fake.sent()
                                     if row["name"] == "node_set_property"])
        finally:
            fake.close()

    def test_batch_did_line_names_each_command(self):
        fake = self.fake()
        try:
            res = fake.bridge.call("batch", {"op": "batch_execute", "commands": [
                {"command": "set_property", "params": {"path": "/Level/Enemies", "property": "position",
                                                       "value": {"x": 1, "y": 2}}},
                {"command": "rename_node", "params": {"path": "/Level/Enemies/Enemy3/Waypoint", "new_name": "Mark"}},
            ]})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            line = res["content"][0]["text"]
            self.assertIn("set_property /Level/Enemies position", line)
            self.assertIn("rename_node /Level/Enemies/Enemy3/Waypoint", line)
            self.assertNotIn("commands", line)
        finally:
            fake.close()

    def test_ports_come_from_editor_settings(self):
        self.assertEqual(B.ports_from_settings("godot_ai/http_port = 8123\ngodot_ai/ws_port = 9654\n"), (8123, 9654))
        # Godot AI 4.3.0 reads each legacy key with its own default, and the v4 override first.
        self.assertEqual(B.ports_from_settings("godot_ai/http_port = 8123\n"), (8123, 9500))
        self.assertIsNone(B.ports_from_settings("godot_ai/telemetry_enabled = false\n"))
        override = 'godot_ai/v4_endpoint_ports = {\n"http_port": 8763,\n"ws_port": 9763\n}\ngodot_ai/http_port = 8123\n'
        self.assertEqual(B.ports_from_settings(override), (8763, 9763))
        self.assertEqual(B.ports_from_settings('godot_ai/v4_endpoint_ports = {\n"http_port": 8763\n}\n'), (0, 0))
        self.assertEqual(B.ports_from_settings(
            'godot_ai/v4_endpoint_ports = {\n"http_port": 8763,\n"ws_port": 8763\n}\n'), (0, 0))
        bad = B.Bridge(argv=["python", "-m", "godot_ai", "attach", "--port", "0", "--ws-port", "0"])
        self.assertIn("v4_endpoint_ports is malformed", bad.ensure_child())
        self.assertIsNone(bad.child)
        support = self.p.root / "support"
        support.mkdir()
        current = support / "editor_settings-4.7.tres"
        older = support / "editor_settings-4.6.tres"
        current.write_text("godot_ai/http_port = 8123\ngodot_ai/ws_port = 9654\n")
        older.write_text("godot_ai/http_port = 1\ngodot_ai/ws_port = 2\n")
        self.assertEqual(B.read_editor_ports(support=support, bundle_version="4.7.2"), (8123, 9654))
        os.utime(older, (time.time() + 50, time.time() + 50))
        self.assertEqual(B.read_editor_ports(support=support, bundle_version="9.9.0"), (1, 2))
        self.assertEqual(B.read_editor_ports(support=support / "missing", bundle_version="4.7.2"), (8000, 9500))
        self.assertEqual(B.class_usage("AnimationPlayer", "current_animation"), 4)
        self.assertEqual(B.class_usage("Label", "position"), 4)
        self.assertEqual(B.class_usage("Sprite2D", "frame_coords"), 4)
        self.assertTrue(B.persists_as_other("MeshInstance3D", "position"))
        self.assertTrue(B.persists_as_other("Sprite3D", "frame_coords"))
        self.assertTrue(B.persists_as_other("Label", "position"))
        self.assertTrue(B.persists_as_other("Label", "size"))
        self.assertFalse(B.persists_as_other("AudioStreamPlayer2D", "playing"))
        self.assertIsNone(B.class_usage("Sprite2D", "position"))
        self.assertIsNone(B.class_usage("Label", "text"))
        self.assertTrue(B.is_control("Button"))
        self.assertFalse(B.is_control("Sprite2D"))
        gut = "Tests                 1\nPassing Tests         1\n"
        self.assertEqual(B.read_test_counts("gut", gut), (1, 0))
        gd = "Overall Summary: 1 test cases | 0 errors | 0 failures | 0 flaky |"
        self.assertEqual(B.read_test_counts("gdunit4", gd), (1, 0))
        missing = "Some GUT class_names have not been imported."
        self.assertIsNone(B.read_test_counts("gut", missing)[0])
        self.assertIn("headless --import", B.cli_count_note(missing, None))
        self.assertIsNone(B.cli_count_note(gut, 1))

    def test_hermes_home_godot_ai_does_not_block_writes(self):
        home = self.p.root / "hermes-home"
        home.mkdir()
        os.environ["HERMES_HOME"] = str(home)
        try:
            (home / "config.yaml").write_text("mcp_servers:\n  godot-ai:\n    command: uvx\n")
            fake = self.fake()
            try:
                res = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies",
                                                    "property": "position", "value": 1})
                self.assertFalse(res["isError"], res["content"][0]["text"])
                self.assertTrue(any(row["name"] == "node_set_property" for row in fake.sent()))
                direct = fake.bridge.call("editor_look", {"op": "state"})
                self.assertIn("not called directly", direct["content"][0]["text"])
                status = fake.bridge._dispatch("editor_look", {"op": "state"}, None, public_read=False)
                self.assertFalse(status["isError"], status["content"][0]["text"])
                self.assertNotIn("godot-ai server", json.dumps(status["content"]))
                self.assertNotIn("hermes mcp remove", json.dumps(status["content"]))
            finally:
                fake.close()
        finally:
            os.environ.pop("HERMES_HOME", None)

    def test_duplicating_an_instance_is_refused(self):
        fake = self.fake()
        try:
            for path in ["/Level/Enemies/Enemy3", "/Level/Enemies"]:
                res = fake.bridge.call("scene_do", {"op": "duplicate", "path": path})
                self.assertIn("twice", res["content"][0]["text"])
            self.assertFalse(fake.bridge.call("scene_do", {"op": "duplicate", "path": "/Level/Enemies/Enemy3/Waypoint"})["isError"])
        finally:
            fake.close()

    def test_existing_files_need_overwrite(self):
        fake = self.fake()
        try:
            res = fake.bridge.call("scene_do", {"op": "new_scene", "path": "res://actors/enemy.tscn"})
            self.assertIn("already exists", res["content"][0]["text"])
            res = fake.bridge.call("script_do", {"op": "create_script", "path": "res://actors/enemy.gd", "content": "x"})
            self.assertTrue(res["isError"])
            self.assertEqual(fake.sent(), [])
            res = fake.bridge.call("script_do", {"op": "create_script", "path": "res://actors/enemy.gd", "content": "x", "overwrite": True})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            created = [row for row in fake.sent() if row["name"] == "script_create"]
            self.assertEqual(created[-1]["arguments"], {"path": "res://actors/enemy.gd", "content": "x"})
        finally:
            fake.close()

    def test_fixed_arguments_reach_godot_ai(self):
        fake = self.fake(nodes=list(LEVEL_NODES))
        try:
            # The editor matches the file: nothing would be discarded, so revert runs on the first approval.
            bare = fake.bridge.call("scene_do", {"op": "revert_scene"})
            self.assertFalse(bare["isError"], bare["content"][0]["text"])
            self.assertNotIn("needs a second approval", bare["content"][0]["text"])
            fake.bridge.call("file_do", {"op": "delete_file_permanently", "path": "res://actors/enemy.gd"})
            fake.bridge.call("file_do", {"op": "delete_file", "path": "res://actors/enemy.gd"})
            sent = fake.sent()
            self.assertEqual(sent[0]["arguments"], {"path": "res://scenes/level.tscn", "force_reload": True})
            self.assertEqual(sent[1]["arguments"]["params"], {"path": "res://actors/enemy.gd", "permanent": True})
            self.assertEqual(sent[2]["arguments"]["params"], {"path": "res://actors/enemy.gd"})
            self.assertTrue(fake.bridge.call("file_do", {"op": "delete_file", "path": "res://actors/enemy.gd", "permanent": True})["isError"])
        finally:
            fake.close()
        named = self.fake(nodes=list(LEVEL_NODES) + ["/Level/OnlyHere"],
                          props={"/Level/Enemies/Enemy3": {"position": {"x": 11, "y": 12}}})
        try:
            added = named.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Hit",
                                                   "parent_path": "/Level"})
            self.assertFalse(added["isError"], added["content"][0]["text"])
            planned = B.Bridge.call(named.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "revert_scene"}]})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            plan_text = planned["content"][0]["text"]
            self.assertIn("discards unsaved work", plan_text)
            self.assertNotIn("position:", plan_text)
            self.assertNotIn("/Level/Hit", plan_text)
            applied = B.Bridge.call(named.bridge, "apply_plan", {"plan_id": _plan_id_of(planned)})
            listed = applied["content"][0]["text"]
            self.assertIn("needs a second approval", listed)
            self.assertIn('position: disk Vector2(1, 2), editor {"x": 11, "y": 12}', listed)
            self.assertIn("/Level/OnlyHere", listed)
            self.assertIn("/Level/Hit", listed)
            self.assertFalse(any(row["name"] == "scene_open" for row in named.sent()))
            confirmed = B.Bridge.call(named.bridge, "confirm_risky_plan", {"plan_id": _plan_id_of(planned)})
            self.assertFalse(confirmed["isError"], confirmed["content"][0]["text"])
            self.assertTrue(any(row["name"] == "scene_open" and (row.get("arguments") or {}).get("force_reload") is True
                                for row in named.sent()))
        finally:
            named.close()

    def test_large_file_is_not_read(self):
        (self.p.root / "big.txt").write_text("x" * (B.MAX_READ_FILE_BYTES + 10))
        fake = self.fake()
        try:
            res = fake.bridge.call("file_look", {"op": "text", "path": "res://big.txt"})
            self.assertIn("no partial read", res["content"][0]["text"])
            self.assertEqual(fake.sent(), [])
        finally:
            fake.close()

    def test_cycle_is_refused(self):
        fake = self.fake()
        try:
            res = fake.bridge.call("scene_do", {"op": "add_node", "scene_path": "res://scenes/level.tscn"})
            self.assertIn("cycle", res["content"][0]["text"])
            self.assertFalse(fake.bridge.call("scene_do", {"op": "add_node", "scene_path": "res://actors/enemy.tscn"})["isError"])
        finally:
            fake.close()

    EDITOR = ["/Level", "/Level/Enemies", "/Level/Enemies/Enemy3", "/Level/Enemies/Enemy6",
              "/Level/Enemies/Enemy6/Sprite", "/Level/Enemies/Enemy3/Waypoint", "/Level/RoomA", "/Level/RoomB"]

    def test_save_after_a_disk_change_in_the_session(self):
        fake = self.fake(nodes=self.EDITOR)
        try:
            self.assertFalse(fake.bridge.call("scene_do", {"op": "save"})["isError"])
            level = self.p.root / "scenes" / "level.tscn"
            level.write_text(LEVEL + '\n[node name="FromTeammate" type="Node2D" parent="."]\n')
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertIn("on disk only: /Level/FromTeammate", res["content"][0]["text"])
            self.assertIn("nobody was asked", res["content"][0]["text"])
            self.assertFalse(fake.bridge.call("scene_do", {"op": "save", "overwrite": True})["isError"])
            self.assertIn("on disk only: /Level/FromTeammate", fake.bridge.second_approvals[-1])
        finally:
            fake.close()

    def test_save_when_editor_and_disk_differ_before_the_session(self):
        level = self.p.root / "scenes" / "level.tscn"
        level.write_text(LEVEL + '\n[node name="FromTeammate" type="Node2D" parent="."]\n')
        fake = self.fake(nodes=self.EDITOR, props={"/Level/Enemies/Enemy3": {"position": {"x": 9, "y": 9}}})
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            text = res["content"][0]["text"]
            self.assertIn("on disk only: /Level/FromTeammate", text)
            # 0.9.0: a plan that saves compares every node before its first step, so the value differs too.
            self.assertIn("/Level/Enemies/Enemy3 position", text)
            self.assertIn("Nothing was sent to the Godot editor", text)
            compared, diffs, why, _skipped = fake.bridge.compare("res://scenes/level.tscn", str(self.p.root))
            self.assertTrue(compared, why)
            self.assertTrue(any("/Level/Enemies/Enemy3 position" in line for line in diffs), diffs)
            self.assertFalse(fake.bridge.call("scene_do", {"op": "save", "overwrite": True})["isError"])
        finally:
            fake.close()

    def test_own_deletion_is_not_a_disk_change(self):
        fake = self.fake(nodes=self.EDITOR)
        try:
            fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Level/Enemies/Enemy6"})
            self.assertFalse(fake.bridge.call("scene_do", {"op": "save"})["isError"])
        finally:
            fake.close()

    def test_prior_disk_diff_blocks_save_and_an_own_edit_does_not(self):
        fake = self.fake(nodes=self.EDITOR, props={"/Level/Enemies/Enemy3": {"position": {"x": 9, "y": 9}}})
        try:
            touched = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy3",
                                                             "property": "position", "value": {"x": 9, "y": 9}})
            self.assertFalse(touched["isError"], touched["content"][0]["text"])
            raw = [json.loads(line) for line in fake.calls.read_text().splitlines() if line.strip()]
            names = [row["name"] for row in raw]
            self.assertLess(names.index("node_get_properties"), names.index("node_set_property"))
            before = raw[:names.index("node_set_property")]
            read_paths = {row["arguments"].get("path") for row in before if row["name"] == "node_get_properties"}
            self.assertEqual(read_paths, {"/Level/Enemies/Enemy3"})
            refused = fake.bridge.call("scene_do", {"op": "save"})
            self.assertTrue(refused["isError"])
            self.assertIn("/Level/Enemies/Enemy3 position", refused["content"][0]["text"])
        finally:
            fake.close()
        own = self.fake(nodes=self.EDITOR)
        try:
            edited = own.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies", "property": "position",
                                                           "value": {"x": 3, "y": 4}})
            self.assertFalse(edited["isError"], edited["content"][0]["text"])
            self.assertFalse(own.bridge.call("scene_do", {"op": "save"})["isError"])
            level = self.p.root / "scenes" / "level.tscn"
            level.write_text(level.read_text(encoding="utf-8").replace("load_steps=3", "load_steps=4"))
            changed = own.bridge.call("scene_do", {"op": "save"})
            self.assertIn("changed on disk after this session", changed["content"][0]["text"])
        finally:
            own.close()

    def test_scene_too_large_to_compare_stops_the_save(self):
        nodes = ["/Level"] + [f"/Level/N{i}" for i in range(B.MAX_COMPARE_NODES + 10)]
        fake = self.fake(nodes=nodes)
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            text = res["content"][0]["text"]
            self.assertIn("could not compare", text)
            self.assertIn("save_as", text)
            self.assertIn("contents were not read", text)
            self.assertEqual(B.MAX_COMPARE_PROPERTY_NODES, B.MAX_COMPARE_NODES)
            self.assertEqual(B.MAX_GROUP_NODES, B.MAX_COMPARE_NODES)
        finally:
            fake.close()

    def _apply_steps(self, bridge, steps):
        ready = prepare_save(bridge, steps)
        if ready is not None:
            return ready
        planned = B.Bridge.call(bridge, "plan_change", {"steps": steps})
        self.assertFalse(planned.get("isError"), (planned.get("content") or [{}])[0].get("text", ""))
        plan_id = _plan_id_of(planned)
        applied = B.Bridge.call(bridge, "apply_plan", {"plan_id": plan_id})
        text = (applied.get("content") or [{}])[0].get("text", "")
        if not applied.get("isError") and "needs a second approval" in text:
            bridge.second_approvals.append(text)
            applied = B.Bridge.call(bridge, "confirm_risky_plan", {"plan_id": plan_id})
        return applied

    def test_save_keeps_a_script_this_plan_wrote_when_the_editor_writes_the_old_tab(self):
        script = self.p.root / "actors" / "enemy.gd"
        old = script.read_text(encoding="utf-8")
        planned = old + "var hp := 9\n"
        fake = self.fake(nodes=list(LEVEL_NODES), clobber={"res://actors/enemy.gd": old})
        try:
            applied = self._apply_steps(fake.bridge, [
                {"tool": "file_do", "op": "write_file", "path": "res://actors/enemy.gd",
                 "content": planned, "overwrite": True},
                {"tool": "scene_do", "op": "save"},
            ])
            text = (applied.get("content") or [{}])[0].get("text", "")
            self.assertFalse(applied.get("isError"), text)
            self.assertEqual(script.read_text(encoding="utf-8"), planned)
            self.assertNotIn("did not name", text)
        finally:
            fake.close()

    def test_save_still_restores_a_script_the_plan_did_not_write(self):
        script = self.p.root / "actors" / "enemy.gd"
        old = script.read_text(encoding="utf-8")
        outside = old + "var outside := 1\n"
        fake = self.fake(nodes=list(LEVEL_NODES), clobber={"res://actors/enemy.gd": outside})
        try:
            applied = self._apply_steps(fake.bridge, [{"tool": "scene_do", "op": "save"}])
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertTrue(applied.get("isError"), text)
            self.assertEqual(script.read_text(encoding="utf-8"), old)
            head = text[:2048]
            self.assertIn("enemy.gd", head)
            self.assertIn("line ", head)
            self.assertIn("did not name", head)
            self.assertTrue(head.startswith("game-editor: res://actors/enemy.gd"))
            self.assertIn("bytes already on disk", head)
            self.assertIn("does not keep an older editor tab", head)
            self.assertNotIn("Name each path in the step's also list", text)
        finally:
            fake.close()

    def test_editor_write_of_the_planned_text_is_not_put_back(self):
        script = self.p.root / "actors" / "enemy.gd"
        planned = "extends Node\nvar hp := 9\n"
        script.write_text(planned, encoding="utf-8")
        fake = self.fake(nodes=list(LEVEL_NODES))
        try:
            root = str(self.p.root)
            fake.bridge.written_scripts["res://actors/enemy.gd"] = B.file_sha(script)
            before = fake.bridge._capture_files(root)
            before["actors/enemy.gd"] = ("file", b"extends Node\n")
            restored = fake.bridge._restore_strays(root, before, set())
            self.assertEqual(restored, [])
            self.assertEqual(script.read_text(encoding="utf-8"), planned)
        finally:
            fake.close()

    def test_also_names_the_bytes_that_stay_and_the_save_keeps_them(self):
        script = self.p.root / "actors" / "enemy.gd"
        old = script.read_text(encoding="utf-8")
        disk_d = old + "var score := 0 # D\n"
        disk_b = old + "var score := 0 # B\n"
        fake = self.fake(nodes=list(LEVEL_NODES), clobber={"res://actors/enemy.gd": disk_b})
        try:
            wrote = self._apply_steps(fake.bridge, [
                {"tool": "file_do", "op": "write_file", "path": "res://actors/enemy.gd",
                 "content": disk_d, "overwrite": True},
            ])
            self.assertFalse(wrote.get("isError"), (wrote.get("content") or [{}])[0].get("text", ""))
            self.assertEqual(script.read_text(encoding="utf-8"), disk_d)
            ready = prepare_save(fake.bridge, [{"tool": "scene_do", "op": "save"}])
            self.assertIsNone(ready)
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "save", "also": ["res://actors/enemy.gd"]}]})
            plan_text = (planned.get("content") or [{}])[0].get("text", "")
            self.assertFalse(planned.get("isError"), plan_text)
            self.assertIn("res://actors/enemy.gd", plan_text)
            self.assertIn("also keeps", plan_text)
            self.assertIn("# D", plan_text)
            self.assertNotIn("# B", plan_text)
            applied = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(planned)})
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertEqual(script.read_text(encoding="utf-8"), disk_d)
            self.assertNotIn("Nothing was written back", text)
            self.assertNotIn("did not name", text)
        finally:
            fake.close()

    def test_also_of_a_missing_file_makes_no_plan(self):
        fake = self.fake(nodes=list(LEVEL_NODES))
        try:
            ready = prepare_save(fake.bridge, [{"tool": "scene_do", "op": "save"}])
            self.assertIsNone(ready)
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "save", "also": ["res://actors/missing.gd"]}]})
            text = (planned.get("content") or [{}])[0].get("text", "")
            self.assertTrue(planned.get("isError"), text)
            self.assertIn("missing.gd", text)
            self.assertIn("not known", text)
            self.assertIn("No plan was saved", text)
            self.assertNotIn("plan_id:", text)
        finally:
            fake.close()

    def _tres(self, name: str, body: str) -> Path:
        folder = self.p.root / "assets"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_save_names_a_tres_the_scene_uses_and_keeps_godots_rewrite(self):
        scene = self.p.root / "scenes" / "level.tscn"
        scene.write_text(LEVEL.replace(
            '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]',
            '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]\n'
            '[ext_resource type="SpriteFrames" path="res://assets/hero.tres" id="3"]',
        ), encoding="utf-8")
        original = '[gd_resource type="SpriteFrames" load_steps=1 format=3]\n\n[resource]\n'
        rewritten = '[gd_resource type="SpriteFrames" format=3 uid="uid://cnew"]\n\n[resource]\n'
        tres = self._tres("hero.tres", original)
        fake = self.fake(nodes=list(LEVEL_NODES), clobber={"res://assets/hero.tres": rewritten})
        try:
            ready = prepare_save(fake.bridge, [{"tool": "scene_do", "op": "save"}])
            self.assertIsNone(ready, ready)
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "scene_do", "op": "save"}]})
            plan_text = (planned.get("content") or [{}])[0].get("text", "")
            self.assertFalse(planned.get("isError"), plan_text)
            self.assertIn("res://assets/hero.tres", plan_text)
            self.assertIn("rewrites those resources", plan_text)
            applied = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(planned)})
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertEqual(tres.read_text(encoding="utf-8"), rewritten)
            self.assertNotIn("did not name", text)
        finally:
            fake.close()

    def test_save_names_a_tres_a_step_names_and_keeps_godots_rewrite(self):
        original = '[gd_resource type="SpriteFrames" load_steps=1 format=3]\n\n[resource]\n'
        rewritten = '[gd_resource type="SpriteFrames" format=3 uid="uid://cstep"]\n\n[resource]\n'
        tres = self._tres("hero.tres", original)
        fake = self.fake(nodes=list(LEVEL_NODES), clobber={"res://assets/hero.tres": rewritten})
        try:
            ready = prepare_save(fake.bridge, [{"tool": "scene_do", "op": "save"}])
            self.assertIsNone(ready, ready)
            applied = self._apply_steps(fake.bridge, [
                {"tool": "scene_do", "op": "set_node_property", "path": "/Level/Enemies/Enemy3",
                 "property": "sprite_frames", "value": "res://assets/hero.tres"},
                {"tool": "scene_do", "op": "save"},
            ])
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertEqual(tres.read_text(encoding="utf-8"), rewritten)
            self.assertNotIn("did not name", text)
        finally:
            fake.close()

    def test_revert_names_this_scene_when_another_scene_is_open(self):
        other = self.p.root / "actors" / "enemy.tscn"
        other.write_text(ENEMY + "\n; OTHER_SCENE_MARKER\n", encoding="utf-8")
        before = other.read_bytes()
        fake = self.fake(nodes=list(LEVEL_NODES) + ["/Level/OnlyHere"],
                         props={"/Level/Enemies/Enemy3": {"position": {"x": 9, "y": 4}}},
                         open_scenes=["res://scenes/level.tscn", "res://actors/enemy.tscn"])
        try:
            added = fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Hit",
                                                  "parent_path": "/Level"})
            self.assertFalse(added["isError"], added["content"][0]["text"])
            other_plan = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "revert_scene", "path": "res://actors/enemy.tscn"}]})
            self.assertFalse(other_plan["isError"], other_plan["content"][0]["text"])
            refused = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(other_plan)})
            refused_text = refused["content"][0]["text"]
            self.assertTrue(refused["isError"], refused_text)
            self.assertIn("cannot be named", refused_text)
            self.assertEqual(other.read_bytes(), before)
            self.assertFalse(any(row["name"] == "scene_open" for row in fake.sent()))
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "revert_scene"}]})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            applied = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(planned)})
            listed = applied["content"][0]["text"]
            self.assertFalse(applied["isError"], listed)
            self.assertIn("needs a second approval", listed)
            self.assertIn('position: disk Vector2(1, 2), editor {"x": 9, "y": 4}', listed)
            self.assertIn("/Level/OnlyHere", listed)
            self.assertIn("/Level/Hit", listed)
            self.assertNotIn("script null", listed)
            self.assertNotIn("in editor only: /Level/Hit script", listed)
            self.assertIn("discards res://scenes/level.tscn", listed)
            self.assertNotIn("discards res://actors/enemy.tscn", listed)
            self.assertNotIn("OTHER_SCENE_MARKER", listed)
            confirmed = B.Bridge.call(fake.bridge, "confirm_risky_plan", {"plan_id": _plan_id_of(planned)})
            self.assertFalse(confirmed["isError"], confirmed["content"][0]["text"])
            opens = [row for row in fake.sent() if row["name"] == "scene_open"]
            self.assertEqual(len(opens), 1)
            self.assertEqual(opens[0]["arguments"].get("path"), "res://scenes/level.tscn")
            self.assertTrue(opens[0]["arguments"].get("force_reload"))
            self.assertEqual(other.read_bytes(), before)
        finally:
            fake.close()

    def test_a_uid_an_earlier_step_named_stays_when_a_later_step_writes_it(self):
        uid = self.p.root / "scripts" / "gem.gd.uid"
        body = "extends Node\n"
        fake = self.fake(nodes=list(LEVEL_NODES), clobber={"res://scripts/gem.gd.uid": "uid://cnew\n"})
        try:
            ready = prepare_save(fake.bridge, [
                {"tool": "script_do", "op": "create_script", "path": "res://scripts/gem.gd", "content": body},
                {"tool": "scene_do", "op": "save"},
            ])
            self.assertIsNone(ready, ready)
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "script_do", "op": "create_script", "path": "res://scripts/gem.gd", "content": body},
                {"tool": "scene_do", "op": "save"},
            ]})
            plan_text = (planned.get("content") or [{}])[0].get("text", "")
            self.assertFalse(planned.get("isError"), plan_text)
            self.assertIn("also writes res://scripts/gem.gd.uid beside the script", plan_text)
            applied = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(planned)})
            text = (applied.get("content") or [{}])[0].get("text", "")
            if not applied.get("isError") and "needs a second approval" in text:
                applied = B.Bridge.call(fake.bridge, "confirm_risky_plan", {"plan_id": _plan_id_of(planned)})
                text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertNotIn("did not name", text)
            self.assertEqual(uid.read_text(encoding="utf-8"), "uid://cnew\n")
            self.assertEqual((self.p.root / "scripts" / "gem.gd").read_text(encoding="utf-8"), body)
        finally:
            fake.close()

    def test_a_script_no_step_named_is_still_put_back(self):
        stranger = self.p.root / "scripts" / "stranger.gd"
        fake = self.fake(nodes=list(LEVEL_NODES), clobber={"res://scripts/stranger.gd": "extends Node\n"})
        try:
            applied = self._apply_steps(fake.bridge, [
                {"tool": "scene_do", "op": "set_node_property", "path": "/Level/Enemies/Enemy3",
                 "property": "position", "value": {"x": 4, "y": 5}},
                {"tool": "scene_do", "op": "save"},
            ])
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertTrue(applied.get("isError"), text)
            self.assertIn("stranger.gd", text)
            self.assertIn("did not name", text)
            self.assertFalse(stranger.exists())
        finally:
            fake.close()

    def test_a_uid_beside_a_script_from_another_plan_stays(self):
        scripts = self.p.root / "scripts"
        scripts.mkdir()
        (scripts / "gem.gd").write_text("extends Node\n", encoding="utf-8")
        uid = scripts / "gem.gd.uid"
        fake = self.fake(nodes=list(LEVEL_NODES), game_status="live", play_touch=[
            {"path": "res://scripts/gem.gd.uid", "text": "uid://crossplan1\n"}])
        try:
            applied = self._apply_steps(fake.bridge, [
                {"tool": "game_do", "op": "play_session", "goal": "walk",
                 "steps": [{"op": "wait", "seconds": 0}]},
            ])
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertEqual(uid.read_text(encoding="utf-8"), "uid://crossplan1\n")
            kept = [line for line in text.splitlines() if "was kept" in line]
            self.assertEqual(kept, ["game-editor: res://scripts/gem.gd.uid beside res://scripts/gem.gd "
                                    "was written by the editor and was kept."])
            self.assertNotIn("did not name", text)
        finally:
            fake.close()

    def test_an_import_beside_a_file_placed_from_outside_stays(self):
        png = self.p.root / "hero.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")
        imp = self.p.root / "hero.png.import"
        body = "[remap]\n\nimporter=\"texture\"\n"
        fake = self.fake(nodes=list(LEVEL_NODES), game_status="live", play_touch=[
            {"path": "res://hero.png.import", "text": body}])
        try:
            applied = self._apply_steps(fake.bridge, [
                {"tool": "game_do", "op": "play_session", "goal": "walk",
                 "steps": [{"op": "wait", "seconds": 0}]},
            ])
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertEqual(imp.read_text(encoding="utf-8"), body)
            kept = [line for line in text.splitlines() if "was kept" in line]
            self.assertEqual(len(kept), 1)
            self.assertIn("res://hero.png.import beside res://hero.png", kept[0])
            self.assertNotIn("did not name", text)
        finally:
            fake.close()

    def test_an_unplanned_script_and_a_bad_uid_are_put_back(self):
        scripts = self.p.root / "scripts"
        scripts.mkdir()
        (scripts / "fake.gd").write_text("extends Node\n", encoding="utf-8")
        sneaky = scripts / "sneaky.gd"
        bad = scripts / "fake.gd.uid"
        missing = scripts / "missing.gd.uid"
        fake = self.fake(nodes=list(LEVEL_NODES), game_status="live", play_touch=[
            {"path": "res://scripts/sneaky.gd", "text": "extends Node\n"},
            {"path": "res://scripts/fake.gd.uid", "text": "this is not a godot uid\n"},
            {"path": "res://scripts/missing.gd.uid", "text": "uid://nobody\n"},
        ])
        try:
            applied = self._apply_steps(fake.bridge, [
                {"tool": "game_do", "op": "play_session", "goal": "walk",
                 "steps": [{"op": "wait", "seconds": 0}]},
            ])
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertTrue(applied.get("isError"), text)
            self.assertIn("did not name", text)
            self.assertIn("sneaky.gd", text)
            self.assertIn("fake.gd.uid", text)
            self.assertIn("missing.gd.uid", text)
            self.assertFalse(sneaky.exists())
            self.assertFalse(bad.exists())
            self.assertFalse(missing.exists())
            self.assertTrue((scripts / "fake.gd").is_file())
            self.assertNotIn("was kept", text)
        finally:
            fake.close()

    def test_a_failed_play_reply_leads_with_the_step_and_fits(self):
        stranger = self.p.root / "scripts" / "stranger.gd"
        fillers = [
            {"tool": "scene_do", "op": "set_node_property", "path": "/Level/Enemies/Enemy3",
             "property": "position", "value": {"x": 4, "y": index}}
            for index in range(16)
        ]
        steps = fillers + [
            {"tool": "game_do", "op": "play_session", "goal": "walk",
             "steps": [{"op": "wait", "seconds": 0.4}]},
        ]
        fake = self.fake(nodes=list(LEVEL_NODES), play_touch=["res://scripts/stranger.gd"])
        try:
            applied = self._apply_steps(fake.bridge, steps)
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertTrue(applied.get("isError"), text[:300])
            self.assertLessEqual(len(text), 2048)
            self.assertEqual(len(applied.get("content") or []), 1)
            head = text[:2048]
            first = head.splitlines()[0]
            self.assertIn("step 17 of 17", first)
            self.assertIn("play_session ok=", first)
            self.assertIn("stranger.gd", head)
            self.assertIn("did not name", head)
            self.assertFalse(stranger.exists())
        finally:
            fake.close()

    def test_compare_of_a_node_with_no_script_agrees(self):
        fake = self.fake(nodes=list(LEVEL_NODES))
        try:
            applied = fake.bridge.call("scene_do", {"op": "compare"})
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertIn("the editor and the file agree", text)
            self.assertNotIn("script null", text)
        finally:
            fake.close()
        fake = self.fake(nodes=list(LEVEL_NODES) + ["/Level/Hit"])
        try:
            applied = fake.bridge.call("scene_do", {"op": "compare"})
            text = "\n".join(item.get("text", "") for item in applied.get("content") or [] if isinstance(item, dict))
            self.assertFalse(applied.get("isError"), text)
            self.assertIn("in editor only: /Level/Hit", text)
            self.assertNotIn("script null", text)
            self.assertNotIn("in editor only: /Level/Hit script", text)
            self.assertNotIn("the editor and the file agree", text)
        finally:
            fake.close()

    def test_quit_still_stops_when_another_scene_is_open(self):
        fake = self.fake(nodes=list(LEVEL_NODES),
                         open_scenes=["res://scenes/level.tscn", "res://actors/enemy.tscn"])
        try:
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "editor_do", "op": "quit"}]})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            applied = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(planned)})
            text = applied["content"][0]["text"]
            self.assertTrue(applied["isError"], text)
            self.assertIn("is open", text)
            self.assertFalse(any(row["name"] == "editor_manage" for row in fake.sent()))
        finally:
            fake.close()


class EdgeTest(unittest.TestCase):
    def test_tool_list_does_not_start_godot_ai(self):
        fake = FakeChild("port_busy")
        try:
            reply = fake.bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.assertEqual(len(reply["result"]["tools"]), len(B.tool_definitions()))
            self.assertIsNone(fake.bridge.child)
        finally:
            fake.close()

    def test_missing_op_is_hidden(self):
        fake = FakeChild("drop_op")
        try:
            self.assertEqual(fake.bridge.ensure_child(), "")
            defs = {d["name"]: d for d in B.tool_definitions(fake.bridge.available)}
            self.assertEqual(defs["script_look"]["inputSchema"]["properties"]["op"]["enum"], ["source", "outline"])
            res = fake.bridge.call("script_look", {"op": "outline", "path": "res://a.gd"})
            self.assertTrue(res["isError"])
        finally:
            fake.close()

    def test_start_failure_returns_upstream_stderr(self):
        fake = FakeChild("port_busy")
        try:
            res = fake.bridge.call("scene_look", {"op": "scene_tree"})
            self.assertTrue(res["content"][0]["text"].startswith("godot-ai attach [PORT_OCCUPIED]"))
            self.assertEqual(len(B.tool_definitions(fake.bridge.available)), len(B.tool_definitions()))
        finally:
            fake.close()

    def test_child_exit_names_the_next_step(self):
        bridge = B.Bridge(argv=[sys.executable, "-c", "import sys; sys.exit(3)"])
        try:
            res = bridge.call("scene_look", {"op": "scene_tree"})
            text = res["content"][0]["text"]
            self.assertTrue(res["isError"])
            self.assertIn("exited with code 3", text)
            self.assertIn("Open the project in Godot with the Godot AI add-on enabled", text)
            self.assertIn("Nothing was sent", text)
        finally:
            bridge.shutdown()

    def test_large_reply_is_marked_truncated_first(self):
        fake = FakeChild("big")
        try:
            direct = fake.bridge.call("editor_look", {"op": "read_logs"})
            self.assertIn("not called directly", direct["content"][0]["text"])
            res = fake.bridge._dispatch("editor_look", {"op": "read_logs"}, None, public_read=False)
            self.assertIs(json.loads(res["content"][0]["text"])["truncated"], True)
        finally:
            fake.close()

    def test_timeout_is_reported(self):
        fake = FakeChild("slow")
        old = B.CALL_TIMEOUT
        B.CALL_TIMEOUT = 1.0
        try:
            res = fake.bridge._dispatch("editor_look", {"op": "read_logs"}, None, public_read=False)
            self.assertIn("did not answer within 1 seconds", res["content"][0]["text"])
        finally:
            B.CALL_TIMEOUT = old
            fake.close()


class StdioTest(unittest.TestCase):
    def test_initialize_list_and_no_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls.jsonl"
            runner = (
                "import importlib.util,sys\n"
                f"spec=importlib.util.spec_from_file_location('b', {str(BRIDGE_PATH)!r})\n"
                "b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)\n"
                f"sys.exit(b.Bridge(argv=[sys.executable,'-I',{str(FAKE)!r},{str(calls)!r}]).serve())\n"
            )
            proc = subprocess.Popen([sys.executable, "-I", "-c", runner], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=tmp)
            msgs = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
                {"jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "scene_look", "arguments": {}}},
            ]
            out, _ = proc.communicate("".join(json.dumps(m) + "\n" for m in msgs).encode(), timeout=60)
            replies = {r["id"]: r for r in (json.loads(x) for x in out.decode().splitlines() if x.strip())}
            self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-11-25")
            self.assertEqual(set(replies[1]["result"]["capabilities"]), {"tools"})
            self.assertEqual(len(replies[2]["result"]["tools"]), len(B.tool_definitions()))
            self.assertEqual(replies[3]["error"]["code"], -32601)
            self.assertNotIn("result", replies[3])
            self.assertEqual(replies[4]["error"]["code"], -32601)
            self.assertTrue(replies[5]["result"]["isError"])


PREMISE_NODES = ["/Main", "/Main/Label", "/Main/Shape", "/Main/Keeper", "/Main/Ground", "/Main/Room",
                  "/Main/Room/Guard"]


class ReviewFixTest(unittest.TestCase):
    def setUp(self):
        self.p = Project()
        (self.p.root / "scenes" / "premise.tscn").write_text(PREMISE)

    def tearDown(self):
        self.p.close()

    def fake(self, **kw):
        kw.setdefault("nodes", PREMISE_NODES)
        return FakeChild(project=self.p.root, scene="res://scenes/premise.tscn", **kw)

    def test_matching_scene_saves_including_prefab_children(self):
        fake = self.fake()
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertTrue(res["content"][0]["text"].startswith("game-editor did: save_scene"))
        finally:
            fake.close()

    def _refuses(self, needle: str, **kw):
        fake = self.fake(**kw)
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertTrue(res["isError"], res["content"][0]["text"])
            self.assertIn(needle, res["content"][0]["text"])
            again = fake.bridge.call("scene_do", {"op": "save", "overwrite": True})
            self.assertFalse(again["isError"], again["content"][0]["text"])
            said = "\n".join(item.get("text", "") for item in again["content"] if isinstance(item, dict))
            self.assertIn("drops the disk side", said)
            # 1.0.0: an overwrite that drops something needs the second approval, which lists it.
            self.assertTrue(fake.bridge.second_approvals)
            self.assertIn(needle.split(":")[0], fake.bridge.second_approvals[-1])
            return said
        finally:
            fake.close()

    def _compare_has(self, needle: str, **kw):
        """A full compare names the diff. 0.9.0: a plan that saves compares the whole scene before its first
        step, so the save is refused with the same line and nothing is sent."""
        fake = self.fake(**kw)
        try:
            self.assertEqual(fake.bridge.ensure_child(), "")
            compared, diffs, why, skipped = fake.bridge.compare("res://scenes/premise.tscn", str(self.p.root))
            self.assertTrue(compared, (why, skipped, diffs))
            self.assertTrue(any(needle in line for line in diffs), diffs)
            saved = fake.bridge.call("scene_do", {"op": "save"})
            self.assertTrue(saved["isError"], saved["content"][0]["text"])
            self.assertIn(needle, saved["content"][0]["text"])
            self.assertFalse(any(item["name"] == "scene_save" for item in fake.sent()))
        finally:
            fake.close()

    def test_prefab_and_engine_connections_are_not_conflicts(self):
        (self.p.root / "actors" / "coin.tscn").write_text(
            '[gd_scene format=3]\n\n[node name="Coin" type="Area2D" groups=["pickup"]]\n\n'
            '[connection signal="body_entered" from="." to="." method="_on_body_entered"]\n')
        path = self.p.root / "scenes" / "premise.tscn"
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="1"]',
            '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="1"]\n'
            '[ext_resource type="PackedScene" path="res://actors/coin.tscn" id="2"]')
        text = text.replace(
            '[connection signal="ready"',
            '[node name="Coin" parent="." instance=ExtResource("2")]\n\n'
            '[connection signal="collected" from="Coin" to="." method="_on_coin"]\n'
            '[connection signal="ready"')
        path.write_text(text, encoding="utf-8")
        signals = {
            "/Main": [{"signal": "ready", "target": "/Main/Keeper", "method": "_on_ready"}],
            "/Main/Coin": [
                {"signal": "body_entered", "target": "/Main/Coin", "method": "_on_body_entered"},
                {"signal": "collected", "target": "/Main", "method": "_on_coin"},
                {"signal": "item_rect_changed", "target": "/Main/Coin", "method": "Control::_size_changed"},
            ],
            "/Main/Label": [{"signal": "minimum_size_changed", "target": "/Main/Label",
                             "method": "Label::_maximum_size_changed"}],
        }
        fake = self.fake(nodes=PREMISE_NODES + ["/Main/Coin"], signals=signals,
                         groups={"/Main/Coin": ["pickup"]})
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
        finally:
            fake.close()
        missing = {key: value for key, value in signals.items()}
        missing["/Main/Coin"] = [item for item in signals["/Main/Coin"] if item["method"] != "_on_coin"]
        self._compare_has("connection on disk only", nodes=PREMISE_NODES + ["/Main/Coin"], signals=missing,
                          groups={"/Main/Coin": ["pickup"]})

    def test_hidden_layout_offsets_are_compared(self):
        same = {"/Main/Ground": {"offset_left": -1000.0, "offset_top": -20.0,
                                 "offset_right": 1000.0, "offset_bottom": 20.0}}
        fake = self.fake(layout=same)
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
        finally:
            fake.close()
        self._compare_has("offset_right", layout={"/Main/Ground": {**same["/Main/Ground"], "offset_right": 1.0}})

    def test_connection_group_slash_packed_subresource_and_editor_only_node(self):
        self._compare_has("connection on disk only", signals={"/Main": []})
        self._compare_has("groups:", groups={"/Main": []})
        self._compare_has("font_color", props={"/Main/Label": {"theme_override_colors/font_color": {"r": 0, "g": 0, "b": 1, "a": 1}}})
        self._compare_has("metadata/_tags", props={"/Main/Label": {"metadata/_tags": ["blue"]}})
        self._compare_has("subresource", resources={"/Main/Shape shape": {"type": "RectangleShape2D", "properties": {"size": {"x": 1.0, "y": 1.0}}}})
        note = self._refuses("in editor only: /Main/Dirty", nodes=PREMISE_NODES + ["/Main/Dirty"])
        self.assertIn("nodes", note)

    def test_unparsed_value_is_not_treated_as_a_match(self):
        path = self.p.root / "scenes" / "premise.tscn"
        path.write_text(PREMISE.replace("theme_override_font_sizes/font_size = 32", "odd_value = Object(1)"))
        fake = self.fake()
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertIn("unparsed", res["content"][0]["text"])
            self.assertIn("could not compare", res["content"][0]["text"])
        finally:
            fake.close()

    def test_run_game_not_live_is_an_error(self):
        fake = FakeChild(game_status="stopped")
        try:
            res = fake.bridge.call("project_do", {"op": "run_game", "mode": "main"})
            self.assertTrue(res["isError"])
            self.assertTrue(res["content"][0]["text"].startswith("game-editor: run_game not_live"))
            self.assertNotIn("ran the scene files", json.dumps(res["content"]))
            self.assertTrue(json.loads(res["content"][1]["text"])["game_status"]["not_live"])
        finally:
            fake.close()

    def test_screenshot_retries_a_2d_scene_once(self):
        fake = self.fake(screenshot="2d")
        try:
            res = fake.bridge.call("editor_do", {"op": "screenshot"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("viewport_2d was used", res["content"][-1]["text"])
            shots = [row for row in fake.sent() if row["name"] == "editor_screenshot"]
            self.assertEqual([row["arguments"].get("source", "viewport") for row in shots], ["viewport", "viewport_2d"])
            explicit = fake.bridge.call("editor_do", {"op": "screenshot", "source": "viewport_2d"})
            self.assertFalse(explicit["isError"])
            shots = [row for row in fake.sent() if row["name"] == "editor_screenshot"]
            self.assertEqual(len(shots), 3)
        finally:
            fake.close()

    def test_screenshot_says_when_the_2d_view_is_empty(self):
        fake = self.fake(screenshot="2d", screenshot_2d="empty")
        try:
            res = fake.bridge.call("editor_do", {"op": "screenshot"})
            self.assertTrue(res["isError"])
            blob = json.dumps(res["content"])
            self.assertIn("did not return a picture", blob)
            self.assertIn("empty", blob)
        finally:
            fake.close()

    def test_script_diagnostics_and_editor_log(self):
        fake = self.fake()
        try:
            direct = fake.bridge.call("editor_look", {"op": "read_logs", "source": "editor"})
            self.assertIn("not called directly", direct["content"][0]["text"])
            empty = apply_one(fake.bridge, {"tool": "editor_look", "op": "read_logs", "source": "editor"})
            self.assertIn("not in this log", empty["content"][0]["text"])
            res = fake.bridge.call("script_do", {"op": "create_script", "path": "res://bad.gd", "content": "extends Node\nfunc broken(\n"})
            self.assertTrue(res["isError"])
            self.assertIn("res://bad.gd:2:", res["content"][0]["text"])
            self.assertIn("did not compile", res["content"][0]["text"])
            self.assertIn("upstream_quote:", res["content"][0]["text"])
            body = json.loads(res["content"][1]["text"])
            self.assertEqual(body["diagnostics"][0]["resolved"], {"path": "res://bad.gd", "line": 2})
            self.assertTrue(body["committed"])
            logs = apply_one(fake.bridge, {"tool": "editor_look", "op": "read_logs", "source": "editor"})
            self.assertIn("res://bad.gd:2:", logs["content"][0]["text"])
        finally:
            fake.close()

    def test_control_position_is_sent_and_a_near_zero_size_is_noted(self):
        fake = self.fake(props={"/Main/Label": {
            "offset_left": 16.0, "offset_right": 17.0, "offset_top": 8.0, "offset_bottom": 40.0}},
            node_types={"/Main/Label": "Label"})
        try:
            res = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Main/Label", "property": "position",
                                                         "value": {"x": 16, "y": 8}})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("near-zero size", res["content"][-1]["text"])
            self.assertTrue(any(row["name"] == "node_set_property" for row in fake.sent()))
            kept = self.fake(props={"/Main/Label": {
                "offset_left": 50.0, "offset_right": 150.0, "offset_top": 20.0, "offset_bottom": 60.0}},
                node_types={"/Main/Label": "Label"})
            try:
                wide = kept.bridge.call("scene_do", {"op": "set_node_property", "path": "/Main/Label", "property": "position",
                                                              "value": {"x": 50, "y": 20}})
                self.assertFalse(wide["isError"], wide["content"][0]["text"])
                self.assertNotIn("near-zero size", json.dumps(wide["content"]))
                self.assertNotIn("become 0", json.dumps(wide["content"]))
            finally:
                kept.close()
        finally:
            fake.close()

    def test_session_id_is_a_sibling_of_op(self):
        fake = FakeChild()
        try:
            direct = fake.bridge.call("editor_look", {"op": "selection", "session_id": "abc"})
            self.assertIn("not called directly", direct["content"][0]["text"])
            res = fake.bridge._dispatch("editor_look", {"op": "selection", "session_id": "abc"}, None, public_read=False)
            self.assertFalse(res["isError"], res["content"][0]["text"])
            rows = [json.loads(line) for line in fake.calls.read_text().splitlines() if line.strip()]
            row = [item for item in rows if item["name"] == "editor_manage"][-1]
            self.assertEqual(row["arguments"]["session_id"], "abc")
            self.assertNotIn("session_id", row["arguments"]["params"])
            flat = fake.bridge._dispatch("editor_look", {"op": "state", "session_id": "abc"}, None, public_read=False)
            self.assertFalse(flat["isError"], flat["content"][0]["text"])
            rows = [json.loads(line) for line in fake.calls.read_text().splitlines() if line.strip()]
            state = [item for item in rows if item["name"] == "editor_state"][-1]
            self.assertEqual(state["arguments"].get("session_id"), "abc")
            self.assertNotIn("params", state["arguments"])
            nested = fake.bridge.call("batch", {"op": "batch_execute", "commands": [{"command": "set_property", "params": {
                "path": "/Main/A", "property": "position", "value": 1, "session_id": "no"}}]})
            self.assertTrue(nested["isError"])
            self.assertIn("session_id", nested["content"][0]["text"])
        finally:
            fake.close()

    def test_user_save_can_be_read_and_a_key_cannot(self):
        fake = FakeChild()
        try:
            direct = fake.bridge.call("file_look", {"op": "text", "path": "user://save.json"})
            self.assertTrue(direct["isError"])
            self.assertFalse(any(item["name"] == "filesystem_manage" for item in fake.sent()))
            res = apply_one(fake.bridge, {"tool": "file_look", "op": "text", "path": "user://save.json"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertTrue(res["content"][0]["text"].startswith("game-editor note: this reply is player data"))
            self.assertTrue(any(item["name"] == "filesystem_manage" for item in fake.sent()))
            secret = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "file_look", "op": "text", "path": "user://private.key"}]})
            self.assertTrue(secret["isError"])
            self.assertIn("Nothing was sent", secret["content"][0]["text"])
            climb = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "file_look", "op": "text", "path": "user://../save.json"}]})
            self.assertTrue(climb["isError"])
            self.assertIn("'..'", climb["content"][0]["text"])
        finally:
            fake.close()

    def test_open_scene_force_reload_is_a_real_argument(self):
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn", nodes=list(LEVEL_NODES))
        try:
            # Nothing differs: one approval, and force_reload is the argument Godot AI receives.
            bare = fake.bridge.call("scene_do", {"op": "open_scene", "path": "res://scenes/level.tscn",
                                                  "force_reload": True})
            self.assertFalse(bare["isError"], bare["content"][0]["text"])
            self.assertNotIn("needs a second approval", bare["content"][0]["text"])
            sent = [item for item in fake.sent() if item["name"] == "scene_open"]
            self.assertEqual(sent[-1]["arguments"].get("force_reload"), True)
            missing = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "open_scene", "path": "res://scenes/missing.tscn", "force_reload": True}]})
            self.assertFalse(missing["isError"], missing["content"][0]["text"])
            refused = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(missing)})
            self.assertTrue(refused["isError"], refused["content"][0]["text"])
            self.assertIn("not a file", refused["content"][0]["text"])
            self.assertFalse(any((item.get("arguments") or {}).get("path") == "res://scenes/missing.tscn"
                                 for item in fake.sent() if item["name"] == "scene_open"))
        finally:
            fake.close()
            project.close()
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn",
                         nodes=list(LEVEL_NODES) + ["/Level/OnlyHere"],
                         props={"/Level/Enemies/Enemy3": {"position": {"x": 11, "y": 12}}})
        try:
            added = fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Hit",
                                                  "parent_path": "/Level"})
            self.assertFalse(added["isError"], added["content"][0]["text"])
            res = fake.bridge.call("scene_do", {"op": "open_scene", "path": "res://scenes/level.tscn",
                                                "force_reload": True})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertIn("discarded unsaved edits", json.dumps(res["content"]))
            listed = " ".join(fake.bridge.second_approvals)
            self.assertIn('position: disk Vector2(1, 2), editor {"x": 11, "y": 12}', listed)
            self.assertIn("/Level/OnlyHere", listed)
            self.assertIn("/Level/Hit", listed)
            sent = [item for item in fake.sent() if item["name"] == "scene_open"]
            self.assertEqual(sent[-1]["arguments"].get("force_reload"), True)
        finally:
            fake.close()
            project.close()

    def test_tool_node_properties_names_inspect_node(self):
        project = Project()
        try:
            (project.root / "actors" / "hud.gd").write_text("@tool\nextends Node\n\nfunc _get(_p):\n\treturn null\n")
            level = project.root / "scenes" / "level.tscn"
            text = level.read_text(encoding="utf-8")
            text = text.replace(
                '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]',
                '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]\n'
                '[ext_resource type="Script" path="res://actors/hud.gd" id="3"]')
            text = text.replace(
                '[node name="Enemies" type="Node2D" parent="."]',
                '[node name="Enemies" type="Node2D" parent="."]\nscript = ExtResource("3")')
            level.write_text(text, encoding="utf-8")
            fake = FakeChild(project=project.root, scene="res://scenes/level.tscn", nodes=["/Level", "/Level/Enemies"])
            try:
                res = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/Enemies"})
                self.assertFalse(res["isError"], res["content"][0]["text"])
                self.assertIn("editor was not asked", res["content"][0]["text"])
                self.assertFalse(any(item["name"] == "node_get_properties" for item in fake.sent()))
                plain = (project.root / "actors" / "hud.gd")
                plain.write_text("extends Node\n")
                allowed = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/Enemies"})
                self.assertFalse(allowed["isError"], allowed["content"][0]["text"])
                self.assertFalse(any(item["name"] == "node_get_properties" for item in fake.sent()))
                plain.write_text("@tool\nextends Node\n")
                quiet = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/Enemies"})
                self.assertFalse(quiet["isError"], quiet["content"][0]["text"])
                plain.write_text("@tool\nextends Node\nvar hp := 3:\n\tget:\n\t\treturn 4\n")
                asked = fake.bridge.call("scene_do", {"op": "properties", "path": "/Level/Enemies"})
                self.assertFalse(asked["isError"], asked["content"][0]["text"])
                self.assertTrue(any(item["name"] == "node_get_properties" for item in fake.sent()))
            finally:
                fake.close()
        finally:
            project.close()

    def test_resources_are_forwarded_and_secrets_are_not(self):
        fake = FakeChild()
        try:
            listed = fake.bridge.handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {}})
            self.assertEqual(listed["error"]["code"], -32601)
            self.assertNotIn("result", listed)
            secret = fake.bridge.handle({"jsonrpc": "2.0", "id": 8, "method": "resources/read",
                                         "params": {"uri": "res://private.key"}})
            self.assertEqual(secret["error"]["code"], -32601)
            self.assertNotIn("private.key", fake.calls.read_text() if fake.calls.exists() else "")
            read = fake.bridge.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/read",
                                       "params": {"uri": "godot://editor/state"}})
            self.assertEqual(read["error"]["code"], -32601)
            self.assertNotIn("godot://editor/state", fake.calls.read_text() if fake.calls.exists() else "")
        finally:
            fake.close()

    def test_resource_uris_use_the_same_path_and_tool_checks(self):
        project = Project()
        outside = Path(project.tmp.name) / "outside"
        outside.mkdir()
        (project.root / "linked").symlink_to(outside)
        (project.root / "actors" / "hud.gd").write_text("extends Node\n@tool\nfunc _ready():\n    pass\nfunc _get(_p):\n    return null\n")
        level = project.root / "scenes" / "level.tscn"
        text = level.read_text(encoding="utf-8")
        text = text.replace(
            '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]',
            '[ext_resource type="PackedScene" path="res://actors/room.tscn" id="2"]\n'
            '[ext_resource type="Script" path="res://actors/hud.gd" id="3"]')
        text = text.replace(
            '[node name="Enemies" type="Node2D" parent="."]',
            '[node name="Enemies" type="Node2D" parent="."]\nscript = ExtResource("3")')
        level.write_text(text, encoding="utf-8")
        (project.root / "actors" / "Tool.cs").write_text("using Godot;\n[Tool]\npublic partial class Box : Node {}\n")
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn",
                         nodes=["/Level", "/Level/Enemies"])
        try:
            self.assertTrue(fake.bridge._script_is_tool("res://actors/hud.gd", str(project.root)))
            self.assertTrue(fake.bridge._script_is_tool("res://actors/Tool.cs", str(project.root)))
            later = fake.bridge.call("scene_look", {"op": "properties", "path": "/Level/Enemies"})
            self.assertFalse(later["isError"], later["content"][0]["text"])
            self.assertIn("editor was not asked", later["content"][0]["text"])
            self.assertFalse(any(item["name"] == "node_get_properties" for item in fake.sent()))
            for uri in ("godot://script/export_credentials.cfg", "godot://script/.env",
                        "godot://shader/secrets.env", "res://linked/note.txt",
                        "godot://shader/res://a.gdshader", "godot://visual_shader/res://v.tres",
                        "godot://node/Level/Enemies%2Fproperties/children", "godot://unknown",
                        "godot://node/Level/Enemies/properties", "godot://editor/state",
                        "godot://node/Level/Enemies/children"):
                closed = fake.bridge.handle({"jsonrpc": "2.0", "id": 8, "method": "resources/read",
                                             "params": {"uri": uri}})
                self.assertEqual(closed["error"]["code"], -32601, uri)
                self.assertNotIn("result", closed)
                self.assertNotIn(uri, fake.calls.read_text() if fake.calls.exists() else "")
        finally:
            fake.close()
            project.close()

    def test_rich_scene_values_compare_without_overwrite(self):
        project = Project()
        rich = '''[gd_scene format=3]

[sub_resource type="Animation" id="anim"]
tracks/0/keys = {
"times": PackedFloat32Array(0, 1),
"transitions": PackedFloat32Array(1, 1),
"update": 0,
"values": [Vector2(0, 0), Vector2(10, 0)]
}

[sub_resource type="AnimationLibrary" id="lib"]
_data = {
&"walk": SubResource("anim")
}

[node name="Stage" type="Node2D"]

[node name="Icon" type="Sprite2D" parent="."]
region_rect = Rect2(1, 2, 32, 48)

[node name="Body" type="Node3D" parent="."]
transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 4, 5, 6)

[node name="Anim" type="AnimationPlayer" parent="."]
libraries/ = SubResource("lib")
'''
        (project.root / "scenes" / "rich.tscn").write_text(rich, encoding="utf-8")
        fake = FakeChild(project=project.root, scene="res://scenes/rich.tscn",
                         nodes=["/Stage", "/Stage/Icon", "/Stage/Body", "/Stage/Anim"])
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
            self.assertNotIn("could not compare", res["content"][0]["text"])
            original = fake.bridge.upstream

            def upstream(name, arguments, timeout=None, mid=None):
                params = arguments.get("params") if name == "resource_manage" else None
                if isinstance(params, dict) and str(params.get("property", "")).startswith("libraries"):
                    return {"isError": True, "content": [{"type": "text", "text":
                            "Property 'libraries/' on AnimationPlayer is not a native Resource property"}]}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            compared, diffs, why, skipped = fake.bridge.compare("res://scenes/rich.tscn", str(project.root))
            self.assertTrue(compared, (why, diffs, skipped))
            self.assertEqual(diffs, [])
        finally:
            fake.close()
            project.close()

    def test_classdb_gap_and_control_size_on_a_prefab_child(self):
        fake = FakeChild(project=self.p.root, scene="res://scenes/level.tscn")
        try:
            original = fake.bridge.upstream

            def upstream(name, arguments, timeout=None, mid=None):
                fields = arguments.get("fields") if name == "node_get_properties" else None
                table = {
                    "size": {"node_type": "Button",
                             "properties": [{"name": "size", "value": {"x": 100, "y": 40}}]},
                    "handle_binary_image": {"node_type": "GLTFState",
                                            "properties": [{"name": "handle_binary_image", "value": 0}]},
                }
                if isinstance(fields, list) and len(fields) == 1 and fields[0] in table:
                    return {"content": [{"type": "text", "text": json.dumps(table[fields[0]])}], "isError": False}
                return original(name, arguments, timeout, mid)

            fake.bridge.upstream = upstream
            size = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                          "property": "size", "value": {"x": 180, "y": 36}})
            # Allowed and sent; the stubbed read answers 100x40, so the read-back says Godot did not keep 180x36.
            self.assertTrue(size["isError"])
            self.assertIn("read back after the step", size["content"][0]["text"])
            self.assertTrue(any(item["name"] == "node_set_property"
                                and (item.get("arguments") or {}).get("property") == "size"
                                for item in fake.sent()))
            gap = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Level/Enemies/Enemy6/Sprite",
                                                         "property": "handle_binary_image", "value": 1})
            self.assertTrue(gap["isError"])
            self.assertIn("does not store", gap["content"][0]["text"])
            self.assertIn("8", gap["content"][0]["text"])
            for cls, prop in (("LightmapGIData", "light_textures"),
                              ("RDPipelineSpecializationConstant", "value"),
                              ("VisualShaderNodeVec4Constant", "constant_v4")):
                self.assertIsInstance(B.class_usage(cls, prop), int)
                self.assertFalse(B.class_usage(cls, prop) & B.USAGE_STORAGE)
        finally:
            fake.close()

    def test_control_position_note_remains_when_the_class_is_unknown(self):
        fake = self.fake(props={"/Main/Label": {
            "offset_left": 16.0, "offset_right": 17.0, "offset_top": 8.0, "offset_bottom": 40.0}})
        try:
            res = fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Main/Label", "property": "position",
                                                         "value": {"x": 16, "y": 8}})
            self.assertFalse(res["isError"])
            self.assertIn("near-zero size", res["content"][-1]["text"])
            self.assertIn("offset_right=17.0", res["content"][-1]["text"])
        finally:
            fake.close()

    def test_second_copy_under_an_instance_is_a_conflict(self):
        self._refuses("second copy", nodes=PREMISE_NODES + ["/Main/Room/Sprite"])
        fake = self.fake(nodes=PREMISE_NODES + ["/Main/Room/Hat"])
        try:
            res = fake.bridge.call("scene_do", {"op": "save"})
            self.assertFalse(res["isError"], res["content"][0]["text"])
        finally:
            fake.close()

    def test_delete_and_game_code_name_what_ran(self):
        fake = self.fake()
        try:
            deleted = fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Main/Keeper"})
            self.assertTrue(deleted["content"][0]["text"].startswith("game-editor did: delete_node delete /Main/Keeper"))
            coded = fake.bridge.call("game_do", {"op": "run_game_code", "code": "var score = 1\n" + ("x" * 200)})
            self.assertTrue(coded["content"][0]["text"].startswith("game-editor did: run_game_code var score = 1"))
            self.assertLess(len(coded["content"][0]["text"]), 160)
        finally:
            fake.close()

    def test_headless_start_hint_is_not_added_to_a_busy_port(self):
        bridge = B.Bridge(argv=[sys.executable, "-c", "raise SystemExit"])

        class Child:
            def stop(self):
                return None

            def __init__(self, text):
                self.text = text
                self.proc = None

            def stderr_text(self):
                return self.text

        hinted = bridge._start_failure(Child("BACKEND_START_FAILED before becoming healthy"), {})
        self.assertIn("GODOT_AI_ALLOW_HEADLESS=1", hinted)
        busy = bridge._start_failure(Child("godot-ai attach [PORT_OCCUPIED]: Port 8000 is occupied"), {})
        self.assertTrue(busy.startswith("godot-ai attach [PORT_OCCUPIED]"))
        self.assertNotIn("GODOT_AI_ALLOW_HEADLESS", busy)


class LiveGodotTest(unittest.TestCase):
    """Needs a real Godot editor. Set GAME_EDITOR_LIVE_PYTHON (a Python with godot-ai 4.3.0),
    GAME_EDITOR_GODOT (the Godot binary) and GAME_EDITOR_PROJECT (a project with the Godot AI add-on)."""

    def test_scene_tree_from_a_live_editor(self):
        py = os.environ.get("GAME_EDITOR_LIVE_PYTHON")
        godot = os.environ.get("GAME_EDITOR_GODOT")
        project = os.environ.get("GAME_EDITOR_PROJECT")
        if not (py and godot and project):
            self.skipTest("GAME_EDITOR_LIVE_PYTHON, GAME_EDITOR_GODOT and GAME_EDITOR_PROJECT are not set")
        env = dict(os.environ, GODOT_AI_ALLOW_HEADLESS="1", GODOT_AI_DISABLE_TELEMETRY="true",
                   FASTMCP_CHECK_FOR_UPDATES="off")
        editor = subprocess.Popen([godot, "--headless", "--editor", "--path", project], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        bridge = B.Bridge(argv=[py, "-I", "-m", "godot_ai", "attach", "--port", "8000", "--ws-port", "9500",
                                "--disable-telemetry"], env=env)
        try:
            deadline = time.time() + 180
            res = {}
            while time.time() < deadline:
                res = bridge.call("scene_look", {"op": "scene_tree"})
                if not res.get("isError"):
                    break
                time.sleep(3)
            self.assertFalse(res.get("isError"), res)
            self.assertIn('"path"', res["content"][0]["text"])
        finally:
            bridge.shutdown()
            editor.terminate()
            editor.wait(timeout=30)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][1]["text"])


class PlayExportTest(unittest.TestCase):
    def test_play_report_names_each_kind(self):
        steps = [
            {"op": "wait", "seconds": 0.4, "pressed": True},
            {"op": "action", "action": "ui_right", "pressed": True, "seconds": 1.2},
        ]
        samples = [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 10, "errors": [],
             "nodes": {"/root/Main/Player": {"name": "Player", "found": True,
                                             "props": {"position": {"x": 100, "y": 280}}}}},
            {"step": 1, "seconds": 1.2, "timed_out": False, "process_frames": 12, "errors": ["SCRIPT ERROR: boom"],
             "nodes": {
                 "/root/Main/Player": {"name": "Player", "found": True, "props": {"position": {"x": 102, "y": 281}}},
                 "/root/Main/StuckCoin": {"name": "StuckCoin", "found": True,
                                          "props": {"position": {"x": 100, "y": 280}}},
                 "/root/Main/HUD/Score": {"name": "Score", "found": True, "props": {"text": "Score: 0"}},
             }},
        ]
        report = B.build_play_report("look", steps, samples, {}, True, 1.2, 120)
        kinds = {item["kind"] for item in report["findings"]}
        self.assertEqual(kinds, {"error", "frame_drop", "idle"})
        self.assertNotIn("uncollected", kinds)
        self.assertFalse(report["goal_checked"])
        self.assertIsNone(report["goal_met"])
        self.assertFalse(report["ok"])
        self.assertEqual(report["failure"], "error")
        self.assertTrue(report["findings"][0]["repro"][0].startswith("Run the main scene"))
        fall = B.build_play_report("score >= 1", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": True,
                                        "props": {"position": {"x": 1, "y": 100}, "text": "Score: 0"}}}},
            {"step": 1, "seconds": 0.8, "timed_out": False, "process_frames": 60, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": True,
                                        "props": {"position": {"x": 1, "y": 400}, "text": "Score: 0"}}}},
        ], {}, True, 1.2, 120)
        self.assertIn("fall", [item["kind"] for item in fall["findings"]])
        self.assertFalse(fall["ok"])
        self.assertIn("increased", [item["detail"] for item in fall["findings"] if item["kind"] == "fall"][0])
        jump = B.build_play_report("look", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": True, "props": {"position": {"x": 1, "y": 100}}}}},
            {"step": 1, "seconds": 0.8, "timed_out": False, "process_frames": 60, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": True, "props": {"position": {"x": 1, "y": 400}}}}},
        ], {}, True, 1.2, 120)
        self.assertNotIn("fall", [item["kind"] for item in jump["findings"]])
        # "look" is not a judged goal, so the report is not a pass even with no findings.
        self.assertFalse(jump["ok"])
        self.assertEqual(jump["failure"], "goal_not_checked")
        self.assertFalse(jump["goal_checked"])
        gone = B.build_play_report("score >= 1", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": True,
                                        "props": {"position": {"x": 1, "y": 100}, "text": "Score: 0"}}}},
            {"step": 1, "seconds": 0.4, "timed_out": False, "process_frames": 40, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": False, "props": {"text": "Score: 0"}}}},
        ], {}, True, 1.2, 120)
        self.assertIn("gone", [item["detail"] for item in gone["findings"] if item["kind"] == "fall"][0])
        self.assertFalse(gone["ok"])
        stall = B.build_play_report("look", steps, [
            {"step": 1, "seconds": 0.6, "timed_out": True, "process_frames": None, "errors": [], "nodes": {}},
        ], {}, True, 1.2, 120)
        self.assertEqual(stall["findings"][0]["kind"], "stall")
        self.assertFalse(stall["ok"])
        self.assertEqual(stall["failure"], "stall")
        frozen = B.build_play_report("look", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 5, "errors": [], "nodes": {}},
            {"step": 1, "seconds": 0.5, "timed_out": False, "process_frames": 5, "errors": [], "nodes": {}},
        ], {}, True, 1.2, 120)
        self.assertEqual(frozen["findings"][0]["kind"], "stall")
        slope = B.build_play_report("look", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True, "props": {"position": {"x": 0, "y": 100}}}}},
            {"step": 1, "seconds": 0.8, "timed_out": False, "process_frames": 60, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True, "props": {"position": {"x": 200, "y": 400}}}}},
        ], {}, True, 1.2, 120)
        self.assertNotIn("fall", [item["kind"] for item in slope["findings"]])
        self.assertEqual(slope["failure"], "goal_not_checked")
        judged_slope = B.build_play_report("score >= 1", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True,
                                       "props": {"position": {"x": 0, "y": 100}, "text": "Score: 0"}}}},
            {"step": 1, "seconds": 0.8, "timed_out": False, "process_frames": 60, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True,
                                       "props": {"position": {"x": 200, "y": 400}, "text": "Score: 0"}}}},
        ], {}, True, 1.2, 120)
        slope_fall = [item for item in judged_slope["findings"] if item["kind"] == "fall"]
        self.assertEqual(len(slope_fall), 1)
        self.assertIn("while x moved", slope_fall[0]["detail"])
        self.assertFalse(judged_slope["ok"])
        missed = B.build_play_report("score >= 10 and /Main/Door stays", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True,
                                       "props": {"position": {"x": 1, "y": 100}, "text": "Score: 0"}}}},
            {"step": 1, "seconds": 0.4, "timed_out": False, "process_frames": 40, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True,
                                       "props": {"position": {"x": 1, "y": 100}, "text": "Score: 0"}}}},
        ], {}, True, 1.2, 120)
        self.assertTrue(missed["goal_checked"])
        self.assertFalse(missed["goal_met"])
        self.assertEqual(missed["failure"], "goal_missed")
        self.assertIn("goal", {item["kind"] for item in missed["findings"]})
        met = B.build_play_report("score at least 2 and /Main/Actor stays", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True,
                                       "props": {"position": {"x": 1, "y": 100}, "text": "Score: 2"}}}},
        ], {}, True, 1.2, 120)
        self.assertTrue(met["goal_checked"])
        self.assertTrue(met["goal_met"])
        self.assertNotIn("goal", {item["kind"] for item in met["findings"]})
        down = B.build_play_report("look", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True, "props": {"position": {"x": 1, "y": 400}}}}},
            {"step": 1, "seconds": 0.8, "timed_out": False, "process_frames": 60, "errors": [],
             "nodes": {"/Main/Actor": {"name": "Actor", "found": True, "props": {"position": {"x": 1, "y": 100}}}}},
        ], {}, True, 1.2, 120)
        self.assertNotIn("fall", [item["kind"] for item in down["findings"]])
        self.assertEqual(down["failure"], "goal_not_checked")
        pit = B.build_play_report("score >= 1", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Body": {"name": "Body", "found": True,
                                      "props": {"position": {"x": 0, "y": 10, "z": 0}, "text": "Score: 0"}}}},
            {"step": 1, "seconds": 0.8, "timed_out": False, "process_frames": 60, "errors": [],
             "nodes": {"/Main/Body": {"name": "Body", "found": True,
                                      "props": {"position": {"x": 0, "y": -400, "z": 0}, "text": "Score: 0"}}}},
        ], {}, True, 1.2, 120)
        pit_fall = [item for item in pit["findings"] if item["kind"] == "fall"]
        self.assertEqual(len(pit_fall), 1)
        self.assertIn("decreased", pit_fall[0]["detail"])
        self.assertFalse(pit["ok"])
        overlap = B.build_play_report("look", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/root/Floor": {"name": "Floor", "found": True, "props": {"position": {"x": 0, "y": 400},
                                                                                 "text": "Score: 0"}},
                       "/root/Hero": {"name": "Hero", "found": True, "props": {"position": {"x": 10, "y": 390}}}}},
        ], {}, True, 1.2, 120)
        self.assertNotIn("uncollected", [item["kind"] for item in overlap["findings"]])
        self.assertEqual(overlap["failure"], "goal_not_checked")
        self.assertFalse(overlap["goal_checked"])
        coin = B.build_play_report("コインを取る", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": True, "props": {"position": {"x": 1, "y": 100}}}}},
            {"step": 1, "seconds": 0.8, "timed_out": False, "process_frames": 60, "errors": [],
             "nodes": {"/Main/Player": {"name": "Player", "found": True, "props": {"position": {"x": 40, "y": 100}}}}},
        ], {}, True, 1.2, 120)
        self.assertFalse(coin["goal_checked"])
        self.assertIsNone(coin["goal_met"])
        # A Japanese goal is not judged, so it is not reported as a pass.
        self.assertFalse(coin["ok"])
        self.assertEqual(coin["failure"], "goal_not_checked")
        empty = B.build_play_report("look", steps, [
            {"step": 0, "seconds": 0.4, "timed_out": False, "process_frames": 30, "errors": ["SCRIPT ERROR: boom"],
             "nodes": {}},
        ], {}, True, 1.2, 120, watched=False)
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["failure"], "position_unwatched")
        self.assertEqual({item["kind"] for item in empty["findings"]}, {"error", "unwatched"})
        self.assertEqual(B._log_errors({"lines": [
            {"level": "info", "text": "[godot_ai game_helper] registered"},
            {"level": "error", "text": "addons/godot_ai/plugin.gd boom"},
            {"level": "error", "text": "SCRIPT ERROR: real"},
        ]}), ["SCRIPT ERROR: real"])

    def test_parse_play_and_export_reject_bad_input(self):
        bad = [
            {"goal": " ", "steps": [{"op": "wait"}]},
            {"goal": "see", "steps": []},
            {"goal": "see", "steps": [{"op": "mouse", "seconds": 0.1}]},
            {"goal": "see", "steps": [{"op": "wait", "seconds": True}]},
            {"goal": "see", "steps": [{"op": "wait", "seconds": 3}]},
            {"goal": "see", "steps": [{"op": "wait", "seconds": 2}] * 7},
            {"goal": "see", "steps": [{"op": "wait"}] * 17},
            {"goal": "see", "steps": [{"op": "action", "action": "ui right"}]},
            {"goal": "see", "steps": [{"op": "wait"}], "watch": ["../Player"]},
            {"goal": "see", "steps": [{"op": "wait"}], "watch": ["/root/../Player"]},
            {"goal": "see", "steps": [{"op": "wait"}], "idle_seconds": True},
            {"goal": "see", "steps": [{"op": "wait"}], "mode": "custom"},
        ]
        for keys in bad:
            parsed, err = B.parse_play(keys)
            self.assertIsNone(parsed, keys)
            self.assertIn("Nothing was run", err)
        self.assertIsNone(B.parse_export({"platform": "pc"})[0])
        self.assertIsNone(B.parse_export({"platform": "android", "serve": True})[0])
        self.assertIn("Nothing was exported", B.parse_export({"platform": "web", "output": "user://x"})[1])
        need = B.template_need("9.9.9", Path("/no/such/templates"), "web")
        self.assertIsNone(need["acquire"]["bytes"])
        self.assertFalse(need["downloaded"])
        ready = Path(tempfile.mkdtemp())
        folder = ready / "4.7.2.stable"
        folder.mkdir()
        for name in B.TEMPLATE_FILES["web"]:
            (folder / name).write_text("x")
        self.assertIsNone(B.template_need("4.7.2", ready, "web"))
        source = (ROOT / "bridge" / "game_editor_bridge.py").read_text(encoding="utf-8")
        self.assertNotIn("urlopen", source)
        self.assertNotIn("urlretrieve", source)

    def _bridge(self) -> tuple[object, list]:
        bridge = B.Bridge(argv=[sys.executable, "-c", "import sys; sys.exit(0)"])
        bridge._sleep = lambda _seconds: None
        calls = []

        def upstream(name, arguments, timeout=None, mid=None):
            calls.append((name, arguments, timeout))
            if name == "project_run":
                body = {"helper_live": True, "game_status": {"status": "live"}}
            elif name == "game_manage" and arguments.get("op") == "debug_status":
                body = {"state": {"process_frames": 30 + len(calls)}}
            elif name == "game_manage" and arguments.get("op") == "get_node_info":
                body = {"name": "Player", "found": True, "properties": {"position": {"x": 10, "y": 20}}}
            elif name == "logs_read":
                body = {"lines": []}
            else:
                body = {}
            return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False}

        bridge.upstream = upstream
        return bridge, calls

    def test_play_session_sends_the_script_and_stops(self):
        bridge, calls = self._bridge()
        result = bridge.play_session({
            "goal": "walk",
            "steps": [{"op": "wait", "seconds": 0.4},
                      {"op": "action", "action": "ui_right", "pressed": True, "seconds": 0.6},
                      {"op": "key", "key": "Space", "pressed": False, "seconds": 0}],
            "watch": ["/root/Main/Player"],
        }, "/proj", None)
        # "walk" is not judged: ok is false with goal_not_checked, and the summary says so.
        self.assertFalse(result["isError"])  # a report; ok is false in the payload
        self.assertTrue(result["content"][0]["text"].startswith(
            "game-editor: play_session ok=false failure=goal_not_checked goal_checked=false"))
        self.assertIn("The game did play", result["content"][0]["text"])
        names = [(name, (args.get("op") if isinstance(args, dict) else None)) for name, args, _ in calls]
        self.assertEqual(names[0], ("project_manage", "stop"))
        run = next(i for i, (name, _args, _timeout) in enumerate(calls) if name == "project_run")
        self.assertEqual([name for name, _args, _timeout in calls[:run]],
                         ["project_manage", "logs_read", "logs_read"])
        self.assertFalse(calls[run][1]["autosave"])
        self.assertIn(("game_manage", "input_action"), names)
        self.assertIn(("game_manage", "input_key"), names)
        self.assertIn(("logs_read", None), names)
        self.assertEqual(names[-1], ("project_manage", "stop"))
        shot = [args for name, args, _ in calls if name == "editor_screenshot"]
        # 0.9.0: a small picture after each step (for the findings' evidence), then the closing one.
        self.assertEqual(shot[-1], {"source": "game", "include_image": True, "max_resolution": 320})
        self.assertTrue(all(item == {"source": "game", "include_image": True, "max_resolution": 240} for item in shot[:-1]))
        payload = _payload(result)
        self.assertIn("replay", payload)
        self.assertTrue(all("evidence" in item for item in payload["findings"]))
        self.assertLessEqual(len(_payload(result)["samples"]), 3)

    def test_play_session_timeout_skips_the_rest_and_still_stops(self):
        bridge, calls = self._bridge()
        seen = {"debug": 0}

        def upstream(name, arguments, timeout=None, mid=None):
            calls.append((name, arguments, timeout))
            if name == "game_manage" and arguments.get("op") == "debug_status":
                seen["debug"] += 1
                if seen["debug"] == 1:
                    return {"bridge_error": "timeout"}
            if name == "project_run":
                body = {"helper_live": True, "game_status": {"status": "live"}}
                return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False}
            return {"content": [{"type": "text", "text": "{}"}], "isError": False}

        bridge.upstream = upstream
        result = bridge.play_session({
            "goal": "loop",
            "steps": [{"op": "action", "action": "ui_right", "seconds": 0.2},
                      {"op": "action", "action": "ui_left", "seconds": 0.2}],
            "watch": ["/root/Main/Player"],
        }, "/proj", None)
        self.assertFalse(result["isError"])  # a report; ok is false in the payload
        self.assertIn("ok=false", result["content"][0]["text"])
        self.assertEqual(_payload(result)["failure"], "stall")
        self.assertEqual(_payload(result)["findings"][0]["kind"], "stall")
        self.assertEqual(sum(1 for name, args, _ in calls if name == "game_manage" and args.get("op") == "input_action"), 1)
        self.assertNotIn("editor_screenshot", [name for name, _, _ in calls])
        self.assertEqual(calls[-1][0], "project_manage")

    def test_play_session_not_live_invents_nothing(self):
        bridge, calls = self._bridge()

        def upstream(name, arguments, timeout=None, mid=None):
            calls.append((name, arguments, timeout))
            if name == "project_run":
                return {"content": [{"type": "text", "text": "game did not start"}], "isError": True}
            return {"content": [{"type": "text", "text": "{}"}], "isError": False}

        bridge.upstream = upstream
        result = bridge.play_session({"goal": "walk", "steps": [{"op": "wait", "seconds": 0.4}]}, "/proj", None)
        self.assertTrue(result["isError"])
        self.assertIn("not_live", result["content"][0]["text"])
        self.assertEqual(_payload(result)["findings"], [])
        self.assertEqual([name for name, _, _ in calls],
                         ["project_manage", "logs_read", "logs_read", "project_run",
                          "logs_read", "logs_read", "project_manage"])

    def test_a_dead_run_names_the_script_line(self):
        root = tempfile.TemporaryDirectory()
        try:
            scripts = Path(root.name) / "scripts"
            scripts.mkdir()
            (scripts / "boom.gd").write_text("extends Node2D\nfunc _ready():\n\tprint(missing)\n", encoding="utf-8")
            bridge, calls = self._bridge()
            # logs_read puts path and line beside the message. The Output panel's "at:" line is not in text.
            lines = {"lines": [
                {"level": "error", "source": "editor",
                 "text": 'Parse Error: Identifier "missing" not declared in the current scope.',
                 "path": "res://scripts/boom.gd", "line": 3.0, "function": "GDScript::reload"},
                {"level": "error", "source": "editor",
                 "text": 'Failed to load script "res://scripts/boom.gd" with error "Parse error".',
                 "path": "res://scripts/boom.gd", "line": 0, "function": ""},
                {"level": "error", "source": "editor",
                 "text": 'Condition "ret != noErr" is true. Returning: ""',
                 "path": "platform/macos/os_macos.mm", "line": 1035, "function": "get_system_ca_certificates"},
            ]}

            def upstream(name, arguments, timeout=None, mid=None):
                calls.append((name, arguments, timeout))
                ran = any(item[0] == "project_run" for item in calls)
                if name == "project_run":
                    body = {"game_status": {"status": "stopped"}}
                elif name == "logs_read" and ran:
                    body = lines
                else:
                    body = {}
                return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False}

            bridge.upstream = upstream
            result = bridge.play_session({
                "goal": "score >= 1", "mode": "custom", "scene": "res://scenes/boom.tscn",
                "steps": [{"op": "wait", "seconds": 0.4}],
            }, root.name, None)
            self.assertFalse(result["isError"], result["content"][0]["text"])
            payload = _payload(result)
            self.assertEqual(payload["failure"], "error")
            self.assertFalse(payload["oracle"]["playing"])
            named = [item for item in payload["findings"] if item.get("file")]
            self.assertEqual(len(named), 1, payload["findings"])
            self.assertEqual(named[0]["kind"], "error")
            self.assertEqual(named[0]["file"], "res://scripts/boom.gd")
            self.assertEqual(named[0]["line"], 3)
            self.assertEqual(named[0]["code"], "\tprint(missing)")
            self.assertIn("boom.gd", named[0]["next"])
            self.assertTrue(any(name == "logs_read" for name, _, _ in calls))
        finally:
            root.cleanup()

    def test_play_ignores_an_error_already_in_the_log(self):
        root = tempfile.TemporaryDirectory()
        try:
            scripts = Path(root.name) / "scripts"
            scripts.mkdir()
            (scripts / "level.gd").write_text("extends Node2D\nvar score := 0\nfunc _ready():\n\tmissing\n",
                                              encoding="utf-8")
            bridge, calls = self._bridge()
            old = {"level": "error", "source": "editor",
                   "text": 'Parse Error: Identifier "bad2" not declared in the current scope.',
                   "path": "res://scripts/bad2.gd", "line": 3}
            fresh = {"level": "error", "source": "editor",
                     "text": 'Parse Error: Identifier "missing" not declared in the current scope.',
                     "path": "res://scripts/level.gd", "line": 4}

            def upstream(name, arguments, timeout=None, mid=None):
                calls.append((name, arguments, timeout))
                ran = any(item[0] == "project_run" for item in calls)
                if name == "project_run":
                    body = {"helper_live": True, "game_status": {"status": "live"}}
                elif name == "game_manage" and arguments.get("op") == "debug_status":
                    body = {"state": {"process_frames": 40}}
                elif name == "logs_read":
                    body = {"lines": [old] + ([fresh] if ran else [])}
                else:
                    body = {}
                return {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False}

            bridge.upstream = upstream
            result = bridge.play_session({
                "goal": "walk", "steps": [{"op": "wait", "seconds": 0.4}],
            }, root.name, None)
            self.assertFalse(result["isError"], result["content"][0]["text"])
            payload = _payload(result)
            errors = [item for item in payload["findings"] if item.get("kind") == "error"]
            self.assertEqual(len(errors), 1, payload["findings"])
            self.assertIn("level.gd", errors[0]["detail"])
            self.assertNotIn("bad2.gd", errors[0]["detail"])
            self.assertEqual(errors[0].get("file"), "res://scripts/level.gd")
            self.assertEqual(errors[0].get("line"), 4)
        finally:
            root.cleanup()

    def test_play_session_releases_held_input_and_empty_watch_is_not_success(self):
        bridge, calls = self._bridge()
        result = bridge.play_session({
            "goal": "walk",
            "steps": [{"op": "action", "action": "ui_right", "pressed": True, "seconds": 0.4}],
            "watch": ["/root/Main/Player"],
        }, "/proj", None)
        self.assertEqual(_payload(result)["failure"], "goal_not_checked")
        presses = [args for name, args, _ in calls if name == "game_manage" and args.get("op") == "input_action"]
        self.assertEqual([item["params"]["pressed"] for item in presses], [True, False])
        bare, _calls = self._bridge()
        empty = bare.play_session({
            "goal": "walk",
            "steps": [{"op": "wait", "seconds": 0.4}],
        }, "/proj", None)
        # 0.9.0: a play that ran is a report (not an error, which Hermes cuts to 2,048 characters); ok is false.
        self.assertFalse(empty["isError"])
        self.assertFalse(_payload(empty)["ok"])
        self.assertEqual(_payload(empty)["failure"], "position_unwatched")
        self.assertIn("unwatched", {item["kind"] for item in _payload(empty)["findings"]})

    def test_macos_export_is_a_platform(self):
        parsed, err = B.parse_export({"platform": "macos"})
        self.assertEqual(err, "")
        self.assertEqual(parsed["output"], "build/macos/Game.app")
        self.assertEqual(B.EXPORT_PLATFORM["macos"], "macOS")
        note = B.export_failure_note(
            "macos",
            "ETC2 ASTC テクスチャフォーマットが無効になっている場合、ユニバーサルまたは arm64 用にエクスポートできません。")
        self.assertIn("import_etc2_astc", note)
        self.assertIn("did not change that setting", note)
        bundle = B.export_failure_note("macos", "無効なバンドルID: 識別子が欠落しています。")
        self.assertIn("application/bundle_identifier", bundle)
        self.assertIn("export_presets.cfg", bundle)
        self.assertEqual(B.export_failure_note("web", "ETC2"), "")

    def test_macos_export_writes_the_preset_and_build(self):
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "proj"
        proj.mkdir()
        self._templates(tmp, "macos")
        bridge, log = self._export_bridge(tmp)
        result = bridge.export_game({"platform": "macos"}, str(proj))
        self.assertFalse(result["isError"], result)
        body = _payload(result)
        self.assertTrue(body["ok"])
        self.assertEqual(body["output"], "build/macos/Game.app")
        self.assertTrue((proj / "build" / "macos" / "Game.app" / "ok.txt").is_file())
        preset = (proj / "export_presets.cfg").read_text(encoding="utf-8")
        self.assertIn('platform="macOS"', preset)
        self.assertNotIn("script_encryption_key", preset)
        ran = [json.loads(line) for line in log.read_text().splitlines()]
        exported = [row for row in ran if any(arg.startswith("--export") for arg in row)]
        self.assertEqual(len(exported), 1)
        self.assertIn("--export-debug", exported[0])
        self.assertIn("macOS", exported[0])
        self.assertTrue(exported[0][-1].endswith("Game.app"))

    def test_bad_play_arguments_do_not_start_the_game(self):
        child = FakeChild(project=Path("."))
        try:
            result = child.bridge.call("game_do", {"op": "play_session", "goal": "", "steps": [{"op": "wait"}]})
        finally:
            child.close()
        self.assertTrue(result["isError"])
        self.assertNotIn("project_run", [row["name"] for row in child.sent()])

    def _fake_godot(self, log: Path) -> Path:
        path = log.parent / "godot"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"log = {str(log)!r}\n"
            "open(log, 'a', encoding='utf-8').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if '--version' in sys.argv:\n"
            "    print('4.7.2.stable.official.test')\n"
            "    raise SystemExit(0)\n"
            "if any(a.startswith('--export') for a in sys.argv):\n"
            "    dest = sys.argv[-1]\n"
            "    os.makedirs(os.path.dirname(dest), exist_ok=True)\n"
            "    if dest.endswith('.html') or dest.endswith('.apk'):\n"
            "        open(dest, 'w', encoding='utf-8').write('<html>play</html>')\n"
            "    else:\n"
            "        os.makedirs(dest, exist_ok=True)\n"
            "        open(os.path.join(dest, 'ok.txt'), 'w', encoding='utf-8').write('ok')\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(97)\n",
            encoding="utf-8")
        path.chmod(0o755)
        return path

    def _templates(self, root: Path, platform: str = "web") -> Path:
        folder = root / "templates" / "4.7.2.stable"
        folder.mkdir(parents=True)
        for name in B.TEMPLATE_FILES[platform]:
            (folder / name).write_text("template")
        return root / "templates"

    def _export_bridge(self, tmp: Path) -> tuple[object, Path]:
        log = tmp / "godot.log"
        bridge = B.Bridge(argv=[sys.executable, "-c", "import sys; sys.exit(0)"])
        bridge.godot_binary = lambda: str(self._fake_godot(log))
        bridge.export_template_root = lambda: tmp / "templates"
        return bridge, log

    def test_missing_templates_do_not_export_or_download(self):
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "proj"
        proj.mkdir()
        bridge, log = self._export_bridge(tmp)
        result = bridge.export_game({"platform": "web"}, str(proj))
        self.assertTrue(result["isError"])
        self.assertTrue(result["content"][0]["text"].startswith(
            "game-editor: export_templates_missing. Nothing was downloaded and nothing was written."))
        body = _payload(result)
        self.assertEqual(body["failure"], "export_templates_missing")
        self.assertFalse(body["downloaded"])
        self.assertEqual(body["acquire"]["bytes"], 1281349702)
        self.assertIn("Godot_v4.7.2-stable_export_templates.tpz", body["acquire"]["file"])
        self.assertIn("github.com/godotengine/godot-builds", body["acquire"]["source"])
        self.assertFalse((proj / "export_presets.cfg").exists())
        ran = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertTrue(ran)
        self.assertTrue(all("--version" in row for row in ran))
        self.assertFalse(any(any(arg.startswith("--export") for arg in row) for row in ran))

    def test_web_export_serves_on_localhost_and_shutdown_stops_it(self):
        import socket
        import urllib.request
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        try:
            socket.create_connection(probe.getsockname(), timeout=1).close()
        except RuntimeError as exc:
            self.skipTest(f"connections to 127.0.0.1 are blocked in this run ({exc})")
        finally:
            probe.close()
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "proj"
        proj.mkdir()
        self._templates(tmp, "web")
        bridge, log = self._export_bridge(tmp)
        try:
            result = bridge.export_game({"platform": "web", "serve": True}, str(proj))
            self.assertFalse(result["isError"], result)
            body = _payload(result)
            self.assertTrue(body["ok"])
            self.assertTrue(body["served"])
            self.assertTrue(body["url"].startswith("http://127.0.0.1:"))
            self.assertNotIn("/Us" + "ers/", body["url"])
            page = urllib.request.urlopen(body["url"] + "index.html", timeout=2).read()
            self.assertIn(b"play", page)
            preset = (proj / "export_presets.cfg").read_text(encoding="utf-8")
            self.assertIn('platform="Web"', preset)
            self.assertNotIn("password", preset.lower())
            ran = [json.loads(line) for line in log.read_text().splitlines()]
            exported = [row for row in ran if any(arg.startswith("--export") for arg in row)]
            self.assertEqual(len(exported), 1)
            self.assertIn("--export-debug", exported[0])
            self.assertIn("Web", exported[0])
        finally:
            bridge.shutdown()
        self.assertIsNone(bridge.web_server)
        self.assertRaises(OSError, urllib.request.urlopen, body["url"] + "index.html", timeout=1)

    def test_export_refuses_secrets_outside_paths_and_existing_files(self):
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "proj"
        proj.mkdir()
        self._templates(tmp, "web")
        bridge, log = self._export_bridge(tmp)
        outside = bridge.export_game({"platform": "web", "output": str(tmp / "out.html")}, str(proj))
        self.assertIn("outside the project", outside["content"][0]["text"])
        secret = bridge.export_game({"platform": "web", "output": "build/export_credentials.cfg"}, str(proj))
        self.assertIn("refused", secret["content"][0]["text"])
        source = bridge.export_game({"platform": "web", "output": "build/game.gd"}, str(proj))
        self.assertIn("source or", source["content"][0]["text"])
        dest = proj / "build" / "web" / "index.html"
        dest.parent.mkdir(parents=True)
        dest.write_text("old")
        again = bridge.export_game({"platform": "web"}, str(proj))
        self.assertIn("already exists", again["content"][0]["text"])
        self.assertEqual(dest.read_text(encoding="utf-8"), "old")
        self.assertFalse((proj / "export_presets.cfg").exists())
        sibling = proj / "build" / "web" / "game.js"
        sibling.write_text("keep")
        beside = bridge.export_game({"platform": "web", "output": "build/web/game.html"}, str(proj))
        self.assertIn("already exists", beside["content"][0]["text"])
        self.assertIn("game.js", beside["content"][0]["text"])
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep")
        self.assertFalse((proj / "build" / "web" / "game.html").exists())
        source_beside = proj / "build" / "web" / "game.gd"
        source_beside.write_text("extends Node\n")
        forced = bridge.export_game({"platform": "web", "output": "build/web/game.html", "overwrite": True}, str(proj))
        self.assertIn("source or", forced["content"][0]["text"])
        self.assertEqual(source_beside.read_text(encoding="utf-8"), "extends Node\n")
        self.assertFalse((proj / "export_presets.cfg").exists())
        ran = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        self.assertFalse(any(any(arg.startswith("--export") for arg in row) for row in ran))
        link = proj / "linked"
        link.symlink_to(tmp)
        escaped = bridge.export_game({"platform": "web", "output": "linked/page.html"}, str(proj))
        self.assertIn("outside the project", escaped["content"][0]["text"])

    def test_android_without_sdk_does_not_export(self):
        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "proj"
        proj.mkdir()
        self._templates(tmp, "android")
        (tmp / "templates" / "4.7.2.stable" / "version.txt").write_text("4.7.2")
        bridge, log = self._export_bridge(tmp)
        empty = tmp / "sdk"
        empty.mkdir()
        result = bridge.export_game({"platform": "android", "android_sdk": str(empty)}, str(proj))
        self.assertTrue(result["isError"])
        self.assertTrue(result["content"][0]["text"].startswith(
            "game-editor: android_sdk_missing. Nothing was downloaded and nothing was exported."))
        self.assertEqual(_payload(result)["sdk"], "the android_sdk path")
        self.assertFalse((proj / "export_presets.cfg").exists())
        ran = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertFalse(any(any(arg.startswith("--export") for arg in row) for row in ran))
        (empty / "platform-tools").mkdir()
        built = bridge.export_game({"platform": "android", "android_sdk": str(empty), "debug": False}, str(proj))
        self.assertFalse(built["isError"], built)
        preset = (proj / "export_presets.cfg").read_text(encoding="utf-8")
        self.assertIn("gradle_build/use_gradle_build=false", preset)
        self.assertIn("permissions/internet=false", preset)
        self.assertNotIn("password", preset.lower())
        ran = [json.loads(line) for line in log.read_text().splitlines()]
        exported = [row for row in ran if "--export-release" in row]
        self.assertEqual(len(exported), 1)


class PlanApplyTest(unittest.TestCase):
    def test_quit_plan_names_unsaved_work_and_apply_is_once(self):
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn", nodes=list(LEVEL_NODES))
        try:
            # Planning does not ask the editor. The list is not known yet, so the plan does not invent one.
            before = fake.calls.read_text(encoding="utf-8") if fake.calls.exists() else ""
            bare = fake.bridge.call("plan_change", {"steps": [{"tool": "editor_do", "op": "quit"}]})
            self.assertFalse(bare["isError"], bare["content"][0]["text"])
            text = bare["content"][0]["text"]
            self.assertIn("plan_id:", text)
            self.assertIn("discards unsaved work", text)
            self.assertNotIn("position:", text)
            self.assertNotIn("discards no unsaved value", text)
            planned_rows = [json.loads(line) for line in fake.calls.read_text(encoding="utf-8")[len(before):].splitlines()
                            if line.strip()]
            sent = {(row["name"], (row.get("arguments") or {}).get("op")) for row in planned_rows}
            self.assertNotIn(("editor_state", None), sent)
            self.assertNotIn(("scene_manage", "get_roots"), sent)
            self.assertNotIn(("scene_get_hierarchy", None), sent)
            self.assertIn(("session_manage", "list"), sent)
            # The editor matches the file: apply_plan drops the placeholder and quit runs. One approval.
            applied = fake.bridge.call("apply_plan", {"plan_id": _plan_id_of(bare)})
            self.assertFalse(applied["isError"], applied["content"][0]["text"])
            self.assertNotIn("needs a second approval", applied["content"][0]["text"])
            self.assertTrue(any(row["name"] == "editor_manage" and (row.get("arguments") or {}).get("op") == "quit"
                                for row in fake.sent()))
        finally:
            fake.close()
            project.close()
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn",
                         nodes=list(LEVEL_NODES) + ["/Level/OnlyHere"],
                         props={"/Level/Enemies/Enemy3": {"position": {"x": 11, "y": 12}}})
        try:
            added = fake.bridge.call("scene_do", {"op": "add_node", "type": "Node2D", "name": "Hit",
                                                  "parent_path": "/Level"})
            self.assertFalse(added["isError"], added["content"][0]["text"])
            planned = fake.bridge.call("plan_change", {"steps": [{"tool": "editor_do", "op": "quit"}]})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            self.assertNotIn("position:", planned["content"][0]["text"])
            denied = fake.bridge.call("apply_plan", {"plan_id": "missing"})
            self.assertIn("does not know this plan_id", denied["content"][0]["text"])
            applied = fake.bridge.call("apply_plan", {"plan_id": _plan_id_of(planned)})
            self.assertFalse(applied["isError"], applied["content"][0]["text"])
            self.assertIn("needs a second approval", applied["content"][0]["text"])
            self.assertIn('position: disk Vector2(1, 2), editor {"x": 11, "y": 12}', applied["content"][0]["text"])
            self.assertIn("/Level/OnlyHere", applied["content"][0]["text"])
            self.assertIn("/Level/Hit", applied["content"][0]["text"])
            self.assertNotIn("script null", applied["content"][0]["text"])
            self.assertFalse(any(row["name"] == "editor_manage" for row in fake.sent()))
            fake.bridge.created_local.setdefault("res://scenes/level.tscn", set()).add("Ghost")
            blocked = fake.bridge.call("confirm_risky_plan", {"plan_id": _plan_id_of(planned)})
            self.assertTrue(blocked["isError"], blocked["content"][0]["text"])
            self.assertIn("Ghost", blocked["content"][0]["text"])
            self.assertFalse(any(row["name"] == "editor_manage" for row in fake.sent()))
        finally:
            fake.close()
            project.close()

    def test_overwrite_confirms_a_script_this_session_attached(self):
        project = Project()
        (project.root / "scenes" / "play.tscn").write_text(
            '[gd_scene format=3]\n\n[node name="Play" type="Node2D"]\n', encoding="utf-8")
        (project.root / "actors" / "mover.gd").write_text("extends Node2D\nvar score := 0\n", encoding="utf-8")
        fake = FakeChild(project=project.root, scene="res://scenes/play.tscn", nodes=["/Play"],
                         props={"/Play": {"script": "res://actors/mover.gd"}})
        try:
            fake.bridge.note_property_edit(
                "res://scenes/play.tscn", "", "script", False, "res://actors/mover.gd", True)
            compared = B.Bridge.call(fake.bridge, "plan_change", {"steps": [{"tool": "scene_do", "op": "compare"}]})
            self.assertFalse(compared["isError"], compared["content"][0]["text"])
            applied = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": _plan_id_of(compared)})
            self.assertFalse(applied["isError"], applied["content"][0]["text"])
            self.assertIn("mover.gd", applied["content"][0]["text"])
            planned = B.Bridge.call(fake.bridge, "plan_change", {"steps": [
                {"tool": "scene_do", "op": "save", "overwrite": True}]})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            self.assertIn("disk (none)", planned["content"][0]["text"])
            self.assertIn("mover.gd", planned["content"][0]["text"])
            plan_id = _plan_id_of(planned)
            first = B.Bridge.call(fake.bridge, "apply_plan", {"plan_id": plan_id})
            self.assertIn("needs a second approval", first["content"][0]["text"])
            self.assertFalse(any(row["name"] == "scene_save" for row in fake.sent()))
            done = B.Bridge.call(fake.bridge, "confirm_risky_plan", {"plan_id": plan_id})
            self.assertFalse(done["isError"], done["content"][0]["text"])
            self.assertNotIn("differ in changed", done["content"][0]["text"])
            self.assertTrue(any(row["name"] == "scene_save" for row in fake.sent()))
        finally:
            fake.close()
            project.close()

    def test_changed_file_blocks_apply(self):
        project = Project()
        scene = project.root / "scenes" / "level.tscn"
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn",
                         nodes=["/Level", "/Level/Enemies", "/Level/Enemies/Enemy6"])
        try:
            planned = fake.bridge.call("plan_change", {"steps": [
                {"tool": "scene_do", "op": "delete_node", "path": "/Level/Enemies/Enemy6"}]})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            self.assertIn("/Level/Enemies/Enemy6", planned["content"][0]["text"])
            scene.write_text(scene.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            applied = fake.bridge.call("apply_plan", {"plan_id": _plan_id_of(planned)})
            self.assertTrue(applied["isError"])
            self.assertIn("project changed", applied["content"][0]["text"])
            self.assertIn("Nothing was sent", applied["content"][0]["text"])
            self.assertFalse(any(row["name"] == "node_manage" and (row.get("arguments") or {}).get("op") == "delete"
                                 for row in fake.sent()))
        finally:
            fake.close()
            project.close()

    def test_direct_write_and_elicitation_are_absent(self):
        fake = FakeChild()
        try:
            res = fake.bridge.call("scene_do", {"op": "delete_node", "path": "/Main/A"})
            # The test wrapper plans this call. A raw dispatch refuses the tool name.
            raw = _bridge_call(fake.bridge, "scene_do", {"op": "delete_node", "path": "/Main/A"})
            self.assertTrue(raw["isError"])
            self.assertIn("plan_change", raw["content"][0]["text"])
            self.assertIn("Nothing was sent", raw["content"][0]["text"])
            self.assertNotIn("elicitation/create", json.dumps(res))
        finally:
            fake.close()

    def test_one_plan_holds_two_steps(self):
        project = Project()
        fake = FakeChild(project=project.root, scene="res://scenes/level.tscn",
                         nodes=["/Level", "/Level/Enemies", "/Level/Enemies/Enemy6"])
        try:
            planned = fake.bridge.call("plan_change", {"steps": [
                {"tool": "scene_do", "op": "rename", "path": "/Level/Enemies/Enemy6", "new_name": "Guard"},
                {"tool": "scene_do", "op": "delete_node", "path": "/Level/Enemies/Enemy6"},
            ]})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            text = planned["content"][0]["text"]
            self.assertIn("1. Godot: scene_do op rename", text)
            self.assertIn("2. Godot: scene_do op delete_node", text)
            self.assertIn("Guard", text)
            applied = fake.bridge.call("apply_plan", {"plan_id": _plan_id_of(planned)})
            self.assertIn("needs a second approval", applied["content"][0]["text"])
            applied = fake.bridge.call("confirm_risky_plan", {"plan_id": _plan_id_of(planned)})
            texts = "".join(item.get("text", "") for item in applied["content"])
            self.assertIn("\nstep 1:\n", texts)
            self.assertTrue(texts.startswith("game-editor: apply_plan step 2 of 2 failed: ") or
                            texts.startswith("game-editor: apply_plan ran all 2 steps."), texts[:200])
            names = [row["name"] for row in fake.sent()]
            self.assertIn("node_manage", names)
        finally:
            fake.close()
            project.close()


class PlanReadsOnlyTest(unittest.TestCase):
    """plan_change sends only PLAN_UPSTREAM calls: no property read (a @tool _get), no batch, no write."""

    EMBEDDED = (PREMISE.replace(
        '[sub_resource type="RectangleShape2D" id="shape1"]',
        '[sub_resource type="GDScript" id="trap"]\nscript/source = "@tool\nextends Node\n\nfunc _get(p):\n'
        '\treturn null\n"\n\n[sub_resource type="RectangleShape2D" id="shape1"]')
        .replace('[node name="Keeper" type="Node" parent="."]',
                 '[node name="Keeper" type="Node" parent="."]\nscript = SubResource("trap")'))

    def setUp(self):
        self.p = Project()
        (self.p.root / "scenes" / "premise.tscn").write_text(self.EMBEDDED)

    def tearDown(self):
        self.p.close()

    def _raw(self, fake) -> list[dict]:
        if not fake.calls.exists():
            return []
        return [json.loads(line) for line in fake.calls.read_text().splitlines() if line.strip()]

    def test_plan_sends_only_editor_state_and_tree_shape(self):
        fake = FakeChild(project=self.p.root, scene="res://scenes/premise.tscn", nodes=PREMISE_NODES,
                         layout={"/Main/Ground": {"offset_left": -1000.0, "offset_top": -20.0,
                                                  "offset_right": 1000.0, "offset_bottom": 20.0}})
        steps = [
            {"tool": "scene_do", "op": "set_node_property", "path": "/Main/Ground", "property": "color",
             "value": "#ff0000"},
            {"tool": "scene_do", "op": "set_node_property", "path": "/Main/Keeper", "property": "name",
             "value": "Keeper"},
            {"tool": "batch", "commands": [{"command": "set_property", "params": {
                "path": "/Main/Label", "property": "text", "value": "score: 1"}}]},
            {"tool": "scene_do", "op": "properties", "path": "/Main/Label"},
            {"tool": "asset_look", "op": "preset_fade", "player_path": "/Main/Keeper", "target_path": "/Main/Label"},
        ]
        try:
            planned = fake.bridge.call("plan_change", {"steps": steps})
            self.assertFalse(planned["isError"], planned["content"][0]["text"])
            text = planned["content"][0]["text"]
            sent = {(row["name"], (row.get("arguments") or {}).get("op")) for row in self._raw(fake)}
            # Planning reads the session list only. Editor state and the open-scene list run a root @tool _get.
            allowed = set(B.PLAN_UPSTREAM) | set(B.PLAN_READS)
            self.assertLessEqual(sent, allowed, sent - allowed)
            self.assertIn(("session_manage", "list"), sent)
            self.assertNotIn(("editor_state", None), sent)
            self.assertNotIn(("scene_manage", "get_roots"), sent)
            self.assertNotIn(("scene_get_hierarchy", None), sent)
            self.assertNotIn(("node_get_properties", None), sent)
            # Keeper has an embedded @tool script. The plan names that from the file. It does not read the tree.
            self.assertIn("asset_look op preset_fade (runs project code, writes)", text)
            self.assertIn("scene_do op properties (reads values)", text)
            self.assertIn("This plan deletes, replaces, discards or runs something:", text)
            self.assertNotIn("The editor's tree shape was not read", text)
        finally:
            fake.close()

    def test_upstream_refuses_anything_else_while_planning(self):
        fake = FakeChild(project=self.p.root, scene="res://scenes/premise.tscn", nodes=PREMISE_NODES)
        try:
            fake.bridge.ensure_child()
            fake.bridge._local.planning = True
            with self.assertRaises(B.Refused):
                fake.bridge.upstream("node_get_properties", {"path": "/Main/Keeper"})
            with self.assertRaises(B.Refused):
                fake.bridge.upstream("batch_execute", {"commands": []})
            fake.bridge._local.planning = False
            self.assertFalse(any(row["name"] in ("node_get_properties", "batch_execute") for row in self._raw(fake)))
        finally:
            fake.close()

    def test_embedded_tool_script_is_not_read_when_comparing(self):
        fake = FakeChild(project=self.p.root, scene="res://scenes/premise.tscn", nodes=PREMISE_NODES)
        fake.bridge.confirmer = lambda _message: True
        try:
            root = str(self.p.root)
            self.assertEqual(fake.bridge._node_script_res("/Main/Keeper", "res://scenes/premise.tscn", root),
                             "res://scenes/premise.tscn#sub:trap")
            self.assertTrue(fake.bridge._node_runs_tool("/Main/Keeper", "res://scenes/premise.tscn", root))
            self.assertTrue(fake.bridge._script_is_tool("?", root))
            self.assertTrue(fake.bridge._script_is_tool("uid://doesnotresolve", root))
            fake.bridge.call("scene_do", {"op": "set_node_property", "path": "/Main/Keeper",
                                          "property": "process_mode", "value": 0})
            saved = fake.bridge.call("scene_do", {"op": "save"})
            self.assertTrue(saved["isError"], saved["content"][0]["text"])
            blob = saved["content"][0]["text"]
            self.assertTrue("not compared" in blob or "@tool" in blob or "could not compare" in blob, blob)
            self.assertFalse(any(row["name"] == "scene_save" for row in self._raw(fake)))
            touched = {row["arguments"].get("path") or (row["arguments"].get("params") or {}).get("node_path")
                       for row in self._raw(fake) if row["name"] in ("node_get_properties", "resource_manage")}
            self.assertNotIn("/Main/Keeper", touched)
        finally:
            fake.close()

    def test_plan_checks_play_arguments_before_approval(self):
        fake = FakeChild(project=self.p.root, scene="res://scenes/premise.tscn", nodes=PREMISE_NODES)
        try:
            bad = fake.bridge.call("plan_change", {"steps": [{"tool": "game_do", "op": "play_session", "goal": "score >= 1",
                                                             "steps": [{"op": "wait", "seconds": 2.8}]}]})
            self.assertTrue(bad["isError"])
            self.assertIn("seconds must be a number from 0 to 2.0", bad["content"][0]["text"])
            self.assertIn("no plan was saved", bad["content"][0]["text"])
        finally:
            fake.close()

    def test_change_guide_and_plan_step_rules(self):
        listed = B.change_guide({})
        self.assertIn("scene_do:", listed["content"][0]["text"])
        self.assertNotIn("scene_tree", listed["content"][0]["text"].split("scene_look:")[1].split("\n")[0])
        one = B.change_guide({"tool": "scene_do", "op": "add_node"})["content"][0]["text"]
        self.assertIn("arguments:", one)
        self.assertIn("example step:", one)
        self.assertTrue(B.change_guide({"tool": "scene_do", "op": "nope"})["isError"])
        fake = FakeChild(project=self.p.root, scene="res://scenes/premise.tscn", nodes=PREMISE_NODES)
        try:
            no_op = fake.bridge.call("plan_change", {"steps": [{"tool": "scene_do", "path": "/Main"}]})
            self.assertTrue(no_op["isError"])
            self.assertIn("needs op", no_op["content"][0]["text"])
            read = fake.bridge.call("plan_change", {"steps": [{"tool": "scene_look", "op": "groups",
                                                               "path": "/Main/Label"}]})
            self.assertFalse(read["isError"], read["content"][0]["text"])
            self.assertIn("touches the editor", read["content"][0]["text"])
            self.assertIn("runs project code", read["content"][0]["text"])
            tree = fake.bridge.call("scene_look", {"op": "scene_tree"})
            self.assertFalse(tree["isError"], tree["content"][0]["text"])
            self.assertIn("editor was not asked", tree["content"][0]["text"])
            stepped = fake.bridge.call("plan_change", {"steps": [{"tool": "scene_look", "op": "scene_tree"}]})
            self.assertFalse(stepped["isError"], stepped["content"][0]["text"])
            self.assertIn("runs project code", stepped["content"][0]["text"])
            nav = fake.bridge.call("asset_look", {"op": "nav_path", "from_point": {"x": 0, "y": 0},
                                                  "to_point": {"x": 1, "y": 1}, "force_sync": True})
            self.assertTrue(nav["isError"])
            self.assertIn("not called directly", nav["content"][0]["text"])
            self.assertEqual(fake.sent(), [])
            planned_nav = fake.bridge.call("plan_change", {"steps": [
                {"tool": "asset_look", "op": "nav_path", "from_point": {"x": 0, "y": 0},
                 "to_point": {"x": 1, "y": 1}, "force_sync": True}]})
            self.assertFalse(planned_nav["isError"], planned_nav["content"][0]["text"])
            self.assertIn("force_sync", planned_nav["content"][0]["text"])
            direct = B.Bridge.call(fake.bridge, "change_guide", {"tool": "batch"})
            self.assertFalse(direct["isError"])
            buses = B.change_guide({"tool": "place_assets", "op": "buses"})
            self.assertFalse(buses["isError"], buses["content"][0]["text"])
            self.assertIn('{"tool": "place_assets", "op": "buses"}', buses["content"][0]["text"])
            self.assertNotIn("KeyError", buses["content"][0]["text"])
        finally:
            fake.close()


if __name__ == "__main__":
    unittest.main()
