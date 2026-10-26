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


def _http_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "game-editor-fixture-check"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except Exception as exc:
        raise AssertionError("could not open " + url) from exc


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
            scene_hash = hashlib.sha256((root / "studio" / "loop.tscn").read_bytes()).hexdigest()
            self.assertEqual(studio.latest_row(passed_rows, name)["scene"], scene_hash)
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
        try:
            with self.assertRaises(studio.Refused):
                studio.cache_dir()
        finally:
            os.environ["HERMES_HOME"] = saved

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
        catalogs: dict[str, tuple[str, str]] = {}
        for row in pin["excerpts"]:
            seen.add(row["file"])
            text = (fixture_dir / row["file"]).read_text(encoding="utf-8")
            self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), row["sha256"])
            for marker in markers[row["file"]]:
                self.assertIn(marker, text)
            self.assertEqual(len(row["sha"]), 40)
            self.assertRegex(row["repo"], r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
            opened = _pinned_blob(row["repo"], row["sha"], row["source"])
            self.assertEqual(_slice_lines(opened, row["lines"]), text)
            plugin = row["plugin"]
            if plugin not in catalogs:
                catalogs[plugin] = _catalog_sha_version(plugin)
            catalog_sha, catalog_version = catalogs[plugin]
            self.assertEqual(catalog_sha, row["sha"])
            self.assertEqual(catalog_version, row["version"])
        self.assertEqual(seen, set(markers))
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

            def fake_run(_cmd, _timeout, extra=None):
                del extra
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
