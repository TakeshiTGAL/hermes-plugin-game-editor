"""Plan steps that build a tested Godot loop and a loopback Web export.

Loaded by file path from the bridge. Standard library only.
ship_build does not download export templates. The web op here downloads one
pinned Godot 4.7.2 template, and only after sha256 and sha512 match.
"""
from __future__ import annotations

import atexit
import hashlib
import http.server
import io
import json
import os
import pwd
import re
import secrets
import stat
import struct
import subprocess
import sys
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

STUDIO_OPS = ("test", "loop", "web", "import_asset", "record")

TEMPLATE_VERSION = "4.7.2"
TEMPLATE_FILE = "Godot_v4.7.2-stable_export_templates.tpz"
TEMPLATE_URL = "https://github.com/godotengine/godot/releases/download/4.7.2-stable/Godot_v4.7.2-stable_export_templates.tpz"
TEMPLATE_BYTES = 1281349702
TEMPLATE_SHA256 = "f298490b8d44d934be425a5a65a51bf15f422428b229a06a6e11d9ffea248011"
TEMPLATE_SHA512 = (
    "ca4d71c4d7b81dfc15d1a98baa07534aa95b03fdda78a0075b06672e1648d2e5"
    "f40980c9adc28d23e1b92e732ee7bf3461997aa804af74ec2fcd7a93ccb84079"
)
# None uses <HERMES_HOME>/plugin-data/game-editor/export-templates. Tests may set a directory.
CACHE_DIR: Path | None = None
ALLOWED_HOSTS = frozenset({
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
})
MAX_IMPORT_BYTES = 64 * 1024 * 1024
ORIGIN_MAX = 4096
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,64}$")
NOT_RECORDED = (
    "not recorded: no macOS GUI session, so Movie Maker was not started. "
    "Headless --write-movie on Godot 4.7.2 exits with signal 11 "
    "(SIGSEGV in texture_2d_get, parameter t is null). "
    "A still image was not saved in its place."
)
NOT_RECORDED_LINUX = (
    "not recorded: no display (no X11 socket and no Wayland socket), so Movie Maker was not started. "
    "Headless --write-movie on Godot 4.7.2 exits with signal 11 "
    "(SIGSEGV in texture_2d_get, parameter t is null). "
    "A still image was not saved in its place."
)
_KEYS = {
    "test": {"goal"},
    "loop": {"goal"},
    "web": {"goal", "overwrite"},
    "import_asset": {"goal", "path", "overwrite", "collision"},
    "record": {"goal"},
}
SERVERS: list = []
# Canonical payloads of exit 0 rows this process appended after wait() on Godot.
# A line in the ledger file is not a success by itself. There is no key file.
_OBSERVED_SUCCESS: set[str] = set()
_GODOT_VERSIONS: dict[str, str] = {}


class Refused(Exception):
    pass


def _load_safe_write():
    import importlib.util
    path = Path(__file__).resolve().parent / "safe_write.py"
    spec = importlib.util.spec_from_file_location("safe_write", path)
    if spec is None or spec.loader is None:
        raise ImportError("safe_write")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SW = _load_safe_write()


def plan_digest(step: dict) -> str:
    """Hash of this step, printed so the plan text names the plan being approved.

    Hermes does not store it and does not treat it as an always approval.
    A different step is a different digest. The same step text is the same plan.
    """
    blob = json.dumps(step, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "studio:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def redact(text: str) -> str:
    """Hide secret-shaped strings. The key name stays; the value does not."""
    if not text:
        return ""
    text = re.sub(r"([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@", r"\1***:***@", text, flags=re.I)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer ***", text)
    text = re.sub(r"(?i)\b([A-Z0-9_]*TOKEN)\s*=\s*\S+", r"\1=***", text)
    text = re.sub(r"(?i)(--password(?:=|\s+))\S+", r"\1***", text)
    text = re.sub(r"(?i)(Authorization\s*:\s*)\S+", r"\1***", text)
    text = re.sub(r"xox[a-z]-[A-Za-z0-9-]+", "xoxb-***", text)
    text = re.sub(
        r'(?i)("(?:password|passwd|secret|token|access_token|api_key|authorization|refresh_token)"\s*:\s*)"(?:\\.|[^"\\])*"',
        r'\1"***"',
        text,
    )
    return text


def _one_line(text: str, limit: int = 400) -> str:
    cleaned = redact(text or "").replace("\r", " ").replace("\n", " ").replace("\x00", "")
    return cleaned[:limit]


def limit_seconds(goal: str) -> int:
    match = re.search(r"\d+", goal or "")
    if not match:
        return 60
    value = int(match.group(0))
    return value if 1 <= value <= 180 else 60


PARTS = ("fell", "sound", "picked", "door", "timeout_lose", "restart")
CANNOT_TEST = "This sentence cannot be a test."
EXAMPLE_GOAL = (
    "The key is picked and a sound plays and the door opens. "
    "After 60, timeout_lose, then restart."
)
# The part name itself, as a word. floor, seconds, collect and try again are not parts.
PART_PATTERNS = {name: re.compile(r"\b" + name + r"\b", re.I) for name in PARTS}
LEDGER_FIELDS = ("bits", "exit_code", "godot", "scene", "test", "time")
_BITS_TOKEN = re.compile(r"^[a-z_= ]{1,240}$")
_GODOT_TOKEN = re.compile(r"^[0-9A-Za-z.+_-]{1,40}$")
_SCENE_TOKEN = re.compile(r"^(?:[0-9a-fA-F]{64}|missing)$")
_TEST_TOKEN = re.compile(r"^[a-z_+]{1,80}$")
_TIME_TOKEN = re.compile(r"^[0-9T:.+-Z]{1,40}$")
_CHECK_LINE = re.compile(r"^CHECK ([0-9a-f]{64}) (STUDIO_FAIL|STUDIO) ([a-z_= ]+)$")


def matched_parts(goal: str) -> tuple[str, ...]:
    """Parts this package can score. The sentence is not copied into the check."""
    text = goal or ""
    return tuple(name for name in PARTS if PART_PATTERNS[name].search(text))


def require_parts(goal: str) -> tuple[str, ...]:
    parts = matched_parts(goal)
    if not parts:
        raise Refused(CANNOT_TEST)
    return parts


def test_name(parts: tuple[str, ...]) -> str:
    return "+".join(parts)


def cache_dir() -> Path:
    """Pinned export templates live in plugin-data, not a fixed temporary directory."""
    if CACHE_DIR is not None:
        return CACHE_DIR
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        raise Refused("game-editor: HERMES_HOME is unset, so the export template was not cached.")
    path = Path(home) / "plugin-data" / "game-editor" / "export-templates"
    if path.is_symlink():
        raise Refused("game-editor: studio refused a symlink template cache.")
    return path


def ledger_dir() -> Path | None:
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        return None
    path = Path(home) / "plugin-data" / "game-editor"
    if path.is_symlink():
        raise Refused("game-editor: studio refused a symlink ledger directory.")
    return path


def _ledger_path(directory: Path) -> Path:
    return directory / "ledger.jsonl"


def _ledger_text(directory: Path) -> str:
    path = _ledger_path(directory)
    if not path.is_file() or path.is_symlink():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def canonical_payload(row: dict) -> str:
    body = {name: row[name] for name in LEDGER_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_ledger_line(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    if set(row) != set(LEDGER_FIELDS):
        return None
    bits = row.get("bits")
    code = row.get("exit_code")
    godot = row.get("godot")
    scene = row.get("scene")
    name = row.get("test")
    when = row.get("time")
    if not isinstance(bits, str) or not _BITS_TOKEN.fullmatch(bits):
        return None
    if code not in (0, 1) or isinstance(code, bool):
        return None
    if not isinstance(godot, str) or not _GODOT_TOKEN.fullmatch(godot):
        return None
    if not isinstance(scene, str) or not _SCENE_TOKEN.fullmatch(scene):
        return None
    if not isinstance(name, str) or not _TEST_TOKEN.fullmatch(name):
        return None
    if not isinstance(when, str) or not _TIME_TOKEN.fullmatch(when):
        return None
    return row


def read_trusted(directory: Path) -> list[dict]:
    """Failure rows in the file count. An exit 0 counts only after this process waited on Godot."""
    found = []
    for line in _ledger_text(directory).splitlines():
        row = parse_ledger_line(line.strip())
        if row is None:
            continue
        if row["exit_code"] == 0 and canonical_payload(row) not in _OBSERVED_SUCCESS:
            continue
        found.append(row)
    return found


def open_failure(directory: Path | None) -> dict | None:
    """The latest trusted row for a test, when that row is not exit 0."""
    if directory is None:
        return None
    latest: dict[str, dict] = {}
    for row in read_trusted(directory):
        latest[row["test"]] = row
    for row in latest.values():
        if row["exit_code"] != 0:
            return row
    return None


def _append_ledger_row(directory: Path, row: dict, *, success: bool) -> None:
    path = _ledger_path(directory)
    if path.is_symlink() or directory.is_symlink():
        raise Refused("game-editor: studio refused a symlink ledger.")
    directory.mkdir(parents=True, exist_ok=True)
    payload = canonical_payload(row)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, (payload + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    if success:
        _OBSERVED_SUCCESS.add(payload)


def _web_needs_pass() -> str:
    return (
        "web is not served until a Godot test this process waited on exits 0 "
        "and that row is the latest trusted row for its test."
    )


def web_unserved(directory: Path | None) -> str | None:
    """No page until a trusted exit 0 is the latest row. An empty file is not a pass."""
    if directory is None:
        return _web_needs_pass()
    rows = read_trusted(directory)
    if not rows:
        return _web_needs_pass()
    latest: dict[str, dict] = {}
    for row in rows:
        latest[row["test"]] = row
    for row in latest.values():
        if row["exit_code"] != 0:
            return _web_blocked(row)
    return None


def _web_blocked(row: dict) -> str:
    return (
        "STUDIO_FAIL " + row["bits"] + "\n"
        f"exit={row['exit_code']}\n"
        "web is not served until a later exit 0 from the godot process is the latest row for "
        f"test={row['test']}."
    )


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def godot_version(binary: str) -> str:
    """The version token from `godot --version`, not a string the project script prints."""
    cached = _GODOT_VERSIONS.get(binary)
    if cached:
        return cached
    _code, output = _run([binary, "--version"], 20)
    token = "unknown"
    for piece in output.replace(",", " ").split():
        if piece and piece[0].isdigit() and _GODOT_TOKEN.fullmatch(piece):
            token = piece
            break
    _GODOT_VERSIONS[binary] = token
    return token


def scene_hash(root: Path) -> str:
    """Hash the scene and the loop script. A swapped script cannot keep the scene-only hash."""
    chunks: list[bytes] = []
    for name in ("loop.tscn", "loop.gd"):
        path = root / "studio" / name
        if not path.is_file() or path.is_symlink():
            return "missing"
        chunks.append(name.encode("ascii") + b"\0" + path.read_bytes())
    return hashlib.sha256(b"\0".join(chunks)).hexdigest()


def normalized_loop_body(text: str) -> str:
    """The package script, with the goal comment and the second limit folded back."""
    out: list[str] = []
    for line in text.split("\n"):
        if line.startswith("# goal:"):
            out.append("# goal:")
        elif line.startswith("var limit_seconds :="):
            out.append("var limit_seconds := 60.0")
        else:
            out.append(line)
    return "\n".join(out)


def loop_body_sha256() -> str:
    return hashlib.sha256(normalized_loop_body(LOOP_GD).encode("utf-8")).hexdigest()


def package_scene_sha256() -> str:
    return hashlib.sha256(LOOP_TSCN.encode("utf-8")).hexdigest()


def _hex64(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return ""


def _written_path() -> Path | None:
    directory = ledger_dir()
    if directory is None:
        return None
    return directory / "written.json"


def load_written() -> dict[str, str]:
    """sha256 of files this package wrote, recorded at write time. Not a ledger key."""
    path = _written_path()
    if path is None or not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, str] = {}
    for key, value in data.items():
        digest = _hex64(value)
        if isinstance(key, str) and re.fullmatch(r"res://[A-Za-z0-9_./-]+", key) and digest:
            found[key] = digest
    return found


def _remember_written(relative: str, data: bytes) -> None:
    if not isinstance(relative, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative):
        return
    if relative.startswith("/") or ".." in relative.split("/"):
        return
    directory = ledger_dir()
    if directory is None:
        return
    if directory.is_symlink():
        raise Refused("game-editor: studio refused a symlink written-file record.")
    current = load_written()
    current["res://" + relative] = _sha(data)
    path = directory / "written.json"
    temporary = directory / ".written.json.tmp"
    if path.is_symlink() or temporary.is_symlink():
        raise Refused("game-editor: studio refused a symlink written-file record.")
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(current, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def is_package_loop_text(text: str) -> bool:
    return normalized_loop_body(text) == normalized_loop_body(LOOP_GD)


def is_package_loop(root: Path) -> bool:
    path = root / "studio" / "loop.gd"
    if not path.is_file() or path.is_symlink():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return is_package_loop_text(text)


def _bits_from_line(line: str, parts: tuple[str, ...]) -> str:
    raw = ""
    if line.startswith("STUDIO_FAIL "):
        raw = line[len("STUDIO_FAIL "):].strip()
    elif line.startswith("STUDIO "):
        raw = line[len("STUDIO "):].strip()
    if raw and _BITS_TOKEN.fullmatch(raw):
        return raw
    return " ".join(f"{part}=false" for part in parts)


def _parts_all_true(bits: str, parts: tuple[str, ...]) -> bool:
    flags: dict[str, str] = {}
    for piece in bits.split():
        name, sep, value = piece.partition("=")
        if not sep or not name:
            return False
        flags[name] = value
    return all(flags.get(part) == "true" for part in parts)


def latest_row(rows: list[dict], name: str) -> dict | None:
    found = None
    for row in rows:
        if row["test"] == name:
            found = row
    return found


def fail_pass_pair(rows: list[dict]) -> tuple[dict, dict] | None:
    """A non-zero row and a later exit 0 for the same test. Project files are not consulted."""
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["test"], []).append(row)
    best = None
    for group in grouped.values():
        failed = None
        for row in group:
            if row["exit_code"] != 0:
                failed = row
            elif failed is not None:
                best = (failed, row)
                failed = None
    return best


def verdict_line(row: dict) -> str:
    prefix = "STUDIO " if row["exit_code"] == 0 else "STUDIO_FAIL "
    return prefix + row["bits"]


def _check_script(parts: tuple[str, ...], seconds: int) -> str:
    quoted = ", ".join(json.dumps(part) for part in parts)
    written = load_written()
    return (
        CHECK_GD_TEMPLATE
        .replace("__PARTS__", quoted)
        .replace("__TEST_NAME__", test_name(parts))
        .replace("__LOOP_BODY_SHA256__", loop_body_sha256())
        .replace("__EXPECTED_SECONDS__", f"{int(seconds)}.0")
        .replace("__WRITTEN_LOOP__", _hex64(written.get("res://studio/loop.gd", "")))
        .replace("__WRITTEN_SCENE__", _hex64(written.get("res://studio/loop.tscn", "")))
        .replace("__PACKAGE_SCENE__", package_scene_sha256())
    )


def meters_from_goal(goal: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(cm|m)\b", goal or "", re.I)
    if not match:
        return 1.0
    value = float(match.group(1))
    if match.group(2).lower() == "cm":
        value = value / 100.0
    if value < 0.01:
        return 0.01
    if value > 1000.0:
        return 1000.0
    return value


def url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    return host in ALLOWED_HOSTS


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    account = pwd.getpwuid(os.getuid())
    env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
        "HOME": account.pw_dir,
        "TMPDIR": "/tmp",
        "LANG": "C",
        "USER": account.pw_name,
    }
    for key, value in (extra or {}).items():
        if not re.fullmatch(r"[A-Z0-9_]+", key) or "\n" in value or "\x00" in value:
            raise Refused("game-editor: studio refused a child environment value.")
        env[key] = value
    return env


def find_godot(given: str | None = None) -> str | None:
    if given and os.path.isfile(given) and os.access(given, os.X_OK):
        return given
    for candidate in (
        "/opt/homebrew/bin/godot",
        "/usr/local/bin/godot",
        "/Applications/Godot.app/Contents/MacOS/Godot",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    import shutil
    found = shutil.which("godot")
    return found if found else None


def display_present() -> bool:
    """True when a real GUI session exists. A headless movie is not started."""
    try:
        if sys.platform == "darwin":
            proc = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                env=child_env(),
            )
            return proc.returncode == 0
        uid = os.getuid()
        x11 = Path("/tmp/.X11-unix")
        if x11.is_dir() and any(x11.iterdir()):
            return True
        run = Path(f"/run/user/{uid}")
        if run.is_dir() and any(path.name.startswith("wayland-") for path in run.iterdir()):
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False


def _not_recorded() -> str:
    if sys.platform == "darwin":
        return NOT_RECORDED
    return NOT_RECORDED_LINUX


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": redact(text)}], "isError": True}


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": redact(text)}], "isError": False}


def _check_step(step: dict) -> str:
    if not isinstance(step, dict):
        raise Refused("game-editor: studio step must be an object.")
    op = step.get("op")
    if op not in STUDIO_OPS:
        raise Refused(
            "game-editor: studio needs op, one of: " + ", ".join(STUDIO_OPS) + ".")
    extra = sorted(set(step) - {"tool", "op"} - _KEYS[op])
    if extra:
        raise Refused(f"game-editor: studio op {op} does not take {', '.join(extra)}.")
    if step.get("tool") not in (None, "studio"):
        raise Refused("game-editor: studio step tool must be studio.")
    goal = step.get("goal", "")
    if goal is not None and not isinstance(goal, str):
        raise Refused("game-editor: studio goal must be a string.")
    if "overwrite" in step and step.get("overwrite") is not True:
        raise Refused("game-editor: studio overwrite must be true when it is set.")
    if op == "import_asset":
        path = step.get("path")
        if not isinstance(path, str) or not path.strip():
            raise Refused("game-editor: studio op import_asset needs path.")
        collision = step.get("collision", "box")
        if collision not in ("box", "convex"):
            raise Refused("game-editor: studio collision must be box or convex.")
    return op


def _root_path(root: str) -> Path:
    """The project directory. Parent links such as the OS temp directory are resolved.

    The project directory itself must not be a symlink. Writes then go through safe_write,
    which refuses a symlink inside the project.
    """
    if not isinstance(root, str) or not root:
        raise Refused("game-editor: studio needs the open project's folder.")
    path = Path(root)
    if path.is_symlink():
        raise Refused("game-editor: studio refused a symlink project folder.")
    try:
        real = path.resolve()
    except OSError as exc:
        raise Refused("game-editor: studio needs the open project's folder.") from exc
    if not real.is_dir() or real.is_symlink():
        raise Refused("game-editor: studio needs the open project's folder.")
    return real


def _write(root: Path, relative: str, data: bytes) -> None:
    try:
        _SW.write_bytes(root, relative, data, create=True, exclusive=False, cut_hardlink=False)
    except _SW.WriteRefused as exc:
        raise Refused(f"game-editor: refused to write {relative} ({exc.kind}).") from exc
    _remember_written(relative, data)


def _read_text(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file() or path.is_symlink():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _replace_line(relative: str, old: bytes, new: bytes) -> str:
    return (
        f"replaces the existing file {relative} "
        f"(old sha256 {_sha(old)[:16]}, new sha256 {_sha(new)[:16]})"
    )


def _gnum(value: float) -> str:
    if value == 0:
        value = 0.0
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if "." in text else text + ".0"


def mesh_fix(minimum: tuple[float, float, float], maximum: tuple[float, float, float], meters: float) -> dict:
    """Scale the longest axis to meters and move the bottom center to the origin."""
    spans = tuple(maximum[i] - minimum[i] for i in range(3))
    longest = max(spans)
    scale = 1.0 if longest <= 1e-8 else meters / longest
    center_x = (minimum[0] + maximum[0]) / 2.0
    center_z = (minimum[2] + maximum[2]) / 2.0
    origin = (-scale * center_x, -scale * minimum[1], -scale * center_z)
    size = tuple(scale * spans[i] for i in range(3))
    return {"scale": scale, "origin": origin, "size": size}


def parse_glb(data: bytes) -> tuple[dict, bytes]:
    if len(data) < 20 or data[:4] != b"glTF":
        raise Refused("game-editor: the file is not a GLB.")
    total = struct.unpack_from("<I", data, 8)[0]
    offset = 12
    document = None
    blob = b""
    while offset + 8 <= min(len(data), total):
        length, kind = struct.unpack_from("<I4s", data, offset)
        offset += 8
        chunk = data[offset:offset + length]
        offset += length
        if kind == b"JSON":
            document = json.loads(chunk.decode("utf-8"))
        elif kind == b"BIN\x00":
            blob = chunk
    if not isinstance(document, dict):
        raise Refused("game-editor: the GLB has no JSON chunk.")
    return document, blob


def glb_bounds(data: bytes) -> tuple[tuple[float, float, float], tuple[float, float, float], list[tuple[float, float, float]]]:
    document, blob = parse_glb(data)
    accessors = document.get("accessors") or []
    views = document.get("bufferViews") or []
    meshes = document.get("meshes") or []
    indexes: list[int] = []
    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        for prim in mesh.get("primitives") or []:
            if isinstance(prim, dict):
                attr = prim.get("attributes") or {}
                if isinstance(attr, dict) and isinstance(attr.get("POSITION"), int):
                    indexes.append(attr["POSITION"])
    if not indexes:
        raise Refused("game-editor: the GLB has no mesh position data.")
    minimum = [float("inf"), float("inf"), float("inf")]
    maximum = [float("-inf"), float("-inf"), float("-inf")]
    points: list[tuple[float, float, float]] = []
    for index in indexes:
        if index < 0 or index >= len(accessors) or not isinstance(accessors[index], dict):
            continue
        acc = accessors[index]
        if acc.get("type") != "VEC3":
            continue
        found_min = acc.get("min")
        found_max = acc.get("max")
        if (isinstance(found_min, list) and isinstance(found_max, list)
                and len(found_min) >= 3 and len(found_max) >= 3):
            for axis in range(3):
                minimum[axis] = min(minimum[axis], float(found_min[axis]))
                maximum[axis] = max(maximum[axis], float(found_max[axis]))
        count = int(acc.get("count") or 0)
        view_index = acc.get("bufferView")
        if (acc.get("componentType") == 5126 and isinstance(view_index, int)
                and 0 <= view_index < len(views) and isinstance(views[view_index], dict)
                and 0 < count <= 100000):
            view = views[view_index]
            start = int(view.get("byteOffset") or 0) + int(acc.get("byteOffset") or 0)
            stride = int(view.get("byteStride") or 12)
            if stride < 12:
                stride = 12
            for item in range(count):
                at = start + item * stride
                if at + 12 > len(blob):
                    break
                point = struct.unpack_from("<fff", blob, at)
                points.append(point)
                if not (isinstance(found_min, list) and isinstance(found_max, list)):
                    for axis in range(3):
                        minimum[axis] = min(minimum[axis], point[axis])
                        maximum[axis] = max(maximum[axis], point[axis])
    if minimum[0] == float("inf"):
        raise Refused("game-editor: the GLB positions could not be read.")
    return (minimum[0], minimum[1], minimum[2]), (maximum[0], maximum[1], maximum[2]), points


def _project_text(current: str, main_scene: str | None) -> str:
    if not current.strip():
        lines = [
            "config_version=5",
            "",
            "[application]",
            'config/name="StudioLoop"',
            'config/features=PackedStringArray("4.7", "GL Compatibility")',
        ]
        if main_scene:
            lines.append(f'run/main_scene="{main_scene}"')
        lines += [
            "",
            "[display]",
            "window/size/viewport_width=640",
            "window/size/viewport_height=360",
            "",
            "[rendering]",
            'renderer/rendering_method="gl_compatibility"',
            'renderer/rendering_method.mobile="gl_compatibility"',
            "",
        ]
        return "\n".join(lines)
    text = current
    if main_scene:
        if re.search(r'(?m)^run/main_scene="[^"]*"', text):
            text = re.sub(r'(?m)^run/main_scene="[^"]*"', f'run/main_scene="{main_scene}"', text, count=1)
        elif "[application]" in text:
            text = text.replace("[application]\n", f'[application]\nrun/main_scene="{main_scene}"\n', 1)
        else:
            text += f'\n[application]\nrun/main_scene="{main_scene}"\n'
    if "[display]" not in text:
        text += "\n[display]\nwindow/size/viewport_width=640\nwindow/size/viewport_height=360\n"
    if "[rendering]" not in text and "renderer/rendering_method" not in text:
        text += (
            '\n[rendering]\nrenderer/rendering_method="gl_compatibility"\n'
            'renderer/rendering_method.mobile="gl_compatibility"\n'
        )
    if not text.endswith("\n"):
        text += "\n"
    return text


def _disable_shader_cache(project: str) -> str:
    """A stale GLES shader cache for this project name records a flat clear frame."""
    line = "shader_compiler/shader_cache/enabled=false"
    if re.search(r"(?m)^shader_compiler/shader_cache/enabled=", project):
        return re.sub(r"(?m)^shader_compiler/shader_cache/enabled=.*$", line, project)
    if "[rendering]" in project:
        return project.replace("[rendering]\n", "[rendering]\n" + line + "\n", 1)
    return project + "\n[rendering]\n" + line + "\n"


def _loop_script(goal: str) -> str:
    seconds = limit_seconds(goal)
    body = LOOP_GD
    body = body.replace("var limit_seconds := 60.0", f"var limit_seconds := {seconds}.0", 1)
    return body.replace("# goal:", "# goal: " + _one_line(goal), 1)


def _names(root: Path) -> set[str]:
    found: set[str] = set()
    if not root.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [name for name in dirnames if name != ".godot"]
        for name in filenames:
            path = Path(dirpath) / name
            found.add(path.relative_to(root).as_posix())
    return found


def _sweep(root: Path, before: set[str], keep: tuple[str, ...]) -> None:
    for rel in _names(root) - before:
        if rel.startswith(keep) or rel in ("project.godot", "export_presets.cfg"):
            continue
        path = root / rel
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
        except OSError:
            pass


def _run(
    cmd: list[str],
    timeout: int,
    extra: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env(extra),
    )
    try:
        out, _ = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        pid = proc.pid
        proc.kill()
        proc.communicate()
        return 124, f"godot timed out (pid {pid})"
    text = (out or b"").decode("utf-8", "replace")
    code = proc.returncode if proc.returncode is not None else 1
    return code, text


def _foreign_line(output: str) -> str:
    found = ""
    prefix = "unverified script: "
    for line in output.splitlines():
        text = line.strip()
        if text.startswith(prefix) and " " not in text[len(prefix):]:
            found = text
    return found


def _frames_line(output: str) -> str:
    found = ""
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("physics_frames=") and text[len("physics_frames="):].isdigit():
            found = text
    return found


def _observed_verdict(output: str, nonce: str) -> tuple[bool, str] | None:
    """The one line the check printed. A STUDIO line from the loop is not this line."""
    found: list[tuple[bool, str]] = []
    for line in output.splitlines():
        match = _CHECK_LINE.match(line.strip())
        if not match or match.group(1) != nonce:
            continue
        bits = match.group(3).strip()
        if not _BITS_TOKEN.fullmatch(bits):
            continue
        found.append((match.group(2) == "STUDIO", bits))
    if len(found) != 1:
        return None
    return found[0]


def _append_credits(root: Path, line: str) -> None:
    current = _read_text(root, "studio/CREDITS")
    rows = [row for row in current.splitlines() if row.strip()]
    if line not in rows:
        rows.append(line)
    _write(root, "studio/CREDITS", ("\n".join(rows) + "\n").encode("utf-8"))


def _origin_tokens(source: Path) -> tuple[str, str]:
    side = source.with_name(source.stem + ".origin.json")
    try:
        info = side.lstat()
    except OSError:
        return "unknown", "unknown"
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > ORIGIN_MAX:
        return "unknown", "unknown"
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "unknown", "unknown"
    if not isinstance(data, dict):
        return "unknown", "unknown"
    source_name = data.get("source")
    license_name = data.get("license")
    source_token = source_name if isinstance(source_name, str) and TOKEN.match(source_name) else "unknown"
    license_token = license_name if isinstance(license_name, str) and TOKEN.match(license_name) else "unknown"
    return source_token, license_token


def _safe_basename(name: str) -> str:
    base = Path(name).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._")
    if not cleaned:
        cleaned = "asset.bin"
    return cleaned[:80]


def _resolve_import(root: Path, raw: str) -> Path:
    text = raw.strip()
    if text.lower().startswith("res://"):
        rel = text[6:]
        parts = Path(rel).parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise Refused("game-editor: studio refused that import path.")
        path = root.joinpath(*parts)
    else:
        path = Path(text)
        if not path.is_absolute():
            raise Refused("game-editor: studio import path must be res:// or an absolute path.")
    try:
        info = path.lstat()
    except OSError as exc:
        raise Refused(f"game-editor: studio could not read the import ({exc.__class__.__name__}).") from exc
    if stat.S_ISLNK(info.st_mode):
        raise Refused("game-editor: studio refused a symlink.")
    if not stat.S_ISREG(info.st_mode):
        raise Refused("game-editor: studio import path is not a file.")
    if info.st_size > MAX_IMPORT_BYTES:
        raise Refused("game-editor: studio refused an import larger than 64MB.")
    return path


def _credits_line(source_name: str, relative: str, license_name: str) -> str:
    return f"source={source_name} file={relative} license={license_name}"


def _wrapper_scene(glb_rel: str, fix: dict, shape: str, points: list[tuple[float, float, float]]) -> str:
    scale = fix["scale"]
    ox, oy, oz = fix["origin"]
    sx, sy, sz = fix["size"]
    transform = (
        f"Transform3D({_gnum(scale)}, 0, 0, 0, {_gnum(scale)}, 0, 0, 0, {_gnum(scale)}, "
        f"{_gnum(ox)}, {_gnum(oy)}, {_gnum(oz)})"
    )
    if shape == "convex" and len(points) >= 3:
        flat: list[str] = []
        for point in points[:32]:
            moved = (
                scale * point[0] + ox,
                scale * point[1] + oy,
                scale * point[2] + oz,
            )
            flat.extend(_gnum(item) for item in moved)
        shape_text = (
            '[sub_resource type="ConvexPolygonShape3D" id="hit"]\n'
            f"points = PackedVector3Array({', '.join(flat)})\n"
        )
        shape_id = "hit"
        shape_node = (
            '[node name="Shape" type="CollisionShape3D" parent="Body"]\n'
            'shape = SubResource("hit")\n'
        )
    else:
        shape_text = (
            '[sub_resource type="BoxShape3D" id="hit"]\n'
            f"size = Vector3({_gnum(sx)}, {_gnum(sy)}, {_gnum(sz)})\n"
        )
        shape_id = "hit"
        shape_node = (
            '[node name="Shape" type="CollisionShape3D" parent="Body"]\n'
            f"transform = Transform3D(1, 0, 0, 0, 1, 0, 0, 0, 1, 0, {_gnum(sy / 2.0)}, 0)\n"
            'shape = SubResource("hit")\n'
        )
    del shape_id
    return (
        "[gd_scene load_steps=3 format=3]\n\n"
        f'[ext_resource type="PackedScene" path="res://{glb_rel}" id="1"]\n\n'
        f"{shape_text}\n"
        '[node name="Import" type="Node3D"]\n\n'
        '[node name="Mesh" type="Node3D" parent="."]\n'
        f"transform = {transform}\n\n"
        '[node name="Model" parent="Mesh" instance=ExtResource("1")]\n\n'
        '[node name="Body" type="StaticBody3D" parent="."]\n\n'
        f"{shape_node}"
    )


def _cache_file() -> Path:
    return cache_dir() / TEMPLATE_FILE


def template_matches(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        if path.stat().st_size != TEMPLATE_BYTES:
            return False
    except OSError:
        return False
    digest256 = hashlib.sha256()
    digest512 = hashlib.sha512()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > TEMPLATE_BYTES:
                    return False
                digest256.update(chunk)
                digest512.update(chunk)
    except OSError:
        return False
    return total == TEMPLATE_BYTES and digest256.hexdigest() == TEMPLATE_SHA256 and digest512.hexdigest() == TEMPLATE_SHA512


class _PinRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl):
            raise Refused("game-editor: studio refused a template redirect off the pinned host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def acquire_template() -> Path:
    """Return the pinned tpz. Download only when the cache is missing or the wrong size."""
    dest = _cache_file()
    if dest.is_file() and not dest.is_symlink() and dest.stat().st_size == TEMPLATE_BYTES:
        if template_matches(dest):
            return dest
        raise Refused(
            "game-editor: the cached export template does not match the pinned sha256 and sha512. "
            "Nothing was installed.")
    if not url_allowed(TEMPLATE_URL):
        raise Refused("game-editor: the template URL is not on the pinned host.")
    cache_dir().mkdir(parents=True, exist_ok=True)
    part = cache_dir() / "templates.tpz.part"
    if part.is_symlink():
        raise Refused("game-editor: studio refused a symlink in the template cache.")
    if part.exists():
        part.unlink()
    opener = build_opener(_PinRedirect)
    request = Request(TEMPLATE_URL, headers={"User-Agent": "game-editor-template-pin"})
    digest256 = hashlib.sha256()
    digest512 = hashlib.sha512()
    total = 0
    try:
        with opener.open(request, timeout=180) as response:
            final = response.geturl()
            if not url_allowed(final):
                raise Refused("game-editor: studio refused the template host.")
            length = response.headers.get("Content-Length")
            if length is not None and length != str(TEMPLATE_BYTES):
                raise Refused("game-editor: the template size does not match the pin. Nothing was installed.")
            with part.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > TEMPLATE_BYTES:
                        raise Refused("game-editor: the template is larger than the pin. Nothing was installed.")
                    digest256.update(chunk)
                    digest512.update(chunk)
                    handle.write(chunk)
    except Refused:
        if part.exists() and not part.is_symlink():
            part.unlink()
        raise
    except Exception as exc:
        if part.exists() and not part.is_symlink():
            part.unlink()
        raise Refused(f"game-editor: the template download failed ({exc.__class__.__name__}). Nothing was installed.") from exc
    if (total != TEMPLATE_BYTES or digest256.hexdigest() != TEMPLATE_SHA256
            or digest512.hexdigest() != TEMPLATE_SHA512):
        if part.exists() and not part.is_symlink():
            part.unlink()
        raise Refused("game-editor: the downloaded template does not match the pin. Nothing was installed.")
    os.replace(part, dest)
    if not template_matches(dest):
        raise Refused("game-editor: the stored template does not match the pin. Nothing was installed.")
    return dest


def _member_safe(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    parts = Path(name).parts
    return bool(parts) and not any(part in ("", ".", "..") for part in parts)


def extract_templates(tpz: Path) -> tuple[Path, Path, bool]:
    """Extract the web zips. Prefer the nothreads pair. Return (debug, release, nothreads)."""
    pairs = (
        (True, "templates/web_nothreads_debug.zip", "templates/web_nothreads_release.zip"),
        (False, "templates/web_debug.zip", "templates/web_release.zip"),
    )
    with zipfile.ZipFile(tpz) as archive:
        names = archive.namelist()
        for name in names:
            checked = name[:-1] if name.endswith("/") else name
            if not _member_safe(checked):
                raise Refused("game-editor: studio refused a template member.")
        name_set = set(names)
        if "templates/version.txt" not in name_set:
            raise Refused("game-editor: the template pack has no version.txt.")
        version = archive.read("templates/version.txt")
        if b"4.7.2" not in version or len(version) > 64:
            raise Refused("game-editor: the template pack version is not 4.7.2.")
        chosen = None
        for nothreads, debug_name, release_name in pairs:
            if debug_name in name_set and release_name in name_set:
                chosen = (nothreads, debug_name, release_name)
                break
        if chosen is None:
            raise Refused("game-editor: the template pack has no web zip.")
        nothreads, debug_name, release_name = chosen
        out = (cache_dir() / TEMPLATE_VERSION).resolve()
        out.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name in (debug_name, release_name):
            info = archive.getinfo(name)
            if info.file_size > 80_000_000:
                raise Refused("game-editor: a template member is too large.")
            _write(out, Path(name).name, archive.read(name))
            written.append(out / Path(name).name)
    return written[0], written[1], nothreads


def _preset_block(index: int, debug_zip: Path, release_zip: Path, nothreads: bool) -> str:
    options = [
        f'custom_template/debug="{debug_zip}"',
        f'custom_template/release="{release_zip}"',
    ]
    if nothreads:
        options.append("variant/thread_support=false")
    lines = [
        f"[preset.{index}]",
        "",
        'name="Web"',
        'platform="Web"',
        "runnable=true",
        "dedicated_server=false",
        'custom_features=""',
        'export_filter="all_resources"',
        'include_filter=""',
        'exclude_filter=""',
        'export_path=""',
        "script_export_mode=2",
        "",
        f"[preset.{index}.options]",
        "",
        *options,
        "",
    ]
    return "\n".join(lines)


def _ensure_web_preset(current: str, debug_zip: Path, release_zip: Path, nothreads: bool) -> str:
    if not current.strip():
        return _preset_block(0, debug_zip, release_zip, nothreads)
    blocks = re.split(r"(?=^\[preset\.\d+\]\s*$)", current, flags=re.M)
    head = blocks[0]
    rest = blocks[1:]
    web_at = None
    for index, block in enumerate(rest):
        options_at = block.find(".options]")
        body = block if options_at == -1 else block[:options_at]
        if re.search(r'(?m)^platform="Web"', body):
            web_at = index
            break
    addition = _preset_block(len(rest) if web_at is None else web_at, debug_zip, release_zip, nothreads)
    if web_at is None:
        text = current
        if text and not text.endswith("\n"):
            text += "\n"
        return text + addition
    rest[web_at] = addition
    return head + "".join(rest)


def _serve(directory: Path) -> str:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, fmt, *args):
            return

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = httpd.server_address[:2]
    if host != "127.0.0.1":
        httpd.server_close()
        raise Refused("game-editor: studio refused to serve on a non-loopback address.")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    SERVERS.append(httpd)
    return f"http://{host}:{port}/index.html"


def stop_servers() -> None:
    while SERVERS:
        httpd = SERVERS.pop()
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass


atexit.register(stop_servers)


def tree_rels(root: str) -> set[str]:
    base = Path(root) if isinstance(root, str) else None
    found: set[str] = set()
    if base is None or not base.is_dir():
        return found
    for prefix in ("studio", "build"):
        folder = base / prefix
        if not folder.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = [name for name in dirnames if name != ".godot"]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                found.add(path.relative_to(base).as_posix())
    for name in ("project.godot", "export_presets.cfg"):
        path = base / name
        if path.is_file() and not path.is_symlink():
            found.add(name)
    return found


def side_res(step: dict) -> set[str]:
    op = step.get("op") if isinstance(step, dict) else ""
    out: set[str] = set()
    if op == "test":
        out.update({"res://studio/check.gd", "res://studio/check.tscn"})
    elif op == "loop":
        out.update({
            "res://studio/loop.gd", "res://studio/loop.tscn", "res://studio/CREDITS", "res://project.godot",
        })
    elif op == "web":
        out.update({"res://project.godot", "res://export_presets.cfg", "res://build/web/index.html"})
    elif op == "import_asset":
        out.add("res://studio/CREDITS")
    elif op == "record":
        out.update({"res://studio/record.gd", "res://studio/record.tscn", "res://studio/take.avi"})
    return out


def _losses_for(root: Path, files: dict[str, bytes]) -> list[str]:
    losses = []
    for relative, data in files.items():
        path = root / relative
        if path.is_symlink():
            raise Refused(f"game-editor: studio refused a symlink at {relative}.")
        if path.is_file():
            try:
                old = path.read_bytes()
            except OSError as exc:
                raise Refused(f"game-editor: studio could not read {relative}.") from exc
            if old != data:
                losses.append(_replace_line(relative, old, data))
    return losses


def _cache_will_download() -> bool:
    dest = _cache_file()
    try:
        return not (dest.is_file() and not dest.is_symlink() and dest.stat().st_size == TEMPLATE_BYTES)
    except OSError:
        return True


def plan_block(step: dict, index: int, root: str) -> tuple[str, list[str]]:
    del index
    op = _check_step(step)
    base = _root_path(root)
    goal = step.get("goal") or ""
    losses: list[str] = []
    lines = [f"Godot: studio op {op}", f"plan_digest: {plan_digest(step)}"]
    if op == "test":
        parts = require_parts(goal)
        files = {
            "studio/check.gd": _check_script(parts, limit_seconds(goal)).encode("utf-8"),
            "studio/check.tscn": CHECK_TSCN.encode("utf-8"),
        }
        losses.extend(_losses_for(base, files))
        losses.insert(0, "runs code written in this plan: godot --headless --fixed-fps 60 runs res://studio/check.gd")
        lines.append(
            f"writes res://studio/check.gd for {', '.join(parts)} and runs it. It does not write the loop."
        )
        lines.append("A missing loop prints STUDIO_FAIL and exits non-zero.")
        lines.append(
            "A STUDIO line printed by the loop is not a pass. "
            "The check prints the observation line after it watches the loop."
        )
        lines.append(
            "It scores the key becoming a child of the player, not flags the loop assigns. "
            "The running root has to be the loop.gd this package wrote. "
            "Keeping that file and attaching another script is not a pass. "
            "A child script, an inherited scene, an autoload, or any other resource this package did not write "
            "fails as unverified script: and the path, before that script runs. "
            "timeout_lose counts physics frames only: the sentence's seconds times 60, so 60 seconds is 3600 frames, "
            "with Engine.time_scale held at 1. The Limit timeout signal is not a pass. "
            "Stopping Limit before those frames is not exit 0. "
            "restart is read after those frames: the key is back on the floor, the door collision is back, "
            "and the player is back at the start position with velocity near zero. "
            "The scene hash covers loop.tscn and loop.gd."
        )
        lines.append(
            "The run uses a project file with no autoloads, then puts an existing project.godot back."
        )
        lines.append(
            "After that process exits, this bridge appends the ledger row. "
            "Exit 0 is recorded only when that process returned 0 and the check's observation line is a pass. "
            "The Godot process does not write the row and is not given a ledger key."
        )
    elif op == "loop":
        parts = require_parts(goal)
        files = {
            "studio/loop.gd": _loop_script(goal).encode("utf-8"),
            "studio/loop.tscn": LOOP_TSCN.encode("utf-8"),
        }
        project = _project_text(_read_text(base, "project.godot"), "res://studio/loop.tscn").encode("utf-8")
        files["project.godot"] = project
        credit = _credits_line("game-editor", "studio/loop.gd", "MIT").encode("utf-8")
        current = _read_text(base, "studio/CREDITS")
        rows = [row for row in current.splitlines() if row.strip()]
        line = credit.decode("utf-8")
        if line not in rows:
            rows.append(line)
        files["studio/CREDITS"] = ("\n".join(rows) + "\n").encode("utf-8")
        losses.extend(_losses_for(base, files))
        lines.append(
            f"writes the {limit_seconds(goal)} second loop for {', '.join(parts)}. "
            "It does not run it. The check scores only those parts."
        )
        lines.append("CREDITS " + _credits_line("game-editor", "studio/loop.gd", "MIT"))
    elif op == "web":
        blocked = web_unserved(ledger_dir())
        if blocked is not None:
            raise Refused(blocked)
        if not (base / "studio" / "loop.tscn").is_file():
            raise Refused("game-editor: studio op web needs res://studio/loop.tscn. Plan studio op loop first.")
        web = base / "build" / "web"
        if web.is_dir() and any(web.iterdir()) and step.get("overwrite") is not True:
            raise Refused("game-editor: build/web already has files. Pass overwrite true. Nothing was exported.")
        project = _project_text(_read_text(base, "project.godot"), "res://studio/loop.tscn")
        losses.extend(_losses_for(base, {"project.godot": project.encode("utf-8")}))
        if web.is_dir() and any(path.is_file() for path in web.rglob("*")):
            losses.append("replaces existing build files under build/web")
        losses.insert(0, "runs code written in this plan: godot --headless --export-release writes build/web/index.html")
        if _cache_will_download():
            losses.append(
                "writes outside the project: downloads the pinned Godot 4.7.2 export template "
                "only after sha256 and sha512 match"
            )
            lines.append("downloads the pinned template into the cache on this computer, then exports.")
        else:
            lines.append("verifies the cached export template against the pinned sha256 and sha512, then exports.")
        lines.append("stops the previous 127.0.0.1 page before this export.")
        lines.append("template cache: plugin-data/game-editor/export-templates")
        lines.append("serves on 127.0.0.1 only. It does not upload.")
        lines.append("does not serve until a trusted exit 0 is the latest row for a test.")
    elif op == "import_asset":
        source = _resolve_import(base, step["path"])
        suffix = source.suffix.lower()
        if suffix == ".blend":
            raise Refused("game-editor: export a GLB; do not run Blender.")
        if suffix == ".mp4":
            raise Refused("game-editor: that file is not a mesh.")
        if suffix not in {".glb", ".wav", ".ogg", ".mp3"}:
            raise Refused("game-editor: studio imports .glb, .wav, .ogg or .mp3.")
        name = _safe_basename(source.name)
        relative = f"studio/imports/{name}"
        dest = base / relative
        if dest.is_file() and step.get("overwrite") is not True:
            raise Refused(f"game-editor: {relative} already exists. Pass overwrite true.")
        if dest.is_file():
            new_hash = _sha(source.read_bytes())
            losses.append(
                f"replaces the existing file {relative} "
                f"(old sha256 {_sha(dest.read_bytes())[:16]}, new sha256 {new_hash[:16]})"
            )
        lines.append(f"copies into res://{relative}. It does not run Godot and it does not upload.")
        if suffix == ".glb":
            lines.append("fixes scale, bottom-center pivot and a box or convex collision.")
        else:
            lines.append("scale, pivot and collision do not apply to audio.")
    elif op == "record":
        if not display_present():
            lines.append(_not_recorded())
            lines.append("Movie Maker is not started.")
        else:
            directory = ledger_dir()
            pair = fail_pass_pair(read_trusted(directory)) if directory is not None else None
            if pair is None:
                raise Refused(
                    "game-editor: studio op record needs a failing ledger row and a later exit 0 "
                    "for the same test. Project text files are not a recording."
                )
            losses.append("runs code written in this plan: Movie Maker writes res://studio/take.avi")
            lines.append(
                "writes res://studio/take.avi. The picture shows the loop playing under the ledger lines "
                "from the failed row and the later exit 0. Project text files are not read."
            )
    else:
        raise Refused("game-editor: studio op is not known.")
    return "\n".join(lines), losses


def guide(op: str | None) -> dict:
    if op is None:
        lines = ["studio ops (each is its own plan_change step, not an MCP tool):"]
        lines.append("- test: write the check and run it headless. Does not write the loop.")
        lines.append("- loop: write the 60 second loop. Does not run it.")
        lines.append("- web: export Web to build/web and serve it on 127.0.0.1. Does not upload.")
        lines.append("- import_asset: copy a GLB or audio file, fix a mesh, append one CREDITS line.")
        lines.append("- record: Movie Maker when a display exists, otherwise not recorded.")
        lines.append(
            "example step: " + json.dumps({"tool": "studio", "op": "test", "goal": EXAMPLE_GOAL})
        )
        lines.append("A sentence that names none of those parts as a whole word is not a test.")
        return _ok("\n".join(lines))
    if op not in STUDIO_OPS:
        return _err(f"game-editor: studio has no op {op!r}; ops: {', '.join(STUDIO_OPS)}.")
    example = {"tool": "studio", "op": op, "goal": EXAMPLE_GOAL}
    if op == "import_asset":
        example["path"] = "res://assets/hero.glb"
    if op == "web":
        example.pop("goal", None)
    return _ok(
        f"studio op {op}\n"
        "Run test, then loop, then test again, then web, as four plans. "
        "apply_plan stops at the first error, so the failing test must not share a plan with loop.\n"
        "example step: " + json.dumps(example)
    )


def _write_files(root: Path, files: dict[str, bytes]) -> None:
    for relative, data in files.items():
        _write(root, relative, data)


def _execute_test(step: dict, root: Path, godot: str) -> dict:
    """Run the packaged check. This process appends the one ledger row after wait()."""
    parts = require_parts(step.get("goal") or "")
    name = test_name(parts)
    # The nonce is not written into the project and is not an environment variable.
    # The check reads it from stdin before it adds the loop. A loop print cannot carry it.
    nonce = secrets.token_hex(32)
    before_project = _read_text(root, "project.godot")
    had_project = (root / "project.godot").is_file() and not (root / "project.godot").is_symlink()
    files = {
        "studio/check.gd": _check_script(parts, limit_seconds(step.get("goal") or "")).encode("utf-8"),
        "studio/check.tscn": CHECK_TSCN.encode("utf-8"),
        # The running project has no autoloads. The check reads this copy and counts them.
        "studio/_scan_project.godot": before_project.encode("utf-8"),
        "project.godot": _project_text("", "res://studio/check.tscn").encode("utf-8"),
    }
    before = _names(root)
    directory = ledger_dir()
    before_ledger = _ledger_text(directory) if directory is not None else None
    _write_files(root, files)
    try:
        code, output = _run(
            [godot, "--headless", "--fixed-fps", "60", "--path", str(root), "res://studio/check.tscn"],
            150,
            stdin=(nonce + "\n").encode("ascii"),
        )
    finally:
        scan = root / "studio" / "_scan_project.godot"
        if scan.is_file() and not scan.is_symlink():
            scan.unlink()
        if had_project:
            _write(root, "project.godot", before_project.encode("utf-8"))
        else:
            _write(root, "project.godot", _project_text("", None).encode("utf-8"))
    _sweep(root, before, ("studio/",))
    observed = _observed_verdict(output, nonce)
    foreign = _foreign_line(output)
    frames = _frames_line(output)
    if observed is None:
        passed_line = False
        bits = " ".join(f"{part}=false" for part in parts)
    else:
        passed_line, bits = observed
    # A replaced loop.gd is not a pass, even when Godot was talked into exit 0.
    if not is_package_loop(root) or foreign:
        passed_line = False
        bits = " ".join(f"{part}=false" for part in parts)
    if directory is None:
        shown = ("STUDIO " if passed_line else "STUDIO_FAIL ") + bits
        reply = f"{shown}\nexit={code if code else 1}\nledger: HERMES_HOME is unset\nplan_digest: {plan_digest(step)}"
        if foreign:
            reply = foreign + "\n" + reply
        return _err(reply)
    tampered = _ledger_text(directory) != before_ledger
    passed = code == 0 and passed_line and not tampered and not foreign and _parts_all_true(bits, parts)
    if tampered:
        note = "ledger: changed outside the godot process"
    else:
        row = {
            "bits": bits,
            "exit_code": 0 if passed else 1,
            "godot": godot_version(godot),
            "scene": scene_hash(root),
            "test": name,
            "time": _utc_stamp(),
        }
        _append_ledger_row(directory, row, success=passed)
        note = (
            f"ledger: godot={row['godot']} test={row['test']} "
            f"scene={row['scene']} exit_code={row['exit_code']} time={row['time']}"
        )
    if passed:
        reply = f"STUDIO {bits}\nexit=0\n{note}\nplan_digest: {plan_digest(step)}"
        if frames:
            reply += "\n" + frames
        return _ok(reply)
    shown_exit = code if code not in (0, None) else 1
    reply = f"STUDIO_FAIL {bits}\nexit={shown_exit}\n{note}\nplan_digest: {plan_digest(step)}"
    if foreign:
        reply = foreign + "\n" + reply
    if frames:
        reply += "\n" + frames
    return _err(reply)


def _execute_loop(step: dict, root: Path) -> dict:
    goal = step.get("goal") or ""
    parts = require_parts(goal)
    files = {
        "studio/loop.gd": _loop_script(goal).encode("utf-8"),
        "studio/loop.tscn": LOOP_TSCN.encode("utf-8"),
        "project.godot": _project_text(_read_text(root, "project.godot"), "res://studio/loop.tscn").encode("utf-8"),
    }
    _write_files(root, files)
    line = _credits_line("game-editor", "studio/loop.gd", "MIT")
    _append_credits(root, line)
    return _ok(
        "wrote res://studio/loop.tscn\n"
        f"limit_seconds={limit_seconds(goal)}\n"
        f"parts: {', '.join(parts)}\n"
        f"goal: {_one_line(goal)}\n"
        f"CREDITS {line}\n"
        f"plan_digest: {plan_digest(step)}"
    )


def _execute_import(step: dict, root: Path) -> dict:
    source = _resolve_import(root, step["path"])
    suffix = source.suffix.lower()
    if suffix == ".blend":
        raise Refused("game-editor: export a GLB; do not run Blender.")
    if suffix == ".mp4":
        raise Refused("game-editor: that file is not a mesh.")
    if suffix not in {".glb", ".wav", ".ogg", ".mp3"}:
        raise Refused("game-editor: studio imports .glb, .wav, .ogg or .mp3.")
    data = source.read_bytes()
    if len(data) > MAX_IMPORT_BYTES:
        raise Refused("game-editor: studio refused an import larger than 64MB.")
    name = _safe_basename(source.name)
    relative = f"studio/imports/{name}"
    dest = root / relative
    if dest.is_file() and step.get("overwrite") is not True:
        raise Refused(f"game-editor: {relative} already exists. Pass overwrite true.")
    source_name, license_name = _origin_tokens(source)
    _write(root, relative, data)
    if suffix == ".glb":
        minimum, maximum, points = glb_bounds(data)
        fix = mesh_fix(minimum, maximum, meters_from_goal(step.get("goal") or ""))
        count = len(points)
        asked = step.get("collision")
        if asked == "convex" and 3 <= count <= 48:
            shape = "convex"
            note = f"collision=convex positions={min(count, 32)}"
        elif asked == "convex":
            shape = "box"
            note = "collision=box because the mesh has more than 48 positions"
        elif asked is None and 3 <= count <= 32:
            shape = "convex"
            note = f"collision=convex positions={min(count, 32)}"
        else:
            shape = "box"
            note = "collision=box"
        if shape == "convex" and len(points) < 3:
            shape = "box"
            note = "collision=box"
        wrapper = _wrapper_scene(relative, fix, shape, points)
        _write(root, f"studio/imports/{Path(name).stem}.tscn", wrapper.encode("utf-8"))
        detail = (
            f"scale={_gnum(fix['scale'])} pivot=bottom-center "
            f"origin={_gnum(fix['origin'][0])},{_gnum(fix['origin'][1])},{_gnum(fix['origin'][2])} {note}"
        )
    else:
        detail = "scale, pivot and collision do not apply to audio"
    line = _credits_line(source_name, relative, license_name)
    _append_credits(root, line)
    return _ok(f"imported res://{relative}\n{detail}\nCREDITS {line}\nplan_digest: {plan_digest(step)}")


def _execute_web(step: dict, root: Path, godot: str) -> dict:
    stop_servers()
    blocked = web_unserved(ledger_dir())
    if blocked is not None:
        return _err(blocked)
    if not (root / "studio" / "loop.tscn").is_file():
        raise Refused("game-editor: studio op web needs res://studio/loop.tscn. Plan studio op loop first.")
    web = root / "build" / "web"
    if web.is_dir() and any(web.iterdir()) and step.get("overwrite") is not True:
        raise Refused("game-editor: build/web already has files. Pass overwrite true.")
    tpz = acquire_template()
    debug_zip, release_zip, nothreads = extract_templates(tpz)
    project = _project_text(_read_text(root, "project.godot"), "res://studio/loop.tscn")
    preset = _ensure_web_preset(_read_text(root, "export_presets.cfg"), debug_zip, release_zip, nothreads)
    before = _names(root)
    _write(root, "project.godot", project.encode("utf-8"))
    _write(root, "export_presets.cfg", preset.encode("utf-8"))
    (root / "build" / "web").mkdir(parents=True, exist_ok=True)
    output = root / "build" / "web" / "index.html"
    code, output_text = _run(
        [godot, "--headless", "--path", str(root), "--export-release", "Web", str(output)],
        180,
    )
    _write(root, "project.godot", project.encode("utf-8"))
    _write(root, "export_presets.cfg", preset.encode("utf-8"))
    _sweep(root, before, ("studio/", "build/"))
    if code != 0 or not output.is_file():
        tail = "\n".join(output_text.splitlines()[-12:])
        return _err(f"game-editor: web export failed exit={code}\n{tail}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(web.rglob("*")):
            if path.is_file() and not path.is_symlink():
                archive.write(path, path.relative_to(web).as_posix())
    _write(root, "build/web.zip", buffer.getvalue())
    url = _serve(web)
    return _ok(
        f"web index.html and build/web.zip are ready\nurl: {url}\n"
        "upload it yourself if you want it public. This step does not upload.\n"
        f"plan_digest: {plan_digest(step)}"
    )


def _execute_record(step: dict, root: Path, godot: str) -> dict:
    if not display_present():
        return _ok(_not_recorded() + f"\nplan_digest: {plan_digest(step)}")
    directory = ledger_dir()
    pair = fail_pass_pair(read_trusted(directory)) if directory is not None else None
    if pair is None:
        raise Refused(
            "game-editor: studio op record needs a failing ledger row and a later exit 0 "
            "for the same test. Project text files are not a recording."
        )
    fail_text = verdict_line(pair[0])
    pass_text = verdict_line(pair[1])
    files = {
        "studio/record.gd": RECORD_GD.encode("utf-8"),
        "studio/record.tscn": RECORD_TSCN.encode("utf-8"),
    }
    before_project = _read_text(root, "project.godot")
    project = _disable_shader_cache(_project_text(before_project, "res://studio/record.tscn"))
    files["project.godot"] = project.encode("utf-8")
    before = _names(root)
    _write_files(root, files)
    movie = root / "studio" / "take.avi"
    if movie.is_symlink():
        raise Refused("game-editor: studio refused a symlink at studio/take.avi.")
    if movie.exists():
        movie.unlink()
    code, output = _run(
        [godot, "--path", str(root), "--write-movie", str(movie), "res://studio/record.tscn"],
        60,
        {"GAME_EDITOR_FAIL_LINE": fail_text, "GAME_EDITOR_PASS_LINE": pass_text},
    )
    # Put the playable loop back as the main scene when it exists.
    restore_main = "res://studio/loop.tscn" if (root / "studio" / "loop.tscn").is_file() else None
    _write(root, "project.godot", _project_text(before_project, restore_main).encode("utf-8"))
    _sweep(root, before, ("studio/",))
    if code != 0 or not movie.is_file() or movie.stat().st_size <= 0:
        if movie.is_file() and not movie.is_symlink():
            movie.unlink()
        crash = ""
        if code in (134, 139, -11) or "SIGSEGV" in output or "signal 11" in output:
            crash = " Headless --write-movie on Godot 4.7.2 exits with signal 11 (SIGSEGV in texture_2d_get, parameter t is null)."
        return _ok(
            "not recorded: Movie Maker did not write a movie."
            + crash
            + " A still image was not saved in its place.\n"
            + f"plan_digest: {plan_digest(step)}"
        )
    return _ok(
        "recorded res://studio/take.avi\n"
        "the picture shows the loop under these ledger lines:\n"
        f"{fail_text}\n{pass_text}\n"
        f"plan_digest: {plan_digest(step)}"
    )


def execute(step: dict, root: str, godot: str | None) -> dict:
    try:
        op = _check_step(step)
        base = _root_path(root)
        if op in ("test", "loop"):
            require_parts(step.get("goal") or "")
        if op == "loop":
            return _execute_loop(step, base)
        if op == "import_asset":
            return _execute_import(step, base)
        binary = find_godot(godot)
        if not binary:
            return _err("game-editor: studio could not find the Godot binary. Nothing was run.")
        if op == "test":
            return _execute_test(step, base, binary)
        if op == "web":
            return _execute_web(step, base, binary)
        if op == "record":
            return _execute_record(step, base, binary)
        return _err("game-editor: studio op is not known.")
    except Refused as exc:
        return _err(str(exc))
    except _SW.WriteRefused as exc:
        return _err(f"game-editor: refused to write ({exc.kind}).")


LOOP_GD = """extends Node2D
# goal:

signal restarted

var picked := false
var door_open := false
var won := false
var timed_out := false
var test_axis := Vector2.ZERO
var limit_seconds := 60.0
var _beep_left := 0

func _ready() -> void:
    $Limit.wait_time = limit_seconds
    $Limit.one_shot = true
    $Limit.start()

func _physics_process(_delta: float) -> void:
    var axis := test_axis
    if axis == Vector2.ZERO:
        axis = Input.get_vector("ui_left", "ui_right", "ui_up", "ui_down")
    if axis.length() > 1.0:
        axis = axis.normalized()
    $Player.velocity = axis * 160.0
    $Player.move_and_slide()
    _try_pickup()
    _try_exit()
    var left := int($Limit.time_left)
    $Hud.text = "exit in %s" % left

func _try_pickup() -> void:
    var key := get_node_or_null("Key") as RigidBody2D
    if key == null:
        key = $Player.get_node_or_null("Key") as RigidBody2D
    if picked or key == null or key.get_parent() != self:
        return
    if key.position.y < $Floor.position.y - 48.0:
        return
    if $Player.global_position.distance_to(key.global_position) > 36.0:
        return
    picked = true
    key.freeze = true
    key.linear_velocity = Vector2.ZERO
    key.reparent($Player)
    key.position = Vector2(0, -18)
    _play_beep()
    _open_door()

func _try_exit() -> void:
    if won or not picked or not door_open:
        return
    if $Player.global_position.distance_to($Exit.global_position) > 28.0:
        return
    won = true
    $Limit.stop()
    $Hud.text = "out"

func _process(_delta: float) -> void:
    if _beep_left > 0 and $Beep.playing:
        var playback = $Beep.get_stream_playback()
        if playback:
            for _i in 32:
                playback.push_frame(Vector2(0.2, 0.2))
        _beep_left -= 1

func _play_beep() -> void:
    var gen := AudioStreamGenerator.new()
    gen.mix_rate = 22050.0
    gen.buffer_length = 0.4
    $Beep.stream = gen
    $Beep.play()
    _beep_left = 30

func _open_door() -> void:
    door_open = true
    $Door/Shape.disabled = true
    $Door.position.y -= 90.0

func _on_limit_timeout() -> void:
    if won:
        return
    timed_out = true
    restarted.emit()
    if get_tree().current_scene == self:
        get_tree().reload_current_scene()
    else:
        _reset_round()

func _reset_round() -> void:
    picked = false
    door_open = false
    won = false
    timed_out = false
    test_axis = Vector2.ZERO
    var key := get_node_or_null("Key") as RigidBody2D
    if key == null:
        key = $Player.get_node_or_null("Key") as RigidBody2D
    if key != null and key.get_parent() != self:
        key.reparent(self)
    if key != null:
        key.freeze = false
        key.position = Vector2(0, 40)
        key.linear_velocity = Vector2.ZERO
    $Player.position = Vector2(-180, 168)
    $Player.velocity = Vector2.ZERO
    $Door.position = Vector2(150, 168)
    $Door/Shape.disabled = false
    $Limit.wait_time = limit_seconds
    $Limit.start()
"""

LOOP_TSCN = """[gd_scene load_steps=6 format=3]

[ext_resource type="Script" path="res://studio/loop.gd" id="1"]

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

[node name="FloorView" type="ColorRect" parent="Floor"]
offset_left = -260.0
offset_top = -20.0
offset_right = 260.0
offset_bottom = 20.0
color = Color(0.35, 0.28, 0.2, 1)

[node name="Key" type="RigidBody2D" parent="."]
position = Vector2(0, 40)
collision_layer = 2
collision_mask = 1
lock_rotation = true

[node name="Shape" type="CollisionShape2D" parent="Key"]
shape = SubResource("key")

[node name="View" type="ColorRect" parent="Key"]
offset_left = -8.0
offset_top = -8.0
offset_right = 8.0
offset_bottom = 8.0
color = Color(0.95, 0.8, 0.15, 1)

[node name="Player" type="CharacterBody2D" parent="."]
position = Vector2(-180, 168)

[node name="Shape" type="CollisionShape2D" parent="Player"]
shape = SubResource("body")

[node name="View" type="ColorRect" parent="Player"]
offset_left = -9.0
offset_top = -14.0
offset_right = 9.0
offset_bottom = 14.0
color = Color(0.2, 0.55, 0.9, 1)

[node name="Door" type="StaticBody2D" parent="."]
position = Vector2(150, 168)

[node name="Shape" type="CollisionShape2D" parent="Door"]
shape = SubResource("door")

[node name="View" type="ColorRect" parent="Door"]
offset_left = -8.0
offset_top = -32.0
offset_right = 8.0
offset_bottom = 32.0
color = Color(0.45, 0.28, 0.12, 1)

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

[connection signal="timeout" from="Limit" to="." method="_on_limit_timeout"]
"""

CHECK_GD_TEMPLATE = """extends Node

var required := PackedStringArray([__PARTS__])
var test_name := "__TEST_NAME__"
var expected_loop := "__LOOP_BODY_SHA256__"
var expected_seconds := __EXPECTED_SECONDS__
var written_loop := "__WRITTEN_LOOP__"
var written_scene := "__WRITTEN_SCENE__"
var package_scene := "__PACKAGE_SCENE__"
var foreign := ""
var _emit: Callable
var _done := false

func _ready() -> void:
    Engine.time_scale = 1.0
    Engine.physics_ticks_per_second = 60
    var secret := OS.read_string_from_stdin(80).strip_edges()
    if secret.length() != 64:
        get_tree().quit(1)
        return
    _emit = _bind(secret, self)
    if _loop_body_sha() != expected_loop:
        if _emit.call("reject"):
            _seal()
        return
    if not _attach_loop():
        if foreign != "":
            print("unverified script: " + foreign)
        if _emit.call("reject"):
            _seal()
        return

func _process(_delta: float) -> void:
    if _done or not _emit.is_valid():
        return
    if _emit.call("tick"):
        _seal()

func _on_limit_timeout() -> void:
    if _done or not _emit.is_valid():
        return
    if _emit.call("timeout"):
        _seal()

func _exit_tree() -> void:
    _seal()

func _seal() -> void:
    if _done:
        return
    _done = true
    Engine.time_scale = 1.0
    if _emit.is_valid():
        _emit.call("finish")
    else:
        get_tree().quit(1)

func _sha_text(text: String) -> String:
    var lines := text.split("\\n")
    var out: PackedStringArray = []
    for line in lines:
        if line.begins_with("# goal:"):
            out.append("# goal:")
        elif line.begins_with("var limit_seconds :="):
            out.append("var limit_seconds := 60.0")
        else:
            out.append(line)
    var ctx := HashingContext.new()
    ctx.start(HashingContext.HASH_SHA256)
    ctx.update("\\n".join(out).to_utf8_buffer())
    return ctx.finish().hex_encode().to_lower()

func _loop_body_sha() -> String:
    if not FileAccess.file_exists("res://studio/loop.gd"):
        return ""
    return _sha_text(FileAccess.get_file_as_string("res://studio/loop.gd"))

func _packed_matches(packed: PackedScene) -> bool:
    if packed == null:
        return false
    var state := packed.get_state()
    if state == null or state.get_node_count() < 1:
        return false
    if str(state.get_node_path(0)) != ".":
        return false
    var script = null
    for i in state.get_node_property_count(0):
        if str(state.get_node_property_name(0, i)) == "script":
            script = state.get_node_property_value(0, i)
            break
    if script == null:
        return false
    var path := str(script.resource_path)
    var source := str(script.source_code)
    if path != "res://studio/loop.gd":
        return false
    return _sha_text(source) == expected_loop

func _live_matches(node) -> bool:
    if node == null:
        return false
    var script = node.get_script()
    if script == null:
        return false
    return str(script.resource_path) == "res://studio/loop.gd"

func _limit_matches(limit) -> bool:
    if limit == null:
        return false
    if not is_equal_approx(float(limit.wait_time), expected_seconds):
        return false
    return float(limit.time_left) >= expected_seconds - 0.5

func _sha_file(path: String) -> String:
    if not FileAccess.file_exists(path):
        return ""
    var file := FileAccess.open(path, FileAccess.READ)
    if file == null:
        return ""
    var ctx := HashingContext.new()
    ctx.start(HashingContext.HASH_SHA256)
    ctx.update(file.get_buffer(file.get_length()))
    file.close()
    return ctx.finish().hex_encode().to_lower()

func _known_loop_script(path: String) -> bool:
    if path != "res://studio/loop.gd" or written_loop == "":
        return false
    if _sha_file(path) != written_loop:
        return false
    return _sha_text(FileAccess.get_file_as_string(path)) == expected_loop

func _known_loop_scene(path: String) -> bool:
    if path != "res://studio/loop.tscn" or written_scene == "" or package_scene == "":
        return false
    var digest := _sha_file(path)
    return digest != "" and digest == written_scene and digest == package_scene

func _prefer(bad: PackedStringArray) -> String:
    if bad.is_empty():
        return ""
    for item in bad:
        if item != "res://studio/loop.gd" and item != "res://studio/loop.tscn":
            return item
    return bad[0]

func _embedded_script(text: String, path: String) -> String:
    var markers := PackedStringArray(['[sub_resource type="GDScript"', '[sub_resource type="Script"'])
    for marker in markers:
        var at := text.find(marker)
        if at < 0:
            continue
        var id_at := text.find('id="', at)
        if id_at < 0:
            return path + "#GDScript:inline"
        var id_end := text.find('"', id_at + 4)
        if id_end < 0:
            return path + "#GDScript:inline"
        return path + "#GDScript:" + text.substr(id_at + 4, id_end - (id_at + 4))
    return ""

func _res_paths(text: String) -> PackedStringArray:
    var out: PackedStringArray = []
    var needle := 'path="res://'
    var from := 0
    while true:
        var at := text.find(needle, from)
        if at < 0:
            return out
        var start := at + 6
        var end := text.find('"', start)
        if end < 0:
            return out
        out.append(text.substr(start, end - start))
        from = end + 1
    return PackedStringArray()

func _scan_scene(path: String, seen: Dictionary) -> String:
    if seen.has(path):
        return ""
    seen[path] = true
    if not FileAccess.file_exists(path):
        return path
    var text := FileAccess.get_file_as_string(path)
    var bad: PackedStringArray = []
    var inline := _embedded_script(text, path)
    if inline != "":
        bad.append(inline)
    for child in _res_paths(text):
        if child.ends_with(".gd"):
            if not _known_loop_script(child):
                bad.append(child)
        elif child.ends_with(".tscn"):
            if child == path:
                continue
            var nested := _scan_scene(child, seen)
            if nested != "":
                bad.append(nested)
        else:
            bad.append(child)
    var picked := _prefer(bad)
    if picked != "":
        return picked
    if not _known_loop_scene(path):
        return path
    return ""

func _scan_autoloads() -> String:
    var path := "res://studio/_scan_project.godot"
    if not FileAccess.file_exists(path):
        return path
    var in_auto := false
    for raw in FileAccess.get_file_as_string(path).split("\\n"):
        var line := raw.strip_edges()
        if line.begins_with("["):
            in_auto = line == "[autoload]"
            continue
        if not in_auto or line == "" or line.begins_with(";"):
            continue
        var q1 := line.find('"')
        var q2 := line.rfind('"')
        var quoted := ""
        if q1 >= 0 and q2 > q1:
            quoted = line.substr(q1 + 1, q2 - q1 - 1)
        if quoted.begins_with("*"):
            quoted = quoted.substr(1)
        if quoted.begins_with("res://"):
            return quoted
        if line.find("res://") >= 0:
            return line
    return ""

func _back_at_start(loop: Node) -> bool:
    var player = loop.get_node_or_null("Player")
    var key = loop.get_node_or_null("Key")
    var door = loop.get_node_or_null("Door")
    var shape = loop.get_node_or_null("Door/Shape")
    if player == null or key == null or door == null or shape == null:
        return false
    if key.get_parent() != loop or player.get_node_or_null("Key") != null:
        return false
    if shape.disabled:
        return false
    if bool(key.freeze):
        return false
    if player.position.distance_to(Vector2(-180, 168)) > 3.0:
        return false
    if player.velocity.length() > 5.0:
        return false
    if key.position.distance_to(Vector2(0, 40)) > 12.0:
        return false
    if door.position.distance_to(Vector2(150, 168)) > 3.0:
        return false
    return true

func _attach_loop() -> bool:
    var auto := _scan_autoloads()
    if auto != "":
        foreign = auto
        return false
    var bad := _scan_scene("res://studio/loop.tscn", {})
    if bad != "":
        foreign = bad
        return false
    if not ResourceLoader.exists("res://studio/loop.tscn"):
        return false
    var packed = load("res://studio/loop.tscn") as PackedScene
    if not _packed_matches(packed):
        return false
    var loop = packed.instantiate()
    if not _live_matches(loop):
        return false
    loop.name = "Loop"
    add_child(loop)
    return _live_matches(loop)

func _begin_fresh(state: Dictionary) -> bool:
    var bad := _scan_scene("res://studio/loop.tscn", {})
    if bad != "":
        foreign = bad
        return false
    var packed = load("res://studio/loop.tscn") as PackedScene
    if not _packed_matches(packed):
        return false
    var spawned = packed.instantiate()
    if not _live_matches(spawned):
        return false
    spawned.name = "Loop"
    add_child(spawned)
    var limit = spawned.get_node_or_null("Limit")
    state["physics_start"] = Engine.get_physics_frames()
    state["last_left"] = float(limit.time_left) if limit != null else -1.0
    state["limit_ok"] = _live_matches(spawned) and _limit_matches(limit)
    return bool(state["limit_ok"])

func _bind(secret: String, host: Node) -> Callable:
    var state := {
        "fell": false,
        "sound": false,
        "picked": false,
        "door": false,
        "timeout_lose": false,
        "restart": false,
        "phase": 0,
        "frames": 0,
        "started": Time.get_ticks_msec(),
        "sealed": false,
        "force_fail": false,
        "loose": false,
        "door_marked": false,
        "door_y0": 0.0,
        "limit_ok": false,
        "physics_start": 0,
        "last_left": -1.0,
        "saw_key_leave": false,
        "saw_player_leave": false,
        "elapsed_frames": 0,
    }

    var needed := func(part: String) -> bool:
        return host.required.has(part)

    var wants_timeout := func() -> bool:
        return needed.call("timeout_lose") or needed.call("restart")

    var bits_of := func() -> String:
        var parts: PackedStringArray = []
        if needed.call("fell"):
            parts.append("fell=%s" % str(state["fell"]).to_lower())
        if needed.call("sound"):
            parts.append("sound=%s" % str(state["sound"]).to_lower())
        if needed.call("picked"):
            parts.append("picked=%s" % str(state["picked"]).to_lower())
        if needed.call("door"):
            parts.append("door=%s" % str(state["door"]).to_lower())
        if needed.call("timeout_lose"):
            parts.append("timeout_lose=%s" % str(state["timeout_lose"]).to_lower())
        if needed.call("restart"):
            parts.append("restart=%s" % str(state["restart"]).to_lower())
        return " ".join(parts)

    var satisfied := func() -> bool:
        if needed.call("fell") and not state["fell"]:
            return false
        if needed.call("sound") and not state["sound"]:
            return false
        if needed.call("picked") and not state["picked"]:
            return false
        if needed.call("door") and not state["door"]:
            return false
        if needed.call("timeout_lose") and not state["timeout_lose"]:
            return false
        if needed.call("restart") and not state["restart"]:
            return false
        return true

    return func(cmd: String) -> bool:
        if state["sealed"]:
            return false
        if cmd == "reject":
            state["force_fail"] = true
            return true
        if cmd == "timeout":
            # The Limit signal is not a verdict. Physics frames decide timeout_lose.
            return false
        if cmd == "finish":
            state["sealed"] = true
            var ok: bool = (not state["force_fail"]) and satisfied.call()
            var kind := "STUDIO " if ok else "STUDIO_FAIL "
            if int(state.get("elapsed_frames", 0)) > 0:
                print("physics_frames=%d" % int(state["elapsed_frames"]))
            print("CHECK " + secret + " " + kind + bits_of.call())
            host.get_tree().quit(0 if ok else 1)
            return false
        if cmd != "tick":
            return false
        if not is_equal_approx(float(Engine.time_scale), 1.0) or int(Engine.physics_ticks_per_second) != 60:
            state["force_fail"] = true
            return true
        if int(state["phase"]) < 3 and Engine.get_physics_frames() > 1200:
            state["force_fail"] = true
            return true
        var loop = host.get_node_or_null("Loop")
        if loop == null:
            return false
        state["frames"] = int(state["frames"]) + 1
        var player = loop.get_node_or_null("Player")
        var key = loop.get_node_or_null("Key")
        var floor = loop.get_node_or_null("Floor")
        if key is RigidBody2D and floor and key.get_parent() == loop and key.position.y > floor.position.y - 40.0 and abs(key.linear_velocity.y) < 40.0:
            state["fell"] = true
        if key != null and player != null and key.get_parent() != player:
            state["loose"] = true
        if key == null and player != null:
            key = player.get_node_or_null("Key")
        if state["loose"] and key != null and player != null and key.get_parent() == player:
            state["picked"] = true
        var beep = loop.get_node_or_null("Beep")
        if state["picked"] and beep != null and beep.playing and beep.stream is AudioStreamGenerator:
            state["sound"] = true
        var door = loop.get_node_or_null("Door")
        var shape = loop.get_node_or_null("Door/Shape")
        if door != null and not state["door_marked"]:
            state["door_marked"] = true
            state["door_y0"] = door.position.y
        if state["picked"] and shape != null and door != null and shape.disabled and float(state["door_y0"]) - door.position.y > 40.0:
            state["door"] = true
        var phase := int(state["phase"])
        var frames := int(state["frames"])
        if phase == 0 and state["fell"] and frames > 20:
            phase = 1
        if phase == 1 and not state["picked"] and key != null and player != null and key.get_parent() != player:
            var aim: Vector2 = key.global_position - player.global_position
            loop.set("test_axis", aim.normalized() if aim.length() > 6.0 else Vector2.ZERO)
        if phase == 1 and state["picked"] and state["door"]:
            phase = 2
        var exit_node = loop.get_node_or_null("Exit")
        if phase == 2 and player != null and exit_node != null:
            var aim2: Vector2 = exit_node.global_position - player.global_position
            if aim2.length() > 28.0:
                loop.set("test_axis", aim2.normalized())
            elif state["picked"] and state["door"]:
                if not wants_timeout.call():
                    state["phase"] = phase
                    return true
                # Freeing under the same name makes Godot rename the fresh round, so the check would watch the finished one.
                loop.name = "Finished"
                loop.process_mode = Node.PROCESS_MODE_DISABLED
                loop.queue_free()
                phase = 3
                state["phase"] = phase
                state["frames"] = 0
                Engine.time_scale = 1.0
                Engine.physics_ticks_per_second = 60
                if not host._begin_fresh(state):
                    if host.foreign != "":
                        print("unverified script: " + host.foreign)
                    state["force_fail"] = true
                    return true
                return false
        state["phase"] = phase
        if phase == 3:
            var fresh = host.get_node_or_null("Loop")
            var limit = fresh.get_node_or_null("Limit") if fresh != null else null
            var elapsed := Engine.get_physics_frames() - int(state["physics_start"])
            var budget := int(host.expected_seconds * 60.0)
            state["elapsed_frames"] = elapsed
            if not host._live_matches(fresh) or limit == null:
                state["force_fail"] = true
                return true
            var left := float(limit.time_left)
            if bool(limit.is_stopped()) and elapsed < budget - 8:
                state["force_fail"] = true
                return true
            if float(state["last_left"]) >= 0.0 and left > float(state["last_left"]) + 0.05 and elapsed < budget - 8:
                state["force_fail"] = true
                return true
            state["last_left"] = left
            var body = fresh.get_node_or_null("Player")
            var held = fresh.get_node_or_null("Key")
            if held != null and held.position.distance_to(Vector2(0, 40)) > 12.0:
                state["saw_key_leave"] = true
            if body != null and body.position.x < -190.0:
                state["saw_player_leave"] = true
            if elapsed < 45:
                fresh.set("test_axis", Vector2(-1, 0) if elapsed > 15 else Vector2.ZERO)
            else:
                fresh.set("test_axis", Vector2.ZERO)
            if elapsed >= budget:
                state["timeout_lose"] = true
            if bool(state["timeout_lose"]) and bool(state["saw_key_leave"]) and bool(state["saw_player_leave"]) and host._back_at_start(fresh):
                state["restart"] = true
            if elapsed > budget + 180 and not bool(state["restart"]):
                state["force_fail"] = true
                return true
        if not wants_timeout.call() and satisfied.call():
            return true
        if phase == 3 and satisfied.call():
            return true
        return false
"""

CHECK_TSCN = """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://studio/check.gd" id="1"]

[node name="Check" type="Node"]
script = ExtResource("1")
"""

RECORD_GD = """extends Node2D

var frames := 0
var loop: Node = null

func _ready() -> void:
    $Fail.text = OS.get_environment("GAME_EDITOR_FAIL_LINE")
    $Pass.text = OS.get_environment("GAME_EDITOR_PASS_LINE")
    $Pass.offset_top = 72.0
    $Pass.offset_bottom = 140.0
    var band := ColorRect.new()
    band.offset_right = 640.0
    band.offset_bottom = 150.0
    band.color = Color(0, 0, 0, 0.92)
    band.z_index = 10
    add_child(band)
    $Fail.z_index = 11
    $Pass.z_index = 11
    if ResourceLoader.exists("res://studio/loop.tscn"):
        loop = load("res://studio/loop.tscn").instantiate()
        loop.position = Vector2(320, 210)
        loop.scale = Vector2(0.42, 0.42)
        loop.z_index = 1
        add_child(loop)
        move_child(loop, 1)

func _process(_delta: float) -> void:
    frames += 1
    if loop and is_instance_valid(loop):
        var axis := Vector2(1, 0) if frames < 50 else Vector2(-0.4, 0)
        loop.set("test_axis", axis)
    var limit := 90 if loop != null else 20
    if frames > limit:
        get_tree().quit(0)
"""

RECORD_TSCN = """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://studio/record.gd" id="1"]

[node name="Record" type="Node2D"]
script = ExtResource("1")

[node name="Bg" type="ColorRect" parent="."]
offset_right = 640.0
offset_bottom = 360.0
color = Color(0, 0, 0, 1)

[node name="Fail" type="Label" parent="."]
offset_left = 16.0
offset_top = 24.0
offset_right = 624.0
offset_bottom = 160.0
theme_override_colors/font_color = Color(1, 0.4, 0.4, 1)
autowrap_mode = 3

[node name="Pass" type="Label" parent="."]
offset_left = 16.0
offset_top = 180.0
offset_right = 624.0
offset_bottom = 340.0
theme_override_colors/font_color = Color(0.6, 1, 0.6, 1)
autowrap_mode = 3
"""
