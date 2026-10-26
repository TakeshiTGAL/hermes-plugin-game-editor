"""Studio plan steps: a headless Godot loop, a loopback Web export, and one CREDITS line."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import tempfile
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

GOAL = "Pick up the key and a sound plays and the door opens. If you do not get out in 60 seconds, start over."
SECRETS = (
    "supersecretvalue",
    "jsonsecretvalue",
    "tokensecretvalue",
    "cli-secretvalue",
    "bobsecret",
    "slacksecretvalue",
)
SECRET_GOAL = (
    "60 Pick up the key. Bearer supersecretvalue "
    '{"password":"jsonsecretvalue"} API_TOKEN=tokensecretvalue '
    "--password cli-secretvalue https://alice:bobsecret@example.com/a xoxb-slacksecretvalue"
)
BOX = [(x, y, z) for x in (0.0, 1.0) for y in (0.0, 2.0) for z in (0.0, 1.0)]


def _text(result: dict) -> str:
    return result["content"][0]["text"]


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


class StudioTest(unittest.TestCase):
    def setUp(self):
        self._display = studio.display_present
        studio.display_present = lambda: False
        self._cache = studio.CACHE_DIR

    def tearDown(self):
        studio.display_present = self._display
        studio.CACHE_DIR = self._cache
        studio.stop_servers()

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

    def test_plan_names_the_rule_key_and_writes_nothing(self):
        tmp, root = _project()
        try:
            step = {"tool": "studio", "op": "test", "goal": GOAL}
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            block, losses = studio.plan_block(step, 1, str(root))
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertEqual(before, after)
            self.assertIn("rule_key: " + studio.rule_key(step), block)
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
            failed = studio.execute({"tool": "studio", "op": "test", "goal": SECRET_GOAL}, str(root), None)
            failed_text = _text(failed)
            self.assertTrue(failed.get("isError"), failed_text)
            self.assertIn("STUDIO_FAIL fell=false sound=false picked=false door=false timeout_lose=false restart=false",
                          failed_text)
            self.assertIn("exit=1", failed_text)
            for secret in SECRETS:
                self.assertNotIn(secret, failed_text)
            fail_file = (root / "studio" / "verdict_fail.txt").read_text(encoding="utf-8")
            self.assertTrue(fail_file.startswith("STUDIO_FAIL "))
            self.assertFalse((root / "studio" / "verdict_pass.txt").exists())

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

            passed = studio.execute({"tool": "studio", "op": "test", "goal": GOAL}, str(root), None)
            passed_text = _text(passed)
            self.assertFalse(passed.get("isError"), passed_text)
            self.assertIn(
                "STUDIO fell=true sound=true picked=true door=true timeout_lose=true restart=true",
                passed_text)
            self.assertIn("exit=0", passed_text)
            self.assertTrue((root / "studio" / "verdict_fail.txt").read_text(encoding="utf-8").startswith("STUDIO_FAIL "))
            self.assertTrue((root / "studio" / "verdict_pass.txt").read_text(encoding="utf-8").startswith("STUDIO "))
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

    def test_plan_approval_then_fail_string(self):
        if studio.find_godot(None) is None:
            self.skipTest("godot is not installed")
        bridge_tests = _load("test_bridge_for_studio", ROOT / "tests" / "test_bridge.py")
        tmp, root = _project()
        fake = bridge_tests.FakeChild(project=root, scene="")
        try:
            step = {"tool": "studio", "op": "test", "goal": GOAL}
            planned = fake.bridge.call("plan_change", {"steps": [step]})
            self.assertFalse(planned.get("isError"), _text(planned))
            planned_text = _text(planned)
            self.assertIn("rule_key: studio:", planned_text)
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
        cache = studio._cache_file()
        if not studio.template_matches(cache):
            self.skipTest("pinned export template is not in the cache")
        tmp, root = _project()
        try:
            loop = studio.execute({"tool": "studio", "op": "loop", "goal": GOAL}, str(root), None)
            self.assertFalse(loop.get("isError"), _text(loop))
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


if __name__ == "__main__":
    unittest.main()
