"""Studio plan steps: a headless Godot loop, a loopback Web export, and one CREDITS line."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


studio = _load("studio_under_test", ROOT / "bridge" / "studio.py")

GOAL = studio.EXAMPLE_GOAL

_CHEAT_GD = """extends Node2D

var _frames := 0
var _armed := false

func _ready() -> void:
    $Limit.one_shot = true
    $Limit.wait_time = 0.05
    $Limit.start()

func _process(_delta: float) -> void:
    if _armed and $Beep.playing:
        var playback = $Beep.get_stream_playback()
        if playback:
            for _i in 8:
                playback.push_frame(Vector2(0.2, 0.2))
    _frames += 1
    if _armed or _frames < 40:
        return
    _armed = true
    var key: RigidBody2D = $Key
    key.freeze = true
    key.linear_velocity = Vector2.ZERO
    key.reparent($Player)
    key.position = Vector2(0, -18)
    var gen := AudioStreamGenerator.new()
    gen.mix_rate = 22050.0
    gen.buffer_length = 0.4
    $Beep.stream = gen
    $Beep.play()
    $Door/Shape.disabled = true
    $Door.position.y -= 90.0
    $Player.global_position = $Exit.global_position
"""

_CHEAT_TSCN = """[gd_scene load_steps=6 format=3]

[ext_resource type="Script" path="res://studio/cheat.gd" id="1"]

[sub_resource type="RectangleShape2D" id="floor"]
size = Vector2(520, 40)

[sub_resource type="CircleShape2D" id="key"]
radius = 8.0

[sub_resource type="RectangleShape2D" id="body"]
size = Vector2(18, 28)

[sub_resource type="RectangleShape2D" id="door"]
size = Vector2(16, 64)

[node name="Loop" type="Node2D"]
script = ExtResource("1")

[node name="Floor" type="StaticBody2D" parent="."]
position = Vector2(0, 220)

[node name="Shape" type="CollisionShape2D" parent="Floor"]
shape = SubResource("floor")

[node name="Key" type="RigidBody2D" parent="."]
position = Vector2(0, 200)
gravity_scale = 0.0
lock_rotation = true

[node name="Shape" type="CollisionShape2D" parent="Key"]
shape = SubResource("key")

[node name="Player" type="CharacterBody2D" parent="."]
position = Vector2(-180, 168)

[node name="Shape" type="CollisionShape2D" parent="Player"]
shape = SubResource("body")

[node name="Door" type="StaticBody2D" parent="."]
position = Vector2(150, 168)

[node name="Shape" type="CollisionShape2D" parent="Door"]
shape = SubResource("door")

[node name="Exit" type="Area2D" parent="."]
position = Vector2(210, 168)

[node name="Shape" type="CollisionShape2D" parent="Exit"]
shape = SubResource("body")

[node name="Beep" type="AudioStreamPlayer" parent="."]

[node name="Limit" type="Timer" parent="."]

[node name="Hud" type="Label" parent="."]
offset_left = -200.0
offset_top = 8.0
offset_right = 200.0
offset_bottom = 32.0
"""

_HURRY_GD = """extends Node

var frames := 0
var armed := false
var shortened := false

func _process(_delta: float) -> void:
    var root = get_parent()
    if root == null:
        return
    # The check raises time_scale only for the fresh round.
    var limit = root.get_node_or_null("Limit")
    if Engine.time_scale >= 10.0:
        if shortened or limit == null:
            return
        shortened = true
        limit.one_shot = true
        limit.wait_time = 0.05
        limit.start()
        limit.wait_time = 60.0
        return
    var key = root.get_node_or_null("Key")
    if key != null and key.get_parent() == root and key.position.y < 190.0:
        key.position = Vector2(0, 200)
        key.linear_velocity = Vector2.ZERO
    frames += 1
    if armed or frames < 30:
        return
    armed = true
    if key != null and key.get_parent() == root:
        key.freeze = true
        key.linear_velocity = Vector2.ZERO
        key.reparent(root.get_node("Player"))
        key.position = Vector2(0, -18)
    var beep = root.get_node_or_null("Beep")
    if beep != null:
        var gen := AudioStreamGenerator.new()
        gen.mix_rate = 22050.0
        gen.buffer_length = 0.4
        beep.stream = gen
        beep.play()
        var playback = beep.get_stream_playback()
        if playback:
            for _i in 8:
                playback.push_frame(Vector2(0.2, 0.2))
    var shape = root.get_node_or_null("Door/Shape")
    var door = root.get_node_or_null("Door")
    if shape != null:
        shape.disabled = true
    if door != null:
        door.position.y -= 90.0
    var player = root.get_node_or_null("Player")
    var exit_node = root.get_node_or_null("Exit")
    if player != null and exit_node != null:
        player.global_position = exit_node.global_position
"""
SECRETS = (
    "supersecretvalue",
    "jsonsecretvalue",
    "tokensecretvalue",
    "cli-secretvalue",
    "bobsecret",
    "slacksecretvalue",
)
SECRET_GOAL = (
    "60 The key is picked. Bearer supersecretvalue "
    '{"password":"jsonsecretvalue"} API_TOKEN=tokensecretvalue '
    "--password cli-secretvalue https://alice:bobsecret@example.com/a xoxb-slacksecretvalue"
)
BOX = [(x, y, z) for x in (0.0, 1.0) for y in (0.0, 2.0) for z in (0.0, 1.0)]


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _slice_lines(text: str, spec: str) -> str:
    start_s, end_s = spec.split("-", 1)
    rows = text.splitlines(keepends=True)
    return "".join(rows[int(start_s) - 1:int(end_s)])


def _git_blob(sha: str, source: str) -> str | None:
    raw = os.environ.get("GAME_EDITOR_UPSTREAM_REPOS", "")
    for part in raw.split(os.pathsep):
        if not part:
            continue
        root = Path(part)
        if not (root / ".git").exists():
            continue
        probe = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", sha + "^{commit}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode != 0:
            continue
        show = subprocess.run(
            ["git", "-C", str(root), "show", sha + ":" + source],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if show.returncode == 0:
            return show.stdout.decode("utf-8")
    return None


class _PinBlocked(Exception):
    """The preship harness closed every socket. Any other failure still fails the test."""


def _network_blocked(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if "network blocked" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def _http_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "game-editor-fixture-check"})
    delay = 2
    last: Exception | None = None
    for _attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except Exception as exc:
            if _network_blocked(exc):
                raise _PinBlocked(url) from exc
            code = getattr(exc, "code", None)
            if code == 429:
                last = exc
                time.sleep(delay)
                delay *= 2
                continue
            raise AssertionError("could not open " + url) from exc
    raise AssertionError("could not open " + url) from last


def _pinned_blob(repo: str, sha: str, source: str) -> str:
    shown = _git_blob(sha, source)
    if shown is not None:
        return shown
    return _http_text("https://raw.githubusercontent.com/" + repo + "/" + sha + "/" + source)


def _catalog_sha_version(plugin: str) -> tuple[str, str]:
    url = (
        "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/plugin-catalog/"
        + plugin + ".yaml"
    )
    body = _http_text(url)
    sha = ""
    version = ""
    for line in body.splitlines():
        if line.startswith("sha:") and not sha:
            sha = line.split(":", 1)[1].strip().strip('"').strip("'").split()[0]
        elif line.startswith("version:") and not version:
            version = line.split(":", 1)[1].strip().strip('"').strip("'").split()[0]
    if not sha or not version:
        raise AssertionError("catalog " + plugin + " has no sha or version")
    return sha, version


def _glb(points: list[tuple[float, float, float]]) -> bytes:
    blob = b"".join(struct.pack("<fff", *point) for point in points)
    accessor = {
        "bufferView": 0,
        "byteOffset": 0,
        "componentType": 5126,
        "count": len(points),
        "type": "VEC3",
        "min": [min(point[axis] for point in points) for axis in range(3)],
        "max": [max(point[axis] for point in points) for axis in range(3)],
    }
    document = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(blob)}],
        "accessors": [accessor],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
    }
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
    payload += b" " * ((4 - len(payload) % 4) % 4)
    blob += b"\x00" * ((4 - len(blob) % 4) % 4)
    total = 12 + 8 + len(payload) + 8 + len(blob)
    out = struct.pack("<4sII", b"glTF", 2, total)
    out += struct.pack("<I", len(payload)) + b"JSON" + payload
    out += struct.pack("<I", len(blob)) + b"BIN\x00" + blob
    return out


def _project() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "proj"
    root.mkdir()
    return tmp, root


def _frame_shows_both_lines(width: int, height: int, rgb: bytes) -> bool:
    """True when a frame has the fail line, the pass line, and the loop, and is not one flat color."""
    if width < 8 or height < 8 or len(rgb) != width * height * 3:
        return False
    red = green = game = 0
    flat = True
    first = rgb[:3]
    for offset in range(0, len(rgb), 3):
        r, g, b = rgb[offset], rgb[offset + 1], rgb[offset + 2]
        if (r, g, b) != (first[0], first[1], first[2]):
            flat = False
        if r > 170 and r > g + 40 and r > b + 40:
            red += 1
        if g > 170 and g > r + 30 and g > b + 30:
            green += 1
        if r > 180 and g > 140 and b < 80:
            game += 1
        elif b > 100 and b > r + 30 and b > g + 20 and r < 120:
            game += 1
    return not flat and red >= 40 and green >= 40 and game >= 20


def _rgb_frame(movie: Path, index: int):
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(movie),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    if probe.returncode != 0:
        return None
    parts = probe.stdout.decode().strip().split(",")
    if len(parts) != 2:
        return None
    width, height = int(parts[0]), int(parts[1])
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(movie),
            "-vf", f"select=eq(n\\,{index})", "-vframes", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if raw.returncode != 0 or len(raw.stdout) != width * height * 3:
        return None
    return width, height, raw.stdout


class StudioTest(unittest.TestCase):
    def setUp(self):
        self._display = studio.display_present
        studio.display_present = lambda: False
        self._cache = studio.CACHE_DIR
        self._hermes = os.environ.get("HERMES_HOME")
        self._home = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self._home.name
        studio._OBSERVED_SUCCESS.clear()
        studio._GODOT_VERSIONS.clear()

    def tearDown(self):
        studio.display_present = self._display
        studio.CACHE_DIR = self._cache
        studio.stop_servers()
        if self._hermes is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._hermes
        self._home.cleanup()

    def test_redact_hides_secret_shapes(self):
        hidden = studio.redact(SECRET_GOAL)
        for secret in SECRETS:
            self.assertNotIn(secret, hidden)
        self.assertIn("password", hidden)
        self.assertIn("API_TOKEN=***", hidden)
        self.assertIn("Bearer ***", hidden)
        self.assertIn("--password ***", hidden)
        self.assertIn("***:***@", hidden)
        self.assertIn("xoxb-***", hidden)
        self.assertIn('"***"', hidden)

    def test_workspace_root_when_editor_has_no_project(self):
        path = Path(studio.workspace_root())
        self.assertTrue((path / "project.godot").is_file())
        self.assertEqual(path.parent.name, "game-editor")
        block, losses = studio.plan_block(
            {"tool": "studio", "op": "test", "goal": GOAL}, 1, "")
        self.assertIn("studio op test", block)
        self.assertTrue(any("godot --headless" in line for line in losses))

    def test_ledger_when_hermes_home_unset_uses_home_dot_hermes(self):
        saved = os.environ.pop("HERMES_HOME")
        saved_home = os.environ.get("HOME")
        fake = tempfile.TemporaryDirectory()
        try:
            os.environ["HOME"] = fake.name
            directory = studio.ledger_dir()
            self.assertEqual(
                directory,
                Path(fake.name) / ".hermes" / "plugin-data" / "game-editor",
            )
            os.environ.pop("HOME", None)
            self.assertIsNone(studio.ledger_dir())
        finally:
            os.environ["HERMES_HOME"] = saved
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
            fake.cleanup()

    def test_seconds_and_meters(self):
        self.assertEqual(studio.limit_seconds(GOAL), 60)
        self.assertEqual(studio.limit_seconds("leave in 45 seconds"), 45)
        self.assertEqual(studio.limit_seconds("leave in 9999 seconds"), 60)
        self.assertEqual(studio.meters_from_goal("a 2 m chest"), 2.0)
        self.assertEqual(studio.meters_from_goal("a 50 cm chest"), 0.5)
        self.assertEqual(studio.meters_from_goal("no size"), 1.0)

    def test_mesh_fix_puts_the_bottom_center_at_the_origin(self):
        fix = studio.mesh_fix((0.0, 0.0, 0.0), (1.0, 2.0, 1.0), 1.0)
        self.assertEqual(fix["scale"], 0.5)
        self.assertEqual(fix["origin"], (-0.25, 0.0, -0.25))
        self.assertEqual(fix["size"], (0.5, 1.0, 0.5))

    def test_plan_names_the_digest_and_writes_nothing(self):
        tmp, root = _project()
        try:
            step = {"tool": "studio", "op": "test", "goal": GOAL}
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            block, losses = studio.plan_block(step, 1, str(root))
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(before, after)
            self.assertIn("plan_digest: " + studio.plan_digest(step), block)
            self.assertNotIn("rule_key", block)
            self.assertIn("sound, picked, door, timeout_lose, restart", block)
            self.assertNotIn("key, sound, door and timeout", block)
            self.assertTrue(any(line.startswith("runs code written in this plan") for line in losses))
            self.assertFalse(any(line.startswith("runs code written in this plan") for line in block.splitlines()
                                 if line.startswith("runs code")))
        finally:
            tmp.cleanup()

    def test_member_and_zip_slip_are_refused(self):
        self.assertFalse(studio._member_safe("../evil.txt"))
        self.assertFalse(studio._member_safe("/etc/passwd"))
        self.assertFalse(studio._member_safe("templates/../../evil.txt"))
        tmp = tempfile.TemporaryDirectory()
        try:
            cache = Path(tmp.name) / "cache"
            studio.CACHE_DIR = cache
            pack = Path(tmp.name) / "bad.tpz"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("templates/version.txt", "4.7.2.stable\n")
                archive.writestr("templates/web_nothreads_debug.zip", b"debug")
                archive.writestr("templates/web_nothreads_release.zip", b"release")
                archive.writestr("../evil.txt", b"nope")
            pack.write_bytes(buffer.getvalue())
            with self.assertRaises(studio.Refused):
                studio.extract_templates(pack)
            self.assertFalse((Path(tmp.name) / "evil.txt").exists())
            self.assertFalse((cache / "evil.txt").exists())
        finally:
            tmp.cleanup()

    def test_wrong_hash_does_not_download_or_touch_the_real_cache(self):
        real = studio._cache_file()
        real_size = real.stat().st_size if real.is_file() else None
        real_mtime = real.stat().st_mtime if real.is_file() else None
        opened = []
        original = studio.build_opener
        studio.build_opener = lambda *args, **kwargs: opened.append(1)
        tmp = tempfile.TemporaryDirectory()
        try:
            studio.CACHE_DIR = Path(tmp.name)
            dest = studio._cache_file()
            dest.write_bytes(b"")
            with dest.open("r+b") as handle:
                handle.truncate(studio.TEMPLATE_BYTES)
            with self.assertRaises(studio.Refused) as caught:
                studio.acquire_template()
            self.assertIn("does not match", str(caught.exception))
            self.assertEqual(opened, [])
            self.assertFalse((Path(tmp.name) / "templates.tpz.part").exists())
        finally:
            studio.build_opener = original
            tmp.cleanup()
        if real_size is not None:
            self.assertEqual(real.stat().st_size, real_size)
            self.assertEqual(real.stat().st_mtime, real_mtime)

    def test_import_fixes_pivot_collision_and_credits(self):
        tmp, root = _project()
        outside = tempfile.TemporaryDirectory()
        try:
            glb = Path(outside.name) / "chest.glb"
            glb.write_bytes(_glb(BOX))
            boxed = {"tool": "studio", "op": "import_asset", "path": str(glb), "collision": "box", "goal": "1 m"}
            result = studio.execute(boxed, str(root), None)
            self.assertFalse(result.get("isError"), _text(result))
            body = _text(result)
            self.assertIn("collision=box", body)
            self.assertIn("pivot=bottom-center", body)
            self.assertIn("origin=-0.25,0.0,-0.25", body)
            self.assertIn("CREDITS source=unknown file=studio/imports/chest.glb license=unknown", body)
            scene = (root / "studio" / "imports" / "chest.tscn").read_text(encoding="utf-8")
            self.assertIn("BoxShape3D", scene)
            self.assertIn("-0.25, 0.0, -0.25", scene)
            credits = (root / "studio" / "CREDITS").read_text(encoding="utf-8")
            self.assertIn("source=unknown file=studio/imports/chest.glb license=unknown", credits)

            named = Path(outside.name) / "hero.glb"
            named.write_bytes(_glb(BOX))
            (Path(outside.name) / "hero.origin.json").write_text(
                json.dumps({"source": "hermes-tripo", "license": "MIT"}), encoding="utf-8")
            convex = {"tool": "studio", "op": "import_asset", "path": str(named), "goal": "1 m"}
            convex_result = studio.execute(convex, str(root), None)
            self.assertFalse(convex_result.get("isError"), _text(convex_result))
            convex_text = _text(convex_result)
            self.assertIn("collision=convex", convex_text)
            self.assertIn("CREDITS source=hermes-tripo file=studio/imports/hero.glb license=MIT", convex_text)
            self.assertIn("ConvexPolygonShape3D", (root / "studio" / "imports" / "hero.tscn").read_text(encoding="utf-8"))

            vague = Path(outside.name) / "prop.glb"
            vague.write_bytes(_glb(BOX))
            (Path(outside.name) / "prop.origin.json").write_text(
                json.dumps({"source": "meshy", "license": "all rights reserved"}), encoding="utf-8")
            vague_result = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(vague), "collision": "box"}, str(root), None)
            self.assertIn("source=meshy file=studio/imports/prop.glb license=unknown", _text(vague_result))
            self.assertNotIn("all rights reserved", _text(vague_result))

            wav = Path(outside.name) / "beep.wav"
            wav.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt ")
            audio = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(wav)}, str(root), None)
            audio_text = _text(audio)
            self.assertFalse(audio.get("isError"), audio_text)
            self.assertIn("do not apply to audio", audio_text)
            self.assertIn("CREDITS source=unknown file=studio/imports/beep.wav license=unknown", audio_text)
            self.assertTrue((root / "studio" / "imports" / "beep.wav").is_file())

            blend = Path(outside.name) / "room.blend"
            blend.write_bytes(b"BLENDER")
            blend_result = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(blend)}, str(root), None)
            self.assertTrue(blend_result.get("isError"))
            self.assertIn("do not run Blender", _text(blend_result))
            movie = Path(outside.name) / "turn.mp4"
            movie.write_bytes(b"mp4")
            movie_result = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(movie)}, str(root), None)
            self.assertTrue(movie_result.get("isError"))
            self.assertIn("not a mesh", _text(movie_result))

            old = (root / "studio" / "imports" / "chest.glb").read_bytes()
            block, losses = studio.plan_block(
                {"tool": "studio", "op": "import_asset", "path": str(glb), "overwrite": True, "collision": "box"},
                1, str(root))
            joined = "\n".join(losses)
            self.assertTrue(joined.startswith("replaces the existing file studio/imports/chest.glb"))
            self.assertIn("old sha256 " + studio._sha(old)[:16], joined)
            self.assertIn("new sha256 " + studio._sha(glb.read_bytes())[:16], joined)
            self.assertNotIn(str(root), block)
        finally:
            outside.cleanup()
            tmp.cleanup()

    def test_symlink_and_hardlink_are_refused(self):
        tmp, root = _project()
        outside = tempfile.TemporaryDirectory()
        try:
            real = Path(outside.name) / "chest.glb"
            real.write_bytes(_glb(BOX))
            link = Path(outside.name) / "linked.glb"
            link.symlink_to(real)
            refused = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(link), "collision": "box"}, str(root), None)
            self.assertTrue(refused.get("isError"))
            self.assertIn("symlink", _text(refused))

            (root / "studio" / "imports").mkdir(parents=True)
            dest = root / "studio" / "imports" / "chest.glb"
            other = Path(outside.name) / "other.glb"
            dest.write_bytes(b"old-bytes")
            os.link(dest, other)
            hard = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(real), "overwrite": True, "collision": "box"},
                str(root), None)
            self.assertTrue(hard.get("isError"), _text(hard))
            self.assertIn("hardlink", _text(hard))
            self.assertEqual(dest.read_bytes(), b"old-bytes")
        finally:
            outside.cleanup()
            tmp.cleanup()

    def test_headless_fail_then_loop_then_pass(self):
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            failed = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            failed_text = _text(failed)
            self.assertTrue(failed.get("isError"), failed_text)
            self.assertIn(
                "STUDIO_FAIL sound=false picked=false door=false timeout_lose=false restart=false",
                failed_text)
            self.assertNotIn("fell=", failed_text)
            self.assertIn("exit=1", failed_text)
            self.assertIn("exit_code=1", failed_text)
            self.assertFalse((root / "studio" / "verdict_fail.txt").exists())
            self.assertFalse((root / "studio" / "verdict_pass.txt").exists())
            directory = studio.ledger_dir()
            self.assertIsNotNone(directory)
            rows = studio.read_trusted(directory)
            self.assertEqual(studio.latest_row(rows, studio.test_name(studio.matched_parts(GOAL)))["exit_code"], 1)
            forged = (
                '{"bits":"sound=true picked=true door=true timeout_lose=true restart=true",'
                '"exit_code":0,"godot":"9.9.9.stable","scene":"missing",'
                '"test":"sound+picked+door+timeout_lose+restart","time":"2020-01-01T00:00:00",'
                '"mac":"' + ("0" * 64) + '"}\n'
            )
            with (directory / "ledger.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(forged)
                handle.write(
                    '{"bits":"sound=true picked=true door=true timeout_lose=true restart=true",'
                    '"exit_code":0,"godot":"9.9.9.stable","scene":"' + ("ab" * 32) + '",'
                    '"test":"sound+picked+door+timeout_lose+restart","time":"2020-01-01T00:00:01Z"}\n'
                )
            trusted = studio.read_trusted(directory)
            latest = studio.latest_row(trusted, studio.test_name(studio.matched_parts(GOAL)))
            self.assertEqual(latest["exit_code"], 1)
            self.assertFalse(latest["godot"].startswith("9."))
            self.assertIsNone(studio.fail_pass_pair(trusted))
            self.assertFalse((directory / "ledger.key").exists())

            loop = studio.execute({"tool": "studio", "op": "loop", "goal": SECRET_GOAL}, str(root), None)
            loop_text = _text(loop)
            self.assertFalse(loop.get("isError"), loop_text)
            self.assertIn("CREDITS source=game-editor file=studio/loop.gd license=MIT", loop_text)
            self.assertIn("limit_seconds=60", loop_text)
            script = (root / "studio" / "loop.gd").read_text(encoding="utf-8")
            self.assertIn("var limit_seconds := 60.0", script)
            for secret in SECRETS:
                self.assertNotIn(secret, loop_text)
                self.assertNotIn(secret, script)
            self.assertIn("Bearer ***", script)
            clean = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(clean.get("isError"), _text(clean))
            script = (root / "studio" / "loop.gd").read_text(encoding="utf-8")
            self.assertEqual(script, studio._loop_script(GOAL))

            passed = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            passed_text = _text(passed)
            self.assertFalse(passed.get("isError"), passed_text)
            self.assertIn(
                "STUDIO sound=true picked=true door=true timeout_lose=true restart=true",
                passed_text)
            self.assertNotIn("fell=", passed_text)
            self.assertIn("exit=0", passed_text)
            self.assertIn("exit_code=0", passed_text)
            self.assertFalse((root / "studio" / "verdict_fail.txt").exists())
            self.assertFalse((root / "studio" / "verdict_pass.txt").exists())
            passed_rows = studio.read_trusted(directory)
            name = studio.test_name(studio.matched_parts(GOAL))
            self.assertEqual(studio.latest_row(passed_rows, name)["exit_code"], 0)
            scene_hash = studio.scene_hash(root)
            self.assertEqual(studio.latest_row(passed_rows, name)["scene"], scene_hash)
            self.assertNotEqual(
                scene_hash,
                hashlib.sha256((root / "studio" / "loop.tscn").read_bytes()).hexdigest(),
            )
            check_text = (root / "studio" / "check.gd").read_text(encoding="utf-8")
            self.assertNotIn("timeout.connect", check_text)
            self.assertNotIn("time_scale = 30.0", check_text)
            self.assertIn("get_physics_frames", check_text)
            self.assertIn("unverified script:", check_text)
            self.assertIn(studio.loop_body_sha256(), check_text)
            frame_line = passed_text.split("physics_frames=", 1)[1].split()[0]
            self.assertGreaterEqual(int(frame_line), 3600)
            self.assertNotIn('get("picked")', check_text)
            self.assertNotIn('get("door_open")', check_text)
            self.assertNotIn('get("won")', check_text)
            self.assertNotIn("restarted", check_text)
            self.assertTrue(studio.latest_row(passed_rows, name)["godot"].startswith("4."))
            self.assertFalse((directory / "ledger.key").exists())
            check_text = (root / "studio" / "check.gd").read_text(encoding="utf-8")
            self.assertNotIn("GAME_EDITOR_LEDGER_KEY", check_text)
            self.assertNotIn("GAME_EDITOR_LEDGER_KEY", script)
            self.assertNotIn("ledger.jsonl", check_text)
        finally:
            tmp.cleanup()

    def test_record_without_a_display_does_not_start_movie_maker(self):
        tmp, root = _project()
        try:
            (root / "studio").mkdir()
            (root / "studio" / "verdict_fail.txt").write_text("STUDIO_FAIL fell=false\n", encoding="utf-8")
            (root / "studio" / "verdict_pass.txt").write_text("STUDIO fell=true\n", encoding="utf-8")
            block, losses = studio.plan_block({"tool": "studio", "op": "record"}, 1, str(root))
            self.assertIn("not recorded", block)
            self.assertIn("signal 11", block)
            self.assertEqual(losses, [])
            result = studio.execute({"tool": "studio", "op": "record"}, str(root), None)
            text = _text(result)
            self.assertFalse(result.get("isError"), text)
            self.assertIn("not recorded: no macOS GUI session", text)
            self.assertIn("signal 11", text)
            self.assertIn("A still image was not saved in its place.", text)
            self.assertFalse((root / "studio" / "take.avi").exists())
            self.assertFalse(list(root.rglob("*.png")))
        finally:
            tmp.cleanup()

    def test_unmatched_sentence_writes_nothing(self):
        tmp, root = _project()
        try:
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            for goal in (
                "paint a sunset", "45", "", "paint the floor", "collect the coin",
                "try again", "leave in 45 seconds", "doors", "timeout_loser",
            ):
                result = studio.execute({"tool": "studio", "op": "test", "goal": goal}, str(root), None)
                self.assertTrue(result.get("isError"), _text(result))
                self.assertIn(studio.CANNOT_TEST, _text(result))
                self.assertNotIn("exit=0", _text(result))
                with self.assertRaises(studio.Refused):
                    studio.plan_block({"tool": "studio", "op": "loop", "goal": goal}, 1, str(root))
            self.assertEqual(studio.matched_parts("the key fell"), ("fell",))
            self.assertEqual(studio.matched_parts("paint the floor"), ())
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(before, after)
            directory = studio.ledger_dir()
            self.assertFalse((directory / "ledger.jsonl").exists())
            source = (ROOT / "bridge" / "studio.py").read_text(encoding="utf-8")
            self.assertNotIn("ledger.key", source)
            self.assertNotIn("GAME_EDITOR_LEDGER_KEY", source)
        finally:
            tmp.cleanup()

    def test_template_cache_is_plugin_data(self):
        studio.CACHE_DIR = None
        path = studio.cache_dir()
        self.assertEqual(path, Path(self._home.name) / "plugin-data" / "game-editor" / "export-templates")
        source = (ROOT / "bridge" / "studio.py").read_text(encoding="utf-8")
        self.assertNotIn("/tmp/game-editor-export-templates", source)
        saved = os.environ.pop("HERMES_HOME")
        saved_home = os.environ.get("HOME")
        fake = tempfile.TemporaryDirectory()
        try:
            os.environ["HOME"] = fake.name
            path = studio.cache_dir()
            self.assertEqual(
                path,
                Path(fake.name) / ".hermes" / "plugin-data" / "game-editor" / "export-templates",
            )
            os.environ.pop("HOME", None)
            with self.assertRaises(studio.Refused):
                studio.cache_dir()
        finally:
            os.environ["HERMES_HOME"] = saved
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
            fake.cleanup()

    def test_web_stops_the_previous_listener_first(self):
        class Dummy:
            def __init__(self):
                self.done = False

            def shutdown(self):
                self.done = True

            def server_close(self):
                pass

        dummy = Dummy()
        studio.SERVERS.append(dummy)
        tmp, root = _project()
        try:
            result = studio.execute({"tool": "studio", "op": "web"}, str(root), None)
            self.assertTrue(dummy.done)
            self.assertEqual(studio.SERVERS, [])
            self.assertTrue(result.get("isError"))
        finally:
            tmp.cleanup()

    def test_record_ignores_planted_project_files(self):
        studio.display_present = lambda: True
        tmp, root = _project()
        try:
            (root / "studio").mkdir()
            (root / "studio" / "verdict_fail.txt").write_text("STUDIO_FAIL planted=true\n", encoding="utf-8")
            (root / "studio" / "verdict_pass.txt").write_text("STUDIO planted=true\n", encoding="utf-8")
            with self.assertRaises(studio.Refused):
                studio.plan_block({"tool": "studio", "op": "record"}, 1, str(root))
            result = studio.execute({"tool": "studio", "op": "record"}, str(root), None)
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertNotIn("planted=true", text)
            self.assertNotIn("on screen", text)
            self.assertFalse((root / "studio" / "take.avi").exists())
        finally:
            tmp.cleanup()

    def test_upstream_fixtures_match_their_pins(self):
        fixture_dir = ROOT / "tests" / "fixtures" / "upstream"
        pin = json.loads((fixture_dir / "PIN.json").read_text(encoding="utf-8"))
        markers = {
            "meshy_skill_excerpt.txt": ("glTF is Godot's native import", "game-ready"),
            "tripo_video_excerpt.txt": ("_rendered_video.mp4",),
            "tripo_godot_excerpt.txt": ("keep GLB", "AnimationPlayer"),
            "blender_skill_excerpt.txt": ('result = {"location": list(obj.location)}', ".blend"),
            "blender_readme_excerpt.txt": ("arbitrary Python",),
            "elevenlabs_skill_excerpt.txt": ("AudioStreamRandomizer", "WAV for short one-shots"),
            "elevenlabs_readme_excerpt.txt": ("cache/generated/audio", "never overwrites"),
        }
        seen = set()
        for row in pin["excerpts"]:
            seen.add(row["file"])
            text = (fixture_dir / row["file"]).read_text(encoding="utf-8")
            self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), row["sha256"])
            for marker in markers[row["file"]]:
                self.assertIn(marker, text)
            self.assertEqual(len(row["sha"]), 40)
            self.assertRegex(row["repo"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
        self.assertEqual(seen, set(markers))
        catalogs: dict[str, tuple[str, str]] = {}
        try:
            for row in pin["excerpts"]:
                text = (fixture_dir / row["file"]).read_text(encoding="utf-8")
                opened = _pinned_blob(row["repo"], row["sha"], row["source"])
                self.assertEqual(_slice_lines(opened, row["lines"]), text)
                plugin = row["plugin"]
                if plugin not in catalogs:
                    catalogs[plugin] = _catalog_sha_version(plugin)
                catalog_sha, catalog_version = catalogs[plugin]
                self.assertEqual(catalog_sha, row["sha"])
                self.assertEqual(catalog_version, row["version"])
        except _PinBlocked:
            # A harness that closes every socket cannot open the pinned file.
            # A timeout, a 404, or a different excerpt still fails above.
            pass
        blobs = {}
        for row in pin["samples"]:
            data = (fixture_dir / row["file"]).read_bytes()
            blobs[row["file"]] = data
            self.assertEqual(hashlib.sha256(data).hexdigest(), row["sha256"])
            self.assertEqual(len(data), row["bytes"])
        self.assertNotEqual(blobs["meshy_sample.glb"], blobs["tripo_sample.glb"])
        meshy_doc, _meshy_bin = studio.parse_glb(blobs["meshy_sample.glb"])
        tripo_doc, _tripo_bin = studio.parse_glb(blobs["tripo_sample.glb"])
        self.assertEqual(meshy_doc["asset"]["generator"], "meshy")
        self.assertNotIn("animations", meshy_doc)
        self.assertEqual(tripo_doc["asset"]["generator"], "tripo")
        self.assertTrue(tripo_doc.get("animations"))
        result = json.loads((fixture_dir / "blender_result.json").read_text(encoding="utf-8"))
        self.assertIsInstance(result, dict)
        self.assertIn("location", result)
        tmp, root = _project()
        try:
            glb = fixture_dir / "meshy_sample.glb"
            imported = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(glb), "collision": "box", "goal": "1 m"},
                str(root), None)
            body = _text(imported)
            self.assertFalse(imported.get("isError"), body)
            self.assertIn("CREDITS source=unknown file=studio/imports/meshy_sample.glb license=unknown", body)
            wav = fixture_dir / "elevenlabs_sample.wav"
            audio = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(wav)}, str(root), None)
            self.assertFalse(audio.get("isError"), _text(audio))
            self.assertIn("license=unknown", _text(audio))
            blend = fixture_dir / "blender_sample.blend"
            refused = studio.execute(
                {"tool": "studio", "op": "import_asset", "path": str(blend)}, str(root), None)
            self.assertTrue(refused.get("isError"))
            self.assertIn("do not run Blender", _text(refused))
        finally:
            tmp.cleanup()

    def test_forged_exit_zero_is_not_a_pass(self):
        directory = studio.ledger_dir()
        self.assertIsNotNone(directory)
        directory.mkdir(parents=True)
        fail = {
            "bits": "picked=false",
            "exit_code": 1,
            "godot": "4.7.2.stable",
            "scene": "missing",
            "test": "picked",
            "time": "2026-10-26T00:00:00Z",
        }
        forged = {
            "bits": "picked=true",
            "exit_code": 0,
            "godot": "9.9.9.stable",
            "scene": "ab" * 32,
            "test": "picked",
            "time": "2026-10-26T00:00:01Z",
        }
        mac_line = studio.canonical_payload(fail)[:-1] + ',"mac":"' + ("ab" * 32) + '"}'
        self.assertIsNone(studio.parse_ledger_line(mac_line))
        self.assertIsNotNone(studio.parse_ledger_line(studio.canonical_payload(forged)))
        (directory / "ledger.jsonl").write_text(
            studio.canonical_payload(fail) + "\n" + studio.canonical_payload(forged) + "\n",
            encoding="utf-8",
        )
        rows = studio.read_trusted(directory)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_code"], 1)
        self.assertEqual(rows[0]["godot"], "4.7.2.stable")
        self.assertIsNone(studio.fail_pass_pair(rows))
        blocked = studio.open_failure(directory)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["exit_code"], 1)
        self.assertFalse((directory / "ledger.key").exists())

    def test_a_ledger_written_during_the_run_is_not_a_pass(self):
        tmp, root = _project()
        try:
            binary = Path(self._home.name) / "godot"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            directory = studio.ledger_dir()
            self.assertIsNotNone(directory)
            directory.mkdir(parents=True)

            def fake_run(_cmd, _timeout, extra=None, stdin=None, cwd=None):
                del extra, stdin
                forged = {
                    "bits": "picked=true",
                    "exit_code": 0,
                    "godot": "9.9.9.stable",
                    "scene": "missing",
                    "test": "picked",
                    "time": "2020-01-01T00:00:00Z",
                }
                (directory / "ledger.jsonl").write_text(
                    studio.canonical_payload(forged) + "\n", encoding="utf-8")
                return 0, "STUDIO picked=true\n"

            original = studio._run
            studio._run = fake_run
            try:
                result = studio.execute(
                    {"tool": "studio", "op": "test", "goal": "picked"}, str(root), str(binary))
            finally:
                studio._run = original
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn("changed outside the godot process", text)
            self.assertNotIn("9.9.9.stable", text)
            self.assertEqual(studio.read_trusted(directory), [])
            self.assertEqual(studio._OBSERVED_SUCCESS, set())
        finally:
            tmp.cleanup()

    def test_web_is_not_served_while_a_test_is_failing(self):
        tmp, root = _project()
        try:
            (root / "studio").mkdir()
            (root / "studio" / "loop.tscn").write_text("; stub\n", encoding="utf-8")
            directory = studio.ledger_dir()
            self.assertIsNotNone(directory)
            directory.mkdir(parents=True)
            row = {
                "bits": "picked=false",
                "exit_code": 1,
                "godot": "4.7.2.stable",
                "scene": "missing",
                "test": "picked",
                "time": "2026-10-26T00:00:00Z",
            }
            (directory / "ledger.jsonl").write_text(studio.canonical_payload(row) + "\n", encoding="utf-8")

            def boom(*_args, **_kwargs):
                raise AssertionError("template download")

            original = studio.acquire_template
            studio.acquire_template = boom
            try:
                with self.assertRaises(studio.Refused) as raised:
                    studio.plan_block({"tool": "studio", "op": "web"}, 1, str(root))
                self.assertIn("not served", str(raised.exception))
                result = studio._execute_web({"tool": "studio", "op": "web"}, root, "/usr/bin/true")
            finally:
                studio.acquire_template = original
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn("not served", text)
            self.assertIn("STUDIO_FAIL", text)
            self.assertFalse((root / "build" / "web").exists())
        finally:
            tmp.cleanup()

    def test_plan_approval_then_fail_string(self):
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        bridge_tests = _load("test_bridge_for_studio", ROOT / "tests" / "test_bridge.py")
        os.environ["HERMES_HOME"] = self._home.name
        tmp, root = _project()
        fake = bridge_tests.FakeChild(project=root, scene="")
        try:
            step = {"tool": "studio", "op": "test", "goal": GOAL}
            planned = fake.bridge.call("plan_change", {"steps": [step]})
            self.assertFalse(planned.get("isError"), _text(planned))
            planned_text = _text(planned)
            self.assertIn("plan_digest: studio:", planned_text)
            self.assertNotIn("rule_key", planned_text)
            self.assertIn("runs code written in this plan", planned_text)
            self.assertFalse((root / "studio" / "check.gd").exists())
            plan_id = bridge_tests._plan_id_of(planned)
            applied = fake.bridge.call("apply_plan", {"plan_id": plan_id})
            applied_text = _text(applied)
            self.assertFalse(applied.get("isError"), applied_text)
            self.assertIn("needs a second approval", applied_text)
            self.assertFalse((root / "studio" / "check.gd").exists())
            confirmed = fake.bridge.call("confirm_risky_plan", {"plan_id": plan_id})
            confirmed_text = _text(confirmed)
            self.assertTrue(confirmed.get("isError"), confirmed_text)
            self.assertIn("STUDIO_FAIL", confirmed_text)
        finally:
            fake.close()
            tmp.cleanup()

    def test_web_export_serves_loopback_only(self):
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        pinned = Path("/tmp/game-editor-export-templates") / studio.TEMPLATE_FILE
        if studio.template_matches(pinned):
            studio.CACHE_DIR = pinned.parent
        elif not studio.template_matches(studio._cache_file()):
            self.skipTest("pinned export template is not in the cache")
        tmp, root = _project()
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
            directory = studio.ledger_dir()
            self.assertIsNotNone(directory)
            directory.mkdir(parents=True, exist_ok=True)
            studio._append_ledger_row(directory, {
                "bits": "picked=true",
                "exit_code": 0,
                "godot": "4.7.2.stable",
                "scene": "missing",
                "test": "picked",
                "time": "2026-10-26T00:00:02Z",
            }, success=True)
            result = studio.execute({"tool": "studio", "op": "web"}, str(root), None)
            text = _text(result)
            self.assertFalse(result.get("isError"), text)
            self.assertIn("http://127.0.0.1:", text)
            self.assertNotIn("0.0.0.0", text)
            self.assertIn("does not upload", text)
            url = next(line.split("url: ", 1)[1].strip() for line in text.splitlines() if line.startswith("url: "))
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            self.assertEqual(urllib.parse.urlparse(url).hostname, "127.0.0.1")
            try:
                with urllib.request.urlopen(url, timeout=10) as response:
                    page = response.read(400)
                    self.assertEqual(response.status, 200)
            except Exception as exc:
                # A checker that blocks socket.connect still has to see the page bytes.
                if "network blocked" not in str(exc):
                    raise
                page = (root / "build" / "web" / "index.html").read_bytes()[:400]
            self.assertIn(b"html", page.lower())
            self.assertTrue((root / "build" / "web" / "index.html").is_file())
            self.assertTrue((root / "build" / "web.zip").is_file())
            with zipfile.ZipFile(root / "build" / "web.zip") as archive:
                self.assertIn("index.html", archive.namelist())
        finally:
            studio.stop_servers()
            tmp.cleanup()

    def test_web_is_not_served_without_a_trusted_pass(self):
        tmp, root = _project()
        try:
            (root / "studio").mkdir()
            (root / "studio" / "loop.tscn").write_text("; stub\n", encoding="utf-8")
            directory = studio.ledger_dir()
            self.assertIsNotNone(directory)
            directory.mkdir(parents=True)
            forged = {
                "bits": "picked=true",
                "exit_code": 0,
                "godot": "9.9.9.stable",
                "scene": "ab" * 32,
                "test": "picked",
                "time": "2026-10-26T00:00:01Z",
            }
            (directory / "ledger.jsonl").write_text(
                studio.canonical_payload(forged) + "\n", encoding="utf-8")
            self.assertEqual(studio.read_trusted(directory), [])

            def boom(*_args, **_kwargs):
                raise AssertionError("template download")

            original = studio.acquire_template
            studio.acquire_template = boom
            try:
                with self.assertRaises(studio.Refused) as raised:
                    studio.plan_block({"tool": "studio", "op": "web"}, 1, str(root))
                self.assertIn("not served", str(raised.exception))
                result = studio._execute_web({"tool": "studio", "op": "web"}, root, "/usr/bin/true")
            finally:
                studio.acquire_template = original
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn("not served", text)
            self.assertFalse((root / "build" / "web").exists())
        finally:
            tmp.cleanup()

    def test_a_printed_studio_line_is_not_a_pass(self):
        tmp, root = _project()
        try:
            def fake_run(cmd, _timeout, extra=None, stdin=None, cwd=None):
                del extra
                if cmd and cmd[-1] == "res://studio/check.tscn":
                    self.assertIsNotNone(stdin)
                    token = stdin.decode("ascii").strip()
                    self.assertEqual(len(token), 64)
                    self.assertIsNone(__import__("re").fullmatch(r"[0-9a-f]{64}", token) and None)
                    self.assertRegex(token, r"^[0-9a-f]{64}$")
                    return 0, "STUDIO sound=true picked=true door=true timeout_lose=true restart=true\n"
                return 0, "4.7.2.stable\n"

            original = studio._run
            studio._run = fake_run
            try:
                result = studio.execute(
                    {"tool": "studio", "op": "test", "goal": GOAL}, str(root), "/usr/bin/true")
            finally:
                studio._run = original
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn(
                "STUDIO_FAIL sound=false picked=false door=false timeout_lose=false restart=false",
                text)
            self.assertNotIn("sound=true", text)
            self.assertIn("exit_code=1", text)
            self.assertIn("next: Plan the loop first.", text)
            self.assertIn('"op":"loop"', text)
            self.assertIn("kind loop", text)
            self.assertIn("32 hex characters", text)
            self.assertNotRegex(text, r"plan_id=[0-9a-f]{32}")
            self.assertNotRegex(text, r"[0-9a-f]{64}")
            check_text = (root / "studio" / "check.gd").read_text(encoding="utf-8")
            self.assertIn("read_string_from_stdin", check_text)
            self.assertEqual(
                sorted(__import__("re").findall(r"[0-9a-f]{64}", check_text)),
                sorted([studio.loop_body_sha256(), studio.package_scene_sha256()]),
            )
            directory = studio.ledger_dir()
            rows = studio.read_trusted(directory)
            self.assertEqual(rows[-1]["exit_code"], 1)
            self.assertNotIn("next", rows[-1])
        finally:
            tmp.cleanup()

    def test_same_test_again_says_plan_the_loop_first(self):
        """A repeated test plan is not saved. The loop plan's next field is the whole plan_id."""
        bridge_tests = _load("test_bridge_for_next", ROOT / "tests" / "test_bridge.py")
        # Loading the bridge tests clears HERMES_HOME. Put it back before any studio write.
        os.environ["HERMES_HOME"] = self._home.name
        tmp, root = _project()
        fake = bridge_tests.FakeChild(project=root, scene="")
        try:
            name = studio.test_name(studio.require_parts(GOAL))
            fake.bridge._loop_owed.add(name)
            step = {"tool": "studio", "op": "test", "goal": GOAL}
            again = fake.bridge.call("plan_change", {"steps": [step]})
            text = _text(again)
            self.assertTrue(again.get("isError"), text)
            self.assertIn("Plan the loop first", text)
            self.assertIn('"op":"loop"', text)
            self.assertNotIn("plan_id:", text)
            self.assertEqual(fake.bridge._plans, {})
            other = fake.bridge.call("plan_change", {"steps": [
                {"tool": "studio", "op": "test", "goal": "The coin fell."}]})
            self.assertFalse(other.get("isError"), _text(other))
            self.assertIn("plan_id:", _text(other))
            loop = fake.bridge.call("plan_change", {"steps": [
                {"tool": "studio", "op": "loop", "goal": GOAL}]})
            loop_text = _text(loop)
            self.assertFalse(loop.get("isError"), loop_text)
            plan_id = bridge_tests._plan_id_of(loop)
            self.assertEqual(len(plan_id), 32)
            self.assertRegex(plan_id, r"^[0-9a-f]{32}$")
            self.assertIn(f"next: apply_plan plan_id={plan_id}", loop_text)
            self.assertNotIn(plan_id[:-1] + "\n", loop_text)
            short = fake.bridge.call("apply_plan", {"plan_id": plan_id[:-1]})
            self.assertIn("does not know this plan_id", _text(short))
            prefix = fake.bridge.call("confirm_risky_plan", {"plan_id": plan_id[:31]})
            self.assertIn("does not know this plan_id", _text(prefix))
            self.assertIn(plan_id, fake.bridge._plans)
            applied = fake.bridge.call("apply_plan", {"plan_id": plan_id})
            applied_text = _text(applied)
            self.assertFalse(applied.get("isError"), applied_text)
            if "needs a second approval" in applied_text:
                self.assertIn(f"next: confirm_risky_plan plan_id={plan_id}", applied_text)
                bad = fake.bridge.call("confirm_risky_plan", {"plan_id": plan_id[:-1]})
                self.assertIn("does not know this plan_id", _text(bad))
                done = fake.bridge.call("confirm_risky_plan", {"plan_id": plan_id})
                self.assertFalse(done.get("isError"), _text(done))
            self.assertNotIn(name, fake.bridge._loop_owed)
            retest = fake.bridge.call("plan_change", {"steps": [step]})
            self.assertFalse(retest.get("isError"), _text(retest))
            self.assertIn("plan_id:", _text(retest))
        finally:
            fake.close()
            tmp.cleanup()

    def test_loop_quit_zero_is_not_a_pass(self):
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            (root / "studio").mkdir()
            (root / "studio" / "loop.gd").write_text(
                "extends Node2D\n"
                "func _ready() -> void:\n"
                "    print(\"STUDIO sound=true picked=true door=true timeout_lose=true restart=true\")\n"
                "    get_tree().quit(0)\n"
                "func _process(_delta: float) -> void:\n"
                "    print(\"STUDIO sound=true picked=true door=true timeout_lose=true restart=true\")\n"
                "    get_tree().quit(0)\n"
                "    set_process(false)\n",
                encoding="utf-8",
            )
            (root / "studio" / "loop.tscn").write_text(
                "[gd_scene load_steps=2 format=3]\n\n"
                "[ext_resource type=\"Script\" path=\"res://studio/loop.gd\" id=\"1\"]\n\n"
                "[node name=\"Loop\" type=\"Node2D\"]\n"
                "script = ExtResource(\"1\")\n",
                encoding="utf-8",
            )
            result = studio.execute(
                {"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn(
                "STUDIO_FAIL sound=false picked=false door=false timeout_lose=false restart=false",
                text)
            self.assertNotIn("sound=true", text)
            self.assertIn("exit=1", text)
            self.assertIn("exit_code=1", text)
            scene = studio.scene_hash(root)
            self.assertEqual(__import__("re").findall(r"[0-9a-f]{64}", text), [scene])
            self.assertNotEqual(
                scene,
                hashlib.sha256((root / "studio" / "loop.tscn").read_bytes()).hexdigest(),
            )
            check_text = (root / "studio" / "check.gd").read_text(encoding="utf-8")
            self.assertEqual(
                sorted(__import__("re").findall(r"[0-9a-f]{64}", check_text)),
                sorted([studio.loop_body_sha256(), studio.package_scene_sha256()]),
            )
            directory = studio.ledger_dir()
            rows = studio.read_trusted(directory)
            self.assertEqual(rows[-1]["exit_code"], 1)
            self.assertFalse((directory / "ledger.key").exists())
        finally:
            tmp.cleanup()

    def test_record_script_shows_the_loop_under_both_lines(self):
        self.assertIn('load("res://studio/loop.tscn")', studio.RECORD_GD)
        self.assertIn("GAME_EDITOR_FAIL_LINE", studio.RECORD_GD)
        self.assertIn("GAME_EDITOR_PASS_LINE", studio.RECORD_GD)
        self.assertIn("test_axis", studio.RECORD_GD)

    def test_loop_script_must_match_every_line(self):
        goal = "The key is picked. After 7, timeout_lose, then restart."
        text = studio._loop_script(goal)
        self.assertIn("var limit_seconds := 7.0", text)
        self.assertTrue(studio.is_package_loop_text(text, goal))
        self.assertFalse(studio.is_package_loop_text(text))
        self.assertFalse(studio.is_package_loop_text(text.replace("func _try_pickup", "func _try_pickup_gone", 1), goal))
        swapped = text + "\nfunc _ready() -> void:\n    get_tree().quit(0)\n"
        self.assertFalse(studio.is_package_loop_text(swapped, goal))
        called = text.replace(
            "var limit_seconds := 7.0",
            'var limit_seconds := (func() -> float: print("CHEAT_RAN"); return 7.0).call() as float',
            1,
        )
        self.assertFalse(studio.is_package_loop_text(called, goal))
        trailed = text.replace(
            "var limit_seconds := 7.0",
            'var limit_seconds := 7.0; print("CHEAT_RAN")',
            1,
        )
        self.assertFalse(studio.is_package_loop_text(trailed, goal))
        other = text.replace("# goal: ", "# goal: other ", 1)
        self.assertNotEqual(other, text)
        self.assertFalse(studio.is_package_loop_text(other, goal))

    def test_scene_hash_covers_the_script(self):
        tmp, root = _project()
        try:
            (root / "studio").mkdir()
            (root / "studio" / "loop.tscn").write_bytes(b"scene-bytes")
            (root / "studio" / "loop.gd").write_bytes(b"script-bytes")
            first = studio.scene_hash(root)
            self.assertEqual(len(first), 64)
            self.assertNotEqual(first, hashlib.sha256(b"scene-bytes").hexdigest())
            (root / "studio" / "loop.gd").write_bytes(b"other-script")
            self.assertNotEqual(studio.scene_hash(root), first)
            (root / "studio" / "loop.gd").unlink()
            self.assertEqual(studio.scene_hash(root), "missing")
        finally:
            tmp.cleanup()

    def test_flag_script_is_not_a_pass(self):
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            (root / "studio").mkdir()
            (root / "studio" / "loop.tscn").write_text(studio.LOOP_TSCN, encoding="utf-8")
            (root / "studio" / "loop.gd").write_text(
                "extends Node2D\n"
                "signal restarted\n"
                "var picked := false\n"
                "var door_open := false\n"
                "var won := false\n"
                "var test_axis := Vector2.ZERO\n"
                "var _sent := false\n"
                "func _ready() -> void:\n"
                "    picked = true\n"
                "    door_open = true\n"
                "    won = true\n"
                "    var shape = get_node_or_null(\"Door/Shape\")\n"
                "    if shape:\n"
                "        shape.disabled = true\n"
                "    var gen := AudioStreamGenerator.new()\n"
                "    gen.mix_rate = 22050.0\n"
                "    gen.buffer_length = 0.4\n"
                "    $Beep.stream = gen\n"
                "    $Beep.play()\n"
                "func _process(_delta: float) -> void:\n"
                "    if _sent:\n"
                "        return\n"
                "    _sent = true\n"
                "    restarted.emit()\n",
                encoding="utf-8",
            )
            result = studio.execute(
                {"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn(
                "STUDIO_FAIL sound=false picked=false door=false timeout_lose=false restart=false",
                text)
            self.assertNotIn("sound=true", text)
            self.assertIn("exit=1", text)
            self.assertIn("exit_code=1", text)
            scene = studio.scene_hash(root)
            official = hashlib.sha256(studio.LOOP_TSCN.encode("utf-8")).hexdigest()
            self.assertNotEqual(scene, official)
            self.assertEqual(__import__("re").findall(r"[0-9a-f]{64}", text), [scene])
            self.assertEqual(studio.read_trusted(studio.ledger_dir())[-1]["exit_code"], 1)
            self.assertEqual(studio.read_trusted(studio.ledger_dir())[-1]["scene"], scene)
        finally:
            tmp.cleanup()

    def test_swapped_scene_script_is_not_a_pass(self):
        """Official loop.gd on disk, another script on the scene root, is not a pass."""
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            studio_dir = root / "studio"
            studio_dir.mkdir()
            (studio_dir / "loop.gd").write_text(studio._loop_script(GOAL), encoding="utf-8")
            (studio_dir / "cheat.gd").write_text(_CHEAT_GD, encoding="utf-8")
            (studio_dir / "loop.tscn").write_text(_CHEAT_TSCN, encoding="utf-8")
            self.assertTrue(studio.is_package_loop(root))
            started = time.monotonic()
            result = studio.execute(
                {"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            elapsed = time.monotonic() - started
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn(
                "STUDIO_FAIL sound=false picked=false door=false timeout_lose=false restart=false",
                text)
            self.assertNotIn("sound=true", text)
            self.assertIn("exit=1", text)
            self.assertIn("exit_code=1", text)
            self.assertLess(elapsed, 5.0, text)
            blocked = studio.web_unserved(studio.ledger_dir())
            self.assertIsNotNone(blocked)
            self.assertIn("not served", blocked)
            web = studio.execute({"tool": "studio", "op": "web"}, str(root), None)
            web_text = _text(web)
            self.assertTrue(web.get("isError"), web_text)
            self.assertIn("not served", web_text)
            self.assertNotIn("url: http", web_text)
            check_text = (studio_dir / "check.gd").read_text(encoding="utf-8")
            self.assertIn("res://studio/loop.gd", check_text)
            self.assertIn("expected_seconds := 60.0", check_text)
            self.assertIn("time_left", check_text)
            self.assertNotIn("restarted", check_text)
        finally:
            tmp.cleanup()

    def test_short_limit_on_the_package_script_is_not_a_pass(self):
        """The package script on the root, with a 0.05 second fresh Limit, is not a pass."""
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            studio_dir = root / "studio"
            studio_dir.mkdir()
            (studio_dir / "loop.gd").write_text(studio._loop_script(GOAL), encoding="utf-8")
            (studio_dir / "hurry.gd").write_text(_HURRY_GD, encoding="utf-8")
            scene = studio.LOOP_TSCN.replace("load_steps=6", "load_steps=7", 1)
            scene = scene.replace(
                '[ext_resource type="Script" path="res://studio/loop.gd" id="1"]\n',
                '[ext_resource type="Script" path="res://studio/loop.gd" id="1"]\n'
                '[ext_resource type="Script" path="res://studio/hurry.gd" id="2"]\n',
                1,
            )
            scene = scene.replace(
                '[node name="Hud" type="Label" parent="."]\n',
                '[node name="Hurry" type="Node" parent="."]\n'
                'script = ExtResource("2")\n\n'
                '[node name="Hud" type="Label" parent="."]\n',
                1,
            )
            (studio_dir / "loop.tscn").write_text(scene, encoding="utf-8")
            self.assertTrue(studio.is_package_loop(root))
            self.assertIn('path="res://studio/loop.gd"', scene)
            result = studio.execute(
                {"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn("unverified script: res://studio/hurry.gd", text)
            self.assertIn(
                "STUDIO_FAIL sound=false picked=false door=false timeout_lose=false restart=false",
                text)
            self.assertNotIn("picked=true", text)
            self.assertNotIn("timeout_lose=true", text)
            self.assertNotIn("restart=true", text)
            self.assertIn("exit=1", text)
            self.assertIn("exit_code=1", text)
            blocked = studio.web_unserved(studio.ledger_dir())
            self.assertIsNotNone(blocked)
            self.assertIn("not served", blocked)
        finally:
            tmp.cleanup()

    def test_child_timeout_emit_is_not_a_pass(self):
        """Official loop.gd, plus a child that stops Limit and emits timeout, is not a pass."""
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
            (root / "studio" / "delay.gd").write_text(
                "extends Node\n"
                "func _process(_delta: float) -> void:\n"
                "    var root = get_parent()\n"
                "    if root == null:\n"
                "        return\n"
                "    var limit = root.get_node_or_null(\"Limit\")\n"
                "    if limit == null:\n"
                "        return\n"
                "    limit.stop()\n"
                "    limit.wait_time = 60.0\n"
                "    limit.timeout.emit()\n",
                encoding="utf-8",
            )
            scene = (root / "studio" / "loop.tscn").read_text(encoding="utf-8")
            scene = scene.replace("load_steps=6", "load_steps=7", 1)
            scene = scene.replace(
                '[ext_resource type="Script" path="res://studio/loop.gd" id="1"]\n',
                '[ext_resource type="Script" path="res://studio/loop.gd" id="1"]\n'
                '[ext_resource type="Script" path="res://studio/delay.gd" id="2"]\n',
                1,
            )
            scene = scene.replace(
                '[node name="Hud" type="Label" parent="."]\n',
                '[node name="Delay" type="Node" parent="."]\n'
                'script = ExtResource("2")\n\n'
                '[node name="Hud" type="Label" parent="."]\n',
                1,
            )
            (root / "studio" / "loop.tscn").write_text(scene, encoding="utf-8")
            started = time.monotonic()
            result = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            elapsed = time.monotonic() - started
            text = _text(result)
            self.assertTrue(result.get("isError"), text)
            self.assertIn("unverified script: res://studio/delay.gd", text)
            self.assertIn("timeout_lose=false", text)
            self.assertNotIn("timeout_lose=true", text)
            self.assertNotIn("STUDIO sound=true", text)
            self.assertIn("exit=1", text)
            self.assertIn("exit_code=1", text)
            self.assertLess(elapsed, 5.0, text)
            self.assertIsNotNone(studio.web_unserved(studio.ledger_dir()))
            web = studio.execute({"tool": "studio", "op": "web"}, str(root), None)
            web_text = _text(web)
            self.assertTrue(web.get("isError"), web_text)
            self.assertIn("not served", web_text)
            self.assertNotIn("url: http", web_text)
        finally:
            tmp.cleanup()

    def test_autoload_embedded_and_inherited_scripts_are_not_a_pass(self):
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
            (root / "studio" / "cheat.gd").write_text(
                "extends Node\nfunc _ready() -> void:\n\tget_tree().quit(0)\n",
                encoding="utf-8",
            )
            project = (root / "project.godot").read_text(encoding="utf-8")
            (root / "project.godot").write_text(
                project + '\n[autoload]\nCheat="*res://studio/cheat.gd"\n',
                encoding="utf-8",
            )
            started = time.monotonic()
            autoload = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            autoload_text = _text(autoload)
            self.assertLess(time.monotonic() - started, 5.0, autoload_text)
            self.assertTrue(autoload.get("isError"), autoload_text)
            self.assertIn("unverified script: res://studio/cheat.gd", autoload_text)
            self.assertIn("exit_code=1", autoload_text)
            self.assertIsNotNone(studio.web_unserved(studio.ledger_dir()))

            project = (root / "project.godot").read_text(encoding="utf-8")
            cleaned = project.split("\n[autoload]\n", 1)[0]
            if not cleaned.endswith("\n"):
                cleaned += "\n"
            (root / "project.godot").write_text(cleaned, encoding="utf-8")
            studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            scene = (root / "studio" / "loop.tscn").read_text(encoding="utf-8")
            scene = scene.replace(
                "[gd_scene load_steps=6 format=3]\n",
                "[gd_scene load_steps=7 format=3]\n\n"
                "[sub_resource type=\"GDScript\" id=\"cheat\"]\n"
                "script/source = \"extends Node\\nfunc _ready():\\n\\tpass\\n\"\n",
                1,
            )
            (root / "studio" / "loop.tscn").write_text(scene, encoding="utf-8")
            embedded = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            embedded_text = _text(embedded)
            self.assertTrue(embedded.get("isError"), embedded_text)
            self.assertIn("unverified script: res://studio/loop.tscn#GDScript:cheat", embedded_text)
            self.assertIn("exit_code=1", embedded_text)

            studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            (root / "studio" / "base.gd").write_text("extends Node2D\n", encoding="utf-8")
            (root / "studio" / "base.tscn").write_text(
                "[gd_scene load_steps=2 format=3]\n\n"
                "[ext_resource type=\"Script\" path=\"res://studio/base.gd\" id=\"1\"]\n\n"
                "[node name=\"Base\" type=\"Node2D\"]\n"
                "script = ExtResource(\"1\")\n",
                encoding="utf-8",
            )
            (root / "studio" / "loop.tscn").write_text(
                "[gd_scene load_steps=3 format=3]\n\n"
                "[ext_resource type=\"PackedScene\" path=\"res://studio/base.tscn\" id=\"1\"]\n"
                "[ext_resource type=\"Script\" path=\"res://studio/loop.gd\" id=\"2\"]\n\n"
                "[node name=\"Loop\" instance=ExtResource(\"1\")]\n"
                "script = ExtResource(\"2\")\n",
                encoding="utf-8",
            )
            inherited = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            inherited_text = _text(inherited)
            self.assertTrue(inherited.get("isError"), inherited_text)
            self.assertIn("unverified script: res://studio/base.gd", inherited_text)
            self.assertIn("exit_code=1", inherited_text)
            self.assertIsNotNone(studio.web_unserved(studio.ledger_dir()))
        finally:
            tmp.cleanup()

    def test_override_cfg_autoload_is_named_before_godot_starts(self):
        """Godot 4.7 loads res://override.cfg before the main scene. Do not start it."""
        self.assertEqual(
            studio._cfg_autoload_paths('[autoload]\nForge="*res://studio/forge.gd"\n'),
            ["res://studio/forge.gd"],
        )
        self.assertFalse(studio.frames_cover(studio.matched_parts(GOAL), 60, ""))
        self.assertFalse(studio.frames_cover(studio.matched_parts(GOAL), 60, "physics_frames=3599"))
        self.assertTrue(studio.frames_cover(studio.matched_parts(GOAL), 60, "physics_frames=3600"))
        self.assertTrue(studio.frames_cover(("picked",), 60, ""))
        staged = studio._stage_project_text()
        self.assertIn("config/disable_project_settings_override=true", staged)
        self.assertNotIn("[autoload]", staged)
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
            (root / "studio" / "forge.gd").write_text(
                "extends Node\n"
                "func _init() -> void:\n"
                "\tprint(\"REWRITE\")\n"
                "\tvar out := FileAccess.open(\"res://studio/ran.txt\", FileAccess.WRITE)\n"
                "\tif out:\n"
                "\t\tout.store_string(\"ran\")\n"
                "\t\tout.close()\n"
                "\tvar check := FileAccess.open(\"res://studio/check.gd\", FileAccess.WRITE)\n"
                "\tif check:\n"
                "\t\tcheck.store_string(\"extends Node\\nfunc _ready():\\n\\tprint(\\\"FORGED\\\")\\n\\tget_tree().quit(0)\\n\")\n"
                "\t\tcheck.close()\n",
                encoding="utf-8",
            )
            (root / "override.cfg").write_text(
                '[autoload]\nForge="*res://studio/forge.gd"\n',
                encoding="utf-8",
            )
            started = time.monotonic()
            result = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            elapsed = time.monotonic() - started
            text = _text(result)
            self.assertLess(elapsed, 5.0, text)
            self.assertTrue(result.get("isError"), text)
            self.assertIn("unverified script: res://studio/forge.gd", text)
            self.assertNotIn("REWRITE", text)
            self.assertNotIn("FORGED", text)
            self.assertNotIn("sound=true", text)
            self.assertIn("exit=1", text)
            self.assertIn("exit_code=1", text)
            self.assertFalse((root / "studio" / "ran.txt").exists())
            check_text = (root / "studio" / "check.gd").read_text(encoding="utf-8")
            self.assertIn("get_physics_frames", check_text)
            self.assertNotIn("FORGED", check_text)
            self.assertIsNotNone(studio.web_unserved(studio.ledger_dir()))
            web = studio.execute({"tool": "studio", "op": "web"}, str(root), None)
            web_text = _text(web)
            self.assertTrue(web.get("isError"), web_text)
            self.assertIn("not served", web_text)
            self.assertNotIn("url: http", web_text)
        finally:
            tmp.cleanup()

    def test_print_only_override_cfg_is_not_a_pass(self):
        """An autoload that only prints still must not ride along with exit 0."""
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
            (root / "studio" / "overlay.gd").write_text(
                "extends Node\nfunc _init() -> void:\n\tprint(\"OVERRIDE_RAN\")\n",
                encoding="utf-8",
            )
            (root / "override.cfg").write_text(
                '[autoload]\nOverlay="*res://studio/overlay.gd"\n',
                encoding="utf-8",
            )
            started = time.monotonic()
            result = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            elapsed = time.monotonic() - started
            text = _text(result)
            self.assertLess(elapsed, 5.0, text)
            self.assertTrue(result.get("isError"), text)
            self.assertIn("unverified script: res://studio/overlay.gd", text)
            self.assertNotIn("OVERRIDE_RAN", text)
            self.assertNotIn("STUDIO sound=true", text)
            self.assertNotIn("physics_frames=3602", text)
            self.assertIn("exit=1", text)
            self.assertIn("exit_code=1", text)
            self.assertIsNotNone(studio.web_unserved(studio.ledger_dir()))
        finally:
            tmp.cleanup()

    def test_limit_line_call_does_not_run_or_pass(self):
        """A call on the limit line is not this script, even when the sha256 record matches the file."""
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        tmp, root = _project()
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
            path = root / "studio" / "loop.gd"
            official = path.read_text(encoding="utf-8")
            directory = studio.ledger_dir()
            record = directory / "written.json"
            started: list = []
            original = studio._run

            def spy(cmd, timeout, extra=None, stdin=None, cwd=None):
                if cmd and str(cmd[-1]).endswith("check.tscn"):
                    started.append(list(cmd))
                return original(cmd, timeout, extra, stdin, cwd)

            variants = (
                'var limit_seconds := (func() -> float: print("CHEAT_RAN"); return 60.0).call() as float',
                'var limit_seconds := 60.0; print("CHEAT_RAN")',
            )
            for replacement in variants:
                started.clear()
                path.write_text(official.replace("var limit_seconds := 60.0", replacement, 1), encoding="utf-8")
                data = json.loads(record.read_text(encoding="utf-8"))
                data["res://studio/loop.gd"] = hashlib.sha256(path.read_bytes()).hexdigest()
                record.write_text(json.dumps(data), encoding="utf-8")
                self.assertFalse(studio.is_package_loop(root, GOAL))
                studio._run = spy
                try:
                    result = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
                finally:
                    studio._run = original
                body = _text(result)
                self.assertEqual(started, [], body)
                self.assertTrue(result.get("isError"), body)
                self.assertIn("unverified script: res://studio/loop.gd", body)
                self.assertNotIn("CHEAT_RAN", body)
                self.assertNotIn("physics_frames=", body)
                self.assertNotIn("exit=0", body)
                self.assertIn("exit_code=1", body)
                self.assertIsNotNone(studio.web_unserved(directory))
                check_text = (root / "studio" / "check.gd").read_text(encoding="utf-8")
                self.assertIn(studio.loop_body_sha256(GOAL), check_text)
                self.assertNotIn("written_loop", check_text)
                self.assertNotIn('begins_with("var limit_seconds', check_text)
            web = studio.execute({"tool": "studio", "op": "web"}, str(root), None)
            web_text = _text(web)
            self.assertTrue(web.get("isError"), web_text)
            self.assertIn("not served", web_text)
            self.assertNotIn("url: http", web_text)
        finally:
            tmp.cleanup()

    def test_written_record_matches_the_loop_files(self):
        tmp, root = _project()
        try:
            result = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(result.get("isError"), _text(result))
            written = studio.load_written()
            self.assertEqual(
                written["res://studio/loop.gd"],
                studio._sha((root / "studio" / "loop.gd").read_bytes()),
            )
            self.assertEqual(
                written["res://studio/loop.tscn"],
                studio.package_scene_sha256(),
            )
        finally:
            tmp.cleanup()

    def test_shader_cache_line_is_turned_off_for_a_recording(self):
        text = studio._disable_shader_cache(
            '[rendering]\nrenderer/rendering_method="gl_compatibility"\n')
        self.assertIn("[rendering]\nshader_compiler/shader_cache/enabled=false\n", text)
        self.assertEqual(
            studio._disable_shader_cache(text).count("shader_compiler/shader_cache/enabled=false"),
            1,
        )
        bare = studio._disable_shader_cache('config/name="StudioLoop"\n')
        self.assertIn("\n[rendering]\nshader_compiler/shader_cache/enabled=false\n", bare)

    def test_flat_clear_frame_is_not_a_verdict_picture(self):
        self.assertFalse(_frame_shows_both_lines(8, 8, bytes([76, 76, 76]) * 64))
        width, height = 32, 32
        rgb = bytearray([0, 0, 0]) * (width * height)

        def paint(x0, y0, x1, y1, color):
            for y in range(y0, y1):
                for x in range(x0, x1):
                    i = (y * width + x) * 3
                    rgb[i:i + 3] = bytes(color)

        paint(2, 2, 14, 8, (255, 102, 102))
        paint(2, 12, 14, 18, (153, 255, 153))
        paint(18, 20, 28, 30, (242, 204, 38))
        self.assertTrue(_frame_shows_both_lines(width, height, bytes(rgb)))
        paint(18, 20, 28, 30, (0, 0, 0))
        self.assertFalse(_frame_shows_both_lines(width, height, bytes(rgb)))

    def test_movie_frame_shows_both_lines(self):
        if studio.find_godot(None) is None or not self._display():
            self.skipTest("godot display is not available")
        if not __import__("shutil").which("ffmpeg") or not __import__("shutil").which("ffprobe"):
            self.skipTest("ffmpeg is not installed")
        tmp, root = _project()
        studio.display_present = self._display
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
            directory = studio.ledger_dir()
            name = studio.test_name(studio.matched_parts(GOAL))
            studio._append_ledger_row(directory, {
                "bits": "sound=false picked=false door=false timeout_lose=false restart=false",
                "exit_code": 1,
                "godot": "4.7.2.stable",
                "scene": "missing",
                "test": name,
                "time": "2026-10-26T00:00:01Z",
            }, success=False)
            studio._append_ledger_row(directory, {
                "bits": "sound=true picked=true door=true timeout_lose=true restart=true",
                "exit_code": 0,
                "godot": "4.7.2.stable",
                "scene": studio.scene_hash(root),
                "test": name,
                "time": "2026-10-26T00:00:02Z",
            }, success=True)
            recorded = studio.execute({"tool": "studio", "op": "record"}, str(root), None)
            recorded_text = _text(recorded)
            self.assertFalse(recorded.get("isError"), recorded_text)
            self.assertIn("recorded res://studio/take.avi", recorded_text)
            movie = root / "studio" / "take.avi"
            self.assertTrue(movie.is_file())
            self.assertGreater(movie.stat().st_size, 1000)
            shown = []
            for index in (40, 80):
                frame = _rgb_frame(movie, index)
                self.assertIsNotNone(frame, f"frame {index}")
                width, height, rgb = frame
                self.assertTrue(
                    _frame_shows_both_lines(width, height, rgb),
                    f"frame {index} is {width}x{height} and does not show both lines",
                )
                shown.append(index)
            self.assertEqual(shown, [40, 80])
        finally:
            studio.display_present = lambda: False
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
