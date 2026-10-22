#!/usr/bin/env python3
"""game-editor: a stdio MCP server between Hermes and Godot AI (godot-ai 4.3.0). Standard library only.

It publishes the read operations in READ_ALLOW (readOnlyHint true), change_guide (local, readOnlyHint
true), plan_change (readOnlyHint true), apply_plan and confirm_risky_plan (readOnlyHint false, so Hermes's
trust gate asks for each). The change tools in tools.json are steps of a plan, never tools of their own.
plan_change sends Godot AI only the session list (the server's own table). It does not read editor
state, the open-scene list, the scene tree or node values: those can run project code. A scene file that cannot be parsed (no file,
a BOM, an inherited scene, a parse failure) is refused when the plan would change it. No note is added
to let that plan through. apply_plan runs only that plan, and sends nothing when the fingerprint changed.
A plan that deletes, overwrites, discards unsaved work, writes outside the project or runs code written in
it needs a second approval: apply_plan returns the list of what is lost and confirm_risky_plan runs it.
The bridge does not use elicitation (Hermes v0.22.2's terminal CLI fails it closed).
It starts `python -m godot_ai attach` as a child process on the first tools/call (a session that never
calls a tool does not start Godot AI). Whether an operation runs code, writes, deletes or sends data is
taken from effects.json, which is extracted from the Godot AI source, not from the tool's name.

Before Godot AI sees a call, the bridge refuses: arguments a tool does not declare; string values in the
arguments (not script or file text) that start with res://, uid:// or user:// and point at hidden or
key-like files, or outside the project; writes to user:// player data (a read of an ordinary user://
save file is allowed, and the reply says it is player data); changes
to children of an instanced scene that would not be saved; files that would be overwritten without
overwrite=true; and a save that would overwrite a change made on disk by someone else. Upstream replies
are not passed through unchanged: a did-line may come first, a truncated marker may come first, a
zero-result note or a player-data note may follow, and passwords in export_presets.cfg are blanked.
The bridge keeps no files of its own: what it remembers (the scenes' disk fingerprints, nodes it added)
lives in memory for the session. play_session and export_game run only from apply_plan. export_game
writes a build inside the project and, when that platform has no preset yet, export_presets.cfg. It
does not download export templates. When serve is true it starts a page on 127.0.0.1 and stops that
process when the bridge exits.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import plistlib
import posixpath
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote

VERSION = "1.2.3"
UPSTREAM_VERSION = "4.3.0"
HTTP_PORT = 8000
WS_PORT = 9500
CALL_TIMEOUT = 120.0
# Godot AI gives a test run 300 s; Hermes stops waiting for any MCP call at 300 s.
TEST_TIMEOUT = 290.0
START_TIMEOUT = 60.0
SHUTDOWN_GRACE = 10.0
MAX_RESULT_BYTES = 256 * 1024
MAX_CLI_OUTPUT = 60 * 1024
MAX_STDERR = 2048
# Hermes cuts an error reply at 2,048 characters. A failed plan fits in that, conclusion first.
FAILED_REPLY_CHARS = 2048
HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
CHILD_PROTOCOL = "2025-06-18"

TABLE = json.loads((Path(__file__).resolve().parent / "tools.json").read_text(encoding="utf-8"))
TOOLS = {tool["name"]: tool for tool in TABLE["tools"]}
EFFECTS = json.loads((Path(__file__).resolve().parent / "effects.json").read_text(encoding="utf-8"))
_EFFECTS_BY_TOOL = EFFECTS.get("by_tool") or {}
_EFFECTS_BY_COMMAND = EFFECTS.get("by_command") or {}
_DANGER = {"run_code", "write", "delete", "send"}
# Published reads. An op that is not listed here is not a read, even when effects.json says "read".
# tools.json can add ops; they stay unavailable until they are added here.
READ_ALLOW = frozenset({
    ("editor_look", "state"), ("editor_look", "open_scenes"), ("editor_look", "selection"),
    ("editor_look", "monitors"), ("editor_look", "sessions"), ("editor_look", "read_logs"),
    ("editor_look", "class"),
    ("scene_look", "scene_tree"), ("scene_look", "find_nodes"), ("scene_look", "children"),
    ("scene_look", "groups"), ("scene_look", "signals"), ("scene_look", "properties"), ("scene_look", "resource"),
    ("script_look", "source"), ("script_look", "outline"),
    ("file_look", "text"), ("file_look", "files"), ("file_look", "resources"),
    ("project_look", "get"), ("project_look", "autoloads"), ("project_look", "input_map"),
    ("project_look", "test_results"), ("project_look", "addon_tools"),
    ("asset_look", "materials"), ("asset_look", "animation"), ("asset_look", "animations"),
    ("asset_look", "animation_check"), ("asset_look", "camera"), ("asset_look", "cameras"),
    ("asset_look", "particles"), ("asset_look", "tiles"), ("asset_look", "gridmap_cells"),
    ("asset_look", "gridmap_items"), ("asset_look", "nav_path"),
})
# The only Godot AI call plan_change may send: the server's session table. Editor state and the open-scene
# list read scene_file_path and can run a root @tool _get. They run at apply_plan, after the first approval.
PLAN_UPSTREAM = frozenset({
    ("session_manage", "list"),
})
# Kept so a caller that sets plan_reads is still limited to these. plan_change does not set plan_reads:
# values in a plan come from the file and from edits this session already made.
PLAN_READS = frozenset({
    ("node_get_properties", None), ("node_manage", "get_groups"), ("signal_manage", "list"),
    ("resource_manage", "inspect"), ("tilemap_manage", "tilemap_get_cells"),
})
# The four tools that only read files on disk (readOnlyHint true). Nothing else is a read: anything that asks the editor
# (its live values, logs, the tree in memory, cameras, tiles) is a plan step, because the editor can run project code
# (a @tool script, a property getter) while it answers.
DISK_READS = {
    "scene_look": {"default": "scene_tree", "first": "Read a Godot scene file's nodes and stored values from disk.",
                   "about": "Reads the .tscn file on disk (the open scene unless scene names another): the node tree, "
                            "a search by name, type or group, a node's children, the values stored for a node, its "
                            "groups and its signal connections. An inherited scene (the root is another scene) is "
                            "refused: the base is not in this file. Unsaved edits in the editor are not in the file; "
                            "the editor's live values are a plan_change step (scene_do op properties).",
                   "ops": {"scene_tree": ["scene", "offset", "limit"], "find_nodes": ["scene", "name", "type", "group"],
                           "children": ["scene", "path"], "properties": ["scene", "path", "fields"],
                           "groups": ["scene", "path"], "signals": ["scene", "path"]}},
    "script_look": {"default": "source", "first": "Read a Godot script file and its outline from disk.",
                    "about": "Reads a GDScript or C# file on disk: its text, or an outline (functions, signals, "
                             "exports, extends, class_name).",
                    "ops": {"source": ["path"], "outline": ["path"]}},
    "file_look": {"default": "text", "first": "Read or search files in the Godot project folder on disk.",
                  "about": "Reads a res:// text file, lists files by name, type or folder, lists resources by type, "
                           "and reads a GameForge playtest report (gameforge_report: report.json, the newest under "
                           "res://playtests when path is left out) into fix steps for plan_change with the file, the "
                           "line and that line's code.",
                  "ops": {"text": ["path"], "files": ["path", "name", "type", "offset", "limit"], "resources": ["type"],
                          "gameforge_report": ["path"]}},
    "project_look": {"default": "get", "first": "Read Godot project settings and the input map.",
                     "about": "Reads project.godot on disk: one setting, the autoloads, and the input map (actions and "
                              "their keys).",
                     "ops": {"get": ["key"], "autoloads": [], "input_map": []}},
}
DISK_ARG_DOC = {
    "scene": "A res:// .tscn path. Left out: the scene the editor last reported open.",
    "path": "A res:// path (a file), or for scene_look a node path from the scene root such as /Main/Player.",
    "fields": "Only these stored properties.", "name": "Part of a node or file name.",
    "type": "A node type (scene_look), a file extension (file_look files) or a resource type (file_look resources).",
    "group": "A group name.", "offset": "Skip this many rows.", "limit": "At most this many rows.",
    "key": "A project setting, such as application/config/name.",
}
DISK_NOTE = ("game-editor: read from the project's files on disk; the editor was not asked, so no project code ran. "
             "Unsaved edits in the editor are not in these files.")
MAX_PLAN_STEPS = 64
# Only the newest plan can run, and only within this many seconds of plan_change.
PLAN_TTL = 600.0
# resources/read is allowed only for these godot:// forms. Node properties, shaders and visual shaders
# load or get values through scripts, so they are not reads here. Anything else is refused.
RESOURCE_READ_ALLOW = tuple(re.compile(pattern) for pattern in (
    r"godot://editor/state", r"godot://scene/current", r"godot://scene/hierarchy", r"godot://sessions",
    r"godot://selection/current", r"godot://performance", r"godot://input_map", r"godot://project/info",
    r"godot://project/settings", r"godot://logs/recent", r"godot://materials", r"godot://custom-tools",
    r"godot://class/[A-Za-z0-9_]+", r"godot://node/[^?#]+/children", r"godot://node/[^?#]+/groups",
    r"godot://script/[^?#]+",
))
# Look ops that the extracted table calls reads but that change the project or run code. They are plan
# steps, labelled with what they really do.
EFFECT_OVERRIDE = {
    ("asset_look", "preset_fade"): {"write"}, ("asset_look", "preset_slide"): {"write"},
    ("asset_look", "preset_shake"): {"write"}, ("asset_look", "preset_pulse"): {"write"},
    ("asset_look", "particle_restart"): {"run_code"},
}
_SOURCE_EXPORT_SUFFIXES = (".gd", ".tscn", ".cs", ".godot", ".cfg", ".gd.uid")
# Godot 4.7.2 ClassDB, own properties only: editor-visible and not stored, or NO_INSTANCE_STATE.
# godot-ai 4.3.0 does not return usage, so the reply is not a source. Walk parents at lookup time.
_PROPERTY_USAGE = json.loads((Path(__file__).resolve().parent / "property_usage.json").read_text(encoding="utf-8"))
_CLASS_PARENTS = _PROPERTY_USAGE.get("parents") if isinstance(_PROPERTY_USAGE.get("parents"), dict) else {}
_CLASS_DROP = _PROPERTY_USAGE.get("drop") if isinstance(_PROPERTY_USAGE.get("drop"), dict) else {}
# These names are not stored on any class that has them. "position" is not here: Node2D.position is stored.
_UNAMBIGUOUS_DROP = frozenset({"playing", "current_animation"})

# ---------------------------------------------------------------- paths and names

SCHEMES = ("res://", "uid://", "user://")
# Values of these keys are script or file text, not paths; they are not checked as paths.
TEXT_KEYS = {"content", "code", "new_text", "old_text", "text", "goal"}
PATH_KEYS = {
    "path", "scene_path", "node_path", "parent_path", "new_parent", "output", "resource_path", "save_to",
    "script_path", "shader_path", "file_path", "directory", "dir", "dirs", "dest", "destination",
    "target_path", "source_path", "player_path", "camera_path", "from_path", "to_path",
}
GODOT_ASSET_EXT = {
    ".tscn", ".scn", ".tres", ".res", ".gd", ".cs", ".gdshader", ".gdshaderinc", ".shader", ".png", ".jpg",
    ".jpeg", ".webp", ".svg", ".bmp", ".tga", ".exr", ".hdr", ".ogg", ".wav", ".mp3", ".glb", ".gltf", ".fbx",
    ".obj", ".blend", ".ttf", ".otf", ".woff", ".woff2", ".fnt", ".import", ".uid", ".gdextension", ".csv",
    ".translation", ".po", ".pot",
}
SECRET_EXT = {".keystore", ".jks", ".p12", ".pfx", ".pem", ".key", ".p8", ".mobileprovision",
              ".provisionprofile", ".ppk", ".gpg", ".kdbx", ".env", ".netrc"}
SECRET_NAMES = {"export_credentials.cfg", "google-services.json", "googleservice-info.plist",
                "credentials.json", "service-account.json", "service_account.json",
                "key.properties", "gradle.properties"}
# Words that mark a key or password file (or a folder of them). Plain "token" is left out on purpose:
# games keep game data such as res://data/tokens.json.
SECRET_WORDS = re.compile(r"credential|secret|passw(or)?d|api[_-]?key|apikey|private[_-]?key|"
                          r"service[_-]?account|(access|auth|api|bearer|refresh|oauth)[_-]?token|keystore",
                          re.IGNORECASE)
REFUSED_WHAT = (
    "files in hidden folders or with hidden names (such as .godot/ or .env), and files or folders whose names "
    "commonly hold keys or passwords (export_credentials.cfg, google-services.json, GoogleService-Info.plist, "
    "key.properties, gradle.properties, *.keystore, *.jks, *.p12, *.pfx, *.pem, *.key, *.p8, *.mobileprovision, "
    "*.env, id_rsa*, and non-Godot "
    "files or folders whose names contain credential, secret, password, api_key, access_token, auth_token or "
    "private_key). This is a file-name rule, not a content check")


def _clean(segment: str) -> str:
    """How the file system will see a name: Windows drops trailing dots and spaces, and names compare
    without case on Windows and (by default) macOS."""
    return segment.rstrip(" .").lower() or segment.lower()


def secret_like(name: str, is_folder: bool = False) -> bool:
    low = _clean(name)
    ext = os.path.splitext(low)[1]
    if not is_folder and (low in SECRET_NAMES or ext in SECRET_EXT):
        return True
    if low.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")):
        return True
    if not is_folder and ext in GODOT_ASSET_EXT:
        return False
    return bool(SECRET_WORDS.search(low))


def _path_parts(rel: str) -> list[str] | None:
    """Path segments; None when a segment means the parent folder ('..', and on Windows '...' or '.. ')."""
    parts = []
    for part in rel.replace("\\", "/").split("/"):
        if part == "" or part.strip(" ") == ".":
            continue
        if part.rstrip(" .") == "":
            return None
        parts.append(part)
    return parts


def _bad_parts(parts: list[str]) -> bool:
    for i, part in enumerate(parts):
        if part.lstrip().startswith("."):
            return True
        if secret_like(part, is_folder=i < len(parts) - 1):
            return True
    return False


def static_refusal(tool: str, value: str) -> str:
    """Rules that need no project folder. value is stripped and starts with res:// (any case)."""
    parts = _path_parts(value[len("res://"):])
    if parts is None:
        return f"game-editor: {tool} refused a path with '..'. Nothing was sent to the Godot editor."
    if _bad_parts(parts):
        return (f"game-editor: {tool} refused {value}: this plugin does not read or write {REFUSED_WHAT}. "
                f"Nothing was sent to the Godot editor.")
    return ""


def resolved_refusal(tool: str, value: str, project_root: str, pm=os.path) -> str:
    """The same rules on the real path after symbolic links; the real path must stay in the project.
    pm is the path module (os.path, or ntpath / posixpath in tests)."""
    parts = [p.rstrip(" .") or p for p in (_path_parts(value[len("res://"):]) or [])]
    root = pm.realpath(project_root)
    real = pm.realpath(pm.join(root, *parts)) if parts else root
    norm_root, norm_real = pm.normcase(root).rstrip("\\/"), pm.normcase(real)
    if norm_real != norm_root and not norm_real.startswith(norm_root + pm.sep):
        return (f"game-editor: {tool} refused {value}: it resolves outside the project folder "
                f"(a symbolic link). Nothing was sent to the Godot editor.")
    rel = pm.relpath(real, root)
    real_parts = [] if rel == "." else [p for p in re.split(r"[\\/]", rel) if p]
    if _bad_parts(real_parts):
        return (f"game-editor: {tool} refused {value}: it resolves to a file this plugin does not read or "
                f"write ({REFUSED_WHAT}). Nothing was sent to the Godot editor.")
    return ""


def path_key_strings(value, key: str = "") -> list[str]:
    """Strings under a path argument. Text keys are skipped, so a script body is not a path."""
    found: list[str] = []
    if key in TEXT_KEYS:
        return found
    if isinstance(value, str) and key in PATH_KEYS:
        found.append(value.strip())
    elif isinstance(value, dict):
        for k, item in value.items():
            found.extend(path_key_strings(item, str(k)))
    elif isinstance(value, (list, tuple)):
        if key in PATH_KEYS:
            found.extend(item.strip() for item in value if isinstance(item, str))
        else:
            for item in value:
                found.extend(path_key_strings(item, key))
    return found


def scheme_strings(value, key: str = "") -> list[str]:
    """Every string, in any list or dict at any depth, that names a resource by res://, uid:// or user://
    after leading spaces. Script and file text (TEXT_KEYS) is skipped."""
    found: list[str] = []
    if key in TEXT_KEYS:
        return found
    if isinstance(value, str):
        s = value.strip()
        if s[:8].lower().startswith(SCHEMES):
            found.append(s)
    elif isinstance(value, dict):
        for k, v in value.items():
            found.extend(scheme_strings(v, str(k)))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(scheme_strings(v, key))
    return found


UID_LINE = re.compile(r'uid="?(uid://[A-Za-z0-9_]+)')


def uid_index(project_root: str) -> dict[str, str]:
    """uid:// -> res:// from .uid sidecars, .import files and scene/resource headers in the project."""
    root = Path(project_root)
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            path = Path(dirpath) / fn
            try:
                if fn.endswith(".uid"):
                    uid = path.read_text(encoding="utf-8", errors="ignore").strip().split()[0]
                    target = path.with_suffix("")
                elif fn.endswith(".import"):
                    m = UID_LINE.search(path.read_text(encoding="utf-8", errors="ignore")[:4000])
                    if not m:
                        continue
                    uid, target = m.group(1), path.with_suffix("")
                elif fn.endswith((".tscn", ".tres")):
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        m = UID_LINE.search(fh.readline())
                    if not m:
                        continue
                    uid, target = m.group(1), path
                else:
                    continue
            except (OSError, IndexError):
                continue
            out[uid] = "res://" + target.relative_to(root).as_posix()
    return out


# ---------------------------------------------------------------- scenes on disk

NODE_HEAD = re.compile(r"^\[node (.*)\]\s*$")
EDITABLE_HEAD = re.compile(r'^\[editable path="([^"]*)"\]\s*$')
EXT_HEAD = re.compile(r"^\[ext_resource (.*)\]\s*$")
CONN_HEAD = re.compile(r"^\[connection (.*)\]\s*$")
SUB_HEAD = re.compile(r"^\[sub_resource (.*)\]\s*$")
ATTR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|[^\s\]]+(?:\([^)]*\))?)')
PROP_LINE = re.compile(r"^([A-Za-z_][\w/:.]*) = (.*)$")
EXT_REF = re.compile(r'ExtResource\(\s*"([^"]+)"\s*\)')
GROUPS_ATTR = re.compile(r"groups=\[([^\]]*)\]")


def _header_groups(header: str) -> list[str]:
    match = GROUPS_ATTR.search(header)
    if not match:
        return []
    return re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))


def _scene_path(root_name: str, raw: str) -> str:
    raw = (raw or ".").strip().strip('"')
    if raw in ("", "."):
        return f"/{root_name}"
    if raw.startswith("/"):
        return raw
    return f"/{root_name}/{raw}"


def _brackets_balanced(text: str) -> bool:
    """True when (), {} and [] are balanced, ignoring braces inside strings."""
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    while i < length:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return True
            stack.pop()
        i += 1
    return not stack and not in_string


def parse_tscn(text: str) -> dict:
    """{'root', 'root_type', 'nodes': {relpath: {'local', 'instance', 'scene', 'type', 'props'}}, 'editable',
    'ext', 'groups', 'root_groups', 'connections', 'subs'}.
    A node declared with type= or instance= belongs to this scene; an entry without them only overrides
    a node that comes from an instanced scene. 'scene' is the instanced .tscn (res:// or uid://).
    A property value that wraps onto later lines is kept until its brackets balance."""
    root = None
    nodes: dict[str, dict] = {}
    editable: set[str] = set()
    ext: dict[str, dict] = {}
    groups: dict[str, list[str]] = {}
    raw_connections: list[dict] = []
    subs: dict[str, dict] = {}
    current = None
    current_sub: dict | None = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        m = NODE_HEAD.match(line)
        if m:
            current_sub = None
            attrs = {k: v.strip('"') for k, v in ATTR.findall(m.group(1))}
            name, parent = attrs.get("name", ""), attrs.get("parent")
            if parent is None:
                root = name
                current = {"local": True, "instance": "instance" in attrs, "scene": None,
                           "type": attrs.get("type") or "", "props": {}}
                rel = ""
            else:
                rel = name if parent == "." else f"{parent}/{name}"
                current = {"local": "type" in attrs or "instance" in attrs, "instance": "instance" in attrs,
                           "scene": None, "type": attrs.get("type") or "", "props": {}}
            ref = EXT_REF.search(attrs.get("instance", ""))
            if ref:
                current["ext_id"] = ref.group(1)
            nodes[rel] = current
            groups[rel] = _header_groups(m.group(1))
            index += 1
            continue
        if line.startswith("["):
            current = None
            current_sub = None
            m = CONN_HEAD.match(line)
            if m:
                raw_connections.append({k: v.strip('"') for k, v in ATTR.findall(m.group(1))})
                index += 1
                continue
            m = SUB_HEAD.match(line)
            if m:
                attrs = {k: v.strip('"') for k, v in ATTR.findall(m.group(1))}
                current_sub = {"type": attrs.get("type", ""), "props": {}}
                if attrs.get("id"):
                    subs[attrs["id"]] = current_sub
                index += 1
                continue
            m = EDITABLE_HEAD.match(line)
            if m:
                editable.add(m.group(1))
                index += 1
                continue
            m = EXT_HEAD.match(line)
            if m:
                attrs = {k: v.strip('"') for k, v in ATTR.findall(m.group(1))}
                if "id" in attrs:
                    ext[attrs["id"]] = {"path": attrs.get("path", ""), "uid": attrs.get("uid", ""),
                                        "type": attrs.get("type", "")}
            index += 1
            continue
        owner = current_sub if current_sub is not None else current
        if owner is not None:
            m = PROP_LINE.match(line)
            if m:
                value = m.group(2)
                index += 1
                while not _brackets_balanced(value) and index < len(lines) and not lines[index].startswith("["):
                    value += "\n" + lines[index]
                    index += 1
                owner["props"][m.group(1)] = value
                continue
        index += 1
    for info in nodes.values():
        if info.get("ext_id") in ext:
            info["scene"] = ext[info["ext_id"]]["path"] or ext[info["ext_id"]]["uid"]
            info["scene_uid"] = ext[info["ext_id"]]["uid"]
    root_info = nodes.pop("", None)
    root_groups = groups.pop("", [])
    connections = []
    if root:
        for attrs in raw_connections:
            connections.append({"signal": attrs.get("signal", ""), "from": _scene_path(root, attrs.get("from", ".")),
                                "to": _scene_path(root, attrs.get("to", ".")), "method": attrs.get("method", "")})
    # An inherited scene ([node name="X" instance=ExtResource(...)] as the root) is the base scene with this
    # file's overrides: the base's root script and nodes are in it.
    root_scene = root_info.get("scene") if root_info and root_info.get("instance") else None
    root_scene_uid = root_info.get("scene_uid") if root_info and root_info.get("instance") else None
    return {"root": root, "root_type": (root_info or {}).get("type") or "", "nodes": nodes, "editable": editable,
            "ext": ext, "root_props": root_info["props"] if root_info else {}, "groups": groups,
            "root_groups": root_groups, "connections": connections, "subs": subs, "root_scene": root_scene,
            "root_scene_uid": root_scene_uid}


def rel_node_path(value: str, root_name: str | None) -> str:
    parts = [p for p in value.strip().split("/") if p]
    if parts and parts[0] == "root" and len(parts) > 1 and parts[1] == root_name:
        parts = parts[1:]
    if parts and parts[0] == root_name:
        parts = parts[1:]
    return "/".join(parts)


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def instanced_scenes(text: str) -> list[tuple[str, str]]:
    """(path, uid) of every PackedScene a .tscn instances."""
    parsed = parse_tscn(text)
    return [(e["path"], e["uid"]) for e in parsed["ext"].values() if e["type"] == "PackedScene"]


def file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# Values written in a .tscn, read back for comparison with the editor. None = not compared.
_NUM = r"-?(?:\d+\.?\d*(?:e[-+]?\d+)?|inf|nan)"


class _Null:
    """A .tscn null. Distinct from an unparsed value, which is Python None."""


_NULL = _Null()
_WS = " \t\r\n"


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WS:
        index += 1
    return index


def _parse_string(text: str, index: int) -> tuple[str, int]:
    if index >= len(text) or text[index] != '"':
        raise ValueError(text[index:index + 12])
    index += 1
    out: list[str] = []
    while index < len(text):
        ch = text[index]
        if ch == "\\":
            if index + 1 >= len(text):
                raise ValueError("escape")
            nxt = text[index + 1]
            out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
            index += 2
            continue
        if ch == '"':
            return "".join(out), index + 1
        out.append(ch)
        index += 1
    raise ValueError("string")


def _parse_number(text: str, index: int) -> tuple[float, int]:
    match = re.match(_NUM, text[index:])
    if not match:
        raise ValueError(text[index:index + 12])
    return float(match.group(0)), index + len(match.group(0))


def _parse_one(text: str, index: int, ext: dict) -> tuple[object, int]:
    index = _skip_ws(text, index)
    if index >= len(text):
        raise ValueError("end")
    if text.startswith("true", index) and not text[index + 4:index + 5].isalnum():
        return True, index + 4
    if text.startswith("false", index) and not text[index + 5:index + 6].isalnum():
        return False, index + 5
    if text.startswith("null", index) and not text[index + 4:index + 5].isalnum():
        return _NULL, index + 4
    if text.startswith('&"', index):
        value, index = _parse_string(text, index + 1)
        return value, index
    if text[index] == '"':
        return _parse_string(text, index)
    if text[index].isdigit() or text[index] in "+-" or text.startswith(("inf", "nan"), index):
        return _parse_number(text, index)
    if text[index] == "{":
        return _parse_dict(text, index, ext)
    if text[index] == "[":
        return _parse_array(text, index, ext)
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[index:])
    if not match:
        raise ValueError(text[index:index + 12])
    name = match.group(0)
    index = _skip_ws(text, index + len(name))
    if index >= len(text) or text[index] != "(":
        raise ValueError(name)
    args, index = _parse_args(text, index + 1, ext)
    return _construct(name, args, ext), index


def _parse_args(text: str, index: int, ext: dict) -> tuple[list, int]:
    args: list = []
    index = _skip_ws(text, index)
    if index < len(text) and text[index] == ")":
        return args, index + 1
    while True:
        value, index = _parse_one(text, index, ext)
        args.append(value)
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == ",":
            index = _skip_ws(text, index + 1)
            if index < len(text) and text[index] == ")":
                return args, index + 1
            continue
        if index < len(text) and text[index] == ")":
            return args, index + 1
        raise ValueError("args")


def _parse_dict(text: str, index: int, ext: dict) -> tuple[dict, int]:
    index += 1
    out: dict = {}
    while True:
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == "}":
            return out, index + 1
        if text.startswith('&"', index):
            key, index = _parse_string(text, index + 1)
        elif index < len(text) and text[index] == '"':
            key, index = _parse_string(text, index)
        else:
            raise ValueError("key")
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise ValueError("colon")
        value, index = _parse_one(text, index + 1, ext)
        out[key] = value
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            return out, index + 1
        raise ValueError("dict")


def _parse_array(text: str, index: int, ext: dict) -> tuple[list, int]:
    index += 1
    out: list = []
    while True:
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == "]":
            return out, index + 1
        value, index = _parse_one(text, index, ext)
        out.append(value)
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "]":
            return out, index + 1
        raise ValueError("array")


def _numbers(args: list) -> list[float] | None:
    if args and all(isinstance(item, float) for item in args):
        return args
    return None


def _vec(args: list, keys: str) -> dict | None:
    nums = _numbers(args)
    if nums is None or len(nums) != len(keys):
        return None
    return {keys[i]: nums[i] for i in range(len(keys))}


def _basis(args: list) -> dict | None:
    """Godot's 9-float Basis is row-major. basis.x is the first column (xx, yx, zx)."""
    nums = _numbers(args)
    if nums is not None and len(nums) == 9:
        xx, xy, xz, yx, yy, yz, zx, zy, zz = nums
        return {
            "x": {"x": xx, "y": yx, "z": zx},
            "y": {"x": xy, "y": yy, "z": zy},
            "z": {"x": xz, "y": yz, "z": zz},
        }
    if len(args) == 3 and all(isinstance(item, dict) for item in args):
        return {"x": args[0], "y": args[1], "z": args[2]}
    return None


def _packed(name: str, args: list) -> list | None:
    if name == "PackedStringArray":
        return args if all(isinstance(item, str) for item in args) else None
    if name == "PackedVector2Array":
        if args and all(isinstance(item, dict) for item in args):
            return args
        nums = _numbers(args)
        if nums is not None and len(nums) % 2 == 0:
            return [{"x": nums[i], "y": nums[i + 1]} for i in range(0, len(nums), 2)]
        return [] if not args else None
    if name == "PackedVector3Array":
        if args and all(isinstance(item, dict) for item in args):
            return args
        nums = _numbers(args)
        if nums is not None and len(nums) % 3 == 0:
            return [{"x": nums[i], "y": nums[i + 1], "z": nums[i + 2]} for i in range(0, len(nums), 3)]
        return [] if not args else None
    if name in ("PackedVector4Array", "PackedColorArray"):
        nums = _numbers(args)
        width = 4
        keys = "xyzw" if name == "PackedVector4Array" else "rgba"
        if nums is not None and len(nums) % width == 0:
            return [{keys[n]: nums[i + n] for n in range(width)} for i in range(0, len(nums), width)]
        if args and all(isinstance(item, dict) for item in args):
            return args
        return [] if not args else None
    if name == "Array" or name.startswith("Packed"):
        return list(args)
    return None


def _construct(name: str, args: list, ext: dict):
    if name == "ExtResource":
        if len(args) != 1 or not isinstance(args[0], str):
            raise ValueError(name)
        entry = ext.get(args[0])
        if not entry or not entry.get("path"):
            raise ValueError(name)
        return entry["path"]
    if name == "SubResource":
        if len(args) != 1 or not isinstance(args[0], str):
            raise ValueError(name)
        return ("sub", args[0])
    if name == "NodePath":
        if len(args) != 1 or not isinstance(args[0], str):
            raise ValueError(name)
        return args[0]
    if name in ("Vector2", "Vector2i"):
        built = _vec(args, "xy")
    elif name in ("Vector3", "Vector3i"):
        built = _vec(args, "xyz")
    elif name in ("Vector4", "Vector4i", "Quaternion"):
        built = _vec(args, "xyzw")
    elif name == "Color":
        if len(args) == 3 and _numbers(args):
            args = list(args) + [1.0]
        built = _vec(args, "rgba")
    elif name in ("Rect2", "Rect2i"):
        nums = _numbers(args)
        if nums is not None and len(nums) == 4:
            built = {"position": {"x": nums[0], "y": nums[1]}, "size": {"x": nums[2], "y": nums[3]}}
        elif len(args) == 2 and all(isinstance(item, dict) for item in args):
            built = {"position": args[0], "size": args[1]}
        else:
            built = None
    elif name == "AABB":
        nums = _numbers(args)
        if nums is not None and len(nums) == 6:
            built = {"position": {"x": nums[0], "y": nums[1], "z": nums[2]},
                     "size": {"x": nums[3], "y": nums[4], "z": nums[5]}}
        elif len(args) == 2 and all(isinstance(item, dict) for item in args):
            built = {"position": args[0], "size": args[1]}
        else:
            built = None
    elif name == "Transform2D":
        nums = _numbers(args)
        if nums is not None and len(nums) == 6:
            xx, xy, yx, yy, ox, oy = nums
            built = {"x": {"x": xx, "y": xy}, "y": {"x": yx, "y": yy}, "origin": {"x": ox, "y": oy}}
        elif len(args) == 3 and all(isinstance(item, dict) for item in args):
            built = {"x": args[0], "y": args[1], "origin": args[2]}
        else:
            built = None
    elif name == "Basis":
        built = _basis(args)
    elif name == "Transform3D":
        nums = _numbers(args)
        if nums is not None and len(nums) == 12:
            built = {"basis": _basis(nums[:9]), "origin": {"x": nums[9], "y": nums[10], "z": nums[11]}}
        elif len(args) == 2 and isinstance(args[0], dict) and isinstance(args[1], dict):
            built = {"basis": args[0], "origin": args[1]}
        else:
            built = None
    elif name == "Plane":
        nums = _numbers(args)
        if nums is not None and len(nums) == 4:
            built = {"normal": {"x": nums[0], "y": nums[1], "z": nums[2]}, "d": nums[3]}
        elif len(args) == 2 and isinstance(args[0], dict) and isinstance(args[1], float):
            built = {"normal": args[0], "d": args[1]}
        else:
            built = None
    elif name.startswith("Packed") or name == "Array":
        built = _packed(name, args)
    else:
        built = None
    if built is None:
        raise ValueError(name)
    return built


def parse_value(text: str, ext: dict):
    text = text.strip()
    if not text:
        return None
    try:
        value, index = _parse_one(text, 0, ext)
        if _skip_ws(text, index) != len(text):
            return None
    except (ValueError, IndexError):
        return None
    return value


def _entries_dict(value):
    """Godot AI's resource inspector writes a Dictionary as {entries: [{key, value}]}."""
    if not isinstance(value, dict) or set(value) != {"entries"} or not isinstance(value.get("entries"), list):
        return value
    out = {}
    for item in value["entries"]:
        if not isinstance(item, dict) or "key" not in item or "value" not in item:
            return value
        key = item["key"]
        if isinstance(key, (dict, list, tuple)):
            return value
        out[str(key)] = item["value"]
    return out


def tile_cells(raw: str) -> set[tuple[int, int, int, int, int, int]] | None:
    """Cells of a TileMapLayer tile_map_data PackedByteArray (Godot 4: a 2-byte format, then 12 bytes a cell:
    x, y, source, atlas x, atlas y, alternative as little-endian int16). None when it cannot be read."""
    import base64
    import struct
    match = re.fullmatch(r'\s*PackedByteArray\(\s*"([A-Za-z0-9+/=]*)"\s*\)\s*', raw or "")
    if not match:
        return None
    try:
        data = base64.b64decode(match.group(1), validate=True)
    except ValueError:
        return None
    if len(data) < 2 or (len(data) - 2) % 12:
        return None
    return {struct.unpack("<hhhhhh", data[2 + i * 12:14 + i * 12]) for i in range((len(data) - 2) // 12)}


def same_value(disk, editor) -> bool:
    if disk is _NULL:
        return editor is None
    editor = _entries_dict(editor)
    if isinstance(disk, bool) or isinstance(editor, bool):
        return disk == editor
    if isinstance(disk, (list, tuple)) and isinstance(editor, (list, tuple)):
        return len(disk) == len(editor) and all(same_value(a, b) for a, b in zip(disk, editor))
    if (isinstance(disk, (int, float)) and not isinstance(disk, bool)
            and isinstance(editor, (int, float)) and not isinstance(editor, bool)):
        return abs(float(disk) - float(editor)) <= 1e-3 * max(1.0, abs(float(disk)))
    if isinstance(disk, dict) and isinstance(editor, dict):
        return all(k in editor and same_value(v, editor[k]) for k, v in disk.items())
    return str(disk) == str(editor)



def _under_instance(rel: str, nodes: dict) -> bool:
    parts = rel.split("/")
    for i in range(1, len(parts)):
        info = nodes.get("/".join(parts[:i]))
        if info and info.get("instance"):
            return True
    return False


def _conn_tuple(signal: str, src: str, dst: str, method: str, root_name: str) -> tuple[str, str, str, str]:
    return (signal, rel_node_path(src, root_name), rel_node_path(dst, root_name), method)


def _abs_node(root_name: str, rel: str) -> str:
    return f"/{root_name}" if not rel else f"/{root_name}/{rel}"


def _resource_properties(data: dict) -> dict | None:
    """Properties of an inspected subresource.

    Godot AI's inspect returns {root, resources: [{id, type, properties}]}, after the server unwraps data.
    The offline stand-in returns {properties} directly.
    """
    props = data.get("properties")
    if isinstance(props, dict):
        return props
    resource = data.get("resource")
    if isinstance(resource, dict) and isinstance(resource.get("properties"), dict):
        return resource["properties"]
    resources = data.get("resources")
    if isinstance(resources, list):
        filled = [item for item in resources if isinstance(item, dict) and isinstance(item.get("properties"), dict)
                  and item["properties"]]
        chosen = filled or [item for item in resources if isinstance(item, dict) and isinstance(item.get("properties"), dict)]
        if chosen:
            return chosen[0]["properties"]
    return None


def _diag_error(item: dict) -> bool:
    kind = str(item.get("severity") or item.get("type") or "").lower()
    if kind in {"error", "parse_error"}:
        return True
    text = str(item.get("text") or item.get("message") or "")
    return text.startswith("Parse Error") or text.startswith("ERROR")


def _loss_note(record: dict) -> str:
    """What an overwrite save drops from the file. Empty when nothing unmatched was recorded."""
    diffs = list(record.get("diffs") or [])
    why = str(record.get("why") or "")
    skipped = list(record.get("skipped") or [])
    if not diffs and not why and not skipped:
        return ""
    blob = " ".join(diffs + skipped) + " " + why
    kinds: list[str] = []
    if "connection " in blob or "connection on disk" in blob or "connection in editor" in blob:
        kinds.append("connections")
    if "subresource" in blob:
        kinds.append("subresources")
    if "in editor only" in blob or "on disk only" in blob:
        kinds.append("nodes")
    if " groups:" in blob:
        kinds.append("groups")
    if "unparsed" in blob or "not compared" in why or skipped:
        kinds.append("values that could not be compared")
    if not kinds:
        kinds.append("values")
    parts = [f"game-editor: overwrite keeps the editor's scene and drops the disk side of {', '.join(kinds)}."]
    if diffs:
        listed = "; ".join(diffs[:8])
        if len(diffs) > 8:
            listed += f"; and {len(diffs) - 8} more"
        parts.append(listed + ".")
    if why:
        parts.append(f"Could not compare: {why}.")
    parts.append("Nothing on disk is kept for those.")
    return " ".join(parts)


def _did_line(name: str, op: str | None, keys: dict) -> str:
    if name == "run_game_code":
        code = " ".join(str(keys.get("code") or "").split())[:120]
        return f"game-editor did: run_game_code {code}".rstrip()
    if name == "batch_execute":
        commands = keys.get("commands") if isinstance(keys.get("commands"), list) else []
        bits = []
        for item in commands:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "command")
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            path = ""
            for key in ("path", "node_path", "parent_path", "player_path", "camera_path", "new_parent"):
                if isinstance(params.get(key), str) and params[key].strip():
                    path = params[key].strip()
                    break
            prop = params.get("property") if isinstance(params.get("property"), str) else ""
            if command == "create_node" and isinstance(params.get("name"), str) and params["name"].strip():
                # Name the node that was made, not only its parent.
                parent = path.rstrip("/") if path else ""
                path = f"{parent}/{params['name'].strip()}" if parent else params["name"].strip()
            bit = command + (f" {path}" if path else "") + (f" {prop}" if prop else "")
            bits.append(bit)
            if len(bits) >= 8:
                break
        extra = f" and {len(commands) - len(bits)} more" if len(commands) > len(bits) else ""
        listed = "; ".join(bits)
        return f"game-editor did: batch_execute {listed}{extra}".rstrip()
    target = ""
    for key in ("path", "node_path", "scene", "output"):
        if isinstance(keys.get(key), str) and keys[key].strip():
            target = keys[key].strip()
            break
    if name == "run_game":
        target = str(keys.get("mode") or target or "main")
    op_bit = f" {op}" if op and op not in (name, None) else ""
    return f"game-editor did: {name}{op_bit} {target}".rstrip()


# ---------------------------------------------------------------- test output

GUT_TOTALS = re.compile(r"^\s*Tests\s+(\d+)\s*$", re.M | re.I)
GUT_FAILS = re.compile(r"^\s*(?:Failing tests|Failures)\s+(\d+)\s*$", re.M | re.I)
GUT_PASSING = re.compile(r"^\s*Passing tests\s+(\d+)\s*$", re.M | re.I)
GDUNIT_TOTALS = re.compile(r"Overall Summary:\s*(\d+)\s*test cases?\s*\|\s*(\d+)\s*errors?\s*\|\s*(\d+)\s*failures?",
                           re.I)


def cli_count_note(output: str, tests_run: int | None) -> str | None:
    """Why passed is false when the summary line is missing. None when a count was read."""
    if tests_run is not None:
        return None
    note = "the test count was not found in the output, so passed is false"
    if "have not been imported" in output:
        note += (". GUT says its class names are not imported. "
                 "Run `godot --headless --import` in the project, then run_tests again.")
    return note


def read_test_counts(framework: str, output: str) -> tuple[int | None, int | None]:
    """(tests run, tests failed or errored) from GUT or gdUnit4 output; None when not found."""
    if framework == "gut":
        totals = GUT_TOTALS.findall(output)
        fails = GUT_FAILS.findall(output)
        passing = GUT_PASSING.findall(output)
        run = int(totals[-1]) if totals else None
        failed = int(fails[-1]) if fails else (run - int(passing[-1]) if run is not None and passing else None)
        return run, failed
    m = GDUNIT_TOTALS.findall(output)
    if m:
        cases, errors, failures = (int(x) for x in m[-1])
        return cases, errors + failures
    return None, None


# ---------------------------------------------------------------- play session and export

PLAY_MAX_STEPS = 16
PLAY_MAX_STEP_SECONDS = 2.0
PLAY_MAX_TOTAL_SECONDS = 12.0
PLAY_SAMPLE_TIMEOUT = 8.0
EXPORT_TIMEOUT = 120.0
MOVE_ACTIONS = {"ui_left", "ui_right", "ui_up", "ui_down", "move_left", "move_right", "move_up", "move_down"}
ERROR_LINE = re.compile(r"(?i)(script error|parse error|parser error|\berror:)")
# A Godot runtime line names the script as "level.gd:7 @ func():". A null instance and a missing node are
# the same kind of failure. Classifying only the word "error:" left these as kind log, with no file or line.
RUNTIME_AT = re.compile(r"(?:res://)?[\w./-]+\.(?:gd|cs):\d+\s*@")
# Godot's script reload prints the location on the next line: at: GDScript::reload (res://scripts/boom.gd:3)
GD_AT = re.compile(r"at:\s+\S.*\((?:res://)?[\w./-]+\.(?:gd|cs):\d+\)")


def is_error_line(line: str) -> bool:
    """True when a log line is a script, parse or runtime error, not a certificate or engine note."""
    if not isinstance(line, str) or not line:
        return False
    if ERROR_LINE.search(line) or RUNTIME_AT.search(line) or GD_AT.search(line):
        return True
    low = line.lower()
    return "on a null instance" in low or "node not found" in low
ACTION_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}")
KEY_NAME = re.compile(r"[A-Za-z0-9_]{1,20}")
WATCH_PATH = re.compile(r"/[A-Za-z0-9_./]{1,120}")
TEMPLATE_FILES = {
    "web": ("version.txt", "web_nothreads_debug.zip", "web_nothreads_release.zip", "web_debug.zip", "web_release.zip"),
    "macos": ("version.txt", "macos.zip"),
    "android": ("version.txt", "android_debug.apk", "android_release.apk", "android_source.zip"),
    "ios": ("version.txt", "ios.zip"),
}
# Published release asset. This table is not refreshed over the network.
TEMPLATE_PACKAGES = {
    "4.7.2": {
        "file": "Godot_v4.7.2-stable_export_templates.tpz",
        "source": "https://github.com/godotengine/godot-builds/releases/download/4.7.2-stable/"
                  "Godot_v4.7.2-stable_export_templates.tpz",
        "bytes": 1281349702,
        "sha256": "f298490b8d44d934be425a5a65a51bf15f422428b229a06a6e11d9ffea248011",
    },
}
EXPORT_PLATFORM = {"web": "Web", "macos": "macOS", "android": "Android", "ios": "iOS"}
DEFAULT_OUTPUT = {"web": "build/web/index.html", "macos": "build/macos/Game.app",
                  "android": "build/android/game.apk", "ios": "build/ios/game"}
# Godot property usage bits. An instance override is kept only with STORAGE and without NO_INSTANCE_STATE.
USAGE_STORAGE = 2
USAGE_NO_INSTANCE_STATE = 1024
OUTPUT_BLOCKED = {"project.godot", "export_presets.cfg", "export_credentials.cfg"}


def _as_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _point(props: dict) -> tuple[float, float] | None:
    if not isinstance(props, dict):
        return None
    for key in ("global_position", "position"):
        raw = props.get(key)
        if isinstance(raw, dict) and _as_float(raw.get("x")) is not None and _as_float(raw.get("y")) is not None:
            return _as_float(raw["x"]), _as_float(raw["y"])
    return None


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _score_of(nodes: dict) -> int | None:
    labeled = None
    prop = None
    for info in nodes.values():
        if not isinstance(info, dict):
            continue
        props = info.get("props") if isinstance(info.get("props"), dict) else {}
        text = props.get("text")
        if isinstance(text, str):
            match = re.search(r"(?i)score:\s*(-?\d+)", text)
            if match:
                labeled = int(match.group(1))
        if prop is None and "score" in props and _as_float(props.get("score")) is not None:
            prop = int(props["score"])
    return labeled if labeled is not None else prop


def _step_line(index: int, step: dict) -> str:
    seconds = step.get("seconds", 0)
    if step.get("op") == "wait":
        return f"{index}. Wait {seconds}s."
    verb = "Press" if step.get("pressed", True) else "Release"
    if step.get("op") == "key":
        return f"{index}. {verb} key {step.get('key')} for {seconds}s."
    return f"{index}. {verb} action {step.get('action')} for {seconds}s."


def repro_steps(steps: list[dict], upto: int | None = None) -> list[str]:
    chosen = steps if upto is None else steps[:upto + 1]
    return ["Run the main scene with autosave false."] + [_step_line(i, step) for i, step in enumerate(chosen, start=1)]


def _position_has_z(props: dict) -> bool:
    if not isinstance(props, dict):
        return False
    for key in ("global_position", "position"):
        raw = props.get(key)
        if isinstance(raw, dict) and "z" in raw:
            return True
    return False


def _continued_down(points: list[tuple[float, float]], fall_dy: float, has_z: bool) -> str | None:
    """Downward motion that keeps going, by at least fall_dy.

    Godot 2D down is +y, so a jump (y decreases, or y goes back up) is not a fall.
    A position that carries z is 3D, where down is -y. One reversal cancels it.
    Sideways movement is named and does not cancel the fall.
    """
    if len(points) < 2:
        return None
    ys = [point[1] for point in points]
    if has_z:
        if any(later > earlier for earlier, later in zip(ys, ys[1:])):
            return None
        drop = ys[0] - ys[-1]
        direction = "decreased"
    else:
        if any(later < earlier for earlier, later in zip(ys, ys[1:])):
            return None
        drop = ys[-1] - ys[0]
        direction = "increased"
    if drop < fall_dy:
        return None
    dx = points[-1][0] - points[0][0]
    return (f"Watched node y {direction} from {round(ys[0], 1)} to {round(ys[-1], 1)} "
            f"while x moved {round(dx, 1)}.")


def _most_moved(before: dict, after: dict) -> tuple[str, float] | None:
    """The watched node that moved the farthest. Watch order is not an actor."""
    best: tuple[str, float] | None = None
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    for path, info in after.items():
        if not isinstance(info, dict) or not info.get("found", True):
            continue
        earlier = before.get(path)
        if not isinstance(earlier, dict) or not earlier.get("found", True):
            continue
        start = _point(earlier.get("props") if isinstance(earlier.get("props"), dict) else {})
        end = _point(info.get("props") if isinstance(info.get("props"), dict) else {})
        if start is None or end is None:
            continue
        distance = _dist(start, end)
        if best is None or distance > best[1]:
            best = (path, distance)
    return best


def _score_condition(goal: str) -> tuple[str, int] | None:
    """(op, number) when goal states a score comparison. None when it does not."""
    match = re.search(r"(?i)\bscore\s*(>=|<=|==|=|>|<)\s*(-?\d+)\b", goal)
    if match:
        op = "==" if match.group(1) == "=" else match.group(1)
        return op, int(match.group(2))
    match = re.search(r"(?i)\bscore\s+(?:is\s+)?(?:at least|reaches)\s+(-?\d+)\b", goal)
    if match:
        return ">=", int(match.group(1))
    match = re.search(r"(?i)\b(?:at least|reaches)\s+(?:a\s+)?score\s+(?:of\s+)?(-?\d+)\b", goal)
    if match:
        return ">=", int(match.group(1))
    match = re.search(r"(?i)\bscore\s+(?:equals|equal to|is)\s+(-?\d+)\b", goal)
    if match:
        return "==", int(match.group(1))
    return None


def _score_holds(score: int, op: str, number: int) -> bool:
    if op == "==":
        return score == number
    if op == ">=":
        return score >= number
    if op == "<=":
        return score <= number
    if op == ">":
        return score > number
    return score < number


def judge_goal(goal: str, samples: list[dict], watched: bool) -> tuple[bool, bool | None, str]:
    """(checked, met, detail). Checked only for a score comparison or a /Path in the goal.

    A goal with neither is not judged, so the report adds no finding. met is None then.
    """
    condition = _score_condition(goal)
    paths = []
    for path in WATCH_PATH.findall(goal):
        if path not in paths:
            paths.append(path)
    if condition is None and not paths:
        return False, None, ""
    last: dict | None = None
    for sample in samples:
        if sample.get("timed_out"):
            break
        last = sample
    nodes = (last or {}).get("nodes") if isinstance(last, dict) else None
    nodes = nodes if isinstance(nodes, dict) else {}
    gaps = []
    met = True
    if condition is not None:
        op, number = condition
        score = _score_of(nodes) if watched else None
        if score is None or not _score_holds(score, op, number):
            met = False
            shown = "missing" if score is None else str(score)
            gap = f"score is {shown}; the goal wants score {op} {number}"
            if score is None:
                gap += (". No watched node had a score property or a text with score: N; watch the node whose "
                        "script has var score (such as /root/Main) or the Label that shows it")
            gaps.append(gap)
    for path in paths:
        info = nodes.get(path)
        if not isinstance(info, dict) or not info.get("found", True):
            met = False
            gaps.append(f"{path} was not in the last sample")
    return True, met, "; ".join(gaps)


def build_play_report(goal: str, steps: list[dict], samples: list[dict], screenshot: dict, stopped: bool,
                      idle_seconds: float, fall_dy: float, watched: bool = True, start_nodes: dict | None = None,
                      frames_before: int | None = None, require_frames: bool = False,
                      session_live: bool | None = None) -> dict:
    """Turn samples into findings. Pure: no editor, no clock, no files. start_nodes is where the watched
    nodes were before the first step; with it the first step is judged for idle like the others."""
    findings: list[dict] = []
    seen_errors: set[str] = set()
    for sample in samples:
        for line in sample.get("errors") or []:
            if line and line not in seen_errors:
                seen_errors.add(line)
                # A script error, a parse error, a line that says ERROR:, a "file.gd:7 @" runtime line,
                # a null instance, or a missing node fails the run. Other lines are quoted and do not.
                kind = "error" if is_error_line(line) else "log"
                findings.append({"kind": kind, "step": sample["step"], "detail": line,
                                 "repro": repro_steps(steps, sample["step"])})
        if sample.get("timed_out"):
            findings.append({"kind": "stall", "step": sample["step"],
                             "detail": "The game did not answer. The frame counter stopped, which is what an "
                                       "infinite loop or a frozen main thread looks like.",
                             "repro": repro_steps(steps, sample["step"])})
            break
    if not watched:
        findings.append({"kind": "unwatched", "step": 0,
                         "detail": "Position was not watched. This report does not say that nothing fell. "
                                   "Pass watch with the node paths to follow.",
                         "repro": repro_steps(steps)})
    checked, met, goal_detail = judge_goal(goal, samples, watched)
    goal_failed = bool(checked and watched and met is False)
    tracks: dict[str, list[tuple[int, tuple[float, float] | None, bool, bool]]] = {}
    if watched:
        for sample in samples:
            if sample.get("timed_out"):
                break
            nodes = sample.get("nodes") if isinstance(sample.get("nodes"), dict) else {}
            step_no = sample["step"] if isinstance(sample.get("step"), int) else 0
            for path, info in nodes.items():
                if not isinstance(info, dict):
                    continue
                props = info.get("props") if isinstance(info.get("props"), dict) else {}
                tracks.setdefault(path, []).append(
                    (step_no, _point(props), _position_has_z(props), bool(info.get("found", True))))
    if goal_failed:
        for path, events in tracks.items():
            seen = False
            reported = False
            points: list[tuple[float, float]] = []
            has_z = False
            for step_no, point, point_z, found in events:
                has_z = has_z or point_z
                if found and point is not None:
                    seen = True
                    points.append(point)
                    continue
                if seen and not reported:
                    reported = True
                    findings.append({"kind": "fall", "step": step_no,
                                     "detail": f"{path} was in the scene and then was gone.",
                                     "repro": repro_steps(steps, step_no)})
            if reported:
                continue
            detail = _continued_down(points, fall_dy, has_z)
            if detail:
                findings.append({"kind": "fall", "step": events[-1][0], "detail": detail,
                                 "repro": repro_steps(steps, events[-1][0])})
    for prev, sample in zip(samples, samples[1:]):
        if prev.get("timed_out") or sample.get("timed_out"):
            break
        before, after = prev.get("process_frames"), sample.get("process_frames")
        elapsed = float(sample.get("seconds") or 0)
        if not isinstance(before, int) or not isinstance(after, int) or elapsed < 0.3:
            continue
        if after <= before:
            seen_error = any(
                is_error_line(line)
                for item in samples[:samples.index(sample) + 1]
                if isinstance(item, dict)
                for line in (item.get("errors") or [])
                if isinstance(line, str))
            findings.append({"kind": "stall", "step": sample["step"],
                             "detail": f"The frame counter stayed at {after} for {elapsed}s." + (
                                 " A script error came first: Godot's debugger pauses a game run from the editor "
                                 "on an error, so fix that error before reading this as a hang." if seen_error else ""),
                             "repro": repro_steps(steps, sample["step"])})
            break
        if elapsed >= 0.4 and (after - before) / elapsed < 15:
            findings.append({"kind": "frame_drop", "step": sample["step"],
                             "detail": f"Frames advanced {after - before} in {elapsed}s "
                                       f"({round((after - before) / elapsed, 1)} per second).",
                             "repro": repro_steps(steps, sample["step"])})
    stalled_steps = {item["step"] for item in findings if item["kind"] in ("stall", "fall")}
    pairs = list(zip(samples, samples[1:]))
    if start_nodes and samples:
        pairs.insert(0, ({"nodes": start_nodes}, samples[0]))
    for prev, sample in pairs:
        step = steps[sample["step"]] if sample["step"] < len(steps) else {}
        if sample.get("timed_out") or sample["step"] in stalled_steps:
            continue
        if step.get("op") != "action" or not step.get("pressed", True):
            continue
        if step.get("action") not in MOVE_ACTIONS or float(sample.get("seconds") or 0) < idle_seconds:
            continue
        if not watched:
            continue
        moved = _most_moved(prev.get("nodes") or {}, sample.get("nodes") or {})
        if moved is None:
            continue
        path, distance = moved
        if distance < 8:
            findings.append({"kind": "idle", "step": sample["step"],
                             "detail": f"Action {step.get('action')} was held for {sample.get('seconds')}s and "
                                       f"{path} barely moved.",
                             "repro": repro_steps(steps, sample["step"])})
    if goal_failed:
        last_step = 0
        for sample in samples:
            if sample.get("timed_out"):
                break
            if isinstance(sample.get("step"), int):
                last_step = sample["step"]
        findings.append({"kind": "goal", "step": last_step,
                         "detail": goal_detail or "The goal was not met.",
                         "repro": repro_steps(steps)})
    by_step = {sample.get("step"): sample for sample in samples if isinstance(sample, dict)}
    last_frames = None
    for sample in samples:
        if isinstance(sample.get("process_frames"), int):
            last_frames = sample["process_frames"]
    for item in findings:
        sample = by_step.get(item.get("step")) or {}
        frames = sample.get("process_frames")
        item["evidence"] = {
            "after_step": (item["step"] + 1) if isinstance(item.get("step"), int) else None,
            "frame": frames if isinstance(frames, int) else None,
            "last_answered_frame": None if isinstance(frames, int) else last_frames,
            "nodes": sample.get("nodes") or {},
            "log": list(sample.get("errors") or []),
        }
    kinds = {item["kind"] for item in findings}
    if not watched:
        ok, failure = False, "position_unwatched"
    elif goal_failed:
        ok, failure = False, "goal_missed"
    else:
        failure = next((kind for kind in ("error", "fall", "stall", "uncollected") if kind in kinds), None)
        if failure is None and not checked:
            # A goal that was not judged is not a pass. Quote the findings and say what was not checked.
            failure = "goal_not_checked"
        ok = failure is None
    oracle_before = frames_before if isinstance(frames_before, int) else None
    oracle_after = last_frames if isinstance(last_frames, int) else None
    advanced = isinstance(oracle_before, int) and isinstance(oracle_after, int) and oracle_after > oracle_before
    if require_frames:
        if session_live is False:
            ok, failure = False, "not_playing"
        elif isinstance(oracle_before, int) and isinstance(oracle_after, int) and not advanced:
            ok, failure = False, "frames_not_advanced"
    return {"ok": ok, "failure": failure, "position_watched": watched, "goal_checked": checked, "goal_met": met,
            "oracle": {"playing": None if session_live is None else bool(session_live),
                       "frames_before": oracle_before, "frames_after": oracle_after, "advanced": advanced},
            "truncated": False, "findings_total": len(findings), "samples_total": len(samples),
            "source": "godot game session", "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "goal": goal, "stopped": stopped, "screenshot": screenshot, "findings": findings,
            "repro": repro_steps(steps), "samples": samples}


def parse_play(keys: dict) -> tuple[dict | None, str]:
    goal = keys.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 240:
        return None, ("game-editor: play_session needs goal, one short sentence of what to look for "
                      "(240 characters or fewer). Nothing was run.")
    steps = keys.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= PLAY_MAX_STEPS:
        return None, (f"game-editor: play_session needs steps, a list of 1 to {PLAY_MAX_STEPS} "
                      "{op, action or key, pressed, seconds}. Nothing was run.")
    total = 0.0
    clean = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return None, f"game-editor: play_session step {index} must be an object. Nothing was run."
        op = step.get("op")
        if op not in ("action", "key", "wait"):
            return None, (f"game-editor: play_session step {index} op must be action, key or wait. "
                          "Mouse, gamepad and sequence are other game_do ops, not play_session. Nothing was run.")
        seconds = step.get("seconds", 0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= float(seconds) <= PLAY_MAX_STEP_SECONDS:
            return None, (f"game-editor: play_session step {index} seconds must be a number from 0 to "
                          f"{PLAY_MAX_STEP_SECONDS}. Nothing was run.")
        pressed = step.get("pressed", True)
        if not isinstance(pressed, bool):
            return None, f"game-editor: play_session step {index} pressed must be true or false. Nothing was run."
        item = {"op": op, "seconds": float(seconds), "pressed": pressed}
        if op == "action":
            action = step.get("action")
            if not isinstance(action, str) or not ACTION_NAME.fullmatch(action):
                return None, (f"game-editor: play_session step {index} needs action, an input-map name such as "
                              "ui_right or jump. Nothing was run.")
            item["action"] = action
        elif op == "key":
            key = step.get("key")
            if not isinstance(key, str) or not KEY_NAME.fullmatch(key):
                return None, f"game-editor: play_session step {index} needs key, such as Space or A. Nothing was run."
            item["key"] = key
        total += float(seconds)
        clean.append(item)
    if total > PLAY_MAX_TOTAL_SECONDS:
        return None, (f"game-editor: play_session steps add up to more than {PLAY_MAX_TOTAL_SECONDS} seconds. "
                      "Shorten them. Nothing was run.")
    watch = keys.get("watch") or []
    if (not isinstance(watch, list) or len(watch) > 8 or
            not all(isinstance(item, str) and WATCH_PATH.fullmatch(item) and ".." not in item for item in watch)):
        return None, ("game-editor: play_session watch takes up to 8 node paths starting with /, such as "
                      "/root/Main/Player. Nothing was run.")
    mode = keys.get("mode") or "main"
    if mode not in ("main", "current", "custom"):
        return None, "game-editor: play_session mode must be main, current or custom. Nothing was run."
    if mode == "custom" and not (isinstance(keys.get("scene"), str) and keys["scene"].strip().lower().startswith("res://")):
        return None, "game-editor: play_session mode custom needs scene, a res:// scene path. Nothing was run."
    autosave = keys.get("autosave", False)
    if not isinstance(autosave, bool):
        return None, "game-editor: play_session autosave must be true or false. Nothing was run."
    idle = keys.get("idle_seconds", 1.2)
    fall_dy = keys.get("fall_dy", 120)
    if isinstance(idle, bool) or not isinstance(idle, (int, float)) or not 0.4 <= float(idle) <= 3:
        return None, "game-editor: play_session idle_seconds must be a number from 0.4 to 3. Nothing was run."
    if isinstance(fall_dy, bool) or not isinstance(fall_dy, (int, float)) or not 40 <= float(fall_dy) <= 2000:
        return None, "game-editor: play_session fall_dy must be a number from 40 to 2000. Nothing was run."
    return {"goal": goal.strip(), "steps": clean, "watch": list(watch), "mode": mode, "autosave": autosave,
            "idle_seconds": float(idle), "fall_dy": float(fall_dy),
            "scene": keys.get("scene") if mode == "custom" else ""}, ""


_SCRIPT_PATH = re.compile(r"(?i)(?:res://)?[A-Za-z0-9_./-]+\.(?:gd|cs)\Z")


def _log_line_number(value) -> int:
    """A logs_read line number is a positive integer. A JSON number can arrive as 3.0."""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        number = int(value)
        return number if number > 0 and number == value else 0
    text = str(value).strip()
    if text.isdigit():
        number = int(text)
        return number if number > 0 else 0
    return 0


def _with_script_location(text: str, item: dict) -> str:
    """logs_read keeps the script path and line beside the message. Godot's Output prints that pair as
    at: GDScript::reload (res://scripts/file.gd:3). The message text alone has neither."""
    path = str(item.get("path") or "").strip()
    line = _log_line_number(item.get("line"))
    if not path or line <= 0 or not _SCRIPT_PATH.search(path):
        return text
    where = f"{path}:{line}"
    if where in text:
        return text
    return f"{text} ({where})" if text else where


def _collect_log_errors(data: dict, limit: int, width: int, merge_following_at: bool) -> list[str]:
    """Error lines from a logs_read body. A script path and line on the entry stay with the message."""
    merged: list[str] = []
    for item in (data.get("lines") or data.get("entries") or []):
        if isinstance(item, str):
            text, level = item, ""
        elif isinstance(item, dict):
            text, level = str(item.get("text") or ""), str(item.get("level") or "")
        else:
            continue
        flat = " ".join(text.split())
        if not flat or "addons/godot_ai" in flat or "[godot_ai" in flat:
            continue
        if merge_following_at and GD_AT.search(flat) and merged:
            merged[-1] = (merged[-1] + " | " + flat)[:width]
            continue
        if isinstance(item, dict):
            flat = _with_script_location(flat, item)
        if level.lower() == "error" or is_error_line(flat):
            merged.append(flat[:width])
    script = [line for line in merged if re.search(r"\.(?:gd|cs):\d+", line)]
    other = [line for line in merged if line not in script]
    return (script + other)[:limit]


def _dead_run_errors(data: dict) -> list[str]:
    """Error lines from a game that never stayed up. A following at: (file.gd:N) line stays with the message."""
    return _collect_log_errors(data, 3, 400, True)


def _log_errors(data: dict) -> list[str]:
    return _collect_log_errors(data, 3, 240, False)


def _log_items(data: dict) -> list:
    """The log entries a logs_read reply carried, in order."""
    if not isinstance(data, dict):
        return []
    raw = data.get("lines")
    if not isinstance(raw, list):
        raw = data.get("entries")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, (str, dict))]


def _log_key(item) -> str:
    if isinstance(item, str):
        return item
    return "\n".join((
        str(item.get("text") or ""),
        str(item.get("level") or ""),
        str(item.get("path") or ""),
        str(item.get("line") or ""),
    ))


def _fresh_log_items(before: list, after: list) -> list:
    """Entries that arrived after the earlier read.

    logs_read returns the tail of one buffer. A later read is that tail plus new lines, or the same
    tail with old lines dropped from the front. Lines already in the earlier read are not this run.
    """
    if not before:
        return list(after)
    before_keys = [_log_key(item) for item in before]
    after_keys = [_log_key(item) for item in after]
    if after_keys[:len(before_keys)] == before_keys:
        return list(after[len(before_keys):])
    overlap = min(len(before_keys), len(after_keys))
    while overlap > 0 and before_keys[-overlap:] != after_keys[:overlap]:
        overlap -= 1
    return list(after[overlap:])


def _kept_props(data: dict) -> dict:
    raw = data.get("properties")
    if isinstance(raw, list):
        props = {item.get("name"): item.get("value") for item in raw if isinstance(item, dict)}
    elif isinstance(raw, dict):
        props = raw
    else:
        props = {}
    return {key: props.get(key) for key in ("position", "global_position", "text", "score") if key in props}


def _frames_from_debug(data: dict) -> int | None:
    sources = [data]
    if isinstance(data.get("state"), dict):
        sources.append(data["state"])
    for src in sources:
        for key in ("process_frames", "frames_drawn", "process_ticks"):
            number = _as_float(src.get(key))
            if number is not None:
                return int(number)
    return None


def parse_export(keys: dict) -> tuple[dict | None, str]:
    platform = keys.get("platform")
    if platform not in EXPORT_PLATFORM:
        return None, ("game-editor: export_game platform must be web, macos, android or ios. Nothing was exported.")
    debug = keys.get("debug", True)
    serve = keys.get("serve", False)
    overwrite = keys.get("overwrite", False)
    for name, value in (("debug", debug), ("serve", serve), ("overwrite", overwrite)):
        if not isinstance(value, bool):
            return None, f"game-editor: export_game {name} must be true or false. Nothing was exported."
    if serve and platform != "web":
        return None, "game-editor: export_game serve is only for platform web. Nothing was exported."
    output = keys.get("output") or DEFAULT_OUTPUT[platform]
    if not isinstance(output, str) or not output.strip() or len(output) > 240:
        return None, "game-editor: export_game output must be a path inside the project. Nothing was exported."
    sdk = keys.get("android_sdk")
    if sdk is not None and not isinstance(sdk, str):
        return None, "game-editor: export_game android_sdk must be a path. Nothing was exported."
    low = output.strip().lower()
    if "://" in low and not low.startswith("res://"):
        return None, "game-editor: export_game output must be a path inside the project. Nothing was exported."
    return {"platform": platform, "debug": debug, "serve": serve, "overwrite": overwrite,
            "output": output.strip(), "android_sdk": sdk}, ""


def template_need(version: str | None, template_root: Path, platform: str) -> dict | None:
    """None when every file for this platform is already installed. Never downloads."""
    folder = f"{version}.stable" if version else ""
    directory = template_root / folder if folder else template_root
    missing = [name for name in TEMPLATE_FILES[platform] if not (directory / name).is_file()]
    if folder and not missing:
        return None
    package = TEMPLATE_PACKAGES.get(version or "", {})
    if version and not package:
        source = (f"https://github.com/godotengine/godot-builds/releases/download/{version}-stable/"
                  f"Godot_v{version}-stable_export_templates.tpz")
        filename = f"Godot_v{version}-stable_export_templates.tpz"
    else:
        source = package.get("source") or "https://github.com/godotengine/godot-builds/releases"
        filename = package.get("file")
    return {"ok": False, "failure": "export_templates_missing", "truncated": False, "platform": platform,
            "files_total": len(missing) if folder else len(TEMPLATE_FILES[platform]),
            "needed_files": missing if folder else list(TEMPLATE_FILES[platform]),
            "version_dir": folder or None, "downloaded": False,
            "acquire": {"file": filename, "source": source, "bytes": package.get("bytes"),
                        "sha256": package.get("sha256")},
            "note": "Install the export templates for this Godot version into the editor's export_templates "
                    "folder (the version.stable directory), then call export_game again. This plugin does not "
                    "download them. Nothing was written."}


def godot_version_text(binary: str) -> str | None:
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = ((proc.stdout or b"") + b"\n" + (proc.stderr or b"")).decode("utf-8", "replace")
    match = re.search(r"(\d+\.\d+\.\d+)\.stable", text)
    return match.group(1) if match else None


def _preset_name(text: str, platform_label: str) -> str | None:
    for block in re.split(r"(?=^\[preset\.\d+\]\s*$)", text, flags=re.M):
        if not block.startswith("[preset."):
            continue
        options_at = block.find(".options]")
        body = block if options_at == -1 else block[:options_at]
        platform = re.search(r'^platform="([^"]*)"', body, re.M)
        name = re.search(r'^name="([^"]*)"', body, re.M)
        if platform and platform.group(1) == platform_label and name and name.group(1) and "\n" not in name.group(1):
            return name.group(1)
    return None


def preset_text(index: int, platform: str) -> str:
    label = EXPORT_PLATFORM[platform]
    options = ['custom_template/debug=""', 'custom_template/release=""']
    if platform == "android":
        # The prebuilt apk. Gradle would download a build; this plugin does not.
        options.append("gradle_build/use_gradle_build=false")
        options.append("permissions/internet=false")
    lines = [
        f"[preset.{index}]", "", f'name="{label}"', f'platform="{label}"', "runnable=true",
        "dedicated_server=false", 'custom_features=""', 'export_filter="all_resources"', 'include_filter=""',
        'exclude_filter=""', 'export_path=""', 'encryption_include_filters=""', 'encryption_exclude_filters=""',
        "encrypt_pck=false", "encrypt_directory=false", "script_export_mode=2", "", f"[preset.{index}.options]", "",
        *options, "",
    ]
    return "\n".join(lines)


def ensure_preset(root: Path, platform: str) -> tuple[str | None, bool, str]:
    """Return (preset name, wrote a new preset, error). Does not touch signing files."""
    path = root / "export_presets.cfg"
    label = EXPORT_PLATFORM[platform]
    existing = ""
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return None, False, (f"game-editor: export_game could not read export_presets.cfg "
                                 f"({type(exc).__name__}). Nothing was exported.")
        found = _preset_name(existing, label)
        if found:
            return found, False, ""
    indexes = [int(n) for n in re.findall(r"^\[preset\.(\d+)\]\s*$", existing, re.M)]
    index = (max(indexes) + 1) if indexes else 0
    addition = preset_text(index, platform)
    try:
        path.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n") + addition,
                        encoding="utf-8")
    except OSError as exc:
        return None, False, (f"game-editor: export_game could not write export_presets.cfg "
                             f"({type(exc).__name__}). Nothing was exported.")
    return label, True, ""


def output_path(root: Path, raw: str) -> tuple[Path | None, str]:
    """A build path inside the project, or an error. Refuses source files and secret names."""
    text = raw.strip()
    if text.lower().startswith("res://"):
        parts = _path_parts(text[6:])
        if parts is None or _bad_parts(parts):
            return None, (f"game-editor: export_game refused {raw}: the output must stay inside the project and "
                          "must not be a hidden or key-like file. Nothing was exported.")
        dest = root.joinpath(*parts)
    else:
        dest = Path(text)
        if not dest.is_absolute():
            dest = root / dest
    try:
        dest = dest.resolve()
        root_real = root.resolve()
    except OSError:
        return None, "game-editor: export_game could not resolve the output path. Nothing was exported."
    if dest != root_real and root_real not in dest.parents:
        return None, "game-editor: export_game refused the output path: it is outside the project. Nothing was exported."
    if dest == root_real:
        return None, "game-editor: export_game refused the output path: it is the project folder itself. Nothing was exported."
    try:
        rel_parts = dest.relative_to(root_real).parts
    except ValueError:
        return None, "game-editor: export_game refused the output path: it is outside the project. Nothing was exported."
    if any(part.startswith(".") or secret_like(part, is_folder=i < len(rel_parts) - 1) for i, part in enumerate(rel_parts)):
        return None, "game-editor: export_game refused the output path: a hidden or key-like name is in it. Nothing was exported."
    if _clean(dest.name) in OUTPUT_BLOCKED or _source_export_name(dest.name):
        return None, (f"game-editor: export_game refused {dest.name}: export writes a build, not a source or "
                      "signing file. Nothing was exported.")
    return dest, ""


def _source_export_name(name: str) -> bool:
    low = name.lower()
    return low.endswith(_SOURCE_EXPORT_SUFFIXES)


def export_neighbor_paths(dest: Path) -> list[Path]:
    """The export file itself and every sibling Godot would write beside it (same stem)."""
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key not in seen:
            seen.add(key)
            found.append(path)

    if dest.exists():
        add(dest)
    prefix = dest.stem + "."
    try:
        children = list(dest.parent.iterdir()) if dest.parent.is_dir() else []
    except OSError:
        children = []
    for child in children:
        if child.name.startswith(prefix):
            add(child)
    return found


def neighbor_refusal(dest: Path, overwrite: bool) -> str:
    """Refuse a source file beside the build, and an existing neighbor unless overwrite is true."""
    neighbors = export_neighbor_paths(dest)
    sources = [path.name for path in neighbors if _source_export_name(path.name)]
    if sources:
        return (f"game-editor: export_game refused {', '.join(sources)}: export writes a build, not a source or "
                "signing file. Nothing was exported.")
    if not overwrite:
        existing = [path.name for path in neighbors if path.exists()]
        if existing:
            return (f"game-editor: export_game refused {', '.join(existing)}: it already exists. Pass "
                    "overwrite: true to replace it. Nothing was exported.")
    return ""


def export_plan_note(root: Path, keys: dict) -> str:
    """Files an export would replace, and whether a preset would be added. Does not write."""
    parsed, err = parse_export(keys)
    if parsed is None:
        return err
    dest, dest_err = output_path(root, parsed["output"])
    if dest is None:
        return dest_err
    names = ", ".join(path.name for path in export_neighbor_paths(dest)) or "none"
    label = EXPORT_PLATFORM[parsed["platform"]]
    preset_path = root / "export_presets.cfg"
    existing = ""
    if preset_path.is_file():
        try:
            existing = preset_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    found = _preset_name(existing, label) if existing else None
    if found:
        preset = (f"export_presets.cfg already has preset {found} and is not rewritten. "
                  "Signing keys are not copied.")
    else:
        preset = f"export_presets.cfg will gain a {label} preset. Signing keys are not copied."
    return f"output: {parsed['output']}. Files that already exist beside it: {names}. {preset}"


_PRESET_KEY = re.compile(
    r'(script_encryption_key\s*=\s*)(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\S+)',
    re.IGNORECASE)
_PRESET_KEY_ESC = re.compile(
    r'(script_encryption_key\s*=\s*\\+")(?:\\.|[^"\\])*',
    re.IGNORECASE)


_PRESET_PASSWORD = re.compile(
    r'((?:^|\n)[^\n=]*?(?:password|keystore/[A-Za-z0-9_./+-]*)\s*=\s*)'
    r'(?:"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|\S+)',
    re.IGNORECASE)


def redact_preset_text(text: str) -> str:
    """Blank script_encryption_key, password and keystore/ values. Does not change the file on disk."""
    text = _PRESET_KEY.sub(r'\1""', text)
    text = _PRESET_KEY_ESC.sub(r'\1', text)
    return _PRESET_PASSWORD.sub(r'\1""', text)


def _redact_preset_keys(res: dict) -> None:
    content = res.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            item["text"] = redact_preset_text(item["text"])


def class_usage(class_name: str | None, prop: str) -> int | None:
    """Usage bits from the ClassDB table, walking parents. None when the class or property is unknown.

    Unknown means the reply listed an editor-visible name ClassDB does not drop: an @export, or a stored property.
    """
    if not isinstance(class_name, str) or not class_name or not isinstance(prop, str):
        return None
    seen: set[str] = set()
    cur = class_name
    while cur and cur not in seen:
        seen.add(cur)
        table = _CLASS_DROP.get(cur)
        if isinstance(table, dict) and isinstance(table.get(prop), int) and not isinstance(table.get(prop), bool):
            return table[prop]
        parent = _CLASS_PARENTS.get(cur)
        cur = parent if isinstance(parent, str) else ""
    return None


# ClassDB marks these with no STORAGE bit. Godot 4.7.2 still writes a stored property when they
# are set: Node3D position, rotation and scale become transform; Sprite frame_coords becomes frame;
# Control position and size become the four offsets and keep an existing size.
# Refusing them would drop a change the file keeps.
_PERSIST_AS_OTHER = {
    ("Node3D", "position"),
    ("Node3D", "rotation"),
    ("Node3D", "scale"),
    ("Node3D", "basis"),
    ("Node3D", "quaternion"),
    ("Sprite2D", "frame_coords"),
    ("Sprite3D", "frame_coords"),
    ("Control", "position"),
    ("Control", "size"),
}


def persists_as_other(class_name: str | None, prop: str) -> bool:
    seen: set[str] = set()
    cur = class_name if isinstance(class_name, str) else ""
    while cur and cur not in seen:
        if (cur, prop) in _PERSIST_AS_OTHER:
            return True
        seen.add(cur)
        parent = _CLASS_PARENTS.get(cur)
        cur = parent if isinstance(parent, str) else ""
    return False


def is_control(class_name: str | None) -> bool:
    seen: set[str] = set()
    cur = class_name if isinstance(class_name, str) else ""
    while cur and cur not in seen:
        if cur == "Control":
            return True
        seen.add(cur)
        parent = _CLASS_PARENTS.get(cur)
        cur = parent if isinstance(parent, str) else ""
    return False


def ports_from_settings(text: str) -> tuple[int, int] | None:
    """(http, ws) from a Godot editor_settings tres, in Godot AI 4.3.0's order, or None when the file sets none.

    godot_ai/v4_endpoint_ports ({"http_port": n, "ws_port": m}) wins when present, as in the add-on's
    client_configurator.gd. A present but malformed override gives (0, 0): the add-on starts no server then,
    and the bridge says so instead of guessing a port. Without it, godot_ai/http_port and godot_ai/ws_port
    are read one by one, and a missing one is 8000 or 9500.
    """
    override = re.search(r"(?ms)^\s*godot_ai/v4_endpoint_ports\s*=\s*(\{.*?\})\s*$", text)
    if override:
        body = override.group(1)
        found: dict[str, float] = {}
        for key in ("http_port", "ws_port"):
            match = re.search(r'"' + key + r'"\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*[,}\n]', body)
            if match:
                found[key] = float(match.group(1))
        pairs = re.findall(r'"[^"]*"\s*:', body)
        good = (len(found) == 2 and len(pairs) == 2
                and all(1024 <= value <= 65535 and value == int(value) for value in found.values())
                and int(found["http_port"]) != int(found["ws_port"]))
        if not good:
            return 0, 0
        return int(found["http_port"]), int(found["ws_port"])
    legacy: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*godot_ai/(http_port|ws_port)\s*=\s*(\d+)\s*$", line)
        if not match:
            continue
        value = int(match.group(2))
        if 1 <= value <= 65535:
            legacy[match.group(1)] = value
    if not legacy:
        return None
    return legacy.get("http_port", HTTP_PORT), legacy.get("ws_port", WS_PORT)


def godot_bundle_version(plist_path: Path | None = None) -> str:
    path = plist_path or Path("/Applications/Godot.app/Contents/Info.plist")
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        return ""
    version = data.get("CFBundleShortVersionString") if isinstance(data, dict) else ""
    return version if isinstance(version, str) else ""


def read_editor_ports(support: Path | None = None, bundle_version: str | None = None) -> tuple[int, int]:
    """Ports the open Godot dock uses. 8000 and 9500 when the setting file has neither key.

    support defaults to ~/Library/Application Support/Godot.
    The file matching the Godot bundle's major.minor wins; otherwise the newest file that sets a port.
    (0, 0) means the add-on's v4_endpoint_ports override is malformed and Godot AI starts no server.
    """
    if support is None:
        support = Path.home() / "Library/Application Support/Godot"
    version = godot_bundle_version() if bundle_version is None else bundle_version
    parts = [part for part in version.split(".") if part]
    major_minor = ".".join(parts[:2]) if len(parts) >= 2 else ""
    files: list[Path] = []
    if support.is_dir():
        files = [path for path in support.glob("editor_settings-*.tres") if path.is_file()]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    ordered: list[Path] = []
    preferred = support / f"editor_settings-{major_minor}.tres" if major_minor else None
    if preferred is not None and preferred.is_file():
        ordered.append(preferred)
    for path in files:
        if path not in ordered:
            ordered.append(path)
    for path in ordered:
        try:
            parsed = ports_from_settings(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if parsed:
            return parsed
    return HTTP_PORT, WS_PORT


def default_attach_argv(executable: str | None = None) -> list[str]:
    http, ws = read_editor_ports()
    return [executable or sys.executable, "-I", "-m", "godot_ai", "attach", "--port", str(http),
            "--ws-port", str(ws), "--disable-telemetry"]


def export_failure_note(platform: str, stderr: str) -> str:
    """Next step when Godot rejects the export. Does not change the project."""
    if platform != "macos":
        return ""
    if "ETC2" in stderr:
        return ("macOS export needs the project setting rendering/textures/vram_compression/import_etc2_astc "
                "enabled (Rendering > Textures > VRAM Compression > Import ETC2 ASTC). "
                "This plugin did not change that setting. Nothing was uploaded.")
    if "バンドル" in stderr or "bundle identifier" in stderr.lower():
        return ("macOS export needs application/bundle_identifier in the macOS preset in export_presets.cfg, "
                "a reverse-DNS id such as com.example.game. Godot 4.7 reads that preset option. "
                "This plugin did not write an id. Nothing was uploaded.")
    return ""


def json_result(summary: str, payload: dict, is_error: bool) -> dict:
    return {"content": [{"type": "text", "text": summary},
                        {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error}


# ---------------------------------------------------------------- tool list


def _published_read(tool: dict, available: dict[str, list[str]] | None) -> dict | None:
    """One read tool, with only the ops in READ_ALLOW. None when nothing remains."""
    ops = [op for op in tool["ops"] if (tool["name"], op) in READ_ALLOW]
    if available is not None:
        ops = [op for op in ops if op in available.get(tool["name"], [])]
    if not ops:
        return None
    props = json.loads(json.dumps(tool["properties"]))
    if tool["multi"] and "op" in props:
        props["op"]["enum"] = ops
    talks = any(spec["upstream"] not in ("@cli", "@local") for spec in tool["ops"].values())
    if talks:
        props.setdefault("session_id", {
            "type": "string",
            "description": "Optional. Which open Godot editor this call talks to. Omit it to use the active editor.",
        })
    if tool["name"] == "file_look" and tool["multi"] and "op" in props:
        # A local read of this plugin: GameForge's playtest report, turned into fix steps for plan_change.
        props["op"]["enum"] = list(props["op"]["enum"]) + ["gameforge_report"]
    return {"name": tool["name"], "description": READ_DESCRIPTIONS.get(tool["name"], tool["description"]),
            "inputSchema": {"type": "object", "properties": props, "required": list(tool["required"]),
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": True}}


# Tools and ops a plan step may name: every change tool, plus look ops that are not published reads.
# place_assets is not a Godot AI tool: plan_change expands each of its steps into Godot AI steps (listed in the
# plan) that put a sprite sheet, tile set, sound effect or music loop (the files GameForge writes, or any files
# in the same formats) into the open scene.
ASSET_STEPS = {
    "sprite_sheet": {
        "sig": "sprite_sheet(sheet | frames | atlas, name, parent_path, animation, autoplay, position)",
        "doc": ("An AnimatedSprite2D with a SpriteFrames resource. frames is a SpriteFrames .tres (GameForge writes "
                "<name>_frames.tres); sheet is the .png, and its <name>_frames.tres is used when it is there, or one is "
                "written from <name>.json (TexturePacker JSON-hash with frameTags, which GameForge also writes); atlas "
                "is that .json. animation defaults to the first one; autoplay (default true) plays it."),
        "keys": ["sheet", "frames", "atlas", "name", "parent_path", "animation", "autoplay", "position"]},
    "tileset": {
        "sig": "tileset(tileset | sheet, name, parent_path, map | fill, tile)",
        "doc": ("A TileMapLayer with a TileSet. tileset is a TileSet .tres (GameForge writes one with terrains and "
                "collision); sheet is the .png, and its .tres is used when it is there, or one is written from the "
                "GameForge layout <name>.json. map is the level as rows of text, like GameForge's level.json map: \"~\" "
                "is the lower terrain (water) and any other character the upper one (or a 0/1 mask); the layer gets "
                "GameForge's dual grid with the same formula as its Godot template: the Wang edge tiles where terrains "
                "meet, and fill variants scattered over open ground, up to 64x64 cells. Without map, fill is {x, y, w, h} "
                "in cells (default 0, 0, 10, 6) of one tile, {col, row} (default: the first fill tile of the upper "
                "terrain)."),
        "keys": ["tileset", "sheet", "name", "parent_path", "map", "fill", "tile"]},
    "sfx": {
        "sig": "sfx(stream, name, parent_path, bus, type)",
        "doc": ("An AudioStreamPlayer (type 1d, or 2d) with a sound effect (.wav, .ogg or .mp3) on an audio bus (default "
                "SFX). A bus that the project's bus layout lacks is added to that file. Godot keeps a player on Master "
                "until the editor has loaded the bus, so the reply says which bus the player kept."),
        "keys": ["stream", "name", "parent_path", "bus", "type"]},
    "music": {
        "sig": "music(stream, name, parent_path, bus, loop, autoplay, volume_db)",
        "doc": ("An AudioStreamPlayer that plays a music loop. loop (default true) sets the import loop (WAV loop mode "
                "Forward, or OGG/MP3 loop) in the .import file and reimports it; bus defaults to Music; autoplay "
                "defaults to true."),
        "keys": ["stream", "name", "parent_path", "bus", "loop", "autoplay", "volume_db"]},
    "buses": {
        "sig": "buses()",
        "doc": ("Finish the bus settings an earlier place_assets plan could not: Godot keeps a player on Master until "
                "the editor has loaded a bus that plan added to the bus layout, which happens when the project is "
                "opened again. After reopening, this sets each waiting player (this plugin remembers them in its own "
                "data folder) to its bus and saves, then reads the bus back."),
        "keys": []},
}
PLAN_TOOLS = tuple(tool["name"] for tool in TABLE["tools"] if tool["tier"] != "read") + (
    "scene_look", "script_look", "file_look", "project_look", "editor_look", "asset_look", "place_assets")
WANG_ORDER = [(tl, tr, bl, br) for tl in (0, 1) for tr in (0, 1) for bl in (0, 1) for br in (0, 1)]


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def audio_kind(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    return ""


def spriteframes_from_atlas(texture_res: str, atlas: dict, fps_default: float = 8.0) -> tuple[str, list[str]]:
    """A Godot 4 SpriteFrames .tres from a TexturePacker JSON-hash atlas with Aseprite frameTags (the format
    GameForge writes next to a sheet). Returns (text, animation names)."""
    frames = atlas.get("frames")
    if isinstance(frames, list):
        frames = {str(item.get("filename")): item for item in frames if isinstance(item, dict)}
    if not isinstance(frames, dict) or not frames:
        raise ValueError("the atlas has no frames")
    names = list(frames)
    rects = []
    durations = []
    for name in names:
        rect = (frames[name] or {}).get("frame") or {}
        try:
            rects.append(tuple(int(rect[k]) for k in ("x", "y", "w", "h")))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"frame {name} has no x, y, w, h")
        durations.append((frames[name] or {}).get("duration"))
    tags = ((atlas.get("meta") or {}).get("frameTags")) or []
    anims: dict[str, list[int]] = {}
    for tag in tags:
        if not isinstance(tag, dict) or not isinstance(tag.get("name"), str):
            continue
        try:
            start, end = int(tag["from"]), int(tag["to"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start <= end < len(names):
            anims[re.sub(r"[^A-Za-z0-9_]", "_", tag["name"])] = list(range(start, end + 1))
    if not anims:
        anims = {"default": list(range(len(names)))}
    first = next((d for d in durations if isinstance(d, (int, float)) and d > 0), None)
    fps = round(1000.0 / first, 1) if first else fps_default
    used = sorted({i for frames_ in anims.values() for i in frames_})
    lines = [f'[gd_resource type="SpriteFrames" load_steps={len(used) + 2} format=3]', "",
             f'[ext_resource type="Texture2D" path="{texture_res}" id="1_sheet"]', ""]
    for i in used:
        x, y, w, h = rects[i]
        lines += [f'[sub_resource type="AtlasTexture" id="AtlasTexture_{i}"]', 'atlas = ExtResource("1_sheet")',
                  f"region = Rect2({x}, {y}, {w}, {h})", ""]
    entries = []
    for name, idx in anims.items():
        listed = ", ".join('{"duration": 1.0, "texture": SubResource("AtlasTexture_%d")}' % i for i in idx)
        entries.append('{"frames": [%s], "loop": true, "name": &"%s", "speed": %.1f}' % (listed, name, fps))
    lines += ["[resource]", "animations = [" + ", ".join(entries) + "]", ""]
    return "\n".join(lines), list(anims)


def tileset_from_layout(texture_res: str, layout: dict) -> str:
    """A Godot 4 TileSet .tres from a GameForge tileset layout.json (Wang corner tiles, 4 columns, fill rows)."""
    size = int(layout.get("tile_size") or 16)
    columns = int(layout.get("columns") or 4)
    terrains = [str(t) for t in (layout.get("terrains") or ["lower", "upper"])][:2]
    order = layout.get("wang_order") or [list(c) for c in WANG_ORDER]
    names = ("top_left_corner", "top_right_corner", "bottom_left_corner", "bottom_right_corner")
    lines = ['[gd_resource type="TileSet" load_steps=3 format=3]', "",
             f'[ext_resource type="Texture2D" path="{texture_res}" id="1_tex"]', "",
             '[sub_resource type="TileSetAtlasSource" id="TileSetAtlasSource_1"]', 'texture = ExtResource("1_tex")',
             f"texture_region_size = Vector2i({size}, {size})"]
    for i, corners in enumerate(order):
        key = f"{i % columns}:{i // columns}/0"
        center = 1 if sum(int(c) for c in corners) >= 2 else 0
        lines += [f"{key} = 0", f"{key}/terrain_set = 0", f"{key}/terrain = {center}"]
        lines += [f"{key}/terrains_peering_bit/{n} = {int(c)}" for n, c in zip(names, corners)]
    fill = layout.get("fill_variants") or {}
    for row_key, terrain in (("upper_row", 1), ("lower_row", 0)):
        row = fill.get(row_key)
        if isinstance(row, int):
            for x in range(columns):
                key = f"{x}:{row}/0"
                lines += [f"{key} = 0", f"{key}/terrain_set = 0", f"{key}/terrain = {terrain}"]
                lines += [f"{key}/terrains_peering_bit/{n} = {terrain}" for n in names]
    lines += ["", "[resource]", f"tile_size = Vector2i({size}, {size})", "terrain_set_0/mode = 1"]
    for index, terrain in enumerate(terrains):
        lines.append(f'terrain_set_0/terrain_{index}/name = "{terrain}"')
    lines += ['sources/0 = SubResource("TileSetAtlasSource_1")', ""]
    return "\n".join(lines)


def dual_grid_cells(rows: list[str], has_variants: bool) -> list[tuple[int, int, int, int]]:
    """(x, y, atlas_col, atlas_row) for every corner cell of a map, by GameForge's own Godot template
    (engines/templates/godot_topdown/main.gd, _build_ground): a dual grid where the tile at corner (vx, vy) blends the
    four map cells around it, index = tl*8 + tr*4 + bl*2 + br on the Wang sheet (column idx % 4, row idx / 4), and a
    full or empty corner is swapped for a fill variant (rows 4 and 5) when (vx*73856093 ^ vy*19349663) % 3 == 0.
    A map cell is the lower terrain when it is "~" (or "0" in a 0/1 map) or outside the map, else the upper one."""
    height = len(rows)
    width = max((len(row) for row in rows), default=0)
    binary = all(set(row) <= {"0", "1"} for row in rows)

    def land(x: int, y: int) -> int:
        if x < 0 or y < 0 or y >= height or x >= len(rows[y]):
            return 0
        ch = rows[y][x]
        return (1 if ch == "1" else 0) if binary else (0 if ch == "~" else 1)

    out = []
    for vy in range(height + 1):
        for vx in range(width + 1):
            idx = land(vx - 1, vy - 1) * 8 + land(vx, vy - 1) * 4 + land(vx - 1, vy) * 2 + land(vx, vy)
            col, row = idx % 4, idx // 4
            h = (vx * 73856093) ^ (vy * 19349663)
            if has_variants and idx in (15, 0) and h % 3 == 0:
                col, row = abs(h >> 4) % 4, 4 if idx == 15 else 5
            out.append((vx, vy, col, row))
    return out


def gameforge_atlas(atlas: object) -> bool:
    """A TexturePacker JSON-hash written by GameForge (gfx/atlas.py texturepacker_json: meta.app hermes-gameforge)."""
    return isinstance(atlas, dict) and isinstance(atlas.get("meta"), dict) and atlas["meta"].get("app") == "hermes-gameforge"


def gameforge_layout(layout: object) -> bool:
    """A GameForge tileset layout.json (tools_assets.py forge_tileset: wang_order and corner_bits)."""
    return isinstance(layout, dict) and "wang_order" in layout and "corner_bits" in layout


def bus_layout_with(text: str, bus: str) -> str:
    """An AudioBusLayout .tres with bus added (sent to Master). text is the existing file or ""."""
    numbers = [int(n) for n in re.findall(r"(?m)^bus/(\d+)/name\s*=", text)]
    index = max(numbers + [0]) + 1
    block = (f'bus/{index}/name = &"{bus}"\nbus/{index}/solo = false\nbus/{index}/mute = false\n'
             f'bus/{index}/bypass_fx = false\nbus/{index}/volume_db = 0.0\nbus/{index}/send = &"Master"\n')
    if not text.strip():
        return '[gd_resource type="AudioBusLayout" format=3]\n\n[resource]\n' + block
    if "[resource]" not in text:
        return text.rstrip("\n") + "\n\n[resource]\n" + block
    return text.rstrip("\n") + "\n" + block


def _import_params(text: str) -> dict[str, str]:
    out, in_params = {}, False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_params = stripped == "[params]"
        elif in_params and "=" in stripped:
            key, value = stripped.split("=", 1)
            out[key] = value
    return out


def import_with_loop(text: str, kind: str) -> str:
    """The .import text with the loop turned on in [params], keeping what the user set: a WAV already looping
    (loop mode Forward, Ping-Pong or Backward) and its loop_begin and loop_end are left as they are; a WAV that does
    not loop (Detect From WAV or Disabled) gets Forward, and loop_begin and loop_end are added only when missing.
    An OGG or MP3 gets loop true; its loop_offset is kept. A missing [params] section is added."""
    have = _import_params(text)
    if kind == "wav":
        if have.get("edit/loop_mode", "0").strip() in ("2", "3", "4"):
            wanted = {}
        else:
            wanted = {"edit/loop_mode": "2"}
            if "edit/loop_begin" not in have:
                wanted["edit/loop_begin"] = "0"
            if "edit/loop_end" not in have:
                wanted["edit/loop_end"] = "-1"
    else:
        wanted = {"loop": "true"}
        if "loop_offset" not in have:
            wanted["loop_offset"] = "0"
    if text.strip() and not wanted:
        return text if text.endswith("\n") else text + "\n"
    if not text.strip():
        importer = {"wav": ('wav', 'AudioStreamWAV'), "ogg": ('oggvorbisstr', 'AudioStreamOggVorbis'),
                    "mp3": ('mp3', 'AudioStreamMP3')}[kind]
        text = f'[remap]\n\nimporter="{importer[0]}"\ntype="{importer[1]}"\n\n[params]\n\n'
    lines = text.rstrip("\n").split("\n")
    if "[params]" not in [line.strip() for line in lines]:
        lines += ["", "[params]", ""]
    out, seen, in_params = [], set(), False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_params:
                out += [f"{k}={v}" for k, v in wanted.items() if k not in seen]
                seen |= set(wanted)
            in_params = stripped == "[params]"
            out.append(line)
            continue
        key = stripped.split("=", 1)[0] if "=" in stripped else ""
        if in_params and key in wanted:
            out.append(f"{key}={wanted[key]}")
            seen.add(key)
            continue
        out.append(line)
    if in_params:
        out += [f"{k}={v}" for k, v in wanted.items() if k not in seen]
    return "\n".join(out) + "\n"


def only_ext_paths_differ(old: str, new: str) -> bool:
    """True when two resource texts differ only in [ext_resource ...] lines (a path rewritten, a uid dropped)."""
    a, b = old.splitlines(), new.splitlines()
    if len(a) != len(b) or a == b:
        return False
    return all(x == y or (x.startswith("[ext_resource") and y.startswith("[ext_resource")) for x, y in zip(a, b))


def only_params_differ(old: str, new: str) -> bool:
    """True when two .import texts differ only inside [params] (import settings, not the imported file)."""
    def split(text: str) -> tuple[list[str], list[str]]:
        rest, params, in_params = [], [], False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("["):
                in_params = stripped == "[params]"
            (params if in_params and not stripped.startswith("[") else rest).append(stripped)
        return [x for x in rest if x], [x for x in params if x]
    return split(old)[0] == split(new)[0]


def plan_step_ops(name: str) -> list[str]:
    tool = TOOLS.get(name)
    if tool is None:
        return []
    return [op for op in tool["ops"] if (name, op) not in READ_ALLOW]


PLAN_CHANGE_DESCRIPTION = (
    "Plan a Godot change: nodes, scripts, scenes, play or export. Steps add or delete nodes and prefab "
    "instances, set properties, attach GDScript, connect signals, animate, build UI (a Label, Button or HUD), set "
    "the input map, materials, cameras and tilemap tiles, save, playtest with scripted input, run tests, export, and "
    "put GameForge sprite sheets, tile sets (a level map becomes a dual-grid tilemap), sounds and music loops into the "
    "open scene (place_assets). Nothing is written and no project code runs: the reply is the whole plan as text "
    "(each step's old and new values, and what it replaces, loses or runs), a plan_id and a fingerprint. Show that "
    "text unchanged, then call apply_plan once. One feature is one plan (up to 64 steps). A step is {\"tool\": change "
    "tool, \"op\": operation, ...its arguments}. Change tools: editor_do, scene_do, script_do, file_do, project_do, "
    "asset_do, game_do, checks, batch, ship_build, place_assets; also scene_look and asset_look ops a read refused. "
    "change_guide lists arguments."
)
# Published read descriptions. tools.json keeps every op; these say what the published reads offer.
READ_DESCRIPTIONS = {
    "editor_look": (
        "Godot editor state, logs and class info. Reads the open scene and whether the game is playing, the "
        "selected nodes, the open scenes, performance monitors, editor sessions, the editor's and the running "
        "game's output and errors (read_logs), and a class's properties and methods. Does not change the "
        "project. Ops: state, open_scenes, selection, monitors, sessions, read_logs, class."),
    "scene_look": (
        "Inspect the Godot scene tree, nodes and node properties. Finds nodes by name, type or "
        "group, lists children, groups and signal connections, and reads a node's properties or a held resource. "
        "Does not change the project and never runs a @tool script: a read that would is refused and can be a "
        "plan_change step instead. Ops: scene_tree, find_nodes, children, groups, signals, properties, resource."),
    "script_look": (
        "Read a Godot GDScript file and its outline. Shows a script file's source and its functions, exports "
        "and signals. Does not change the project. Ops: source, outline."),
    "file_look": (
        "Read or search files in the open Godot project. Reads a res:// text file, lists files by name or type, "
        "and finds resources. gameforge_report reads a GameForge playtest report (report.json; the newest under "
        "res://playtests when path is left out) and turns each script error into a fix step for plan_change, with "
        "the file, the line and that line's code, plus a play step to check the fix. Does not change the project. "
        "Ops: text, files, resources, gameforge_report."),
    "project_look": (
        "Read Godot project settings, input map and autoloads. Reads one project setting, the autoloads, the "
        "input map (actions and their keys), the last test results and add-on tool names. Does not change the "
        "project. Ops: get, autoloads, input_map, test_results, addon_tools."),
    "asset_look": (
        "Read Godot materials, animations, cameras and tiles. Lists materials, an AnimationPlayer's animations "
        "and tracks, cameras, particles, tile map and grid map cells, and a navigation path. Does not change "
        "the project. The preset animation ops and particle_restart change it, so they are plan_change steps. "
        "Ops: materials, animation, animations, animation_check, camera, cameras, particles, tiles, "
        "gridmap_cells, gridmap_items, nav_path."),
}
APPLY_PLAN_DESCRIPTION = (
    "Run an approved Godot plan: build, playtest, fix. Asks the user for approval every time. Runs only "
    "the newest plan_id, within 10 minutes, once, in order, reads back each step's result, and stops at the first "
    "error. If the open scene, the scene file's nodes, a file the plan names or changes, or what a save would drop changed "
    "after the plan, nothing is sent. A plan that deletes, overwrites, discards unsaved work, writes outside the "
    "project or runs code written in it does not run here: the reply lists what would be lost; show it, and if the "
    "user agrees call confirm_risky_plan."
)
CONFIRM_RISKY_PLAN_DESCRIPTION = (
    "Run a risky Godot plan after the second approval. It is for a plan that deletes, overwrites, discards unsaved "
    "work, writes outside the project or runs code written in it. Asks the user for approval every time. Call it "
    "only after apply_plan returned that plan's list of what is lost and the user agreed to it. Runs the plan once "
    "if nothing changed since that list."
)
CHANGE_GUIDE_DESCRIPTION = (
    "Godot change ops and arguments for plan_change steps. Local: it does not contact the editor. Without "
    "arguments it lists the change tools and their ops. With tool it lists that tool's ops and signatures. "
    "With tool and op it gives the arguments (required ones first), what the op does, its effects (runs "
    "code, writes, deletes, sends data) and an example step."
)


def tool_definitions(available: dict[str, list[str]] | None = None) -> list[dict]:
    """tools/list. Reads are the disk reads only (DISK_READS). Everything that asks the editor is a plan step."""
    out = []
    for name, spec in DISK_READS.items():
        keys = sorted({key for args in spec["ops"].values() for key in args})
        props = {"op": {"type": "string", "enum": list(spec["ops"]),
                        "description": "Ops (arguments): " + "; ".join(f"{op} ({', '.join(args) or 'none'})"
                                                                         for op, args in spec["ops"].items())}}
        for key in keys:
            kind = {"fields": {"type": "array", "items": {"type": "string"}}, "offset": {"type": "integer"},
                    "limit": {"type": "integer"}}.get(key, {"type": "string"})
            props[key] = dict(kind, description=DISK_ARG_DOC[key])
        out.append({"name": name, "description": f"{spec['first']} {spec['about']} The editor is not asked, so no "
                                                 f"project code runs.",
                    "inputSchema": {"type": "object", "properties": props, "required": [],
                                    "additionalProperties": False},
                    "annotations": {"readOnlyHint": True}})
    out.append({
        "name": "change_guide",
        "description": CHANGE_GUIDE_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "enum": list(PLAN_TOOLS),
                         "description": "A change tool name, such as scene_do or asset_do."},
                "op": {"type": "string", "description": "One op of that tool, such as add_node."},
            },
            "required": [],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    })
    out.append({
        "name": "plan_change",
        "description": PLAN_CHANGE_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PLAN_STEPS,
                    "description": (
                        "From 1 to 64 steps, run in this order. Each is an object with tool, op and that op's "
                        "arguments at the same level, for example {\"tool\": \"scene_do\", \"op\": \"add_node\", "
                        "\"type\": \"Node2D\", \"name\": \"Coin\", \"parent_path\": \"/Main\"}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "enum": list(PLAN_TOOLS),
                                     "description": "The change tool for this step."},
                            "op": {"type": "string", "description": "That tool's operation (see change_guide)."},
                        },
                        "required": ["tool"],
                        "additionalProperties": True,
                    },
                },
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    })
    out.append({
        "name": "apply_plan",
        "description": APPLY_PLAN_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "The plan_id that plan_change returned."},
            },
            "required": ["plan_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    })
    out.append({
        "name": "confirm_risky_plan",
        "description": CONFIRM_RISKY_PLAN_DESCRIPTION,
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "The plan_id whose list apply_plan returned."},
            },
            "required": ["plan_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    })
    return out


def change_guide(arguments) -> dict:
    """Ops and arguments of the change tools, from tools.json and effects.json. Never contacts Godot."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict) or set(arguments) - {"tool", "op"}:
        return error_result("game-editor: change_guide takes tool and op only. Nothing was sent to the Godot editor.")
    name, op = arguments.get("tool"), arguments.get("op")
    if name is None:
        lines = ["Change tools for plan_change steps (tool: ops):"]
        for tool_name in PLAN_TOOLS:
            ops = list(ASSET_STEPS) if tool_name == "place_assets" else plan_step_ops(tool_name)
            lines.append(f"{tool_name}: {', '.join(ops)}")
        lines.append("Call change_guide with tool and op for that op's arguments.")
        return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": False}
    if name not in PLAN_TOOLS:
        return error_result(f"game-editor: change_guide has no tool {name!r}; tools: {', '.join(PLAN_TOOLS)}.")
    if name == "place_assets":
        if op is None:
            lines = ["place_assets ops (each becomes several Godot AI steps, listed in the plan):"]
            lines += [f"- {each}: {spec['sig']}" for each, spec in ASSET_STEPS.items()]
            return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": False}
        if op not in ASSET_STEPS:
            return error_result(f"game-editor: place_assets has no op {op!r}; ops: {', '.join(ASSET_STEPS)}.")
        spec = ASSET_STEPS[op]
        samples = {
            "sprite_sheet": ("sheet", "res://assets/sprites/hero.png"),
            "tileset": ("tileset", "res://assets/tiles/water_grass.tres"),
            "sfx": ("stream", "res://assets/sfx/coin.wav"),
            "music": ("stream", "res://assets/music/theme.wav"),
        }
        example = {"tool": "place_assets", "op": op}
        if op in samples:
            key, value = samples[op]
            example[key] = value
        text = (f"place_assets op {op}: {spec['sig']}\nwhat it does: {spec['doc']}\n"
                f"example step: {json.dumps(example)}")
        return {"content": [{"type": "text", "text": text}], "isError": False}
    ops = plan_step_ops(name)
    tool = TOOLS[name]
    if op is None:
        lines = [f"{name} ops (use one as op in a plan_change step):"]
        for each in ops:
            lines.append(f"- {each}: {tool['ops'][each]['sig']}")
        return {"content": [{"type": "text", "text": "\n".join(lines)}], "isError": False}
    if op not in ops:
        return error_result(f"game-editor: {name} has no plan op {op!r}; ops: {', '.join(ops)}.")
    spec = tool["ops"][op]
    effects = sorted(spec_effects(spec))
    props = tool.get("properties") or {}
    required = list(spec.get("required") or [])
    keys = [key for key in required] + [key for key in spec["keys"] if key not in required]
    lines = [f"{name} op {op}: {spec['sig']}", f"effects: {', '.join(effects)}", f"what it does: {spec['doc']}",
             "arguments:"]
    for key in keys:
        desc = (props.get(key) or {}).get("description", "")
        if desc.startswith("Used by:"):
            desc = ""  # a shared argument's "Used by" list is about other ops, not this one
        kind = (props.get(key) or {}).get("type", "")
        mark = " (required)" if key in required else ""
        lines.append(f"- {key}{mark}{' [' + kind + ']' if kind else ''}: {desc}".rstrip(": "))
    if spec.get("file_create") or (spec.get("guard") == "save_scene"):
        lines.append("- overwrite [boolean]: true replaces an existing file; without it an existing file is refused.")
    example = {"tool": name, "op": op}
    for key in (required or keys[:3]):
        example[key] = "..."
    lines.append("example step: " + json.dumps(example, ensure_ascii=False))
    text = "\n".join(lines)
    return {"content": [{"type": "text", "text": text}], "isError": False}


def upstream_ops(tools: list[dict]) -> dict[str, set[str] | None]:
    out: dict[str, set[str] | None] = {}
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        op = ((tool.get("inputSchema") or {}).get("properties") or {}).get("op")
        out[name] = {str(o) for o in (op.get("enum") or ([op["const"]] if "const" in op else []))} \
            if isinstance(op, dict) else None
    return out


def match_upstream(tools: list[dict]) -> tuple[dict[str, list[str]], list[str]]:
    """Which ops of each bridge tool the running Godot AI can serve, and one line per missing op."""
    ups = upstream_ops(tools)
    available: dict[str, list[str]] = {}
    missing: list[str] = []
    for tool in TABLE["tools"]:
        for bop, spec in tool["ops"].items():
            up, uop = spec["upstream"], spec["op"]
            if up in ("@cli", "@local") or (up in ups and (uop is None or uop in (ups[up] or set()))):
                available.setdefault(tool["name"], []).append(bop)
            else:
                missing.append(f"game-editor: hiding {tool['name']} op {bop}: Godot AI does not offer "
                               f"{up}{'.' + uop if uop else ''}")
    return available, missing


def zero_result_note(tool_name: str, op: str, arguments: dict, content: list) -> str:
    """A one-line note when a search found nothing, or ""."""
    if tool_name not in ("find_nodes", "search_files") or not content:
        return ""
    first = content[0] if isinstance(content[0], dict) else {}
    try:
        data = json.loads(first.get("text", ""))
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict) or data.get("total_count") != 0:
        return ""
    notes = []
    if arguments.get("type"):
        if tool_name == "find_nodes":
            notes.append("type must be an exact Godot class name, capitals included (Camera2D, not camera2d "
                         "or Camera); check it with editor_look op class")
        else:
            notes.append("type must be an exact Godot resource class name, capitals included (GDScript or "
                         "PackedScene, not Script); check it with editor_look op class")
    if tool_name == "find_nodes":
        notes.append("only the open scene is searched (other scenes need scene_do op open_scene)")
    else:
        notes.append("files added outside the editor appear only after a rescan (file_do op scan)")
    text = "; ".join(notes)
    return "game-editor note: 0 results. " + text[:1].upper() + text[1:] + "."


def _content_size(item: dict) -> int:
    if item.get("type") == "text":
        return len(str(item.get("text", "")).encode("utf-8"))
    return len(json.dumps(item, ensure_ascii=False).encode("utf-8"))


def limit_result(content: list, is_error: bool, limit: int = MAX_RESULT_BYTES) -> dict:
    """Pass the upstream content unchanged when it fits. Otherwise put a truncated marker first,
    keep whole items while they fit, cut the first text item that does not, and drop images that do not."""
    total = sum(_content_size(c) for c in content if isinstance(c, dict))
    if total <= limit:
        return {"content": content, "isError": is_error}
    kept: list[dict] = []
    used = 0
    dropped = 0
    for item in content:
        if not isinstance(item, dict):
            continue
        size = _content_size(item)
        if used + size <= limit:
            kept.append(item)
            used += size
            continue
        if item.get("type") == "text" and used < limit:
            raw = str(item.get("text", "")).encode("utf-8")[: limit - used]
            kept.append({**item, "text": raw.decode("utf-8", "ignore")})
            used = limit
            continue
        dropped += 1
    marker = {"truncated": True, "original_bytes": total, "kept_bytes": used, "dropped_items": dropped}
    return {"content": [{"type": "text", "text": json.dumps(marker)}] + kept, "isError": is_error}


def error_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def spec_effects(spec: dict) -> set[str]:
    """Effects of one op from the extracted table. Missing or dynamic is not a read."""
    key = spec.get("effect_key")
    row = _EFFECTS_BY_TOOL.get(key) if isinstance(key, str) else None
    if row is None:
        return {"unresolved"}
    found = set(row.get("effects") or [])
    if spec.get("elicit"):
        found.add("run_code")
    return found


# Script functions Godot calls when something reads or lists a property of the node (Object.get and
# get_property_list reach the script instance first).
GET_HOOKS = frozenset({"_get", "_get_property_list", "_validate_property", "_property_get_revert",
                       "_property_can_revert", "_to_string", "@getter"})
_HOOK_DEF = re.compile(r"(?m)^\s*(?:static\s+)?func\s+(_[A-Za-z0-9_]+)\s*\(")


def script_hooks(text: str) -> set[str]:
    """Names of the underscore functions a GDScript source defines (_get, _ready, ...)."""
    info = gdscript_header(text)
    if info is not None:
        return set(info["funcs"])
    return set(_HOOK_DEF.findall(text))


BOM = chr(0xFEFF)  # a UTF-8 byte order mark, as Python decodes it
_GD_IDENT = re.compile(r"[^\W\d]\w*", re.UNICODE)


def _gd_tokens(text: str) -> list[tuple[str, str]] | None:
    """GDScript tokens as Godot's tokenizer sees them, for the header only: ("ann", name), ("id", word),
    ("str", value), ("nl", ""), ("sym", char). Comments (#, ##) and line continuations are dropped.
    None when a string or bracket is not closed (the file would not parse; the caller fails closed).

    Godot 4.7.2 reads a UTF-8 BOM as nothing (measured: a BOM before @tool still runs in the editor).
    """
    if text.startswith(BOM):
        text = text[1:]
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    depth = 0
    while i < n:
        ch = text[i]
        if ch == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch == "\\" and i + 1 < n and text[i + 1] in "\r\n":
            i += 2 if text[i + 1:i + 3] != "\r\n" else 3
            continue
        if ch in "\r\n":
            if depth == 0:
                out.append(("nl", ""))
            i += 1
            continue
        if ch in " \t\f\v ":
            i += 1
            continue
        start = i
        if ch in "rR&^" and i + 1 < n and text[i + 1] in "\"'":
            i += 1
            ch = text[i]
        if ch in "\"'":
            triple = text[i:i + 3] in ('"""', "'''")
            quote = text[i:i + 3] if triple else ch
            j = i + len(quote)
            value = []
            while True:
                if j >= n:
                    return None
                if text[j] == "\\" and j + 1 < n:
                    value.append(text[j:j + 2])
                    j += 2
                    continue
                if text.startswith(quote, j):
                    j += len(quote)
                    break
                if not triple and text[j] in "\r\n":
                    return None
                value.append(text[j])
                j += 1
            out.append(("str", "".join(value)))
            i = j
            continue
        if ch == "@":
            m = _GD_IDENT.match(text, i + 1)
            if not m:
                out.append(("sym", "@"))
                i += 1
                continue
            out.append(("ann", m.group(0)))
            i = m.end()
            continue
        m = _GD_IDENT.match(text, i)
        if m:
            out.append(("id", m.group(0)))
            i = m.end()
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return None
        out.append(("sym", ch))
        i += 1
    if depth != 0:
        return None
    return out


def gdscript_header(text: str) -> dict | None:
    """What Godot decides from a GDScript file before it runs any of it, or None when the file cannot be read
    that way (the caller then treats it as a @tool script).

    {"tool": bool, "lead": [annotations before class_name/extends], "extends": str|None (a "res://" or
    "uid://" path, or a dotted class name), "class_name": str|None, "funcs": [names of func defined]}.

    The rule is Godot's own parser (GDScriptParser::parse_program), measured on Godot 4.7.2 headless with a
    marker file per variant: @tool counts in the annotations at the top of the script, in any order and on the
    same line as other annotations (`@icon("x") @tool`, `@static_unload @tool`), after a UTF-8 BOM, after doc
    strings, with a comment on the same line, and with CRLF line ends. `@tool` after `extends` or later in the
    body is a parse error in Godot (the script does not load or run); it is still counted here, so a
    misplaced @tool can only make this plugin more careful. A script that `extends` a @tool script without
    @tool of its own is not @tool (measured: by path, by class_name and with no body, none ran), but it
    inherits that script's functions, so the caller unions _get and friends along the extends chain.
    """
    tokens = _gd_tokens(text)
    if tokens is None:
        return None
    lead: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        kind, value = tokens[i]
        if kind == "nl" or kind == "str":
            i += 1
            continue
        if kind == "ann":
            lead.append(value)
            i += 1
            if i < n and tokens[i] == ("sym", "("):
                depth = 0
                while i < n:
                    if tokens[i] == ("sym", "("):
                        depth += 1
                    elif tokens[i] == ("sym", ")"):
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
            continue
        break
    extends = None
    class_name = None
    while i < n:
        kind, value = tokens[i]
        if kind == "nl":
            i += 1
            continue
        if kind == "id" and value == "class_name" and i + 1 < n and tokens[i + 1][0] == "id":
            class_name = tokens[i + 1][1]
            i += 2
            continue
        if kind == "id" and value == "extends" and i + 1 < n:
            nxt = tokens[i + 1]
            if nxt[0] == "str":
                extends = nxt[1]
                i += 2
            elif nxt[0] == "id":
                parts = [nxt[1]]
                i += 2
                while i + 1 < n and tokens[i] == ("sym", ".") and tokens[i + 1][0] == "id":
                    parts.append(tokens[i + 1][1])
                    i += 2
                extends = ".".join(parts)
            else:
                i += 1
            continue
        break
    funcs = []
    for index, (kind, value) in enumerate(tokens):
        if kind == "id" and value == "func" and index + 1 < n and tokens[index + 1][0] == "id":
            funcs.append(tokens[index + 1][1])
        # A property getter (`var hp: get: ...` or `get = _hp`) runs script code whenever the value is read.
        if kind == "id" and value == "get" and index + 1 < n and index > 0 and (
                (tokens[index + 1] in (("sym", ":"), ("sym", "=")) and tokens[index - 1][0] in ("nl", "sym"))
                or (tokens[index + 1] == ("sym", "(") and tokens[index - 1] in (("nl", ""), ("sym", ":")))):
            funcs.append("@getter")
    anywhere = any(kind == "ann" and value == "tool" for kind, value in tokens)
    return {"tool": "tool" in lead or anywhere, "lead": lead, "extends": extends, "class_name": class_name,
            "funcs": funcs}


_CS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_CS_STRING = re.compile(r'@"(?:[^"]|"")*"|"(?:[^"\\\n]|\\.)*"')
# Strings and comments in one pass, left to right, so "/*" inside a string does not start a comment.
_CS_LEXEMES = re.compile(r'@"(?:[^"]|"")*"|"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)


def _cs_code(text: str) -> str:
    return _CS_LEXEMES.sub(lambda m: '""' if m.group(0).startswith(("\"", "@")) else " ", text.lstrip(BOM))
_CS_TOOL = re.compile(r"\[\s*(?:[\w.]+(?:\([^\]]*\))?\s*,\s*)*(?:global::)?(?:Godot\.)?Tool(?:Attribute)?\s*[\],(]")


def csharp_is_tool(text: str) -> bool:
    """A [Tool] attribute anywhere outside comments and strings (also inside a list such as [GlobalClass, Tool])."""
    return _CS_TOOL.search(_cs_code(text)) is not None


def op_node_access(spec: dict) -> dict | None:
    """How an op's Godot AI commands touch nodes, from effects.json (extracted from the upstream source):
    {"reads": ["name"|"values"], "walks": bool}, or None when the handler reads no node value."""
    key = spec.get("effect_key")
    row = _EFFECTS_BY_TOOL.get(key) if isinstance(key, str) else None
    if not isinstance(row, dict):
        return None
    reads: set[str] = set()
    walks = False
    for command in row.get("commands") or []:
        info = _EFFECTS_BY_COMMAND.get(command) or {}
        access = info.get("node_access") if isinstance(info, dict) else None
        if isinstance(access, dict):
            reads |= set(access.get("reads") or [])
            walks = walks or bool(access.get("walks"))
    return {"reads": sorted(reads), "walks": walks} if reads else None


# The second approval's text (H4, APPROVAL_SPEC 10/21 19:00): a chat gateway shows about this many characters of a
# tool reply whole. What is deleted, overwritten, discarded or run comes first; a plan whose list is longer is not made.
CONFIRM_LIMIT = 1700
UNCOMPARED = "what this plugin could not compare"
# Code points written as numbers: C0 and C1 controls (not tab or newline), the line and paragraph separators, the
# bidirectional marks and overrides, and the byte order mark.
_CONTROL_RANGES = ((0x00, 0x08), (0x0B, 0x1F), (0x7F, 0x9F), (0x200E, 0x200F), (0x2028, 0x2029), (0x202A, 0x202E),
                   (0x2066, 0x2069), (0xFEFF, 0xFEFF))
_CONTROL_CHARS = re.compile("[" + "".join(re.escape(chr(a)) + "-" + re.escape(chr(b)) for a, b in _CONTROL_RANGES) + "]")


def escape_controls(text: str) -> str:
    """Control, line-separator and bidirectional characters as \\uXXXX, so a name cannot hide or reorder a line."""
    return _CONTROL_CHARS.sub(lambda m: "\\u%04x" % ord(m.group(0)), text)


# Lines (from step_losses) that need the second approval: something is deleted or overwritten, unsaved work is lost,
# a file outside the project is written, or code written in this plan runs. Playing and tests run the project's own
# code and are not on this list (APPROVAL_SPEC 10/21 19:00).
RISKY_STARTS = ("deletes", "removes", "clears", "replaces the existing file", "drops", "replaces existing build files",
                "discards", "writes outside", "sends data out", "runs code written in this plan")


def risky_line(line: str) -> bool:
    return re.sub(r"^step \d+ ", "", line).startswith(RISKY_STARTS)


def discard_placeholder(where: str) -> str:
    """The plan-time line for quit, revert and force_reload.

    The real list is not known yet: naming it would read the editor. apply_plan compares after the first
    approval, before anything is discarded, and replaces this line. edited_props is not that list.
    """
    place = where if where else "the open scene"
    return (
        "discards unsaved work in " + place + "; apply_plan compares before anything is discarded and lists "
        "every editor-only node, every node this session added and every unsaved value, or it does not run"
    )


def is_discard_placeholder(line: str) -> bool:
    return re.sub(r"^step \d+ ", "", line).startswith("discards unsaved work")


def move_sidecars(gname: str, gop: str, keys: dict) -> list[str]:
    """The .uid and .import beside a move's source and destination.

    Upstream moves those neighbors with the file. A plan that does not name them puts the bytes back and
    leaves the moved file without its uid.
    """
    if gname != "move_file" or not isinstance(keys, dict):
        return []
    src = keys.get("path") if isinstance(keys.get("path"), str) else ""
    dest = ""
    if gop == "rename":
        new_name = keys.get("new_name") if isinstance(keys.get("new_name"), str) else ""
        if src.lower().startswith("res://") and new_name and "/" not in new_name and "\\" not in new_name:
            dest = src.rsplit("/", 1)[0] + "/" + new_name
    else:
        dest = keys.get("new_path") if isinstance(keys.get("new_path"), str) else ""
    out: list[str] = []
    for value in (src, dest):
        if isinstance(value, str) and value.strip().lower().startswith("res://"):
            base = value.strip()
            out.append(base + ".uid")
            out.append(base + ".import")
    return out


def risky_summary(plan_id: str, risky: list[str], others: list[str]) -> str:
    head = [f"game-editor: nothing has run yet. Plan {plan_id} needs a second approval. Once confirmed it:"]
    body = [f"- {line}" for line in dict.fromkeys(risky)]
    tail = [f"Tell the user these lines. If they agree, call confirm_risky_plan with plan_id {plan_id}: Hermes asks "
            f"once more, then the plan runs. If they do not, do nothing: nothing changes and the plan expires."]
    text = "\n".join(head + body + tail)
    extra = []
    for line in dict.fromkeys(others):
        candidate = "\n".join(head + body + ["It also:"] + extra + [f"- {line}"] + tail)
        if len(escape_controls(candidate)) > CONFIRM_LIMIT:
            break
        extra.append(f"- {line}")
    if extra:
        text = "\n".join(head + body + ["It also:"] + extra + tail)
    return escape_controls(text)


# H7 side effects (tools/extract_effects.py, from the upstream source) in the words a plan uses.
SIDE_WORDS = {
    "saves_scene": "saves the scene file", "saves_resource": "saves a resource file (replacing it if it exists)",
    "writes_file": "writes a file on disk", "deletes_file": "deletes a file", "moves_file": "moves or copies a file",
    "reimports": "reimports assets", "rescans": "makes the editor rescan and import the file",
    "saves_project_settings": "saves project.godot", "changes_project_settings": "changes project settings",
    "changes_editor_settings": "changes editor settings",
    "loads_resource": "loads the resources it names (a @tool script in them runs)",
    "instantiates_scene": "instances a scene (its @tool scripts run)",
    "adds_to_tree": "adds nodes to the open scene (their @tool _enter_tree and _ready run)",
    "sets_script": "sets a script on a node (a @tool script starts running)", "removes_nodes": "removes nodes",
    "opens_scene": "opens a scene in the editor", "reloads_scene_from_disk": "can reload a scene from disk",
    "plays_game": "plays the game", "stops_game": "stops the running game", "evaluates_code": "evaluates code",
    "starts_process": "starts a process", "opens_with_system": "opens a file or URL with the system",
    "network": "sends network requests", "toggles_plugin": "turns an editor plugin off and on",
    "clears_logs": "clears the log buffer", "quits_editor": "quits the editor",
    "changes_selection": "changes the editor selection",
}


def upstream_side_effects(spec: dict) -> set[str]:
    key = spec.get("effect_key")
    row = _EFFECTS_BY_TOOL.get(key) if isinstance(key, str) else None
    return set((row or {}).get("side_effects") or [])


def side_effect_sentence(spec: dict, keys: dict) -> str:
    """One line for the plan: what Godot AI's handler also does inside this step (H7)."""
    side = upstream_side_effects(spec)
    if spec.get("effect_key") == "batch_execute" and isinstance(keys.get("commands"), list):
        for item in keys["commands"]:
            if isinstance(item, dict):
                side |= set((_EFFECTS_BY_COMMAND.get(str(item.get("command") or "")) or {}).get("side_effects") or [])
    words = [SIDE_WORDS[tag] for tag in sorted(side) if tag in SIDE_WORDS]
    return ("from its source, Godot AI's handler for this step can also: " + "; ".join(words)) if words else ""


def _plain_value(value) -> bool:
    """A value whose read-back can be compared as is: a number, a bool, a plain string, or a dict of numbers."""
    if isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return "://" not in value and not re.match(r"^[A-Z][A-Za-z0-9]*\(", value.strip())
    if isinstance(value, dict):
        return bool(value) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values())
    return False


def _norm_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).rstrip("\n")


def project_setting(root: Path, key: str) -> str | None:
    """The raw value of a setting in project.godot ("application/config/name" is [application] config/name)."""
    if "/" not in key:
        return None
    section, name = key.split("/", 1)
    try:
        text = (root / "project.godot").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
            continue
        if current == section and stripped.startswith(name + "="):
            return stripped[len(name) + 1:]
    return None


def describe_replacement(res: str, before: str, after: str) -> str:
    """old -> new for a file a step replaces: each changed setting of an .import file or a bus layout, else the
    size and the first line that differs."""
    if before == after:
        return f"{res} already has this content; it is written again unchanged"
    if res.endswith(".import") or res.endswith(".cfg"):
        def keyed(text: str) -> dict[str, str]:
            out, section = {}, ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    section = stripped[1:-1]
                elif "=" in stripped and not stripped.startswith(";"):
                    key, value = stripped.split("=", 1)
                    out[f"{section}/{key}" if section else key] = value
            return out
        old, new = keyed(before), keyed(after)
        changed = [f"{k} {old.get(k, '(unset)')} -> {new.get(k, '(removed)')}" for k in list(old) + [k for k in new if k not in old]
                   if old.get(k) != new.get(k)]
        return f"changes in {res}: " + ("; ".join(changed[:12]) + (f"; and {len(changed) - 12} more" if len(changed) > 12 else "")
                                        if changed else "only spacing")
    buses_before = re.findall(r'(?m)^bus/\d+/name\s*=\s*&?"([^"]+)"', before)
    buses_after = re.findall(r'(?m)^bus/\d+/name\s*=\s*&?"([^"]+)"', after)
    if buses_before or buses_after:
        kept = [name for name in buses_before if name in buses_after]
        lost = [name for name in buses_before if name not in buses_after]
        return (f"buses in {res}: {', '.join(buses_before) or 'none'} -> {', '.join(buses_after) or 'none'}"
                + (f" (removes {', '.join(lost)})" if lost else f" (keeps {', '.join(kept)})" if kept else ""))
    old_lines, new_lines = before.splitlines(), after.splitlines()
    for number in range(max(len(old_lines), len(new_lines))):
        left = old_lines[number] if number < len(old_lines) else ""
        right = new_lines[number] if number < len(new_lines) else ""
        if left != right:
            return (f"replaces {res}: {len(old_lines)} lines -> {len(new_lines)} lines; first change at line "
                    f"{number + 1}: {left.strip()[:60]!r} -> {right.strip()[:60]!r}")
    return f"replaces {res}"


def dangerous_effects(effects: set[str]) -> bool:
    if "dynamic" in effects or "unresolved" in effects:
        return True
    return bool(effects & _DANGER)


def approval_text(name: str, op: str, keys: dict, effects: set[str]) -> str:
    """The whole change, in the approval message. Not cut here; the caller refuses if it is too long."""
    bits = [label for label, flag in (
        ("runs project code", "run_code"), ("writes", "write"), ("deletes or replaces", "delete"),
        ("discards unsaved work", "discard"), ("sends data out", "send"),
    ) if flag in effects]
    if "dynamic" in effects:
        bits.append("the commands below")
    quiet = effects == {"read"}
    head = f"Godot: {name} op {op} ({', '.join(bits) or ('reads values' if quiet else 'changes the editor')})"
    lines = [head]
    for key, value in keys.items():
        if key == "session_id":
            continue
        if key == "commands" and isinstance(value, list) and len(value) > 8 and all(
                isinstance(item, dict) and item.get("command") == "tilemap_set_cell" for item in value):
            paths = sorted({str((item.get("params") or {}).get("path")) for item in value})
            tiles: dict[str, int] = {}
            for item in value:
                params = item.get("params") or {}
                tile = f"({params.get('atlas_col')}, {params.get('atlas_row')})"
                tiles[tile] = tiles.get(tile, 0) + 1
            xs = [int((item.get("params") or {}).get("map_x", 0)) for item in value]
            ys = [int((item.get("params") or {}).get("map_y", 0)) for item in value]
            lines.append(f"commands: {len(value)} tilemap_set_cell on {', '.join(paths)}, cells ({min(xs)}, {min(ys)}) "
                         f"to ({max(xs)}, {max(ys)}); atlas tiles " +
                         ", ".join(f"{tile} x{count}" for tile, count in sorted(tiles.items())))
            continue
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)


def plan_sentence(name: str, op: str, shown: dict, effects: set[str], gname: str, keys: dict,
                  save_note: str, root: str | None, open_now: list[str] | None = None,
                  losses: list[str] | None = None, notes: list[str] | None = None) -> str:
    """One step of a plan as a person reads it before apply_plan. Not cut."""
    extra = list(notes or [])
    if gname == "editor_control" and op == "quit":
        extra.append("quit closes the editor without saving. apply_plan lists every open scene and every "
                     "discarded value (an editor-only node, a node this session added, an unsaved edit) "
                     "before anything is discarded, or it does not run.")
    if gname == "screenshot":
        extra.append("Takes a picture of the editor or the game. A temporary camera it adds for that is removed "
                     "afterwards; nothing of the project is deleted.")
    for line in losses or []:
        extra.append("! " + line)
    if save_note:
        extra.append(save_note)
    if gname == "export_game" and root:
        extra.append(export_plan_note(Path(root), keys))
    text = approval_text(name, op, shown, effects)
    if extra:
        text = text + "\n" + "\n".join(extra)
    return text


def _tres_mentions(value, found: set[str]) -> None:
    """res:// paths in value that name a .tres resource."""
    mentioned: set[str] = set()
    _res_mentions(value, mentioned)
    for item in mentioned:
        if item.lower().endswith(".tres"):
            found.add(item)


def _res_mentions(value, found: set[str]) -> None:
    if isinstance(value, str):
        if value.strip().startswith("res://"):
            found.add(value.strip())
        for uri in _scheme_uris(value):
            if uri.startswith("res://"):
                found.add(uri)
    elif isinstance(value, dict):
        for item in value.values():
            _res_mentions(item, found)
    elif isinstance(value, list):
        for item in value:
            _res_mentions(item, found)


def _uid_mentions(value, found: set[str]) -> None:
    if isinstance(value, str):
        if value.strip().startswith("uid://"):
            found.add(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            _uid_mentions(item, found)
    elif isinstance(value, list):
        for item in value:
            _uid_mentions(item, found)


def ambiguous_path(value: str) -> str:
    """Empty when Godot would open this text. A reason when the text is not that path.

    Godot does not percent-decode a res:// path and does not parse a quoted GDScript
    literal in a path argument. If either form names a different file, refuse.
    """
    raw = value.strip()
    if raw[:1] in ("'", '"') or raw.startswith('&"') or raw.startswith("&'"):
        parsed = parse_value(raw, {})
        if not isinstance(parsed, str) or parsed != raw:
            return "a quoted path would name a different file than the text Godot opens"
    if "\x00" in raw or "\ufffd" in raw:
        return "a path contains a null or a replacement character"
    decoded = raw
    for _ in range(4):
        nxt = unquote(decoded)
        if nxt == decoded:
            break
        decoded = nxt
    if decoded != raw:
        return "percent-encoding would name a different file than the path Godot opens"
    return ""


def batch_block(command: str) -> str:
    """Why a plugin command cannot ride inside a batch, or "" when it can."""
    info = _EFFECTS_BY_COMMAND.get(command)
    if not isinstance(info, dict) or "unresolved" in (info.get("effects") or []) or "dynamic" in (info.get("effects") or []):
        return "unknown"
    if "send" in (info.get("effects") or []):
        return "send"
    if info.get("disk"):
        return "disk"
    return ""


_SCHEME_URI = re.compile(r"(?:res|user|uid)://[^\s\"']+", re.IGNORECASE)


def _scheme_uris(value) -> list[str]:
    if not isinstance(value, str):
        return []
    return [item.rstrip(".,)") for item in _SCHEME_URI.findall(value)]


_GODOT_FILE_URI = re.compile(r"godot://(?:script|shader|visual_shader)/([^\s\"']+)", re.IGNORECASE)
_GODOT_NODE_PROPS = re.compile(r"godot://node/(.+)/properties\b", re.IGNORECASE)


def resource_file_uris(value) -> list[str]:
    """res://, user://, uid://, plus godot://script|shader|visual_shader/ mapped back to res://."""
    if not isinstance(value, str):
        return []
    found = _scheme_uris(value)
    for match in _GODOT_FILE_URI.finditer(value):
        tail = match.group(1).rstrip(".,)").lstrip("/")
        if tail:
            found.append("res://" + tail)
    return found


def resource_read_allowed(uri: str) -> bool:
    """True only for the godot:// resources in RESOURCE_READ_ALLOW. Everything else is refused."""
    if not isinstance(uri, str) or "%" in uri or "\\" in uri:
        return False
    return any(pattern.fullmatch(uri) for pattern in RESOURCE_READ_ALLOW)


def node_property_paths(value) -> list[str]:
    """Scene paths from godot://node/.../properties. Other node URIs are not property getters."""
    if not isinstance(value, str):
        return []
    paths = []
    for match in _GODOT_NODE_PROPS.finditer(value):
        tail = match.group(1).strip().strip("/")
        if tail and ".." not in tail.split("/"):
            paths.append("/" + tail)
    return paths


def _redact_tree(value) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if isinstance(item, str):
                value[key] = redact_preset_text(item)
            else:
                _redact_tree(item)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                value[index] = redact_preset_text(item)
            else:
                _redact_tree(item)


class Child:
    """One `python -m godot_ai attach` process, spoken to over stdio JSON-RPC.

    The environment is not narrowed: with env=None (the default) the child gets this process's
    whole environment, PYTHONPATH included. The child runs with -I in the same uv environment as
    the bridge, where godot_ai is installed, so it does not rely on PYTHONPATH."""

    def __init__(self, argv: list[str], env: dict | None) -> None:
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env)
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending: dict[int, dict] = {}
        self.next_id = 1
        self.stderr_head = b""
        self.closed = threading.Event()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None and not self.closed.is_set()

    def stderr_text(self) -> str:
        return self.stderr_head[:MAX_STDERR].decode("utf-8", "replace").strip()

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in iter(self.proc.stderr.readline, b""):
            if len(self.stderr_head) < 4 * MAX_STDERR:
                self.stderr_head += line
            try:
                sys.stderr.buffer.write(line)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.strip()
            if not line.startswith(b"{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:
                self._answer_server_request(msg)
                continue
            if "id" not in msg or "method" in msg:
                continue  # notifications from the upstream are not forwarded
            with self.lock:
                slot = self.pending.get(msg["id"])
            if slot is not None:
                slot["reply"] = msg
                slot["event"].set()
        self.closed.set()
        with self.lock:
            slots = list(self.pending.values())
        for slot in slots:
            slot["event"].set()

    def _answer_server_request(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "ping":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
        elif method == "roots/list":
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"roots": []}}
        else:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}}
        self.send(reply)

    def send(self, msg: dict) -> bool:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self.write_lock:
                assert self.proc.stdin is not None
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def request(self, method: str, params: dict, timeout: float, on_id=None) -> dict:
        """The JSON-RPC reply, or {"bridge_error": "exited" | "timeout"}."""
        with self.lock:
            rid = self.next_id
            self.next_id += 1
            slot = {"event": threading.Event(), "reply": None}
            self.pending[rid] = slot
        if on_id is not None:
            on_id(rid)
        try:
            if not self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
                return {"bridge_error": "exited"}
            slot["event"].wait(timeout)
            if slot["reply"] is not None:
                return slot["reply"]
            if self.closed.is_set() or self.proc.poll() is not None:
                return {"bridge_error": "exited"}
            self.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                       "params": {"requestId": rid, "reason": "timeout"}})
            return {"bridge_error": "timeout"}
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def stop(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()






# These commands can write a resource file. The batch path does not run the overwrite check, so a file path is refused.
BATCH_NO_FILE = {"create_resource", "gradient_texture_create", "noise_texture_create", "curve_set_points"}
BATCH_NO_NODE_CHECK = {"duplicate_node", "connect_signal", "disconnect_signal"}
NODE_KEYS = ("path", "node_path", "player_path", "camera_path", "parent_path", "new_parent")
GODOT_APP_BINARIES = (
    "/Applications/Godot.app/Contents/MacOS/Godot", "~/Applications/Godot.app/Contents/MacOS/Godot",
    "/Applications/Godot_mono.app/Contents/MacOS/Godot", "~/Applications/Godot_mono.app/Contents/MacOS/Godot",
    "~/Library/Application Support/Steam/steamapps/common/Godot Engine/Godot.app/Contents/MacOS/Godot",
)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MAX_COMPARE_NODES = 5000
MAX_READ_FILE_BYTES = 700 * 1024
# A scene under the node cap is compared in full. The old 600 and 80 caps forced overwrite on ordinary scenes.
MAX_COMPARE_PROPERTY_NODES = MAX_COMPARE_NODES
MAX_GROUP_NODES = MAX_COMPARE_NODES
HEADLESS_HINT = (
    "game-editor: a headless Godot editor disables the Godot AI add-on and prints "
    "`MCP | plugin disabled in headless mode`. Set GODOT_AI_ALLOW_HEADLESS=1 and open the editor again. "
    "Nothing was sent."
)
DID_TOOLS = {
    "batch_execute", "run_game", "run_game_code", "delete_file", "delete_file_permanently",
    "delete_node", "delete_asset", "save_scene",
}
OFFSET_FIELDS = ("offset_left", "offset_right", "offset_top", "offset_bottom")
# Godot stores these on Control, but node_get_properties only returns PROPERTY_USAGE_EDITOR names,
# so they arrive as unknown_fields. A matching HUD would otherwise look like a conflict.
LAYOUT_FIELDS = OFFSET_FIELDS + ("anchor_left", "anchor_top", "anchor_right", "anchor_bottom")


def _first_json(result: dict) -> dict:
    try:
        data = json.loads(((result.get("content") or [{}])[0]).get("text", ""))
    except (ValueError, AttributeError, IndexError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_line(result: dict) -> str:
    try:
        return str((result.get("content") or [{}])[0].get("text", "")).splitlines()[0][:200]
    except (AttributeError, IndexError):
        return str(result.get("bridge_error", "no reply"))


def _step_conclusion(result: dict) -> str:
    """What the step itself concluded. A play report stays this line even when a file note was placed before it."""
    lines: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            lines.extend(str(item.get("text") or "").splitlines())
    for line in lines:
        stripped = line.strip()
        if "play_session ok=" in stripped:
            return stripped
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped
    return _first_line(result)


def _session_live(result: dict) -> bool:
    data = _first_json(result)
    if data.get("helper_live") is True:
        return True
    status = data.get("game_status")
    return isinstance(status, dict) and status.get("status") == "live"


def _viewport_not_3d(keys: dict, result: dict) -> bool:
    """True when the default 3D viewport failed because the scene is 2D. One retry uses viewport_2d."""
    source = keys.get("source")
    if source not in (None, "", "viewport"):
        return False
    if not result.get("isError"):
        return False
    blob = json.dumps(result.get("content", ""), ensure_ascii=False)
    return "viewport_not_3d" in blob or "EDITOR_VIEWPORT_NOT_3D" in blob


def _tail_bytes(raw: bytes) -> tuple[str, bool]:
    if len(raw) > MAX_CLI_OUTPUT:
        return raw[-MAX_CLI_OUTPUT:].decode("utf-8", "ignore"), True
    return raw.decode("utf-8", "replace"), False


class Refused(Exception):
    pass


class _Planned(Exception):
    """Raised instead of running a step while plan_change is building a plan."""

    def __init__(self, text: str, arguments: dict, losses: list[str] | None = None) -> None:
        super().__init__(text)
        self.text = text
        self.arguments = arguments
        self.losses = list(losses or [])


class Bridge:
    def __init__(self, argv: list[str] | None = None, env: dict | None = None) -> None:
        self.argv = list(argv) if argv is not None else default_attach_argv()
        self.env = env
        self.child: Child | None = None
        self.available: dict[str, list[str]] | None = None
        self.start_error = ""
        self.start_lock = threading.Lock()
        self.out_lock = threading.Lock()
        self.inflight: dict[object, tuple[Child, int]] = {}
        self.inflight_lock = threading.Lock()
        self.calls: list[threading.Thread] = []
        self.state_lock = threading.RLock()
        # res:// scene -> {"sha": file hash at the last point the editor and disk were known to agree, or the
        # hash when first seen, "certain": whether they were known to agree, "diffs": what differed, "why",
        # "skipped": values that could not be compared}
        self.scenes: dict[str, dict] = {}
        self.last_script_diagnostics: list[dict] = []
        self.created_local: dict[str, set[str]] = {}   # scene -> nodes this session added (not instances)
        self.created_inst: dict[str, set[str]] = {}    # scene -> scene instances this session added
        self.deleted_nodes: dict[str, set[str]] = {}   # scene -> nodes this session deleted
        self.written_scripts: dict[str, str | None] = {}  # res:// script -> its file hash right after this session wrote it
        # scene -> (rel, property) -> {"prior": disk already differed, "value": editor value this session set}
        self.edited_props: dict[str, dict[tuple[str, str], dict]] = {}
        self._project_snapshot: tuple[str, bytes] | None = None
        self._uid_cache: tuple[float, str, dict[str, str]] = (0.0, "", {})
        self._sleep = time.sleep
        self.web_server: subprocess.Popen | None = None
        self.web_server_lock = threading.Lock()
        # Plans live in memory for this session. plan_change marks its thread so upstream() can refuse any
        # Godot AI call outside PLAN_UPSTREAM while a plan is being built.
        self._plans: dict[str, dict] = {}
        self._retired: dict[str, str] = {}   # plan_id -> "replaced" or "used"
        self._plans_lock = threading.Lock()
        # scene -> what the last approved scene_do op compare found: {"sha", "diffs", "compared", "why", "at"}
        self.compare_snapshots: dict[str, dict] = {}
        self._local = threading.local()
        # No MCP elicitation: in Hermes v0.22.2's terminal CLI it fails closed. The second approval of a risky plan
        # is confirm_risky_plan, a tool Hermes's own gate asks about (APPROVAL_SPEC 10/21 19:00).
        # A Godot AI start that failed with PORT_OCCUPIED: (port, text, time). Reused while the port stays taken.
        self._port_block: tuple[int, str, float] | None = None

    # ---- child (started on the first tools/call, not at startup) ----
    def _start_child(self) -> str:
        if self.child is not None and self.child.alive():
            return ""
        if self.child is not None:
            self.child.stop()
        if "--port" in self.argv and self.argv[self.argv.index("--port") + 1:][:1] == ["0"]:
            return ("game-editor: the Godot editor setting godot_ai/v4_endpoint_ports is malformed, so the Godot AI "
                    "add-on starts no server. In Godot, open Editor Settings and set distinct http_port and ws_port "
                    "numbers between 1024 and 65535 there, or remove that setting, then try again. Nothing was sent "
                    "to the Godot editor.")
        try:
            child = Child(self.argv, self.env)
        except OSError as exc:
            return f"game-editor: could not start Godot AI ({type(exc).__name__}: {exc})"
        self.child = child
        init = child.request("initialize", {"protocolVersion": CHILD_PROTOCOL, "capabilities": {},
                                            "clientInfo": {"name": "game-editor", "version": VERSION}},
                             START_TIMEOUT)
        if "result" not in init:
            return self._start_failure(child, init)
        child.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools: list[dict] = []
        cursor = None
        while True:
            listed = child.request("tools/list", {"cursor": cursor} if cursor else {}, START_TIMEOUT)
            if "result" not in listed:
                return self._start_failure(child, listed)
            tools.extend(listed["result"].get("tools") or [])
            cursor = listed["result"].get("nextCursor")
            if not cursor:
                break
        available, missing = match_upstream(tools)
        for line in missing:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()
        self.available = available
        return ""

    def _start_failure(self, child: Child, reply: dict) -> str:
        child.stop()
        head = child.stderr_text()
        if head:
            text = head
        elif reply.get("bridge_error") == "timeout":
            text = (f"game-editor: Godot AI did not answer within {int(START_TIMEOUT)} seconds. "
                    "Open the project in Godot with the Godot AI add-on enabled, then try once. Nothing was sent.")
        elif "error" in reply:
            text = str((reply["error"] or {}).get("message", reply["error"]))
        else:
            text = (f"game-editor: Godot AI exited with code {child.proc.poll()} before it answered. "
                    "Open the project in Godot with the Godot AI add-on enabled, then try once. Nothing was sent.")
        low = text.lower()
        if "port_occupied" in low:
            return text
        if any(bit in low for bit in ("backend_start_failed", "plugin disabled in headless mode",
                                      "before becoming healthy")):
            return text.rstrip() + "\n" + HEADLESS_HINT
        return text

    def ensure_child(self) -> str:
        with self.start_lock:
            blocked = self._port_still_blocked()
            if blocked:
                return blocked
            self.start_error = self._start_child()
            if "port_occupied" in self.start_error.lower():
                found = re.search(r"[Pp]ort (\d{2,5})", self.start_error)
                if found:
                    self._port_block = (int(found.group(1)), self.start_error, time.time())
            return self.start_error

    def _port_still_blocked(self) -> str:
        """The last PORT_OCCUPIED answer, at once, while that port is still taken (Godot AI takes about 30
        seconds to say it again). Cleared when the port is free, so the next call tries again."""
        if self._port_block is None:
            return ""
        port, text, when = self._port_block
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                taken = True
        except OSError:
            taken = False
        if not taken:
            self._port_block = None
            return ""
        return (f"{text.rstrip()}\ngame-editor: port {port} is still taken (first seen {int(time.time() - when)} s "
                f"ago), so Godot AI was not started again. Stop the program on that port, or open the project in "
                f"Godot with the Godot AI add-on on its own ports. Nothing was sent.")

    def upstream(self, name: str, arguments: dict, timeout: float | None = None, mid=None) -> dict:
        """One Godot AI call. Returns {"content", "isError"} or {"bridge_error": ...}.

        While plan_change builds a plan, only PLAN_UPSTREAM calls are sent; any other call raises Refused,
        so a plan can never write, run a @tool getter or change the editor.
        """
        if getattr(self._local, "planning", False) or getattr(self._local, "plan_reads", False):
            op = arguments.get("op") if isinstance(arguments, dict) else None
            allowed = PLAN_UPSTREAM | (PLAN_READS if getattr(self._local, "plan_reads", False) else frozenset())
            if (name, op) not in allowed:
                raise Refused(f"game-editor: plan_change stopped before asking the editor for {name}"
                              f"{' op ' + str(op) if op else ''}: a plan only reads the session list. "
                              f"Nothing was sent to the Godot editor and no plan was saved.")
        child = self.child
        assert child is not None

        def remember(cid: int) -> None:
            if mid is not None:
                with self.inflight_lock:
                    self.inflight[mid] = (child, cid)

        try:
            reply = child.request("tools/call", {"name": name, "arguments": arguments},
                                  CALL_TIMEOUT if timeout is None else timeout, on_id=remember)
        finally:
            if mid is not None:
                with self.inflight_lock:
                    self.inflight.pop(mid, None)
        if "bridge_error" in reply:
            return reply
        if "error" in reply:
            err = reply.get("error") or {}
            return {"content": [{"type": "text", "text": str(err.get("message", err)) if isinstance(err, dict)
                                 else str(err)}], "isError": True}
        result = reply.get("result") or {}
        content = result.get("content")
        return {"content": content if isinstance(content, list) else [], "isError": bool(result.get("isError"))}

    # ---- facts from the editor ----
    def project_root(self) -> tuple[str | None, str]:
        res = self.upstream("session_manage", {"op": "list"})
        if "bridge_error" in res or res.get("isError"):
            return None, "error"
        data = _first_json(res)
        sessions = [s for s in (data.get("sessions") or []) if isinstance(s, dict)]
        active = [s for s in sessions if s.get("is_active")] or sessions[:1]
        if not active:
            return None, ""
        root = active[0].get("project_path")
        return (root, "") if isinstance(root, str) and root else (None, "error")

    def _virtual(self) -> dict | None:
        """While plan_change builds a plan: the scene the earlier steps opened or created, and the nodes they
        added, so a later step is checked against them (not against the editor before the plan)."""
        if not getattr(self._local, "planning", False):
            return None
        return getattr(self._local, "virtual", None)

    def _created(self, scene: str, instance: bool) -> set[str]:
        found = set((self.created_inst if instance else self.created_local).get(scene, set()))
        virt = self._virtual()
        if virt is not None:
            found |= virt["inst" if instance else "local"].get(scene, set())
        return found

    def current_scene(self) -> str:
        virt = self._virtual()
        if virt is not None and virt.get("scene") is not None:
            return virt["scene"]
        if getattr(self._local, "planning", False) or getattr(self._local, "disk_only", False):
            # A plan is made without asking the editor: the scene the editor last reported to the Godot AI server.
            scene = self.registry_state().get("current_scene")
            return scene if isinstance(scene, str) else ""
        res = self.upstream("editor_state", {})
        if "bridge_error" in res or res.get("isError"):
            return ""
        scene = _first_json(res).get("current_scene")
        return scene if isinstance(scene, str) else ""

    def resolve_uid(self, uid: str, root: str) -> str | None:
        stamp, cached_root, index = self._uid_cache
        if cached_root != root or time.time() - stamp > 20 or uid not in index:
            index = uid_index(root)
            self._uid_cache = (time.time(), root, index)
        return index.get(uid)

    @staticmethod
    def disk_path(res: str, root: str) -> Path:
        return Path(root).joinpath(*(_path_parts(res[len("res://"):]) or []))

    def read_scene(self, ref: str, root: str) -> dict | None:
        virt = self._virtual()
        if virt is not None and ref in virt["scenes"]:
            made = virt["scenes"][ref]
            return {**made, "nodes": {}, "editable": set(), "ext": {}, "groups": {}, "root_groups": [],
                    "connections": [], "subs": {}, "root_props": {}}
        if ref.startswith("uid://"):
            ref = self.resolve_uid(ref, root) or ""
        if not ref.startswith("res://"):
            return None
        try:
            return parse_tscn(self.disk_path(ref, root).read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            return None

    # ---- path checks ----
    def check_paths(self, tool: str, values: list[str], root: str | None) -> list[str]:
        """Refuse bad paths; return the res:// form of every value (uid:// resolved)."""
        resolved = []
        for value in values:
            why = ambiguous_path(value)
            if why:
                raise Refused(f"game-editor: {tool} refused {value!r}: {why}. Nothing was sent to the "
                              f"Godot editor.")
            low = value[:8].lower()
            if low.startswith("user://"):
                tail = value[len("user://"):]
                parts = _path_parts(tail)
                if parts is None:
                    raise Refused(f"game-editor: {tool} refused a path with '..'. Nothing was sent to the "
                                  f"Godot editor.")
                if _bad_parts(parts):
                    raise Refused(f"game-editor: {tool} refused {value}: this plugin does not read or write "
                                  f"{REFUSED_WHAT}. Nothing was sent to the Godot editor.")
                if TOOLS.get(tool, {}).get("tier") != "read":
                    raise Refused(f"game-editor: {tool} refused {value}: user:// is the game's player data "
                                  f"folder outside the project. This plugin does not write it. Nothing was sent "
                                  f"to the Godot editor.")
                resolved.append(value)
                continue
            res = "res://" + value[6:] if low.startswith("res://") else value
            if low.startswith("uid://"):
                if root is None:
                    resolved.append(value)
                    continue
                found = self.resolve_uid(value.split()[0], root)
                if not found:
                    raise Refused(f"game-editor: {tool} could not find {value} in the project, so the path was "
                                  f"not checked. Use the res:// path. Nothing was sent to the Godot editor.")
                res = found
            refusal = static_refusal(tool, res)
            if not refusal and root is not None:
                refusal = resolved_refusal(tool, res, root)
            if refusal:
                raise Refused(refusal)
            resolved.append(res)
        return resolved

    # ---- prefab children ----
    def prefab_owner(self, rel: str, scene: str, root: str) -> str | None:
        """The instance (outer-scene path) whose own child rel is, when Godot would not save a change to rel;
        None when the change is saved. Raises Refused when it cannot be decided."""
        outer = self.read_scene(scene, root) if scene else None
        if outer is None:
            outer = {"root": None, "nodes": {}, "editable": set(), "ext": {}}
        local = self._created(scene, False)
        inst = self._created(scene, True)
        cur = rel
        while cur:
            info = outer["nodes"].get(cur)
            if cur in local or cur in inst or (info and info["local"]):
                return None
            par = _parent(cur)
            if par in inst:
                return par
            pinfo = outer["nodes"].get(par)
            if par and pinfo and pinfo["instance"]:
                if par not in outer["editable"]:
                    return par
                return self._inside(par, pinfo.get("scene") or "", rel[len(par) + 1:], outer["editable"], root, 1)
            cur = par
        return None

    def _inside(self, prefix: str, scene_ref: str, sub: str, editable: set[str], root: str, depth: int) -> str | None:
        """rel is inside an instance with Editable Children: look inside the instanced scene."""
        inner = self.read_scene(scene_ref, root) if scene_ref else None
        if inner is None or depth > 8:
            raise Refused(f"game-editor: could not read {scene_ref or 'the instanced scene'} to check whether "
                          f"Godot would save this change to a child of /{prefix}. Nothing was sent to the "
                          f"Godot editor.")
        cur = sub
        while cur:
            info = inner["nodes"].get(cur)
            if info and info["local"]:
                return None
            par = _parent(cur)
            pinfo = inner["nodes"].get(par)
            if par and pinfo and pinfo["instance"]:
                nested = f"{prefix}/{par}"
                # The inner scene's own [editable path] is enough when this instance is already editable.
                # Godot then writes the joined path (Room/Enemy) into the outer scene.
                if nested not in editable and par not in inner.get("editable", set()):
                    return nested
                return self._inside(nested, pinfo.get("scene") or "", sub[len(par) + 1:], editable, root,
                                    depth + 1)
            cur = par
        return None

    def check_nodes(self, tool: str, values: list[str], scene: str, root: str) -> None:
        root_name = (self.read_scene(scene, root) or {}).get("root") if scene else None
        for value in values:
            owner = self.prefab_owner(rel_node_path(value, root_name), scene, root)
            if owner:
                raise Refused(
                    f"game-editor: {tool} refused {value}: it is part of the instanced scene at /{root_name}/{owner},"
                    f" and Godot does not save changes to an instance's own children unless Editable Children is "
                    f"on for that instance, at every level (in the editor: right-click the instance > Editable "
                    f"Children). Change the instanced scene itself instead (scene_do op open_scene its .tscn), or set the "
                    f"property on the instance root. Nothing was sent to the Godot editor.")

    def _scene_defines(self, scene_ref: str, sub: str, root: str, depth: int) -> bool | None:
        """True when the instanced scene defines sub, False when it does not, None when the file cannot be read."""
        inner = self.read_scene(scene_ref, root) if scene_ref else None
        if inner is None or depth > 8:
            return None
        if not sub:
            return True
        nodes = inner.get("nodes") or {}
        info = nodes.get(sub)
        if info and info.get("local"):
            return True
        cur = sub
        while cur:
            par = _parent(cur)
            pinfo = nodes.get(par) if par else None
            if par and pinfo and pinfo.get("instance"):
                return self._scene_defines(pinfo.get("scene") or "", sub[len(par) + 1:], root, depth + 1)
            if nodes.get(cur, {}).get("local"):
                return True
            if not par:
                break
            cur = par
        return False

    def prefab_defined_node(self, rel: str, scene: str, root: str) -> str | None:
        """The instance path when rel is a child that the prefab defines.

        None for the instance root, a node this file added, and a plain node this session added.
        Raises Refused when the instanced scene cannot be read.
        """
        if not rel:
            return None
        local_created = self._created(scene, False)
        inst_created = self._created(scene, True)
        if rel in local_created or rel in inst_created:
            return None
        for inst_rel in inst_created:
            if rel.startswith(inst_rel + "/"):
                return inst_rel
        outer = self.read_scene(scene, root) if scene else None
        nodes = (outer or {}).get("nodes") or {}
        info = nodes.get(rel)
        if info and info.get("local"):
            return None
        cur = rel
        while cur:
            par = _parent(cur)
            pinfo = nodes.get(par) if par else None
            if par and pinfo and pinfo.get("instance"):
                defined = self._scene_defines(pinfo.get("scene") or "", rel[len(par) + 1:], root, 1)
                if defined is None:
                    raise Refused(
                        f"game-editor: could not read {pinfo.get('scene') or 'the instanced scene'} to check "
                        f"whether /{par} defines /{rel}. Nothing was sent to the Godot editor.")
                return par if defined else None
            if not par:
                break
            cur = par
        return None

    def _prefab_scene_of(self, owner_rel: str, scene: str, root: str) -> str:
        info = ((self.read_scene(scene, root) or {}).get("nodes") or {}).get(owner_rel) or {}
        scene_ref = info.get("scene") or ""
        return scene_ref if isinstance(scene_ref, str) else ""

    def check_structure(self, tool: str, command: str, params: dict, scene: str, root: str) -> None:
        """Refuse a prefab-child change Godot would undo on the next open.

        Editable Children does not keep a delete, a reparent, or a sibling move.
        A persistent group on a prefab child is kept when Editable Children is on, and dropped when it is off.
        PackedScene.pack does not write the sibling index of a prefab-defined child.
        """
        if command not in ("delete", "delete_node", "reparent", "reparent_node", "move", "move_node",
                           "add_to_group", "remove_from_group"):
            return
        path = params.get("path")
        if not isinstance(path, str) or "://" in path:
            return
        root_name = (self.read_scene(scene, root) or {}).get("root") if scene else None
        rel = rel_node_path(path, root_name)
        owner = self.prefab_defined_node(rel, scene, root)
        if not owner:
            return
        shown = f"/{root_name}/{owner}" if root_name else f"/{owner}"
        scene_ref = self._prefab_scene_of(owner, scene, root)
        where = f"scene_do op open_scene {scene_ref}" if scene_ref else "scene_do op open_scene that .tscn"
        if command in ("delete", "delete_node"):
            raise Refused(
                f"game-editor: {tool} refused {path}: Godot does not save deleting a child that the instanced "
                f"scene at {shown} defines, even with Editable Children on. The child comes back when the scene "
                f"is saved and opened again. {where} and delete the node there. Nothing was sent to the "
                f"Godot editor.")
        if command in ("move", "move_node"):
            raise Refused(
                f"game-editor: {tool} refused to move {path}: Godot does not save the sibling order of a child "
                f"the instanced scene at {shown} defines, even with Editable Children on. Change the order in "
                f"the prefab scene ({where}). Nothing was sent to the Godot editor.")
        if command in ("add_to_group", "remove_from_group"):
            if self.prefab_owner(rel, scene, root) is None:
                return
            raise Refused(
                f"game-editor: {tool} refused to change groups on {path}: Godot does not save groups of a child "
                f"the instanced scene at {shown} defines unless Editable Children is on. Turn it on (right-click "
                f"the instance > Editable Children), or change groups in the prefab scene ({where}). Nothing was "
                f"sent to the Godot editor.")
        raise Refused(
            f"game-editor: {tool} refused to reparent {path}: moving a child the instanced scene at {shown} "
            f"defines leaves a second copy when the scene is saved and opened again, including a move that "
            f"stays inside the instance (the prefab brings the child back). {where} and move the node there. "
            f"Nothing was sent to the Godot editor.")

    def check_rename(self, tool: str, path: str, scene: str, root: str) -> None:
        """A prefab-defined name is identity. Godot drops the new name and the old one comes back."""
        if not isinstance(path, str) or "://" in path:
            return
        root_name = (self.read_scene(scene, root) or {}).get("root") if scene else None
        rel = rel_node_path(path, root_name)
        owner = self.prefab_defined_node(rel, scene, root)
        if not owner:
            return
        shown = f"/{root_name}/{owner}" if root_name else f"/{owner}"
        scene_ref = self._prefab_scene_of(owner, scene, root)
        where = f"scene_do op open_scene {scene_ref}" if scene_ref else "scene_do op open_scene that .tscn"
        raise Refused(
            f"game-editor: {tool} refused to rename {path}: that name is how the instanced scene at {shown} "
            f"tells this node apart, and Godot does not keep a new name on the instance. The old name comes "
            f"back when the scene is saved and opened again. {where} and rename the node there. Nothing was "
            f"sent to the Godot editor.")

    def check_connection(self, tool: str, op: str, params: dict, scene: str, root: str) -> None:
        """A connection from a non-editable prefab child is not saved. An inherited disconnect comes back."""
        path = params.get("path")
        if not isinstance(path, str) or "://" in path:
            return
        root_name = (self.read_scene(scene, root) or {}).get("root") if scene else None
        rel = rel_node_path(path, root_name)
        if op != "disconnect":
            owner = self.prefab_owner(rel, scene, root)
            if not owner:
                return
            shown = f"/{root_name}/{owner}" if root_name else f"/{owner}"
            scene_ref = self._prefab_scene_of(owner, scene, root)
            where = f"scene_do op open_scene {scene_ref}" if scene_ref else "scene_do op open_scene that .tscn"
            raise Refused(
                f"game-editor: {tool} refused to connect {params.get('signal')} from {path}: Godot does not "
                f"save a connection from a child of the instanced scene at {shown} unless Editable Children is "
                f"on for that instance. The connection is gone when the scene is saved and opened again. "
                f"{where} and connect it there, or connect from the instance root. Nothing was sent to the "
                f"Godot editor.")
        parsed = self.read_scene(scene, root) if scene else None
        if not parsed:
            return
        _groups, inherited = self._prefab_facts(parsed, root)
        target = params.get("target") if isinstance(params.get("target"), str) else ""
        item = _conn_tuple(str(params.get("signal") or ""), path, target, str(params.get("method") or ""),
                           root_name or "")
        if item not in inherited:
            return
        owner = item[1].split("/")[0]
        # The source may be the instance root or a child. Name the instance that defined the connection.
        for rel_key, info in parsed["nodes"].items():
            if info.get("instance") and (item[1] == rel_key or item[1].startswith(rel_key + "/")):
                owner = rel_key
                break
        shown = f"/{root_name}/{owner}" if root_name else f"/{owner}"
        scene_ref = self._prefab_scene_of(owner, scene, root)
        where = f"scene_do op open_scene {scene_ref}" if scene_ref else "scene_do op open_scene that .tscn"
        raise Refused(
            f"game-editor: {tool} refused to disconnect {params.get('signal')} from {path}: the instanced "
            f"scene at {shown} defines that connection, and Godot brings it back when the scene is saved and "
            f"opened again. {where} and disconnect it there. Nothing was sent to the Godot editor.")

    def _script_exports_storage(self, scene_ref: str, sub: str, prop: str, root: str, depth: int) -> bool:
        """True when the defining scene's script on this node has `@export_storage var prop`."""
        parsed = self.read_scene(scene_ref, root) if scene_ref else None
        if not parsed or depth > 8:
            return False
        props: dict = {}
        if not sub:
            props = parsed.get("root_props") or {}
        else:
            info = (parsed.get("nodes") or {}).get(sub)
            if info and info.get("local"):
                props = info.get("props") or {}
            else:
                cur = sub
                while cur:
                    par = _parent(cur)
                    pinfo = (parsed.get("nodes") or {}).get(par) if par else None
                    if par and pinfo and pinfo.get("instance"):
                        return self._script_exports_storage(pinfo.get("scene") or "", sub[len(par) + 1:], prop,
                                                            root, depth + 1)
                    if not par:
                        break
                    cur = par
        script = props.get("script") if isinstance(props, dict) else None
        if not isinstance(script, str):
            return False
        path = parse_value(script, parsed.get("ext") or {})
        if not isinstance(path, str) or not path.startswith("res://"):
            return False
        try:
            text = self.disk_path(path, root).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        return re.search(r"(?m)^\s*@export_storage\s+var\s+" + re.escape(prop) + r"\b", text) is not None

    def check_stored_property(self, tool: str, path: str, prop: str, scene: str, root: str) -> None:
        """Refuse a property Godot will not keep on a prefab child.

        The ClassDB table decides. A usage field on the reply is ignored. A name the table does not drop,
        and that the editor lists, is stored (@export). `@export_storage` is allowed when the defining
        scene's script declares it, even though the editor lists the name in unknown_fields.
        A name missing from a properties list is refused even when unknown_fields is absent.
        A reply with neither properties nor unknown_fields is not a verdict, so it is allowed.
        """
        root_name = (self.read_scene(scene, root) or {}).get("root") if scene else None
        rel = rel_node_path(path, root_name)
        owner = self.prefab_defined_node(rel, scene, root)
        if not owner:
            return
        res = self.upstream("node_get_properties", {"path": path, "fields": [prop]})
        if res.get("bridge_error") or res.get("isError"):
            raise Refused(
                f"game-editor: {tool} refused {path} {prop}: the editor did not say whether Godot would save "
                f"it on the instance, so it was not set. Nothing was sent to change the property.")
        body = _first_json(res)
        props = body.get("properties")
        unknown = body.get("unknown_fields")
        if not isinstance(props, list) and unknown is None:
            return
        node_type = body.get("node_type") if isinstance(body.get("node_type"), str) else ""
        named = False
        if isinstance(props, list):
            for item in props:
                if isinstance(item, dict) and item.get("name") == prop:
                    named = True
        unknown_names = {str(item) for item in unknown} if isinstance(unknown, list) else set()
        shown = f"/{root_name}/{owner}" if root_name else f"/{owner}"
        scene_ref = self._prefab_scene_of(owner, scene, root)
        where = f"scene_do op open_scene {scene_ref}" if scene_ref else "scene_do op open_scene that .tscn"
        if prop in unknown_names or (isinstance(props, list) and not named):
            sub = rel[len(owner) + 1:] if rel.startswith(owner + "/") else ""
            if self._script_exports_storage(scene_ref, sub, prop, root, 1):
                return
            raise Refused(
                f"game-editor: {tool} refused {path} {prop}: it is not an editor-visible property, so Godot "
                f"does not save it on a child of the instanced scene at {shown}. {where} and set it there. "
                f"Nothing was sent to the Godot editor.")
        usage = class_usage(node_type, prop) if node_type else None
        if usage is None and prop in _UNAMBIGUOUS_DROP:
            usage = 4
        if node_type and persists_as_other(node_type, prop):
            return
        if usage is not None and ((usage & USAGE_NO_INSTANCE_STATE) or not (usage & USAGE_STORAGE)):
            raise Refused(
                f"game-editor: {tool} refused {path} {prop}: Godot does not store it on the instance "
                f"(ClassDB usage {usage}, node_type {node_type or 'unknown'}). {where} and set it there. "
                f"Nothing was sent to the Godot editor.")

    def note_property_edit(self, scene: str, rel: str, prop: str, prior: bool = False,
                           value: object = None, keep_value: bool = False) -> None:
        """Remember an editor value this session wrote, so a later quit or revert can name it.

        keep_value false leaves a value already stored. The first call that passes keep_value true stores value.
        """
        if not scene or not isinstance(prop, str) or not prop:
            return
        with self.state_lock:
            bucket = self.edited_props.setdefault(scene, {})
            old = bucket.get((rel, prop))
            old_prior = False
            stored = None
            had_value = False
            if isinstance(old, dict):
                old_prior = bool(old.get("prior"))
                if "value" in old:
                    stored = old.get("value")
                    had_value = True
            elif isinstance(old, bool):
                old_prior = old
            record = {"prior": bool(old_prior or prior)}
            if keep_value:
                record["value"] = value
            elif had_value:
                record["value"] = stored
            bucket[(rel, prop)] = record

    def _diff_prefix(self, scene: str, root: str, rel: str, prop: str) -> str:
        root_name = (self.read_scene(scene, root) or {}).get("root") or ""
        return f"/{root_name} {prop}:" if not rel else f"/{root_name}/{rel} {prop}:"

    def _had_prior_diff(self, scene: str, root: str, rel: str, prop: str) -> bool:
        record = self.scenes.get(scene) or {}
        prefix = self._diff_prefix(scene, root, rel, prop)
        return any(str(line).startswith(prefix) for line in record.get("diffs") or [])

    def _without_own_edits(self, scene: str, root: str, diffs: list[str]) -> list[str]:
        """Drop a diff line only when this session created it.

        A line whose disk side already differed before the write stays, so a later save still
        refuses to overwrite that disk value.
        """
        edits = self.edited_props.get(scene) or {}
        if not edits:
            return diffs
        kept = []
        for line in diffs:
            own = False
            for (rel, prop), record in edits.items():
                prior = bool(record.get("prior")) if isinstance(record, dict) else bool(record)
                if prior:
                    continue
                if line.startswith(self._diff_prefix(scene, root, rel, prop)):
                    own = True
                    break
            if not own:
                kept.append(line)
        return kept

    def _without_own_nodes(self, scene: str, root: str, diffs: list[str]) -> list[str]:
        """A node this session added stays in the compare reply. It does not, by itself, block a save.

        The save writes that node. A node the editor already had, and any other difference, still blocks.
        """
        made = set(self.created_local.get(scene) or ()) | set(self.created_inst.get(scene) or ())
        if not made or not diffs:
            return diffs
        root_name = (self.read_scene(scene, root) or {}).get("root") or ""
        paths = [f"/{root_name}/{rel}" if rel else f"/{root_name}" for rel in made]

        def own(line: str) -> bool:
            if not line.startswith("in editor only: "):
                return False
            rest = line[len("in editor only: "):]
            return any(rest == path or rest.startswith(path + "/") or rest.startswith(path + " ") for path in paths)

        return [line for line in diffs if not own(line)]

    def check_duplicate(self, tool: str, value: str, scene: str, root: str) -> None:
        """Godot AI's duplicate of a scene instance (or of a node holding one) saves the instance's children a
        second time, so the reopened scene has them twice. Refuse it."""
        parsed = self.read_scene(scene, root) if scene else None
        root_name = (parsed or {}).get("root")
        rel = rel_node_path(value, root_name)
        nodes = (parsed or {"nodes": {}})["nodes"]
        created = self._created(scene, True)
        holds = [r for r, info in nodes.items() if info["instance"] and (r == rel or r.startswith(rel + "/"))]
        holds += [r for r in created if r == rel or r.startswith(rel + "/")]
        if holds:
            raise Refused(f"game-editor: {tool} refused to duplicate {value}: it is or contains a scene instance "
                          f"(/{root_name}/{holds[0]}), and Godot AI's duplicate saves an instance's children a "
                          f"second time, so the reopened scene has them twice. Place another copy with add_node "
                          f"and scene_path instead. Nothing was sent to the Godot editor.")

    def check_cycle(self, tool: str, scene_path: str, current: str, root: str) -> None:
        """Refuse instancing a scene into itself, directly or through the scenes it instances."""
        if not current.startswith("res://"):
            return
        todo, seen = [scene_path], set()
        while todo and len(seen) < 200:
            res = todo.pop()
            if res in seen:
                continue
            seen.add(res)
            if res == current:
                raise Refused(f"game-editor: {tool} refused {scene_path}: instancing it into {current} would make "
                              f"the scene contain itself (a cycle), which Godot cannot load. Nothing was sent to "
                              f"the Godot editor.")
            parsed = self.read_scene(res, root)
            for entry in (parsed or {}).get("ext", {}).values():
                if entry["type"] != "PackedScene":
                    continue
                target = (self.resolve_uid(entry["uid"], root) if entry["uid"] else None) or entry["path"]
                if target.startswith("res://"):
                    todo.append(target)

    def _read_hidden_layout(self, jobs: list[tuple[str, str, object]]) -> tuple[dict, str]:
        """Read Control offsets and anchors, then undo the read.

        set_property is the only call that returns these values. The batch sets each one and then
        fails on purpose so Godot rolls the sets back. A differing value is written once more as
        itself, which clears the redo stack so a later redo cannot apply the disk value.
        """
        found: dict[tuple[str, str], object] = {}
        for start in range(0, len(jobs), 80):
            chunk = jobs[start:start + 80]
            commands = [{"command": "set_property", "params": {"path": path, "property": name, "value": value}}
                        for path, name, value in chunk]
            commands.append({"command": "set_property", "params": {"path": chunk[0][0], "property": ""}})
            res = self.upstream("batch_execute", {"commands": commands, "undo": True})
            if "bridge_error" in res:
                return {}, f"the layout could not be read ({_first_line(res)})"
            body = _first_json(res)
            if body.get("rolled_back") is not True:
                return {}, "the layout could not be read without changing the scene"
            for item in body.get("results") or []:
                if not isinstance(item, dict):
                    continue
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                key = (str(data.get("path") or ""), str(data.get("property") or ""))
                if key[0] and key[1] and "old_value" in data:
                    found[key] = data["old_value"]
            if any((path, name) not in found for path, name, _value in chunk):
                return {}, "the layout could not be read"
        repair = []
        for path, name, disk_value in jobs:
            editor = found.get((path, name))
            if editor is None or same_value(disk_value, editor):
                continue
            repair.append({"command": "set_property", "params": {"path": path, "property": name, "value": editor}})
        if repair:
            res = self.upstream("batch_execute", {"commands": repair, "undo": False})
            if "bridge_error" in res or res.get("isError"):
                return {}, "the layout read was undone, but the editor redo stack could not be cleared"
        return found, ""

    def _prefab_facts(self, parsed: dict, root: str) -> tuple[dict, set]:
        """Groups and connections that belong to an instanced scene, not to this file.

        The editor reports both. Counting them as edits of this file makes an unchanged scene look dirty.
        """
        groups: dict[str, list[str] | None] = {}
        connections: set[tuple] = set()
        cache: dict[str, dict | None] = {}
        for rel, info in parsed["nodes"].items():
            if not info.get("instance") or not info.get("scene"):
                continue
            ref = str(info["scene"])
            if ref not in cache:
                cache[ref] = self.read_scene(ref, root)
            prefab = cache[ref]
            if not prefab or not prefab.get("root"):
                groups[rel] = None
                continue
            groups[rel] = [str(item) for item in prefab.get("root_groups") or [] if not str(item).startswith("_")]
            for item in prefab.get("connections") or []:
                src = rel_node_path(item.get("from", ""), prefab["root"])
                dst = rel_node_path(item.get("to", ""), prefab["root"])
                connections.add((item.get("signal", ""), rel if not src else f"{rel}/{src}",
                                 rel if not dst else f"{rel}/{dst}", item.get("method", "")))
        return groups, connections

    def _prefab_local_names(self, scene_ref: str, root: str, depth: int = 0) -> set[str] | None:
        """Last-segment names the prefab defines, including nested instances. None when a file cannot be read."""
        parsed = self.read_scene(scene_ref, root) if scene_ref else None
        if parsed is None or depth > 8:
            return None
        names: set[str] = set()
        if parsed.get("root"):
            names.add(str(parsed["root"]))
        for rel, info in (parsed.get("nodes") or {}).items():
            if info.get("local"):
                names.add(rel.rsplit("/", 1)[-1])
            if info.get("instance") and info.get("scene"):
                nested = self._prefab_local_names(str(info["scene"]), root, depth + 1)
                if nested is None:
                    return None
                names |= nested
        return names

    def _hierarchy(self) -> tuple[list[dict] | None, str]:
        """The editor's scene tree, or (None, why). A walk can run a @tool _get. Callers that must not
        do that do not call compare."""
        nodes: list[dict] = []
        offset = 0
        while True:
            res = self.upstream("scene_get_hierarchy", {"depth": 1000, "offset": offset, "limit": 500})
            if "bridge_error" in res or res.get("isError"):
                return None, f"the editor's scene tree could not be read ({_first_line(res)})"
            data = _first_json(res)
            batch = data.get("nodes") or []
            nodes.extend(n for n in batch if isinstance(n, dict))
            if len(nodes) > MAX_COMPARE_NODES:
                return None, (
                    f"the scene has more than {MAX_COMPARE_NODES} nodes, so it could not compare. "
                    f"Only a node-count fingerprint was known; contents were not read. "
                    f"Save a copy with scene_do op save_as and a new path."
                )
            if not data.get("has_more") or not batch:
                break
            offset += len(batch)
        return nodes, ""

    def _editor_only_lines(self, editor: set[str], parsed: dict, project_root: str, root_name: str) -> list[str]:
        """Nodes the editor has and the file does not.

        A node this session added is included: it is not on disk yet. A node under an instance counts
        only as a second copy, the same as before.
        """
        lines = []
        for rel in sorted(editor):
            if not rel or rel in parsed["nodes"]:
                continue
            if _under_instance(rel, parsed["nodes"]):
                copy = self._second_copy(rel, parsed, project_root)
                if copy:
                    lines.append(copy)
                continue
            lines.append(f"in editor only: /{root_name}/{rel}")
        return lines

    def _editor_script_line(self, path: str) -> str | None:
        """The script of a node that exists only in the editor. No script is not a second line.
        A failed read is still a difference."""
        prefix = f"in editor only: {path} script "
        got = self.upstream("node_get_properties", {"path": path, "fields": ["script"]})
        if "bridge_error" in got or got.get("isError"):
            return prefix + "could not be read"
        body = _first_json(got)
        props = body.get("properties") if isinstance(body, dict) else None
        if not isinstance(props, list):
            return prefix + "could not be read"
        for item in props:
            if isinstance(item, dict) and item.get("name") == "script":
                value = item.get("value")
                if value in (None, "", "null"):
                    return None
                return prefix + json.dumps(value, ensure_ascii=False)[:80]
        return prefix + "could not be read"

    def _name_editor_only_scripts(self, diffs: list[str]) -> None:
        """Append each editor-only node's script when it has one. The line does not use the
        'path script:' shape, so a later save does not drop it as this session's own property edit."""
        pending = []
        for line in diffs:
            if not line.startswith("in editor only: "):
                continue
            rest = line[len("in editor only: "):]
            if " script " in rest:
                continue
            pending.append(rest)
        for path in pending:
            line = self._editor_script_line(path)
            if line:
                diffs.append(line)

    def _second_copy(self, rel: str, parsed: dict, project_root: str) -> str:
        """A line when an editor-only node under an instance repeats a prefab node's name at a new path.

        A path the prefab defines exactly is a real child, not a copy. A new name (Hat) is not a copy.
        """
        parts = rel.split("/")
        inst_rel = ""
        inst_scene = ""
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            info = (parsed.get("nodes") or {}).get(prefix)
            if info and info.get("instance"):
                inst_rel = prefix
                inst_scene = str(info.get("scene") or "")
                break
        if not inst_rel:
            return ""
        sub = rel[len(inst_rel) + 1:]
        defined = self._scene_defines(inst_scene, sub, project_root, 1)
        if defined is not False:
            return ""
        names = self._prefab_local_names(inst_scene, project_root)
        if not names or parts[-1] not in names:
            return ""
        root_name = parsed.get("root") or ""
        return (f"second copy: /{root_name}/{rel} (the prefab already defines a node named {parts[-1]}, "
                f"and this path is not that node)")

    def _instance_base(self, scene: str, rel: str, root: str) -> tuple[str, str | None, str] | None:
        """(base scene, uid, path inside that scene) when this node inherits a value, else None."""
        parsed = self.read_scene(scene, root) or {}
        nodes = parsed.get("nodes") or {}
        if not rel and parsed.get("root_scene"):
            return str(parsed["root_scene"]), parsed.get("root_scene_uid"), ""
        info = nodes.get(rel) or {}
        if info.get("instance") and info.get("scene"):
            return str(info["scene"]), info.get("scene_uid"), ""
        cur = rel
        while cur and "/" in cur:
            par = _parent(cur)
            parent = nodes.get(par) or {}
            if parent.get("instance") and parent.get("scene"):
                return str(parent["scene"]), parent.get("scene_uid"), rel[len(par) + 1:]
            cur = par
        return None

    def _inherited_value(self, scene: str, rel: str, name: str, root: str) -> tuple[object, bool]:
        """(value, unreadable) the node's base scene stores for name.

        An instance does not copy its base scene's script into this file. Comparing that absence to the
        editor's inherited script is a false difference. Unreadable means the base could not be parsed:
        the caller refuses the compare instead of claiming disk (none).
        """
        if name == "script":
            found = self._script_at(scene, rel, root, 0)
            if found == "?":
                return None, True
            if isinstance(found, str) and (found.startswith(("res://", "uid://")) or "#sub:" in found):
                return found, False
            return None, False
        base = self._instance_base(scene, rel, root)
        if base is None:
            return None, False
        ref, uid, sub = base
        parsed = self._instanced(ref, uid, root)
        if not parsed or not parsed.get("root"):
            return None, True
        if sub:
            info = (parsed.get("nodes") or {}).get(sub) or {}
            raw = (info.get("props") or {}).get(name)
        else:
            raw = (parsed.get("root_props") or {}).get(name)
        if not isinstance(raw, str):
            return None, False
        value = parse_value(raw, parsed.get("ext") or {})
        if value is None or isinstance(value, tuple):
            return None, True
        return value, False

    # ---- the editor and the file on disk ----
    def compare(self, scene: str, root: str, focus: set[str] | None = None) -> tuple[bool, list[str], str, list[str]]:
        """(compared, differences, why not compared, skipped) between the editor and the file.

        A value that cannot be parsed, a subresource that cannot be read, or a group or connection read that
        fails means compared is false. A node the file does not have is a difference, including one this
        session added. An approved compare also names that node's script. An editor-only node inside an
        instance counts only when its path is not in the prefab and its name is: that is the second copy
        left by moving a prefab child.
        focus limits property, group and connection reads to those node paths (relative, "" is the root)
        and does not walk the tree. None reads the whole scene, up to the node cap.
        """
        empty: tuple[bool, list[str], str, list[str]] = (False, [], "the scene file could not be read", [])
        parsed = self.read_scene(scene, root)
        if parsed is None:
            return empty
        # A focused read does not walk the tree. An empty focus still walks, so a huge scene is refused
        # before any property getter, and a node that exists only on one side is still named.
        walk = focus is None or len(focus) == 0
        nodes: list[dict] = []
        if walk:
            nodes, why_tree = self._hierarchy()
            if nodes is None:
                return False, [], why_tree, []
        root_name = parsed["root"] or ""
        if walk:
            editor = {rel_node_path(str(n.get("path", "")), root_name) for n in nodes}
            gone = self.deleted_nodes.get(scene, set())
            diffs = [f"on disk only: /{root_name}/{rel}" for rel in sorted(parsed["nodes"])
                     if rel not in editor and not any(rel == g or rel.startswith(g + "/") for g in gone)]
            diffs.extend(self._editor_only_lines(editor, parsed, root, root_name))
        else:
            editor = set(focus or ())
            diffs = []
        entries = [(rel, info["props"]) for rel, info in parsed["nodes"].items() if info["props"] and rel in editor]
        if parsed["root_props"] and (focus is None or "" in (focus or set())):
            entries.append(("", parsed["root_props"]))
        if focus is None and len(entries) > MAX_COMPARE_PROPERTY_NODES:
            return False, diffs, (
                f"more than {MAX_COMPARE_PROPERTY_NODES} nodes have saved values, so it could not compare. "
                f"Contents were not read. Save a copy with scene_do op save_as and a new path."
            ), []
        group_rels = [""] + [rel for rel in sorted(parsed["nodes"]) if rel in editor]
        file_entries = list(entries)
        if focus is not None:
            group_rels = [rel for rel in group_rels if rel in focus]
            entries = [item for item in entries if item[0] in focus]
        if focus is None and len(group_rels) > MAX_GROUP_NODES:
            return False, diffs, (
                f"more than {MAX_GROUP_NODES} nodes would need group and connection reads, so it could not compare. "
                f"Contents were not read. Save a copy with scene_do op save_as and a new path."
            ), []
        skipped: list[str] = []
        jobs = []
        # An empty focus still parses the file (unparsed values need no getter) and does not read properties.
        fetch = not (focus is not None and len(focus) == 0)
        if fetch and focus is None:
            # A full compare asks for script on a node that stores no other property. Disk (none) against an
            # editor path is a difference. A focused compare stays on the nodes it already reads.
            have = {rel for rel, _props in entries}
            consider = [""]
            for rel in parsed["nodes"]:
                if rel in editor:
                    consider.append(rel)
            for rel in consider:
                if rel in have:
                    continue
                props = parsed.get("root_props") or {} if rel == "" else (parsed["nodes"].get(rel) or {}).get("props") or {}
                entries.append((rel, props if isinstance(props, dict) else {}))
                have.add(rel)
        parse_entries = file_entries if focus is not None and len(focus) == 0 else entries
        tile_jobs: list[tuple[str, set]] = []
        for rel, props in parse_entries:
            path = f"/{root_name}/{rel}" if rel else f"/{root_name}"
            wanted: dict = {}
            subs: list[tuple[str, str, str]] = []
            embedded_script = False
            for key, raw in props.items():
                if key == "tile_map_data":
                    # Storage only (not editor-visible). Compared by the cells the editor reports.
                    cells = tile_cells(raw)
                    if cells is None:
                        skipped.append(f"unparsed value {path} {key}")
                    elif fetch:
                        tile_jobs.append((path, cells))
                    continue
                value = parse_value(raw, parsed["ext"])
                if value is None:
                    skipped.append(f"unparsed value {path} {key}")
                    continue
                if isinstance(value, tuple) and len(value) == 2 and value[0] == "sub":
                    subs.append((key, value[1], raw))
                    if key == "script":
                        embedded_script = True
                    continue
                wanted[key] = value
            # Disk has no script property: still ask the editor. An instance's script lives on its base
            # scene; use that value. Agree is not returned before both sides are seen. A base that cannot
            # be read is unparsed, not disk (none).
            if fetch and not embedded_script and "script" not in wanted:
                inherited, unreadable = self._inherited_value(scene, rel, "script", root)
                if unreadable:
                    skipped.append(f"unparsed value {path} script")
                elif isinstance(inherited, str) and "#sub:" in inherited:
                    embedded_script = True
                else:
                    wanted["script"] = inherited
            if fetch and (wanted or subs):
                jobs.append((path, props, wanted, subs))

        # Pool threads do not see threading.local. Capture the flag on this thread or an approved compare
        # never reads a @tool node, and a later quit cannot name that scene's values.
        runs_code = bool(getattr(self._local, "compare_runs_code", False))

        def read_props(job):
            path, props, wanted, subs = job
            script_res = self._node_script_res(path, scene, root) or ""
            # An embedded script is not a file this compare can open on its own. Reading the node would run it,
            # and the reply could not name both sides, so the node is left unread even after compare is approved.
            embedded = "#sub:" in script_res
            if ((wanted or subs) and self._node_get_hooked(path, scene, root)
                    and (embedded or not runs_code)):
                return (path, props, {}, []), None, [], True
            prop_res = self.upstream("node_get_properties", {"path": path, "fields": sorted(wanted)}) if wanted else None
            sub_res = []
            for key, sid, raw in subs:
                sub_script = self._sub_tool_script(parsed, sid, scene, root)
                if sub_script:
                    # Inspecting a resource reads its properties, which runs its @tool script's _get and getters.
                    sub_res.append((key, sid, raw, {"tool_resource": sub_script}))
                    continue
                sub_res.append((key, sid, raw, self.upstream(
                    "resource_manage", {"op": "inspect", "params": {"node_path": path, "property": key, "depth": 2}})))
            return (path, props, wanted, subs), prop_res, sub_res, False

        def read_meta(rel: str):
            path = f"/{root_name}/{rel}" if rel else f"/{root_name}"
            groups = self.upstream("node_manage", {"op": "get_groups", "params": {"path": path}})
            signals = self.upstream("signal_manage", {"op": "list", "params": {"path": path}})
            return path, rel, groups, signals

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            prop_replies = list(pool.map(read_props, jobs))
            meta_replies = list(pool.map(read_meta, group_rels))
            tile_replies = list(pool.map(lambda job: (job[0], job[1], self.upstream(
                "tilemap_manage", {"op": "tilemap_get_cells", "params": {"path": job[0]}})), tile_jobs))
        for path, disk_cells, reply in tile_replies:
            body = _first_json(reply)
            if "bridge_error" in reply or reply.get("isError") or not isinstance(body.get("cells"), list):
                return False, diffs, f"the tiles of {path} could not be read ({_first_line(reply)})", skipped
            editor_cells = {(int(c.get("x", 0)), int(c.get("y", 0))) for c in body["cells"] if isinstance(c, dict)}
            disk_coords = {(cell[0], cell[1]) for cell in disk_cells}
            if editor_cells != disk_coords:
                diffs.append(f"{path} tiles: disk {len(disk_coords)} cells, editor {len(editor_cells)} cells "
                             f"({len(disk_coords - editor_cells)} only on disk, {len(editor_cells - disk_coords)} only "
                             f"in the editor)")
        for (path, props, wanted, _subs), prop_res, sub_res, tool_node in prop_replies:
            if tool_node:
                skipped.append(f"@tool script on {path} (its values were not read)")
                continue
            if wanted:
                if prop_res is None or "bridge_error" in prop_res or prop_res.get("isError"):
                    return False, diffs, f"the values of {path} could not be read ({_first_line(prop_res or {})})", skipped
                body = _first_json(prop_res)
                if "properties" not in body or not isinstance(body.get("properties"), list):
                    return False, diffs, f"the values of {path} could not be read (no properties)", skipped
                got = {item.get("name"): item.get("value") for item in body["properties"] if isinstance(item, dict)}
                unknown = body.get("unknown_fields") if isinstance(body.get("unknown_fields"), list) else []
                unknown_names = {str(item) for item in unknown}
                layout_jobs: list[tuple[str, str, object]] = []
                for name, disk_value in wanted.items():
                    if isinstance(props.get(name), str):
                        shown = props[name].strip()[:60]
                    else:
                        shown = "(none)"
                    if name == "script":
                        if disk_value in (None, "", "null"):
                            disk_value = None
                            shown = "(none)"
                        elif isinstance(disk_value, str):
                            shown = disk_value[:60]
                        if name in got and got[name] in (None, "", "null"):
                            got[name] = None
                    if name in unknown_names and (name in LAYOUT_FIELDS or (
                            "script" not in name and not name.startswith("metadata/"))):
                        # Stored but not editor-visible (offsets, AnimatedSprite2D autoplay, ...): read through a
                        # set that is rolled back before the reply.
                        layout_jobs.append((path, name, disk_value))
                        continue
                    if name not in got:
                        if name == "script":
                            return False, diffs, f"the script of {path} could not be read", skipped
                        if name in unknown_names:
                            return False, diffs, (f"{path} {name} is not an editor-visible property, so it was "
                                                  "not compared"), skipped
                        diffs.append(f"{path} {name}: disk {shown}, editor did not return it")
                    elif not same_value(disk_value, got[name]):
                        if disk_value in (None, "", "null"):
                            node_rel = "" if path == f"/{root_name}" else path[len(f"/{root_name}/"):]
                            inherited, unreadable = self._inherited_value(scene, node_rel, name, root)
                            if unreadable:
                                return False, diffs, f"the instanced scene behind {path} could not be read", skipped
                            if inherited not in (None, "", "null"):
                                disk_value = inherited
                                shown = inherited[:60] if isinstance(inherited, str) else json.dumps(inherited)[:60]
                        if not same_value(disk_value, got[name]):
                            diffs.append(f"{path} {name}: disk {shown}, editor {json.dumps(got[name])[:60]}")
                if layout_jobs and getattr(self._local, "plan_reads", False):
                    # Godot AI returns these only through a set that is rolled back: a write, so not while a plan
                    # is made. apply_plan reads them.
                    deferred = getattr(self._local, "compare_deferred", None)
                    if isinstance(deferred, list):
                        deferred.extend(path_n for path_n, _n, _v in layout_jobs if path_n not in deferred)
                    layout_jobs = []
                if layout_jobs:
                    layout_got, layout_why = self._read_hidden_layout(layout_jobs)
                    if layout_why:
                        return False, diffs, layout_why, skipped
                    for path_n, name, disk_value in layout_jobs:
                        shown = props[name].strip()[:60]
                        editor = layout_got.get((path_n, name))
                        if not same_value(disk_value, editor):
                            diffs.append(f"{path_n} {name}: disk {shown}, editor {json.dumps(editor)[:60]}")
            for key, sid, raw, sub_reply in sub_res:
                if "tool_resource" in sub_reply:
                    skipped.append(f"@tool script on {path} {key} ({sub_reply['tool_resource']}; that resource was not read)")
                    continue
                if "bridge_error" in sub_reply or sub_reply.get("isError"):
                    line = _first_line(sub_reply)
                    # Animation and AnimationLibrary are not in godot-ai's inspect families, and
                    # libraries/ is a dictionary slot, not a Resource property. The file values were parsed.
                    if "is not a native Resource property" in line or "outside the supported" in line:
                        continue
                    return False, diffs, f"subresource {path} {key} could not be read ({line})", skipped
                editor_props = _resource_properties(_first_json(sub_reply))
                disk_sub = (parsed.get("subs") or {}).get(sid) or {}
                if editor_props is None:
                    return False, diffs, f"subresource {path} {key} could not be read", skipped
                for sub_key, sub_raw in (disk_sub.get("props") or {}).items():
                    sub_value = parse_value(sub_raw, parsed["ext"])
                    if sub_value is None:
                        skipped.append(f"unparsed subresource {path} {key} {sub_key}")
                        continue
                    if sub_key not in editor_props or not same_value(sub_value, editor_props[sub_key]):
                        diffs.append(f"{path} {key} subresource {sub_key}: disk {sub_raw.strip()[:60]}, editor "
                                     f"{json.dumps(editor_props.get(sub_key))[:60]}")
        disk_groups = dict(parsed.get("groups") or {})
        disk_groups[""] = list(parsed.get("root_groups") or [])
        prefab_groups, inherited = self._prefab_facts(parsed, root)
        editor_conns: list[tuple[str, str, str, str]] = []
        for path, rel, groups_res, signals_res in meta_replies:
            if "bridge_error" in groups_res or groups_res.get("isError") or "groups" not in _first_json(groups_res):
                return False, diffs, f"the groups of {path} could not be read ({_first_line(groups_res)})", skipped
            editor_groups = sorted(str(item) for item in (_first_json(groups_res).get("groups") or [])
                                   if not str(item).startswith("_"))
            file_groups = [str(item) for item in disk_groups.get(rel, []) if not str(item).startswith("_")]
            extra = prefab_groups.get(rel, [])
            if extra is None:
                return False, diffs, f"the instanced scene behind {path} could not be read", skipped
            file_groups = sorted(set(file_groups) | set(extra))
            if editor_groups != file_groups:
                diffs.append(f"{path} groups: disk {file_groups}, editor {editor_groups}")
            if "bridge_error" in signals_res or signals_res.get("isError"):
                return False, diffs, f"the connections of {path} could not be read ({_first_line(signals_res)})", skipped
            signal_body = _first_json(signals_res)
            connections = signal_body.get("connections")
            if connections is None and isinstance(signal_body.get("signals"), list):
                connections = []
                for item in signal_body["signals"]:
                    if isinstance(item, dict):
                        connections.extend(item.get("connections") or [])
            if not isinstance(connections, list):
                return False, diffs, f"the connections of {path} could not be read", skipped
            for item in connections:
                if not isinstance(item, dict):
                    continue
                editor_conns.append(_conn_tuple(str(item.get("signal", "")), path,
                                                str(item.get("target") or item.get("to") or ""),
                                                str(item.get("method", "")), root_name))
        disk_conns = [_conn_tuple(item.get("signal", ""), item.get("from", ""), item.get("to", ""),
                                  item.get("method", ""), root_name) for item in parsed.get("connections") or []]
        for item in sorted(set(disk_conns) - set(editor_conns)):
            if focus is not None and item[1] not in focus:
                continue
            diffs.append(f"connection on disk only: signal={item[0]} from={_abs_node(root_name, item[1])} "
                         f"to={_abs_node(root_name, item[2])} method={item[3]}")
        for item in sorted(set(editor_conns) - set(disk_conns)):
            if "::" in item[3] or item in inherited:
                continue
            if focus is not None and item[1] not in focus:
                continue
            diffs.append(f"connection in editor only: signal={item[0]} from={_abs_node(root_name, item[1])} "
                         f"to={_abs_node(root_name, item[2])} method={item[3]}")
        if walk:
            # A property read can run a @tool _get that adds a node. Walk again so that node is not dropped.
            again, why_tree = self._hierarchy()
            if again is None:
                return False, diffs, why_tree, skipped
            editor_again = {rel_node_path(str(n.get("path", "")), root_name) for n in again}
            for line in self._editor_only_lines(editor_again, parsed, root, root_name):
                if line not in diffs:
                    diffs.append(line)
            if runs_code:
                self._name_editor_only_scripts(diffs)
                later, why_tree = self._hierarchy()
                if later is None:
                    return False, diffs, why_tree, skipped
                editor_later = {rel_node_path(str(n.get("path", "")), root_name) for n in later}
                grown = False
                for line in self._editor_only_lines(editor_later, parsed, root, root_name):
                    if line not in diffs:
                        diffs.append(line)
                        grown = True
                if grown:
                    self._name_editor_only_scripts(diffs)
        if skipped:
            kinds = []
            blob = " ".join(skipped)
            if "unparsed" in blob:
                kinds.append("unparsed values")
            if "subresource" in blob:
                kinds.append("subresources")
            why = "not compared: " + ", ".join(kinds or ["values"]) + " (" + "; ".join(skipped[:6]) + ")"
            return False, diffs, why, skipped
        return True, diffs, "", skipped

    def _call_focus(self, name: str, keys: dict, scene: str, root: str) -> set[str]:
        """Relative paths this call will touch, plus each parent. Empty when the call names no node."""
        root_name = (self.read_scene(scene, root) or {}).get("root") if scene else None
        rels: set[str] = set()
        values: list[str] = []
        if name == "batch_execute":
            for item in keys.get("commands") or []:
                if not isinstance(item, dict):
                    continue
                params = item.get("params") if isinstance(item.get("params"), dict) else {}
                for key in NODE_KEYS:
                    if isinstance(params.get(key), str):
                        values.append(params[key])
        else:
            for key in NODE_KEYS:
                if isinstance(keys.get(key), str):
                    values.append(keys[key])
        for value in values:
            if "://" in value:
                continue
            rel = rel_node_path(value, root_name)
            rels.add(rel)
            rels.add(_parent(rel))
        return rels

    def observe(self, scene: str, root: str, focus: set[str] | None = None, full: bool = False) -> None:
        """Record the file fingerprint, and compare the nodes this call will change.

        An empty focus walks the tree shape and does not read every property. A later call adds
        the nodes it names. The fingerprint stays the one from the first look.
        """
        if not scene.startswith("res://"):
            return
        if full:
            with self.state_lock:
                existing = self.scenes.get(scene)
            if existing is not None and existing.get("full"):
                return
            sha = existing.get("sha") if existing else file_sha(self.disk_path(scene, root))
            if sha is None and not existing:
                record = {"sha": None, "certain": True, "diffs": [], "why": "", "skipped": [], "covered": set(),
                          "structure": True, "full": True, "compared": True, "tool_only": False}
            else:
                compared, diffs, why, skipped = self.compare(scene, root, None)
                diffs = self._without_own_nodes(scene, root, self._without_own_edits(scene, root, diffs))
                if existing:
                    # An earlier, narrower look stays: what it found is not forgotten by the full compare.
                    diffs = list(existing.get("diffs") or []) + [d for d in diffs if d not in (existing.get("diffs") or [])]
                    why = why or existing.get("why") or ""
                    skipped = list(existing.get("skipped") or []) + [x for x in skipped if x not in (existing.get("skipped") or [])]
                tool_only = bool(skipped) and all(str(item).startswith("@tool script on") for item in skipped)
                record = {"sha": sha, "certain": bool(compared and not diffs and not why), "diffs": diffs,
                          "why": why, "skipped": skipped, "covered": set(), "structure": True, "full": True,
                          "compared": compared, "tool_only": tool_only and not diffs}
            with self.state_lock:
                self.scenes[scene] = record
            return
        focus = set() if focus is None else set(focus)
        with self.state_lock:
            existing = self.scenes.get(scene)
        if existing is not None and existing.get("full"):
            return
        covered = set() if existing is None else set(existing.get("covered") or ())
        pending = focus - covered
        if existing is not None and not pending and (existing.get("structure") or focus):
            return
        sha = existing.get("sha") if existing else file_sha(self.disk_path(scene, root))
        if sha is None and not existing:
            record = {"sha": None, "certain": True, "diffs": [], "why": "", "skipped": [],
                      "covered": covered | focus, "structure": True}
        else:
            compared, diffs, why, skipped = self.compare(scene, root, pending if existing else focus)
            diffs = self._without_own_nodes(scene, root, self._without_own_edits(scene, root, diffs))
            if existing:
                previous = list(existing.get("diffs") or [])
                diffs = previous + [line for line in diffs if line not in previous]
                why = existing.get("why") or why
                skipped = list(existing.get("skipped") or []) + [line for line in skipped if line not in (existing.get("skipped") or [])]
                compared = bool(existing.get("compared", True) and compared)
            certain = bool(compared and not diffs and not why)
            tool_only = bool(skipped) and all(str(item).startswith("@tool script on") for item in skipped) and not diffs
            record = {"sha": sha, "certain": certain, "diffs": diffs, "why": why, "skipped": skipped,
                      "covered": covered | focus, "structure": bool((existing or {}).get("structure") or not focus),
                      "compared": compared, "tool_only": tool_only, "full": bool((existing or {}).get("full"))}
        with self.state_lock:
            self.scenes[scene] = record

    def in_step(self, scene: str, root: str) -> None:
        """A save, a new scene or a reload wrote the file.

        The file sha is kept so a later byte change is still visible. The snapshot's compared stays
        false: this is not an agreement. Quit, revert and reload cannot treat it as proof that nothing
        would be discarded, and a later edit made outside this bridge is not hidden behind an empty agree.
        The scene record does not store compared false. A false there is a compare that failed, and the
        next look would keep it, so a scene this session just wrote could not be saved.
        """
        if not scene.startswith("res://"):
            return
        sha = file_sha(self.disk_path(scene, root))
        with self.state_lock:
            self.scenes[scene] = {"sha": sha, "certain": False, "diffs": [], "why": "", "skipped": [],
                                  "covered": set(), "structure": False, "full": False, "tool_only": False}
            self.compare_snapshots[scene] = {"sha": sha, "diffs": [], "compared": False, "why": "",
                                             "skipped": [], "at": time.time(), "bytes_moved": False}
            self._local.unsaved_edit = None
            self.created_local.pop(scene, None)
            self.created_inst.pop(scene, None)
            self.deleted_nodes.pop(scene, None)
            self.edited_props.pop(scene, None)

    def open_scene_paths(self) -> list[str]:
        """Open scene files from the editor (scene_manage get_roots, a read)."""
        res = self.upstream("scene_manage", {"op": "get_roots"})
        if res.get("isError") or "bridge_error" in res:
            return []
        body = _first_json(res)
        out = []
        for item in body.get("scenes") or body.get("roots") or []:
            other = item.get("path") if isinstance(item, dict) else item
            if isinstance(other, str) and other:
                out.append(other)
        return out

    def _sub_tool_script(self, parsed: dict, sid: str, scene: str, root: str, depth: int = 0) -> str | None:
        """The @tool script (with a get hook) on a sub-resource or on any sub-resource it holds, or None. Inspecting a
        resource reads its properties two levels down, which runs such a script."""
        if depth > 4:
            return "?"
        sub = (parsed.get("subs") or {}).get(sid) or {}
        props = sub.get("props") or {}
        script = self._script_from_props(props, parsed.get("ext") or {}, scene)
        if script:
            hooks = self._script_tool_hooks(script, root)
            if hooks is None or hooks & GET_HOOKS:
                return script
        for raw in props.values():
            for inner in re.findall(r'SubResource\(\s*"([^"]+)"\s*\)', str(raw)):
                found = self._sub_tool_script(parsed, inner, scene, root, depth + 1)
                if found:
                    return found
        return None

    def _is_relink(self, before: str, after: str, res: str, root: str) -> bool:
        """True when every changed [ext_resource] line pointed at a path this project does not have and now points at
        the file of the same name beside res (what place_assets writes); nothing that worked is replaced."""
        folder = res.rsplit("/", 1)[0]
        for old, new in zip(before.splitlines(), after.splitlines()):
            if old == new:
                continue
            old_path = re.search(r'path="([^"]+)"', old)
            new_path = re.search(r'path="([^"]+)"', new)
            if not old_path or not new_path or self._ref_exists(old_path.group(1), root):
                return False
            if new_path.group(1) != f"{folder}/{old_path.group(1).rsplit('/', 1)[-1]}":
                return False
        return True

    def _ref_exists(self, ref: str, root: str) -> bool:
        if not ref.startswith("res://"):
            return bool(ref)
        try:
            return self.disk_path(self.check_paths("plan_change", [ref], root)[0], root).exists()
        except (Refused, OSError, ValueError):
            return True

    def _virtual_scene_is(self, scene: str) -> bool:
        """True when, at this step of the plan being made, the open scene is still the one open before the plan."""
        virt = self._virtual()
        return virt is None or virt.get("scene") in (None, scene)

    def save_plan_note(self, scene: str, root: str, overwrite: bool) -> str:
        """What a save step does, for the plan. When the plan saves the scene that is open now, the editor and the
        file were compared while the plan was made (reads only); otherwise the comparison runs at apply_plan."""
        if not scene.startswith("res://"):
            return "There is no open scene file to save."
        exists = self.disk_path(scene, root).exists()
        head = f"Replaces {scene} on disk with the editor's scene." if exists else f"Writes {scene}."
        known = getattr(self._local, "plan_diffs", None)
        if known is not None and known.get("scene") == scene and self._virtual_scene_is(scene) and exists \
                and known.get("unknown"):
            raise Refused(
                f"game-editor: plan_change made no plan: {scene} could not be compared "
                f"({known.get('why') or 'not every value could be read'}), so a save cannot name both sides. "
                f"Nothing was sent to the Godot editor.")
        if known is not None and known.get("scene") == scene and self._virtual_scene_is(scene) and exists:
            skipped = [str(item)[len("@tool script on "):].split(" (")[0] for item in known.get("skipped") or []
                       if str(item).startswith("@tool script on")]
            not_read = (f" Not compared, because reading them runs their @tool scripts: {', '.join(skipped[:4])}."
                        if skipped else "")
            later = (f" The Control offsets of {', '.join(known['deferred'][:4])} are compared at apply_plan (Godot AI "
                     f"returns them only through a set it rolls back); one that differs is listed there and needs the "
                     f"second approval." if known.get("deferred") else "")
            if overwrite and known.get("diffs"):
                return (head + f" overwrite is true. The editor and the file differ now in {len(known['diffs'])} "
                        f"place(s), on the ! lines above; the save writes the editor's side and drops the disk side." +
                        not_read + later)
            if overwrite:
                return (head + " overwrite is true, but the editor and the file agree now (compared while this plan "
                        "was made), so nothing on disk is dropped. If the file changes before apply_plan, nothing "
                        "runs." + not_read + later)
            return (head + " The editor and the file agree now (compared while this plan was made). If either "
                    "changes before apply_plan, nothing runs." + not_read + later)
        if overwrite:
            return (head + " overwrite is true: a change made to that file outside this editor is dropped; the "
                    "reply names what was dropped.")
        return (head + " At apply_plan the editor and the file are compared first; if the file was changed "
                "outside this editor, nothing is saved and the reply says why.")

    def check_save(self, tool: str, scene: str, root: str, overwrite: bool) -> str:
        """Refuse a save that could overwrite a change made to the file outside this editor.

        An overwrite call is allowed and returns a sentence naming what the disk side loses.
        """
        if not scene.startswith("res://"):
            return ""
        self.observe(scene, root, set())
        record = self.scenes[scene]
        sha_now = file_sha(self.disk_path(scene, root))
        sha_moved = record.get("sha") is not None and sha_now != record["sha"]
        snap = self.compare_snapshots.get(scene) or {}
        fresh = bool(snap.get("compared")) and snap.get("sha") == sha_now
        blocking = self._without_own_nodes(scene, root, list(snap.get("diffs") or [])) if fresh else []
        if fresh and not blocking and not overwrite:
            return ""
        if fresh and blocking and not overwrite:
            listed = "; ".join(blocking[:8]) + (f"; and {len(blocking) - 8} more" if len(blocking) > 8 else "")
            raise Refused(
                f"game-editor: {tool} refused to save {scene}: the editor and the file differed: {listed}. "
                f"Saving would write the editor's side over the disk side. Ask the user which to keep. "
                f"Nothing was saved.")
        if fresh and overwrite:
            return _loss_note({"diffs": list(snap.get("diffs") or []), "why": "", "skipped": []})
        if overwrite:
            note = _loss_note(record)
            if sha_moved:
                extra = ("game-editor: overwrite keeps the editor's scene. The file on disk changed after this "
                         "session last saw them agree, and that disk change will be dropped.")
                note = (note + " " + extra).strip() if note else extra
            return note
        if sha_moved:
            raise Refused(
                f"game-editor: {tool} refused to save {scene}: the file on disk changed after this session "
                f"last saw the editor and the file agree, so something outside this editor wrote it (a "
                f"teammate, a git checkout, another editor). Saving now would overwrite that change. Ask the "
                f"user what to keep: call {tool} again with overwrite: true to keep the editor's version, or "
                f"revert_scene to load the disk version (this drops the editor's unsaved changes). Nothing "
                f"was saved.")
        if record["certain"]:
            return ""
        if record.get("tool_only") and not record["diffs"]:
            raise Refused(
                f"game-editor: {tool} refused to save {scene}: its values were not compared, because reading them "
                f"runs their @tool scripts, and no scene_do op compare has named both sides since the file last "
                f"changed. Plan that compare, apply it, then the save. Nothing was saved.")
        listed = "; ".join(record["diffs"][:8]) + (f"; and {len(record['diffs']) - 8} more"
                                                  if len(record["diffs"]) > 8 else "")
        if record["why"]:
            extra = f" Skipped: {', '.join(record.get('skipped') or [])}." if record.get("skipped") else ""
            raise Refused(
                f"game-editor: {tool} refused to save {scene}: this plugin could not compare the editor with the "
                f"file ({record['why']}), so it cannot rule out a change made to the file on disk, which saving "
                f"would overwrite.{extra} Save a copy first with scene_do op save_as and a new path, or check "
                f"the file (for example with git) and call {tool} again with overwrite: true. Nothing was saved.")
        raise Refused(
            f"game-editor: {tool} refused to save {scene}: before this session changed anything, the editor and "
            f"the file differed: {listed}. Those can be unsaved edits made in the editor before this session, or "
            f"changes made to the file on disk; this plugin cannot tell which, and saving would overwrite the disk "
            f"side. If they are edits in the editor, call {tool} again with overwrite: true. If the file was "
            f"changed on disk, ask the user before saving. Nothing was saved.")

    def check_batch(self, commands, scene: str, root: str | None) -> list[dict]:
        if not isinstance(commands, list) or not commands:
            raise Refused("game-editor: batch_execute takes commands, a list of {command, params}. Nothing was "
                          "sent to the Godot editor.")
        nodes: list[str] = []
        for item in commands:
            if not isinstance(item, dict) or not isinstance(item.get("command"), str):
                raise Refused("game-editor: each batch_execute command is {\"command\": name, \"params\": {...}}. "
                              "Nothing was sent to the Godot editor.")
            command = item["command"]
            why = batch_block(command)
            if why == "unknown":
                raise Refused(f"game-editor: batch does not run {command}: it is not a known editor command, "
                              f"or its effect could not be classified. Nothing was sent to the Godot editor.")
            if why == "send":
                raise Refused(f"game-editor: batch does not run {command}: it sends data out. Call it on its own. "
                              f"Nothing was sent to the Godot editor.")
            if why == "disk":
                raise Refused(f"game-editor: batch does not run {command}: it writes a file and this path does "
                              f"not apply the overwrite check. Nothing was sent to the Godot editor.")
            params = item.get("params") or {}
            if not isinstance(params, dict) or "session_id" in params:
                raise Refused("game-editor: batch_execute params must be an object without session_id. Nothing "
                              "was sent to the Godot editor.")
            if command in BATCH_NO_FILE and (params.get("resource_path") or params.get("save_to")):
                raise Refused(f"game-editor: batch_execute runs {command} only when it writes no file (no "
                              f"resource_path). Use edit_resources for a file. Nothing was sent to the Godot editor.")
            if command == "create_node" and isinstance(params.get("scene_path"), str) and root is not None:
                self.check_cycle("batch_execute", self.check_paths("batch_execute", [params["scene_path"].strip()],
                                                                   root)[0], scene, root)
            if command == "duplicate_node" and isinstance(params.get("path"), str) and root is not None:
                self.check_duplicate("batch_execute", params["path"], scene, root)
            if command not in BATCH_NO_NODE_CHECK:
                nodes += [params[k] for k in NODE_KEYS if isinstance(params.get(k), str) and "://" not in params[k]]
        if root is not None:
            self.check_nodes("batch_execute", nodes, scene, root)
        return commands

    # ---- tests outside the editor ----
    @staticmethod
    def godot_binary() -> str | None:
        for cand in GODOT_APP_BINARIES:
            path = os.path.expanduser(cand)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return shutil.which("godot")

    def run_cli_tests(self, framework: str, keys: dict, root: str | None) -> dict:
        if root is None:
            return error_result("game-editor: run_tests needs the Godot editor open on the project to find its "
                                "folder. Nothing was run.")
        godot = self.godot_binary()
        if not godot:
            return error_result("game-editor: run_tests could not find the Godot binary (Godot.app or "
                                "Godot_mono.app in /Applications or ~/Applications, Steam, or godot on PATH). "
                                "Nothing was run.")
        dirs = keys.get("dirs") or [d for d in ("res://test", "res://tests") if self.disk_path(d, root).is_dir()]
        if isinstance(dirs, str):
            dirs = [dirs]
        if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
            return error_result("game-editor: run_tests dirs takes res:// folder paths. Nothing was run.")
        self.check_paths("run_tests", [d.strip() for d in dirs], root)
        if framework == "gut":
            runner = "res://addons/gut/gut_cmdln.gd"
            if not self.disk_path(runner, root).is_file():
                return error_result("game-editor: GUT is not installed in this project (addons/gut/gut_cmdln.gd "
                                    "is missing). Install GUT from the Godot Asset Library first. Nothing was run.")
            argv = [godot, "--headless", "--path", root, "-s", runner, "-gexit"]
            if not (Path(root) / ".gutconfig.json").is_file():
                if not dirs:
                    return error_result("game-editor: no res://test or res://tests folder and no .gutconfig.json; "
                                        "pass dirs. Nothing was run.")
                argv.append("-gdir=" + ",".join(dirs))
                argv.append("-ginclude_subdirs")
            if keys.get("suite"):
                argv.append("-gselect=" + str(keys["suite"]))
            if keys.get("test_name"):
                argv.append("-gunit_test_name=" + str(keys["test_name"]))
        else:
            runner = "res://addons/gdUnit4/bin/GdUnitCmdTool.gd"
            if not self.disk_path(runner, root).is_file():
                return error_result("game-editor: gdUnit4 is not installed in this project "
                                    "(addons/gdUnit4/bin/GdUnitCmdTool.gd is missing). Install gdUnit4 from the "
                                    "Godot Asset Library first. Nothing was run.")
            if not dirs:
                return error_result("game-editor: no res://test or res://tests folder; pass dirs. Nothing was run.")
            argv = [godot, "--headless", "--path", root, "-s", "-d", runner, "--ignoreHeadlessMode"]
            for d in dirs:
                argv += ["-a", d]
        started = time.time()
        self.progress(f"running {framework} tests in a headless Godot")
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=TEST_TIMEOUT, cwd=root)
            code = proc.returncode
            output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            code = None
            output = ((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", "replace")
            timed_out = True
        output = ANSI.sub("", output)
        tests_run, tests_failed = read_test_counts(framework, output)
        passed = code == 0 and bool(tests_run) and tests_failed == 0
        cut = len(output.encode("utf-8")) > MAX_CLI_OUTPUT
        tail = output.encode("utf-8")[-MAX_CLI_OUTPUT:].decode("utf-8", "ignore")
        summary = {"passed": passed, "tests_run": tests_run, "tests_failed": tests_failed, "framework": framework,
                   "exit_code": code, "timed_out": timed_out, "seconds": round(time.time() - started, 1),
                   "output_truncated_to_last_bytes": MAX_CLI_OUTPUT if cut else None,
                   "command": [os.path.basename(argv[0])] + argv[1:]}
        note = cli_count_note(output, tests_run)
        if note:
            summary["note"] = note
        head = json.dumps(summary)
        if not passed:
            head = (f"game-editor: tests passed=false tests_run={tests_run} tests_failed={tests_failed}. "
                    f"The count is the result. upstream_quote: {head[:400]}")
        return {"content": [{"type": "text", "text": head}, {"type": "text", "text": tail}],
                "isError": not passed}

    # ---- Hermes side ----
    def write(self, msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self.out_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        mid = msg.get("id")
        if method == "notifications/cancelled":
            self._cancel((msg.get("params") or {}).get("requestId"))
            return None
        if mid is None:
            return None
        if method == "initialize":
            offered = (msg.get("params") or {}).get("protocolVersion")
            version = offered if offered in HANDSHAKE_VERSIONS else CHILD_PROTOCOL
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "game-editor", "version": VERSION}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_definitions(self.available)}}
        if method == "tools/call":
            worker = threading.Thread(target=self._call_and_reply, args=(mid, msg.get("params") or {}), daemon=True)
            self.calls = [t for t in self.calls if t.is_alive()] + [worker]
            worker.start()
            return None
        # MCP resources are not offered: godot:// resources are answered by the editor, where project code can run.
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}

    def _cancel(self, request_id) -> None:
        with self.inflight_lock:
            target = self.inflight.get(request_id)
        if target is not None:
            child, cid = target
            child.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": cid, "reason": "cancelled by the client"}})

    def _call_and_reply(self, mid, params: dict) -> None:  # noqa: C901
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        token = meta.get("progressToken")
        self._local.progress = token if isinstance(token, (str, int)) and not isinstance(token, bool) else None
        self._local.progress_count = 0
        self._local.progress_last = None
        try:
            result = self.call(params.get("name"), params.get("arguments"), mid)
        except Exception as exc:  # noqa: BLE001 - a bridge bug must still answer the call
            result = error_result(f"game-editor: internal error ({type(exc).__name__}).")
        if isinstance(result, dict):
            result.pop("stop_plan", None)
        self.write({"jsonrpc": "2.0", "id": mid, "result": result})

    def progress(self, message: str, done: float | None = None, total: float | None = None) -> None:
        """notifications/progress for this call, only when the client sent a progressToken. Never raises."""
        token = getattr(self._local, "progress", None)
        if token is None:
            return
        count = getattr(self._local, "progress_count", 0) + 1
        self._local.progress_count = count
        last = getattr(self._local, "progress_last", None)
        value = float(done if done is not None else (last + 0.5 if last is not None else 0.0))
        if last is not None and value <= last:
            value = last + 0.001  # MCP: progress increases with each notification
        self._local.progress_last = value
        params: dict = {"progressToken": token, "progress": round(value, 3), "message": message[:200]}
        if total is not None:
            params["total"] = float(total)
        try:
            self.write({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})
        except (OSError, ValueError):
            pass

    def call(self, name, arguments, mid=None) -> dict:
        try:
            return self._call(name, arguments, mid)
        except Refused as exc:
            return error_result(str(exc))

    def _call(self, name, arguments, mid) -> dict:
        if name == "plan_change":
            return self._plan_change(arguments)
        if name == "apply_plan":
            return self._apply_plan(arguments, mid)
        if name == "confirm_risky_plan":
            return self._confirm_risky_plan(arguments, mid)
        if name == "change_guide":
            return change_guide(arguments)
        if name in DISK_READS:
            return self.disk_read(name, arguments)
        tool = TOOLS.get(name) if isinstance(name, str) else None
        if tool is None:
            return error_result(f"game-editor: unknown tool {name!r}. Nothing was sent to the Godot editor.")
        return error_result(
            f"game-editor: {name} is not called directly: it asks the editor, where project code can run. Put it as a "
            f"step in plan_change, show the plan, then call apply_plan. Reads from disk are scene_look, script_look, "
            f"file_look and project_look. Nothing was sent to the Godot editor.")

    # ---- reads from disk only (readOnlyHint true). The editor is never asked: no project code can run. ----
    def registry_state(self) -> dict:
        """The Godot AI server's own session table (project folder, open scene, play state). The server keeps it from
        the events the editor sends; reading it does not reach the editor."""
        res = self.upstream("session_manage", {"op": "list"})
        if "bridge_error" in res or res.get("isError"):
            return {}
        data = _first_json(res)
        sessions = [s for s in (data.get("sessions") or []) if isinstance(s, dict)]
        active = [s for s in sessions if s.get("is_active")] or sessions[:1]
        return active[0] if active else {}

    def _disk_scene(self, keys: dict, root: str) -> tuple[str, dict]:
        """(res path, parsed) of the scene a disk read names (scene), or the scene the editor last reported open."""
        scene = keys.get("scene")
        if scene is None:
            scene = str(self.registry_state().get("current_scene") or "")
            if not scene:
                raise Refused("game-editor: no scene is open in the editor (as the Godot AI server last heard), so "
                              "pass scene, a res:// .tscn path. Nothing was read.")
        if not isinstance(scene, str) or not scene.strip().lower().startswith(("res://", "uid://")):
            raise Refused("game-editor: scene is a res:// path to a .tscn file. Nothing was read.")
        res = self.check_paths("scene_look", [scene.strip()], root)[0]
        if res.startswith("uid://"):
            res = self.resolve_uid(res, root) or ""
        if not res.endswith(".tscn"):
            raise Refused(f"game-editor: {scene} is not a text scene (.tscn); a binary .scn cannot be read from disk. "
                          f"Read it with a plan step instead (scene_do op properties, which asks first). Nothing was read.")
        tree = self._disk_tree(res, root)
        return res, tree

    def _disk_tree(self, res: str, root: str, depth: int = 0) -> dict:
        """{"root": name, "nodes": {rel: {type, instance, script, props, groups, from}}, "connections": [...]} for a
        .tscn, an inherited scene's base folded in (what the editor shows as that scene's nodes). A file that does not
        parse, or a base that cannot be read, is refused: nothing is guessed."""
        if depth > 8:
            raise Refused(f"game-editor: {res} inherits more than 8 levels deep; it was not read.")
        try:
            text = self.disk_path(res, root).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise Refused(f"game-editor: {res} could not be read as text ({type(exc).__name__}). Nothing was read.")
        if text.startswith("\ufeff"):
            raise Refused(f"game-editor: {res} starts with a byte-order mark, so it was not parsed. Nothing was read.")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and not stripped.startswith((
                    "[gd_scene", "[ext_resource", "[sub_resource", "[node", "[connection", "[editable")):
                raise Refused(f"game-editor: {res} has a section this plugin does not parse ({stripped[:60]}). "
                              f"Nothing was read.")
        if not text.lstrip().startswith("[gd_scene") or not _brackets_balanced(text):
            raise Refused(f"game-editor: {res} is not a scene file this plugin can parse. Nothing was read.")
        parsed = parse_tscn(text)
        if not parsed.get("root"):
            raise Refused(f"game-editor: {res} has no root node this plugin can parse. Nothing was read.")
        ext = parsed.get("ext") or {}
        nodes: dict[str, dict] = {}
        base_ref = parsed.get("root_scene")
        if base_ref:
            target = parsed.get("root_scene_uid") or base_ref
            base_res = self.resolve_uid(str(target), root) if str(target).startswith("uid://") else str(target)
            if not base_res or not str(base_res).startswith("res://"):
                base_res = str(base_ref)
            named = ""
            try:
                base_text = self.disk_path(str(base_res), root).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                base_text = ""
            found = re.search(r'\[ext_resource type="Script"[^\]]*path="([^"]+)"', base_text)
            if found:
                named = f" The base file names {found.group(1)}."
            raise Refused(
                f"game-editor: {res} is an inherited scene (its root is an instance of {base_res}). "
                f"The base scene is not in this file, and opening it in the editor would run the base script."
                f"{named} Nothing was read.")
        root_props = dict(parsed.get("root_props") or {})
        merged_root = nodes.get("", {"type": None, "props": {}, "groups": []})
        merged_root["props"] = {**(merged_root.get("props") or {}), **root_props}
        merged_root["groups"] = sorted(set(merged_root.get("groups") or []) | set(parsed.get("root_groups") or []))
        if not base_ref:
            merged_root["type"] = self._node_type_from_text(text, "")
        nodes[""] = merged_root
        for rel, info in (parsed.get("nodes") or {}).items():
            row = nodes.get(rel, {"props": {}, "groups": []})
            row["props"] = {**(row.get("props") or {}), **(info.get("props") or {})}
            row["groups"] = sorted(set(row.get("groups") or []) | set((parsed.get("groups") or {}).get(rel) or []))
            if info.get("instance"):
                row["instance"] = info.get("scene")
            if info.get("local") and not info.get("instance"):
                row["type"] = self._node_type_from_text(text, rel)
            nodes[rel] = row
        for row in nodes.values():
            script = self._script_from_props(row.get("props") or {}, ext, res) if row.get("props") else None
            if script:
                row["script"] = script
        return {"root": parsed["root"], "nodes": nodes, "connections": parsed.get("connections") or [], "res": res}

    @staticmethod
    def _node_type_from_text(text: str, rel: str) -> str | None:
        for line in text.splitlines():
            m = NODE_HEAD.match(line)
            if not m:
                continue
            attrs = {k: v.strip('"') for k, v in ATTR.findall(m.group(1))}
            parent = attrs.get("parent")
            here = "" if parent is None else (attrs.get("name", "") if parent == "." else f"{parent}/{attrs.get('name', '')}")
            if here == rel:
                return attrs.get("type")
        return None

    def disk_read(self, name: str, arguments) -> dict:
        """scene_look, script_look, file_look and project_look: answered from the project's files on disk."""
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return error_result(f"game-editor: {name} takes an object of arguments.")
        keys = dict(arguments)
        op = keys.pop("op", None) or DISK_READS[name]["default"]
        spec = DISK_READS[name]["ops"].get(op)
        if spec is None:
            return error_result(f"game-editor: {name} has no op {op!r}; ops: {', '.join(DISK_READS[name]['ops'])}. "
                                f"Reads from the editor (its live values, logs, cameras, tiles in memory) are plan_change "
                                f"steps: they touch the editor, where project code can run.")
        extra = sorted(set(keys) - set(spec))
        if extra:
            return error_result(f"game-editor: {name} op {op} does not take {', '.join(extra)}; it takes "
                                f"{', '.join(spec) or 'no arguments'}. Nothing was sent to the Godot editor.")
        for value in list(keys.values()):
            if isinstance(value, str):
                why = ambiguous_path(value)
                if why:
                    return error_result(f"game-editor: {name} refused {value!r}: {why}. Nothing was sent to the "
                                        f"Godot editor.")
            if isinstance(value, str) and "://" not in value:
                why = self.odd_node_path(value)
                if why:
                    return error_result(
                        f"game-editor: {name} refused the node path {value!r}: {why}. Use the plain path from "
                        f"scene_look op scene_tree, such as /Main/Player. Nothing was sent to the Godot editor.")
        if name == "file_look" and op == "gameforge_report":
            return self.gameforge_report({"op": op, **keys})
        reason = self.ensure_child()
        if reason:
            return error_result(reason)
        root, problem = self.project_root()
        if problem or not root:
            return error_result("game-editor: the Godot AI server does not know an open project, so there is no folder "
                                "to read. Open the project in Godot with the Godot AI add-on enabled.")
        try:
            payload = getattr(self, f"_disk_{name}_{op}")(keys, root)
        except Refused as exc:
            return error_result(str(exc))
        note = DISK_NOTE
        waiting = self.pending_bus_note(root)
        if waiting:
            note += "\n" + waiting
        text = json.dumps(payload, ensure_ascii=False)
        if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
            return error_result(f"game-editor: the answer is larger than {MAX_RESULT_BYTES // 1024} KB; ask for less "
                                f"(a smaller limit, fields, or one node).")
        content = [{"type": "text", "text": note}, {"type": "text", "text": text}]
        search = "find_nodes" if name == "scene_look" and op == "find_nodes" else (
            "search_files" if name == "file_look" and op == "files" else "")
        zero = zero_result_note(search, op, keys, [{"type": "text", "text": text}]) if search else ""
        if zero:
            content.append({"type": "text", "text": zero})
        return {"content": content, "isError": False}

    def _node_rows(self, tree: dict) -> list[tuple[str, dict]]:
        root_name = tree["root"]
        return [(f"/{root_name}" + (f"/{rel}" if rel else ""), info) for rel, info in sorted(tree["nodes"].items())]

    def _disk_scene_look_scene_tree(self, keys: dict, root: str) -> dict:
        res, tree = self._disk_scene(keys, root)
        rows = []
        for path, info in self._node_rows(tree):
            row = {"path": path, "type": info.get("type")}
            for key in ("instance", "script", "inherited_from"):
                if info.get(key):
                    row[key] = info[key]
            rows.append(row)
        offset, limit = int(keys.get("offset") or 0), int(keys.get("limit") or 500)
        return {"scene": res, "total_count": len(rows), "nodes": rows[offset:offset + limit],
                "has_more": offset + limit < len(rows)}

    def _disk_scene_look_find_nodes(self, keys: dict, root: str) -> dict:
        res, tree = self._disk_scene(keys, root)
        name, kind, group = keys.get("name"), keys.get("type"), keys.get("group")
        found = []
        for path, info in self._node_rows(tree):
            if name and str(name).lower() not in path.rsplit("/", 1)[-1].lower():
                continue
            if kind and info.get("type") != kind:
                continue
            if group and group not in (info.get("groups") or []):
                continue
            found.append({"path": path, "type": info.get("type"), **({"instance": info["instance"]}
                                                                    if info.get("instance") else {})})
        return {"scene": res, "nodes": found, "total_count": len(found)}

    def _disk_rel(self, keys: dict, tree: dict) -> str:
        path = keys.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise Refused("game-editor: path is a node path from the scene root, such as /Main/Player. Nothing was read.")
        rel = rel_node_path(path, tree["root"])
        if rel not in tree["nodes"]:
            raise Refused(f"game-editor: {path} is not in {tree['res']} on disk (a node added in the editor and not "
                          f"saved is not in the file). Nothing was read.")
        return rel

    def _disk_scene_look_children(self, keys: dict, root: str) -> dict:
        res, tree = self._disk_scene(keys, root)
        rel = self._disk_rel(keys, tree)
        kids = [r for r in tree["nodes"] if r and _parent(r) == rel]
        return {"scene": res, "path": keys["path"], "children": [
            {"path": f"/{tree['root']}/{r}", "type": tree["nodes"][r].get("type")} for r in sorted(kids)]}

    def _disk_scene_look_properties(self, keys: dict, root: str) -> dict:
        res, tree = self._disk_scene(keys, root)
        rel = self._disk_rel(keys, tree)
        props = tree["nodes"][rel].get("props") or {}
        fields = keys.get("fields")
        if isinstance(fields, list):
            props = {k: v for k, v in props.items() if k in fields}
        return {"scene": res, "path": keys["path"], "type": tree["nodes"][rel].get("type"),
                "stored": {k: v.strip() for k, v in props.items()},
                "note": "values stored in the file; a property at its default is not stored and not listed"}

    def _disk_scene_look_groups(self, keys: dict, root: str) -> dict:
        res, tree = self._disk_scene(keys, root)
        rel = self._disk_rel(keys, tree)
        return {"scene": res, "path": keys["path"], "groups": tree["nodes"][rel].get("groups") or []}

    def _disk_scene_look_signals(self, keys: dict, root: str) -> dict:
        res, tree = self._disk_scene(keys, root)
        rel = self._disk_rel(keys, tree)
        where = f"/{tree['root']}" + (f"/{rel}" if rel else "")
        rows = [c for c in tree["connections"] if c.get("from") == where or c.get("to") == where]
        return {"scene": res, "path": keys["path"], "connections": rows}

    def _disk_text(self, keys: dict, root: str, what: str) -> tuple[str, str]:
        path = keys.get("path")
        if not isinstance(path, str) or not path.strip().lower().startswith(("res://", "uid://")):
            raise Refused(f"game-editor: path is a res:// path. Nothing was read.")
        res = self.check_paths(what, [path.strip()], root)[0]
        if res.startswith("uid://"):
            res = self.resolve_uid(res, root) or ""
        target = self.disk_path(res, root)
        if not target.is_file():
            raise Refused(f"game-editor: {res} is not a file in the project. Nothing was read.")
        if target.stat().st_size > MAX_READ_FILE_BYTES:
            raise Refused(f"game-editor: {res} is larger than {MAX_READ_FILE_BYTES // 1024} KB; no partial read was "
                          f"returned.")
        try:
            return res, target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise Refused(f"game-editor: {res} is not UTF-8 text. Nothing was read.")

    def _disk_script_look_source(self, keys: dict, root: str) -> dict:
        res, text = self._disk_text(keys, root, "script_look")
        return {"path": res, "source": text, "lines": len(text.splitlines())}

    def _disk_script_look_outline(self, keys: dict, root: str) -> dict:
        res, text = self._disk_text(keys, root, "script_look")
        if res.endswith(".cs"):
            return {"path": res, "language": "csharp", "tool": csharp_is_tool(text),
                    "methods": sorted(set(re.findall(r"\b(?:void|bool|int|float|string|Variant|\w+)\s+(\w+)\s*\(",
                                                     _cs_code(text))))}
        info = gdscript_header(text)
        if info is None:
            raise Refused(f"game-editor: {res} does not parse (a string or bracket is not closed). Read its source.")
        signals = re.findall(r"(?m)^\s*signal\s+([A-Za-z_]\w*)", text)
        exports = re.findall(r"(?m)^\s*@export\w*(?:\([^)]*\))?\s+var\s+([A-Za-z_]\w*)", text)
        return {"path": res, "language": "gdscript", "tool_annotation": bool(info["tool"]), "extends": info["extends"],
                "class_name": info["class_name"], "functions": [f for f in info["funcs"] if f != "@getter"],
                "signals": signals, "exports": exports}

    def _disk_file_look_text(self, keys: dict, root: str) -> dict:
        res, text = self._disk_text(keys, root, "file_look")
        if res.replace("\\", "/").rstrip("/").endswith("export_presets.cfg"):
            text = redact_preset_text(text)
        return {"path": res, "text": text}

    def _disk_walk(self, root: str, folder: str = "res://") -> list[str]:
        base = self.disk_path(self.check_paths("file_look", [folder], root)[0], root) if folder != "res://" else Path(root)
        out = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and not secret_like(d, True))
            for name in sorted(filenames):
                if name.startswith(".") or secret_like(name):
                    continue
                out.append("res://" + (Path(dirpath) / name).relative_to(Path(root)).as_posix())
        return out

    def _disk_file_look_files(self, keys: dict, root: str) -> dict:
        folder = keys.get("path") if isinstance(keys.get("path"), str) and keys.get("path") else "res://"
        name, kind = keys.get("name"), keys.get("type")
        rows = [p for p in self._disk_walk(root, folder)
                if (not name or str(name).lower() in p.rsplit("/", 1)[-1].lower())
                and (not kind or p.lower().endswith("." + str(kind).lower().lstrip(".")))]
        offset, limit = int(keys.get("offset") or 0), int(keys.get("limit") or 200)
        return {"files": rows[offset:offset + limit], "total_count": len(rows), "has_more": offset + limit < len(rows)}

    def _disk_file_look_resources(self, keys: dict, root: str) -> dict:
        kind = keys.get("type")
        rows = []
        for p in self._disk_walk(root):
            if not p.endswith((".tres", ".tscn")):
                continue
            try:
                head = self.disk_path(p, root).read_text(encoding="utf-8", errors="ignore")[:300]
            except OSError:
                continue
            m = re.search(r'\[gd_resource type="([^"]+)"', head)
            found = m.group(1) if m else ("PackedScene" if head.startswith("[gd_scene") else None)
            if kind and found != kind:
                continue
            rows.append({"path": p, "type": found})
        return {"resources": rows[:500], "total_count": len(rows)}

    def _disk_project_look_get(self, keys: dict, root: str) -> dict:
        key = keys.get("key")
        if not isinstance(key, str) or "/" not in key:
            raise Refused("game-editor: key is a project setting such as application/config/name.")
        value = project_setting(Path(root), key)
        return {"key": key, "value": value, "set_in_project_godot": value is not None}

    def _project_section(self, root: str, section: str) -> dict[str, str]:
        try:
            text = (Path(root) / "project.godot").read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise Refused("game-editor: project.godot could not be read.")
        out, current, key = {}, "", None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
                current = stripped[1:-1]
                key = None
                continue
            if current != section:
                continue
            if "=" in line and not line.startswith((" ", "\t")):
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip()
            elif key:
                out[key] += "\n" + line
        return out

    def _disk_project_look_autoloads(self, keys: dict, root: str) -> dict:
        return {"autoloads": [{"name": k, "path": v.strip('"').lstrip("*"), "singleton": v.strip('"').startswith("*")}
                              for k, v in self._project_section(root, "autoload").items()]}

    def _disk_project_look_input_map(self, keys: dict, root: str) -> dict:
        rows = []
        for action, raw in self._project_section(root, "input").items():
            keycodes = re.findall(r'"(?:physical_keycode|keycode)":\s*(\d+)', raw)
            buttons = re.findall(r'"button_index":\s*(\d+)', raw)
            rows.append({"action": action, "keycodes": [int(k) for k in keycodes if k != "0"],
                         "buttons": [int(b) for b in buttons]})
        return {"actions": rows, "note": "actions set in project.godot; Godot's built-in ui_* actions are not listed "
                                         "unless the project changed them"}

    # ---- GameForge's playtest report -> a fix plan here ----
    _GD_AT = re.compile(r"(res://[^\s()'\"|]+?\.(?:gd|cs)):(\d+)")
    _GD_BARE = re.compile(r"(?<![\w/.:])([A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:gd|cs)):(\d+)")

    def _bare_script(self, name: str, root: str) -> str | None:
        """res:// path of the one project script with this file name (Godot's editor log prints "main.gd:7"), or None
        when there is none or more than one."""
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "addons"]
            if name in filenames:
                found.append("res://" + (Path(dirpath) / name).relative_to(Path(root)).as_posix())
            if len(found) > 1:
                return None
        return found[0] if found else None

    def gameforge_report(self, arguments: dict) -> dict:
        """Read a GameForge playtest report (gameforge/engines/godot.py playtest, or the web runner's report.json:
        verdict and issues of severity, title and detail) and return its findings with the res:// file, line and that
        line's code for each script error, plus plan_change steps that fix the line and play again. Reads files only;
        it does not contact the editor."""
        extra = sorted(set(arguments) - {"op", "path"})
        if extra:
            return error_result(f"game-editor: file_look op gameforge_report takes path only (a report.json or a "
                                f"folder holding one). It does not take {', '.join(extra)}.")
        reason = self.ensure_child()
        if reason:
            return error_result(reason)
        root, problem = self.project_root()
        if problem or root is None:
            return error_result("game-editor: file_look op gameforge_report needs the Godot editor open on the "
                                "project, to know which project's reports to read.")
        given = arguments.get("path")
        if given is not None and (not isinstance(given, str) or not given.strip().lower().startswith("res://")):
            return error_result("game-editor: file_look op gameforge_report path is a res:// path to a report.json "
                                "or a folder.")
        if given:
            res = self.check_paths("file_look", [given.strip()], root)[0]
            target = self.disk_path(res, root)
            if target.is_dir():
                res = res.rstrip("/") + "/report.json"
                target = self.disk_path(res, root)
        else:
            found = sorted(Path(root).glob("playtests/*/report.json"), key=lambda path: path.stat().st_mtime)
            if not found:
                return error_result("game-editor: file_look op gameforge_report found no res://playtests/*/report.json. "
                                    "GameForge writes one there when forge_playtest runs (target godot or web); pass "
                                    "path if it is somewhere else.")
            target = found[-1]
            res = "res://" + target.relative_to(Path(root)).as_posix()
            res = self.check_paths("file_look", [res], root)[0]
        try:
            if target.stat().st_size > MAX_READ_FILE_BYTES:
                return error_result(f"game-editor: {res} is larger than {MAX_READ_FILE_BYTES // 1024} KB; not read.")
            report = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return error_result(f"game-editor: {res} could not be read as JSON ({type(exc).__name__}).")
        if not isinstance(report, dict) or not isinstance(report.get("issues"), list) or "verdict" not in report:
            return error_result(f"game-editor: {res} is not a GameForge playtest report (it has no verdict and issues).")
        findings = []
        steps: list[dict] = []
        seen: set[tuple[str, int]] = set()
        for issue in report["issues"][:40]:
            if not isinstance(issue, dict):
                continue
            row = {k: issue.get(k) for k in ("severity", "title", "detail", "t") if k in issue}
            detail = str(issue.get("detail") or "")
            located = self._GD_AT.findall(detail)
            if not located:
                for bare, number in self._GD_BARE.findall(detail):
                    resolved = self._bare_script(bare, root)
                    if resolved:
                        located.append((resolved, number))
            if located:
                script, line_no = located[-1][0], int(located[-1][1])
                for candidate, number in located:
                    if not candidate.startswith("res://addons/"):
                        script, line_no = candidate, int(number)
                        break
                row["file"], row["line"] = script, line_no
                try:
                    lines = self.disk_path(self.check_paths("file_look", [script], root)[0], root).read_text(
                        encoding="utf-8", errors="replace").splitlines()
                    code = lines[line_no - 1] if 0 < line_no <= len(lines) else None
                except (OSError, Refused, ValueError):
                    code = None
                if code is not None:
                    row["code"] = code
                    if (script, line_no) not in seen and code.strip():
                        seen.add((script, line_no))
                        # script_do op script replaces the first match: take lines above until the text is unique.
                        start = line_no - 1
                        old_text = code
                        whole = "\n".join(lines)
                        while whole.count(old_text) > 1 and start > 0:
                            start -= 1
                            old_text = lines[start] + "\n" + old_text
                        if whole.count(old_text) > 1:
                            row["note"] = f"line {line_no} of {script} is not unique in the file; fix it by hand"
                        else:
                            steps.append({"tool": "script_do", "op": "script", "path": script, "old_text": old_text,
                                          "new_text": None})
                else:
                    row["code"] = None
                    row["note"] = f"{script} has no line {line_no} now; the file changed after the playtest"
            moved = re.search(r"at \((-?\d+), (-?\d+)\)", detail)
            if moved and "stuck" in str(issue.get("title") or "").lower():
                x, y = int(moved.group(1)), int(moved.group(2))
                row["cell"] = {"x": x // 16, "y": y // 16, "tile_size": 16}
                row["look"] = "asset_look op tiles on the ground layer near that cell (a wall or a hole there)"
            findings.append(row)
        if steps:
            steps.append({"tool": "game_do", "op": "play_session", "goal": "score >= 1",
                          "steps": [{"op": "action", "action": "ui_right", "seconds": 2}], "autosave": False})
        payload = {"source": res, "verdict": report.get("verdict"), "engine": report.get("engine"),
                   "events": report.get("events"), "findings": findings, "fix_plan": {"steps": steps} if steps else None}
        head = (f"game-editor: GameForge playtest {report.get('verdict')} in {res}: {len(findings)} finding(s)"
                + (f", {len(steps) - 1} line(s) to fix. fix_plan.steps is a plan_change step list: write new_text for "
                   f"each script step (old_text is that line's code now), set the play step's goal and input to this "
                   f"game, then call plan_change and show the plan. GameForge found it; this plugin fixes it in the open "
                   f"project and plays it again." if steps else ". No script error has a res:// file and line to fix "
                   f"here; quote the findings."))
        return json_result(escape_controls(head) + "\n", payload, False)

    # ---- place_assets: GameForge-format files into the open scene ----
    def _asset_file(self, step_no: int, value, root: str, kinds: tuple[str, ...], what: str) -> tuple[str, Path]:
        if not isinstance(value, str) or not value.strip().lower().startswith("res://"):
            raise Refused(f"game-editor: place_assets step {step_no} needs {what}, a res:// path. Nothing was sent to "
                          f"the Godot editor.")
        res = self.check_paths("place_assets", [value.strip()], root)[0]
        if not res.lower().endswith(kinds):
            raise Refused(f"game-editor: place_assets step {step_no}: {res} is not a {' or '.join(kinds)} file. "
                          f"Nothing was sent to the Godot editor.")
        path = self.disk_path(res, root)
        if not path.is_file():
            raise Refused(f"game-editor: place_assets step {step_no}: {res} is not in the project. If it was just "
                          f"written, it is there once the file exists on disk. Nothing was sent to the Godot editor.")
        return res, path

    def _res_neighbor(self, res: str, suffix: str) -> str:
        stem = res.rsplit(".", 1)[0]
        return stem + suffix

    def expand_asset_steps(self, steps: list[dict], root: str | None) -> tuple[list[dict], list[str]]:
        """Replace each place_assets step with the Godot AI steps that do it. The plan lists both."""
        self._local.place_checks = []
        self._local.pending_files = {}
        if not any(isinstance(step, dict) and step.get("tool") == "place_assets" for step in steps):
            return steps, []
        if root is None:
            raise Refused("game-editor: place_assets needs the Godot editor open on the project.")
        out: list[dict] = []
        notes: list[str] = []
        scene_root = (self.read_scene(self.current_scene(), root) or {}).get("root") or ""
        for number, step in enumerate(steps, 1):
            tool = step.get("tool")
            if tool in ("scene_do",) and step.get("op") == "new_scene" and isinstance(step.get("path"), str):
                path = step["path"].strip()
                scene_root = step.get("root_name") or path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            elif tool == "scene_do" and step.get("op") == "open_scene" and isinstance(step.get("path"), str):
                scene_root = (self.read_scene(step["path"].strip(), root) or {}).get("root") or scene_root
            if tool != "place_assets":
                out.append(step)
                continue
            op = step.get("op")
            if op not in ASSET_STEPS:
                raise Refused(f"game-editor: place_assets step {number} needs op, one of: {', '.join(ASSET_STEPS)}. "
                              f"Nothing was sent to the Godot editor.")
            extra = sorted(set(step) - {"tool", "op"} - set(ASSET_STEPS[op]["keys"]))
            if extra:
                raise Refused(f"game-editor: place_assets op {op} does not take {', '.join(extra)}; it takes "
                              f"{', '.join(ASSET_STEPS[op]['keys'])}. Nothing was sent to the Godot editor.")
            parent = step.get("parent_path") if isinstance(step.get("parent_path"), str) and step.get("parent_path") \
                else f"/{scene_root}"
            if not scene_root and not step.get("parent_path") and op != "buses":
                raise Refused(f"game-editor: place_assets step {number}: no scene is open. Open or create the scene "
                              f"first (scene_do op open_scene or new_scene in the same plan). Nothing was sent.")
            made = getattr(self, f"_place_{op}")(number, step, parent.rstrip("/"), root)
            scene_now = self.current_scene()
            if scene_root == ((self.read_scene(scene_now, root) or {}).get("root") or "") and scene_now:
                for added in made["steps"]:
                    if added.get("op") in ("add_node", "audio_player_create") and isinstance(added.get("name"), str):
                        if self.node_taken(scene_now, added.get("parent_path") or parent, added["name"], root):
                            raise Refused(
                                f"game-editor: place_assets step {number} would add {parent.rstrip('/')}/"
                                f"{added['name']}, and a node with that name is already there (Godot would rename "
                                f"the new one and the next steps would change the existing node). Pass another "
                                f"name. Nothing was sent to the Godot editor.")
            first = len(out) + 1
            out.extend(made["steps"])
            notes.append(f"place_assets op {op} (your step {number}) is steps {first} to {len(out)}: {made['summary']}")
            self._local.place_checks.extend(made.get("checks") or [])
        return out, notes

    def _project_nearest(self, root: str) -> bool:
        """True when the project already draws 2D textures with Nearest filtering (GameForge's own projects set
        rendering/textures/canvas_textures/default_texture_filter=0)."""
        value = project_setting(Path(root), "rendering/textures/canvas_textures/default_texture_filter")
        return value is not None and value.strip() == "0"

    def _relink_tres(self, number: int, res: str, text: str, root: str, kinds: tuple[str, ...]) -> tuple[str | None, list[str]]:
        """A .tres whose ext_resource paths point at files this project does not have, rewritten to the file of the
        same name beside the .tres: GameForge writes res:// paths of its own project folder, so a .tres copied into
        another project (or another folder) points nowhere and Godot cannot load it. (new text or None, notes).
        A path with no file of that name beside the .tres is refused."""
        folder = res.rsplit("/", 1)[0]
        new_text = text
        notes = []
        for line in re.findall(r"(?m)^\[ext_resource[^\n]*\]$", text):
            match = re.search(r'path="([^"]+)"', line)
            if not match:
                continue
            ref = match.group(1)
            target = ref if ref.startswith("res://") else None
            if target is not None:
                try:
                    target = self.check_paths("place_assets", [target], root)[0]
                except Refused:
                    target = None
            if target is not None and self.disk_path(target, root).is_file():
                if not target.lower().endswith(kinds):
                    raise Refused(f"game-editor: place_assets step {number}: {res} uses {target}, which is not a "
                                  f"{' or '.join(kinds)} file. Nothing was sent to the Godot editor.")
                continue
            name = ref.replace("\\", "/").rsplit("/", 1)[-1]
            beside = f"{folder}/{name}"
            if not name or not name.lower().endswith(kinds) or not self.disk_path(beside, root).is_file():
                raise Refused(f"game-editor: place_assets step {number}: {res} uses {ref}, which is not in this project "
                              f"and has no file named {name or '(none)'} beside the .tres. A .tres made in another "
                              f"project keeps that project's res:// paths. Copy the image next to the .tres, or pass "
                              f"sheet (the .png) so a new .tres is written. Nothing was sent to the Godot editor.")
            fixed = re.sub(r'\s+uid="[^"]*"', "", line).replace(f'path="{ref}"', f'path="{beside}"')
            new_text = new_text.replace(line, fixed)
            notes.append(f"{res} points at {ref}, which is not in this project (a .tres keeps the res:// paths of the "
                         f"project it was made in, such as GameForge's own folder); the plan rewrites that line to "
                         f"{beside}, the file of the same name beside it")
        return (new_text if new_text != text else None), notes

    def _pixel_filter_step(self, path: str, gameforge: bool, size: int | None, root: str) -> tuple[list[dict], str]:
        """Nearest filtering for GameForge pixel art (32 px or smaller), so the open project's default (Linear)
        does not blur it. Not added when the project already defaults to Nearest."""
        if not gameforge or size is None or size > 32:
            return [], ""
        if self._project_nearest(root):
            return [], "texture filter: the project already draws textures Nearest"
        return ([{"tool": "scene_do", "op": "set_node_property", "path": path, "property": "texture_filter", "value": 1}],
                f"texture_filter Nearest on {path} (GameForge pixel art, {size} px; this project's default filter is "
                f"Linear, which blurs it)")

    def _place_sprite_sheet(self, number: int, step: dict, parent: str, root: str) -> dict:
        frames_res = None
        written = None
        anims: list[str] = []
        atlas_res = None
        if step.get("frames"):
            frames_res, frames_path = self._asset_file(number, step["frames"], root, (".tres",), "frames")
        else:
            if step.get("sheet"):
                sheet_res, _sheet = self._asset_file(number, step["sheet"], root, (".png",), "sheet")
                beside = self._res_neighbor(sheet_res, "_frames.tres")
                if self.disk_path(beside, root).is_file():
                    frames_res = beside
                else:
                    atlas_res = self._res_neighbor(sheet_res, ".json")
            elif step.get("atlas"):
                atlas_res = step["atlas"]
            else:
                raise Refused(f"game-editor: place_assets step {number} op sprite_sheet needs frames (a SpriteFrames "
                              f".tres), sheet (.png) or atlas (.json). Nothing was sent to the Godot editor.")
            if frames_res is None:
                atlas_res, atlas_path = self._asset_file(number, atlas_res, root, (".json",), "atlas")
                try:
                    atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise Refused(f"game-editor: place_assets step {number}: {atlas_res} is not JSON. Nothing was sent.")
                image = ((atlas.get("meta") or {}).get("image")) if isinstance(atlas, dict) else None
                if not isinstance(image, str) or "/" in image or "\\" in image or image.startswith("."):
                    raise Refused(f"game-editor: place_assets step {number}: {atlas_res} does not name its image in "
                                  f"meta.image (a file next to it). Nothing was sent to the Godot editor.")
                sheet_res = atlas_res.rsplit("/", 1)[0] + "/" + image
                sheet_res, sheet_path = self._asset_file(number, sheet_res, root, (".png",), "the sheet")
                size = png_size(sheet_path.read_bytes()[:32])
                try:
                    text, anims = spriteframes_from_atlas(sheet_res, atlas)
                except ValueError as exc:
                    raise Refused(f"game-editor: place_assets step {number}: {atlas_res}: {exc}. Nothing was sent.")
                for rect in re.findall(r"Rect2\((\d+), (\d+), (\d+), (\d+)\)", text):
                    x, y, w, h = (int(v) for v in rect)
                    if size and (x + w > size[0] or y + h > size[1]):
                        raise Refused(f"game-editor: place_assets step {number}: a frame in {atlas_res} lies outside "
                                      f"{sheet_res} ({size[0]}x{size[1]}). Nothing was sent to the Godot editor.")
                frames_res = self._res_neighbor(sheet_res, "_frames.tres")
                written = {"tool": "file_do", "op": "write_file", "path": frames_res, "content": text}
        relink_notes: list[str] = []
        if written is None:
            text = self.disk_path(frames_res, root).read_text(encoding="utf-8", errors="ignore")
            if not re.match(r'\s*\[gd_resource type="SpriteFrames"', text):
                raise Refused(f"game-editor: place_assets step {number}: {frames_res} is not a SpriteFrames resource. "
                              f"Nothing was sent to the Godot editor.")
            fixed, relink_notes = self._relink_tres(number, frames_res, text, root,
                                                    (".png", ".webp", ".svg", ".jpg", ".jpeg", ".tres"))
            if fixed is not None:
                written = {"tool": "file_do", "op": "write_file", "path": frames_res, "content": fixed, "overwrite": True}
                text = fixed
            anims = re.findall(r'"name":\s*&"([^"]+)"', text)
        sheet_refs = re.findall(r'path="(res://[^"]+\.png)"', written["content"] if written else text)
        needs_scan = any(not self.disk_path(ref + ".import", root).is_file() for ref in sheet_refs)
        anim = step.get("animation") if isinstance(step.get("animation"), str) and step.get("animation") else (
            anims[0] if anims else "default")
        if anims and anim not in anims:
            raise Refused(f"game-editor: place_assets step {number}: {frames_res} has no animation {anim!r}; it has "
                          f"{', '.join(anims)}. Nothing was sent to the Godot editor.")
        name = step.get("name") if isinstance(step.get("name"), str) and step.get("name") else \
            re.sub(r"[^A-Za-z0-9_]", "", frames_res.rsplit("/", 1)[-1].split(".")[0].replace("_frames", "").title()) or "Sprite"
        path = f"{parent}/{name}"
        out = []
        if written:
            out.append(written)
        if needs_scan or written:
            out.append({"tool": "file_do", "op": "scan"})
        out += [{"tool": "scene_do", "op": "add_node", "type": "AnimatedSprite2D", "name": name, "parent_path": parent},
                {"tool": "scene_do", "op": "set_node_property", "path": path, "property": "sprite_frames",
                 "value": frames_res},
                {"tool": "scene_do", "op": "set_node_property", "path": path, "property": "animation", "value": anim}]
        if step.get("autoplay", True) is not False:
            out.append({"tool": "scene_do", "op": "set_node_property", "path": path, "property": "autoplay",
                        "value": anim})
        if isinstance(step.get("position"), dict):
            out.append({"tool": "scene_do", "op": "set_node_property", "path": path, "property": "position",
                        "value": step["position"]})
        content = written["content"] if written else text
        sizes = [max(int(w), int(h)) for _x, _y, w, h in re.findall(r"Rect2\((\d+), (\d+), (\d+), (\d+)\)", content)]
        atlas_json = self._res_neighbor(sheet_refs[0], ".json") if sheet_refs else None
        gameforge = False
        if atlas_json and self.disk_path(atlas_json, root).is_file():
            try:
                gameforge = gameforge_atlas(json.loads(self.disk_path(atlas_json, root).read_text(encoding="utf-8")))
            except (OSError, ValueError):
                gameforge = False
        filter_steps, filter_note = self._pixel_filter_step(path, gameforge, max(sizes) if sizes else None, root)
        out += filter_steps
        summary = (f"AnimatedSprite2D {path} with {frames_res}" + (" (written from " + str(atlas_res) + ")" if written
                   and atlas_res else "") + f", animation {anim}" + (f" of {', '.join(anims)}" if anims else "") +
                   "".join(f"; {note}" for note in relink_notes) + (f"; {filter_note}" if filter_note else ""))
        checks = [{"kind": "sprite", "path": path, "frames": frames_res, "animation": anim}]
        if filter_steps:
            checks.append({"kind": "filter", "path": path})
        return {"steps": out, "summary": summary, "checks": checks}

    def _place_tileset(self, number: int, step: dict, parent: str, root: str) -> dict:
        written = None
        layout = None
        relink_notes: list[str] = []
        if step.get("tileset"):
            tres_res, tres_path = self._asset_file(number, step["tileset"], root, (".tres",), "tileset")
            text = tres_path.read_text(encoding="utf-8", errors="ignore")
        elif step.get("sheet"):
            sheet_res, _sheet = self._asset_file(number, step["sheet"], root, (".png",), "sheet")
            tres_res = self._res_neighbor(sheet_res, ".tres")
            if self.disk_path(tres_res, root).is_file():
                text = self.disk_path(tres_res, root).read_text(encoding="utf-8", errors="ignore")
            else:
                layout_res, layout_path = self._asset_file(number, self._res_neighbor(sheet_res, ".json"), root,
                                                           (".json",), "the GameForge layout .json")
                try:
                    layout = json.loads(layout_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise Refused(f"game-editor: place_assets step {number}: {layout_res} is not JSON. Nothing was sent.")
                text = tileset_from_layout(sheet_res, layout if isinstance(layout, dict) else {})
                written = {"tool": "file_do", "op": "write_file", "path": tres_res, "content": text}
        else:
            raise Refused(f"game-editor: place_assets step {number} op tileset needs tileset (a TileSet .tres) or "
                          f"sheet (.png). Nothing was sent to the Godot editor.")
        if not re.match(r'\s*\[gd_resource type="TileSet"', text):
            raise Refused(f"game-editor: place_assets step {number}: {tres_res} is not a TileSet resource. Nothing "
                          f"was sent to the Godot editor.")
        if written is None:
            fixed, relink_notes = self._relink_tres(number, tres_res, text, root, (".png", ".webp", ".svg", ".jpg", ".jpeg"))
            if fixed is not None:
                written = {"tool": "file_do", "op": "write_file", "path": tres_res, "content": fixed, "overwrite": True}
                text = fixed
        refs = re.findall(r'\[ext_resource[^\]]*path="(res://[^"]+)"', text)
        tiles = set(re.findall(r"(?m)^(\d+):(\d+)/0 = 0\s*$", text))
        if layout is None:
            try:
                layout = json.loads(self.disk_path(self._res_neighbor(tres_res, ".json"), root).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                layout = None
        size_match = re.search(r"(?m)^tile_size = Vector2i\((\d+), (\d+)\)", text)
        tile_size = int(size_match.group(1)) if size_match else int((layout or {}).get("tile_size") or 16) \
            if isinstance(layout, dict) else 16
        name = step.get("name") if isinstance(step.get("name"), str) and step.get("name") else "Ground"
        path = f"{parent}/{name}"
        needs_scan = any(not self.disk_path(ref + ".import", root).is_file() for ref in refs)
        out = []
        if written:
            out.append(written)
        if needs_scan or written:
            out.append({"tool": "file_do", "op": "scan"})
        out += [{"tool": "scene_do", "op": "add_node", "type": "TileMapLayer", "name": name, "parent_path": parent},
                {"tool": "scene_do", "op": "set_node_property", "path": path, "property": "tile_set", "value": tres_res}]
        gameforge = gameforge_layout(layout)
        filter_steps, filter_note = self._pixel_filter_step(path, gameforge, tile_size, root)
        if step.get("map") is not None:
            rows = step.get("map")
            if isinstance(rows, str):
                rows = rows.splitlines()
            if (not isinstance(rows, list) or not rows or not all(isinstance(row, str) and row for row in rows)
                    or len(rows) > 64 or max(len(row) for row in rows) > 64):
                raise Refused(f"game-editor: place_assets step {number}: map is a list of 1 to 64 text rows of up to 64 "
                              f"characters (\"~\" is water, any other character land, or a 0/1 mask). Nothing was sent.")
            if "fill" in step or "tile" in step:
                raise Refused(f"game-editor: place_assets step {number}: pass map, or fill and tile, not both. Nothing "
                              f"was sent to the Godot editor.")
            wang = all((str(i % 4), str(i // 4)) in tiles for i in range(16)) if tiles else True
            if not wang:
                raise Refused(f"game-editor: place_assets step {number}: {tres_res} does not have the 16 Wang tiles in "
                              f"rows 0 to 3 that a map needs (GameForge's tileset layout). Use fill and tile. Nothing "
                              f"was sent to the Godot editor.")
            variants = bool(tiles) and all((str(x), str(r)) in tiles for x in range(4) for r in (4, 5))
            cells = dual_grid_cells(rows, variants)
            half = tile_size / 2
            out.append({"tool": "scene_do", "op": "set_node_property", "path": path, "property": "position",
                        "value": {"x": -half, "y": -half}})
            out += filter_steps
            for start in range(0, len(cells), 500):
                out.append({"tool": "batch", "op": "batch_execute", "commands": [
                    {"command": "tilemap_set_cell", "params": {"path": path, "map_x": x, "map_y": y, "source_id": 0,
                                                               "atlas_col": col, "atlas_row": row}}
                    for x, y, col, row in cells[start:start + 500]]})
            edges = sum(1 for _x, _y, col, row in cells if row < 4 and 0 < col + row * 4 < 15)
            swapped = sum(1 for _x, _y, _col, row in cells if row >= 4)
            summary = (f"TileMapLayer {path} with {tres_res}" + (" (written from the layout .json)" if written and not
                       relink_notes else "") + f", GameForge's dual grid over a {max(len(r) for r in rows)}x{len(rows)} "
                       f"map: {len(cells)} cells, {edges} Wang edge tiles where the terrains meet, {swapped} fill "
                       f"variants, layer offset (-{half:g}, -{half:g})")
            summary += "".join(f"; {note}" for note in relink_notes) + (f"; {filter_note}" if filter_note else "")
            checks = [{"kind": "tiles", "path": path, "cells": len(cells)}]
            if filter_steps:
                checks.append({"kind": "filter", "path": path})
            return {"steps": out, "summary": summary, "checks": checks}
        tile = step.get("tile") if isinstance(step.get("tile"), dict) else None
        if tile is None:
            row = ((layout.get("fill_variants") or {}).get("upper_row")) if isinstance(layout, dict) else None
            tile = {"col": 0, "row": int(row) if isinstance(row, int) else 0}
        try:
            col, row = int(tile.get("col", 0)), int(tile.get("row", 0))
        except (TypeError, ValueError):
            raise Refused(f"game-editor: place_assets step {number}: tile is {{col, row}} numbers. Nothing was sent.")
        if tiles and (str(col), str(row)) not in tiles:
            raise Refused(f"game-editor: place_assets step {number}: {tres_res} has no tile at col {col}, row {row}. "
                          f"Nothing was sent to the Godot editor.")
        fill = step.get("fill") if isinstance(step.get("fill"), dict) else {}
        try:
            rect = [int(fill.get(k, d)) for k, d in (("x", 0), ("y", 0), ("w", 10), ("h", 6))]
        except (TypeError, ValueError):
            raise Refused(f"game-editor: place_assets step {number}: fill is {{x, y, w, h}} numbers. Nothing was sent.")
        if rect[2] <= 0 or rect[3] <= 0 or rect[2] * rect[3] > 4096:
            raise Refused(f"game-editor: place_assets step {number}: fill w and h must be above 0 and w*h at most "
                          f"4096 cells. Nothing was sent to the Godot editor.")
        out += filter_steps
        out.append({"tool": "asset_do", "op": "tile_fill_rect", "path": path, "source_id": 0, "atlas_col": col,
                    "atlas_row": row, "rect_x": rect[0], "rect_y": rect[1], "rect_w": rect[2], "rect_h": rect[3]})
        summary = (f"TileMapLayer {path} with {tres_res}" + (" (written from the layout .json)" if written and not
                   relink_notes else "") + f", {rect[2]}x{rect[3]} cells of tile ({col}, {row}) from cell ({rect[0]}, "
                   f"{rect[1]})" + "".join(f"; {note}" for note in relink_notes) +
                   (f"; {filter_note}" if filter_note else ""))
        checks = [{"kind": "tiles", "path": path, "cells": rect[2] * rect[3]}]
        if filter_steps:
            checks.append({"kind": "filter", "path": path})
        return {"steps": out, "summary": summary, "checks": checks}

    def _bus_steps(self, number: int, bus: str, root: str) -> tuple[list[dict], str]:
        """Steps that add bus to the project's bus layout file when it is missing, and a sentence for the plan."""
        if not bus or bus == "Master":
            return [], ""
        if not re.fullmatch(r"[A-Za-z0-9_ ]{1,40}", bus):
            raise Refused(f"game-editor: place_assets step {number}: bus is a name of letters, digits, spaces or "
                          f"underscores. Nothing was sent to the Godot editor.")
        try:
            project = (Path(root) / "project.godot").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            project = ""
        match = re.search(r'(?m)^buses/default_bus_layout\s*=\s*"([^"]+)"', project)
        layout_res = match.group(1) if match else "res://default_bus_layout.tres"
        layout_res = self.check_paths("place_assets", [layout_res], root)[0]
        if not layout_res.endswith(".tres"):
            raise Refused(f"game-editor: place_assets step {number}: the project's bus layout is {layout_res}, not a "
                          f"text .tres file, so bus {bus} was not added to it. Add the bus in Godot's Audio panel, or "
                          f"pass bus Master. Nothing was sent to the Godot editor.")
        path = self.disk_path(layout_res, root)
        pending = getattr(self._local, "pending_files", {})
        exists = path.is_file() or layout_res in pending
        text = pending.get(layout_res) if layout_res in pending else (
            path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else "")
        if re.search(r'(?m)^bus/\d+/name\s*=\s*&?"' + re.escape(bus) + '"', text):
            return [], f"bus {bus} is in {layout_res}"
        step = {"tool": "file_do", "op": "write_file", "path": layout_res, "content": bus_layout_with(text, bus)}
        if exists:
            step["overwrite"] = True
        pending[layout_res] = step["content"]
        self._local.pending_files = pending
        return [step], (f"bus {bus} is added to {layout_res}; the open editor read its bus layout when the project "
                        f"opened, so Godot keeps the player on Master until the project is opened again")

    def _place_audio(self, number: int, step: dict, parent: str, root: str, music: bool) -> dict:
        stream_res, stream_path = self._asset_file(number, step.get("stream"), root, (".wav", ".ogg", ".mp3"), "stream")
        kind = audio_kind(stream_path.read_bytes()[:16])
        if not kind or not stream_res.lower().endswith("." + kind):
            raise Refused(f"game-editor: place_assets step {number}: {stream_res} is not a {stream_res.rsplit('.', 1)[-1]} "
                          f"file (its first bytes say {kind or 'something else'}). Nothing was sent to the Godot editor.")
        bus = step.get("bus") if isinstance(step.get("bus"), str) else ("Music" if music else "SFX")
        default_name = "Music" if music else re.sub(r"[^A-Za-z0-9_]", "", stream_res.rsplit("/", 1)[-1].split(".")[0].title()) + "Sound"
        name = step.get("name") if isinstance(step.get("name"), str) and step.get("name") else default_name
        path = f"{parent}/{name}"
        out: list[dict] = []
        notes = []
        import_res = stream_res + ".import"
        imported = self.disk_path(import_res, root).is_file()
        if music and step.get("loop", True) is not False:
            old = self.disk_path(import_res, root).read_text(encoding="utf-8", errors="ignore") if imported else ""
            new = import_with_loop(old, kind)
            if new != old:
                write = {"tool": "file_do", "op": "write_file", "path": import_res, "content": new}
                if imported:
                    write["overwrite"] = True
                out.append(write)
                out.append({"tool": "file_do", "op": "reimport", "paths": [stream_res]} if imported else
                           {"tool": "file_do", "op": "scan"})
                notes.append("loop on (" + (describe_replacement(import_res, old, new) if old else
                                            ("WAV loop mode Forward" if kind == "wav" else "loop true") + " in " +
                                            import_res) + ")")
            else:
                have = _import_params(old)
                notes.append(f"{import_res} already loops (" + (
                    f"loop mode {have.get('edit/loop_mode')}, loop_begin {have.get('edit/loop_begin', '0')}, loop_end "
                    f"{have.get('edit/loop_end', '-1')}" if kind == "wav" else f"loop_offset {have.get('loop_offset', '0')}")
                    + "); kept as it is")
        elif not imported:
            out.append({"tool": "file_do", "op": "scan"})
        bus_steps, bus_note = self._bus_steps(number, bus, root)
        out += bus_steps
        if bus_note:
            notes.append(bus_note)
        out += [{"tool": "asset_do", "op": "audio_player_create", "parent_path": parent, "name": name,
                 "type": step.get("type") if step.get("type") in ("1d", "2d", "3d") else "1d"},
                {"tool": "asset_do", "op": "audio_set_stream", "player_path": path, "stream_path": stream_res}]
        playback = {"tool": "asset_do", "op": "audio_set_playback", "player_path": path}
        if bus:
            playback["bus"] = bus
        if music:
            playback["autoplay"] = step.get("autoplay", True) is not False
        if isinstance(step.get("volume_db"), (int, float)) and not isinstance(step.get("volume_db"), bool):
            playback["volume_db"] = float(step["volume_db"])
        if len(playback) > 3:
            out.append(playback)
        summary = f"AudioStreamPlayer {path} with {stream_res} ({kind}) on bus {bus or 'Master'}" + (
            "; " + "; ".join(notes) if notes else "")
        return {"steps": out, "summary": summary, "checks": [{"kind": "audio", "path": path, "bus": bus or "Master"}]}

    # ---- buses the open editor had not loaded: remembered until the project is opened again ----
    @staticmethod
    def _ledger_path() -> Path:
        return Path(os.environ.get("GAME_EDITOR_DATA_DIR") or os.getcwd()) / "pending_buses.json"

    def _ledger(self) -> dict:
        try:
            data = json.loads(self._ledger_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _ledger_write(self, data: dict) -> None:
        path = self._ledger_path()
        data = {root: rows for root, rows in data.items() if rows}
        try:
            if not data:
                if path.exists():
                    path.unlink()
                return
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def pending_bus_note(self, root: str | None) -> str:
        rows = self._ledger().get(root or "", []) if root else []
        if not rows:
            return ""
        listed = "; ".join(f"{row.get('path')} in {row.get('scene')} on bus {row.get('bus')}" for row in rows[:6])
        return (f"game-editor note: waiting from an earlier place_assets plan: {listed}. The editor uses a bus a plan "
                f"added only after the project is opened again. If it was reopened, plan place_assets op buses: it "
                f"sets them and saves with one approval, then reads the bus back.")

    def _place_buses(self, number: int, step: dict, parent: str, root: str) -> dict:
        rows = self._ledger().get(root, [])
        if not rows:
            raise Refused(f"game-editor: place_assets step {number} op buses: nothing is waiting. It finishes the bus of "
                          f"a player that an earlier place_assets plan could not set. Nothing was sent to the Godot "
                          f"editor.")
        out: list[dict] = []
        current = self.current_scene()
        by_scene: dict[str, list[dict]] = {}
        for row in rows:
            by_scene.setdefault(str(row.get("scene") or ""), []).append(row)
        order = sorted(by_scene, key=lambda scene: scene != current)
        for scene in order:
            if scene != current:
                out.append({"tool": "scene_do", "op": "open_scene", "path": scene})
            for row in by_scene[scene]:
                out.append({"tool": "asset_do", "op": "audio_set_playback", "player_path": row["path"], "bus": row["bus"]})
            out.append({"tool": "scene_do", "op": "compare"})
            out.append({"tool": "scene_do", "op": "save"})
        summary = "sets the bus an earlier plan could not: " + "; ".join(
            f"{row['path']} -> {row['bus']} in {row['scene']}" for row in rows) + ", and saves"
        return {"steps": out, "summary": summary,
                "checks": [{"kind": "bus_fix", "path": row["path"], "bus": row["bus"], "scene": row["scene"]}
                           for row in rows]}

    def note_pending_buses(self, plan: dict, results: list[dict], root: str | None) -> list[str]:
        """After a plan: remember a player whose bus Godot did not keep (the editor has not loaded that bus yet), and
        forget one that place_assets op buses has now set."""
        if root is None or not plan.get("checks"):
            return []
        ledger = self._ledger()
        rows = list(ledger.get(root, []))
        lines = []
        scene = self.current_scene()
        changed = False
        for check in plan["checks"]:
            if check.get("kind") not in ("audio", "bus_fix") or check.get("bus") in (None, "", "Master"):
                continue
            seen_all = (getattr(self._local, "bus_seen", None) or {}).get(check["path"]) or []
            seen = next((item for item in seen_all if not check.get("scene") or item[0] == check["scene"]), None)
            if seen is not None:
                # Read right after its own step, in the scene that was open then.
                where_seen, kept = seen
            else:
                got = self.upstream("node_get_properties", {"path": check["path"], "fields": ["bus"]})
                if "bridge_error" in got or got.get("isError"):
                    # The step that would have made this node did not. Do not invent a wait.
                    continue
                body = _first_json(got)
                props = body.get("properties") if isinstance(body.get("properties"), list) else None
                if props is None:
                    continue
                kept = {item.get("name"): item.get("value") for item in props if isinstance(item, dict)}.get("bus")
                where_seen = scene
            where = check.get("scene") or where_seen
            match = [row for row in rows if row.get("path") == check["path"] and row.get("scene") == where]
            if kept == check["bus"]:
                if match:
                    rows = [row for row in rows if row not in match]
                    changed = True
                    lines.append(f"game-editor: {check['path']} is on bus {kept} now; nothing is waiting for it.")
            elif not match and where:
                rows.append({"scene": where, "path": check["path"], "bus": check["bus"],
                             "since": time.strftime("%Y-%m-%dT%H:%M:%S")})
                changed = True
                lines.append(f"game-editor: waiting: {check['path']} stays on {kept or 'Master'} until the project is "
                             f"opened again (Project > Reload Current Project), because the open editor has not loaded "
                             f"bus {check['bus']}. Then plan place_assets op buses: it sets the bus and saves with one "
                             f"approval. This plugin remembers it in {self._ledger_path()}.")
        if changed:
            ledger[root] = rows
            self._ledger_write(ledger)
        return lines

    def _place_sfx(self, number: int, step: dict, parent: str, root: str) -> dict:
        return self._place_audio(number, step, parent, root, False)

    def _place_music(self, number: int, step: dict, parent: str, root: str) -> dict:
        return self._place_audio(number, step, parent, root, True)

    def check_placed(self, plan: dict, results: list[dict], root: str | None) -> list[str]:
        """After a plan that placed assets ran: read back what Godot kept (nodes this plan added, no @tool)."""
        checks = plan.get("checks") or []
        if not checks or root is None or any(result.get("isError") for result in results):
            return []
        lines = []
        for check in checks:
            path = check["path"]
            if check["kind"] == "sprite":
                body = _first_json(self.upstream("node_get_properties", {"path": path,
                                                                         "fields": ["sprite_frames", "animation", "autoplay"]}))
                got = {item.get("name"): item.get("value") for item in body.get("properties") or [] if isinstance(item, dict)}
                lines.append(f"game-editor placed: {path} sprite_frames {got.get('sprite_frames')} animation "
                             f"{got.get('animation')}" + (f" autoplay {got['autoplay']}" if got.get("autoplay") else ""))
            elif check["kind"] == "tiles":
                body = _first_json(self.upstream("tilemap_manage", {"op": "tilemap_get_cells", "params": {"path": path}}))
                lines.append(f"game-editor placed: {path} has {body.get('count')} cells (asked {check['cells']})")
            elif check["kind"] == "audio":
                body = _first_json(self.upstream("node_get_properties", {"path": path, "fields": ["stream", "bus", "autoplay"]}))
                got = {item.get("name"): item.get("value") for item in body.get("properties") or [] if isinstance(item, dict)}
                kept = got.get("bus")
                line = f"game-editor placed: {path} stream {got.get('stream')} bus {kept} autoplay {got.get('autoplay')}"
                if check["bus"] != "Master" and kept != check["bus"]:
                    line += f". Godot kept bus {kept}, not {check['bus']}: the open editor has not loaded that bus yet"
                lines.append(line)
            elif check["kind"] == "filter":
                body = _first_json(self.upstream("node_get_properties", {"path": path, "fields": ["texture_filter"]}))
                got = {item.get("name"): item.get("value") for item in body.get("properties") or [] if isinstance(item, dict)}
                lines.append(f"game-editor placed: {path} texture_filter {got.get('texture_filter')}"
                             + (" (Nearest)" if got.get("texture_filter") == 1 else ""))
        return lines

    def _virtual_after(self, tool_name: str, op: str, args: dict, root: str | None) -> None:
        """Carry a planned step's effect to the later steps of the same plan."""
        virt = self._virtual()
        if virt is None or root is None:
            return
        spec = TOOLS[tool_name]["ops"].get(op) or {}
        gname = spec.get("guard") or tool_name
        path = args.get("path").strip() if isinstance(args.get("path"), str) else ""
        if path.startswith("uid://"):
            path = self.resolve_uid(path, root) or path
        if gname == "edit_project_settings" and op == "set_main_scene" and path.startswith("res://"):
            virt["main_scene"] = path
            return
        if gname in ("write_file", "create_script") and path.startswith("res://"):
            virt["files"][path] = str(args.get("content") or "")
            return
        if gname == "edit_script" and path.startswith("res://"):
            after = self._script_text_after(gname, args, path, root)
            if after is not None:
                virt["files"][path] = after
            return
        scene_now = self.current_scene()
        if gname == "edit_node" and op == "attach_script" and isinstance(args.get("script_path"), str) and path:
            root_name = ((virt.get("scenes") or {}).get(scene_now) or {}).get("root") or \
                (self.read_scene(scene_now, root) or {}).get("root")
            virt.setdefault("scripts", {}).setdefault(scene_now, {})[rel_node_path(path, root_name)] = \
                args["script_path"].strip()
            return
        if gname == "set_node_property" and args.get("property") == "text" and isinstance(args.get("value"), str) and path:
            root_name = ((virt.get("scenes") or {}).get(scene_now) or {}).get("root") or \
                (self.read_scene(scene_now, root) or {}).get("root")
            virt.setdefault("texts", {}).setdefault(scene_now, {})[rel_node_path(path, root_name)] = args["value"]
            return
        if gname == "new_scene" and path.startswith("res://"):
            if not path.endswith((".tscn", ".scn")):
                path += ".tscn"
            virt["files"][path] = ""
            root_name = args.get("root_name") if isinstance(args.get("root_name"), str) and args.get("root_name") \
                else path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            virt["scenes"][path] = {"root": root_name}
            virt["scene"] = path
            return
        if gname == "open_scene" and path.startswith("res://"):
            virt["scene"] = path
            return

        def add(scene: str, params: dict) -> None:
            root_name = (self.read_scene(scene, root) or {}).get("root")
            parent = params.get("parent_path") if isinstance(params.get("parent_path"), str) and params.get("parent_path") \
                else f"/{root_name or ''}"
            scene_path = params.get("scene_path") if isinstance(params.get("scene_path"), str) else ""
            name = params.get("name") if isinstance(params.get("name"), str) and params.get("name") else (
                params.get("type") if isinstance(params.get("type"), str) and params.get("type")
                else scene_path.rsplit("/", 1)[-1].rsplit(".", 1)[0])
            if not name:
                return
            rel = rel_node_path(parent.rstrip("/") + "/" + name, root_name)
            virt["inst" if scene_path else "local"].setdefault(scene, set()).add(rel)

        scene = self.current_scene()
        if gname == "add_node":
            add(scene, args)
        elif gname == "batch_execute":
            for item in args.get("commands") or []:
                if isinstance(item, dict) and item.get("command") == "create_node" and isinstance(item.get("params"), dict):
                    add(scene, item["params"])

    def _plan_save(self, steps: list[dict]) -> bool | None:
        """How the plan saves the scene that is open now, before it opens or makes another: True for a save with
        overwrite, False for a plain save (or a play with autosave), None when it does not save that scene."""
        for step in steps:
            spec = (TOOLS.get(step.get("tool") or "", {}).get("ops", {}) or {}).get(step.get("op") or "", {})
            gname = spec.get("guard") or step.get("tool")
            if gname in ("open_scene", "new_scene", "revert_scene"):
                return None
            if gname == "save_scene" and spec.get("guard_op", step.get("op")) == "save":
                return step.get("overwrite") is True
            if gname in ("run_game", "play_session") and step.get("autosave") is True:
                return False
        return None

    def _plan_compare(self, scene: str, root: str) -> dict | None:
        """Compare the open scene with its file while the plan is made, through reads only (no @tool getter runs:
        a node whose script has one is not read and is named as not compared; Control offsets, which Godot AI
        returns only through a rolled-back set, are left to apply_plan). None when the tree shape cannot be read
        without running a @tool _get."""
        if not scene.startswith("res://") or self.scene_get_nodes(scene, root):
            return None
        if not self.disk_path(scene, root).exists():
            return {"compared": True, "diffs": [], "why": "", "skipped": [], "deferred": [], "new_file": True}
        self._local.plan_reads = True
        self._local.compare_deferred = []
        try:
            compared, diffs, why, skipped = self.compare(scene, root, None)
        except Refused:
            return None
        finally:
            self._local.plan_reads = False
        deferred = list(getattr(self._local, "compare_deferred", []) or [])
        self._local.compare_deferred = []
        diffs = self._without_own_edits(scene, root, diffs)
        tool_only = bool(skipped) and all(str(item).startswith("@tool script on") for item in skipped)
        # A node that was not read is not compared. Calling it compared, or dropping the reason, would let a
        # save through on a note. The caller refuses.
        return {"compared": bool(compared), "diffs": diffs, "why": why,
                "skipped": skipped, "deferred": deferred, "tool_only": tool_only, "new_file": False}

    def _overwrite_refusal(self, steps: list[dict], scene_now: str, root: str) -> str:
        """Why no plan is made for a save with overwrite: a plan that drops values names both sides of each (the path,
        the field, the file's value and the editor's value). Those come from an earlier approved scene_do op compare of
        the scene open now, while its file has not changed since. A scene the plan opens or makes first cannot have
        them."""
        opened = False
        for step in steps:
            spec = (TOOLS.get(step.get("tool") or "", {}).get("ops", {}) or {}).get(step.get("op") or "", {})
            gname = spec.get("guard") or step.get("tool")
            if gname in ("open_scene", "new_scene", "revert_scene"):
                opened = True
            if gname == "save_scene" and step.get("overwrite") is True:
                if opened or not scene_now:
                    return ("game-editor: plan_change made no plan: a save with overwrite drops whatever differs on disk, "
                            "and a plan names both sides of each dropped value only for the scene open now. Open that "
                            "scene with a plan of its own, then plan scene_do op compare, then the overwrite. Nothing was "
                            "sent to the Godot editor.")
                snap = self.compare_snapshots.get(scene_now)
                if snap is None or snap.get("sha") != file_sha(self.disk_path(scene_now, root)):
                    return (f"game-editor: plan_change made no plan: a save with overwrite drops whatever differs between "
                            f"the editor and {scene_now}, and the plan must name both sides of each value. Those are "
                            f"known only after a scene_do op compare step has run since the file last changed. Plan "
                            f"{{\"tool\": \"scene_do\", \"op\": \"compare\"}} (it asks the editor, where project code "
                            f"can run), apply it, then plan the overwrite. Nothing was sent to the Godot editor.")
                if not snap.get("compared"):
                    return (f"game-editor: plan_change made no plan: the last compare could not read every value of "
                            f"{scene_now} ({snap.get('why')}), so the values a save with overwrite drops cannot all be "
                            f"named. Save a copy with scene_do op save_as instead. Nothing was sent to the Godot editor.")
        return ""

    def _plan_change(self, arguments) -> dict:
        if not isinstance(arguments, dict) or set(arguments) - {"steps"}:
            return error_result("game-editor: plan_change takes steps only. Nothing was sent to the Godot editor.")
        steps = arguments.get("steps")
        if (not isinstance(steps, list) or not 1 <= len(steps) <= MAX_PLAN_STEPS
                or not all(isinstance(step, dict) for step in steps)):
            return error_result(
                f"game-editor: plan_change needs steps, a list of 1 to {MAX_PLAN_STEPS} objects such as "
                f"{{\"tool\": \"scene_do\", \"op\": \"save\"}}. Nothing was sent to the Godot editor.")
        steps = json.loads(json.dumps(steps))
        reason = self.ensure_child()
        if reason:
            return error_result(reason)
        root, problem = self.project_root()
        if problem:
            return error_result(
                "game-editor: plan_change could not confirm the project folder, so no plan was saved. "
                "Nothing was sent to the Godot editor.")
        self._local.tree_rels = {}
        self._local.plan_diffs = None
        self._local.planning = True  # from here the editor is not asked: files on disk and the server's session table
        self._local.active_steps = None
        try:
            steps, expansions = self.expand_asset_steps(steps, root)
        except Refused as exc:
            self._local.planning = False
            return error_result(f"{exc} No plan was saved.")
        if len(steps) > 4 * MAX_PLAN_STEPS:
            self._local.planning = False
            return error_result("game-editor: the plan expands to too many steps. Split it. No plan was saved.")
        scene_now = self.current_scene() if root else ""
        for index, step in enumerate(steps, 1):
            if "session_id" in step:
                self._local.planning = False
                return error_result(
                    f"game-editor: plan_change step {index} has session_id. A plan runs on the active editor, "
                    f"which it checks and compares; switch editors first with a plan of its own (editor_do op "
                    f"activate_session). No plan was saved.")
        save_mode = self._plan_save(steps)
        planned_diffs = None
        found = None
        why_not = self._overwrite_refusal(steps, scene_now, root) if root else ""
        if why_not:
            self._local.planning = False
            return error_result(escape_controls(why_not))
        snap = self.compare_snapshots.get(scene_now) if scene_now else None
        fresh = snap is not None and snap.get("sha") == file_sha(self.disk_path(scene_now, root)) if snap else False
        if save_mode is True and fresh:
            planned_diffs = list(snap["diffs"])
            self._local.plan_diffs = {"scene": scene_now, "diffs": planned_diffs, "why": "", "skipped": [],
                                      "deferred": [], "at": snap.get("at")}
        elif save_mode is False and scene_now and self.disk_path(scene_now, root).is_file():
            blocking = self._without_own_nodes(scene_now, root, list(snap.get("diffs") or [])) if fresh and snap else []
            if fresh and blocking:
                listed = "; ".join(blocking[:8]) + (f"; and {len(blocking) - 8} more" if len(blocking) > 8 else "")
                self._local.planning = False
                return error_result(escape_controls(
                    f"game-editor: plan_change made no plan: when the open scene was last compared, the editor and "
                    f"{scene_now} differed: {listed}. This plan's save would write the editor's side over the disk side. "
                    f"Ask the user which to keep. To keep the editor's version, plan again with overwrite: true on the save "
                    f"step (the plan then names each value it drops and needs a second approval). To keep the disk version, "
                    f"plan scene_do op revert_scene. Nothing was sent to the Godot editor and nobody was asked."))
            if fresh and snap.get("compared") and not blocking and not snap.get("diffs") and snap.get("bytes_moved"):
                self._local.planning = False
                return error_result(
                    f"game-editor: plan_change made no plan: {scene_now} changed on disk after this session last "
                    f"compared it. The values that were compared still agree, but the file's bytes changed, and a save "
                    f"would rewrite bytes this compare does not list. Plan the save with overwrite: true to replace "
                    f"the file, or scene_do op revert_scene to load it. Nothing was sent to the Godot editor and "
                    f"nobody was asked.")
            compared_in_plan = False
            for step in steps:
                if step.get("tool") == "scene_do" and step.get("op") == "compare":
                    compared_in_plan = True
                if step.get("tool") == "scene_do" and step.get("op") == "save" and step.get("overwrite") is not True:
                    break
            if not (fresh and snap and snap.get("compared") and not blocking) and not compared_in_plan:
                self._local.planning = False
                return error_result(
                    "game-editor: plan_change made no plan: a save of the scene open now writes the editor's scene over "
                    "the file, and both sides (each value on disk and in the editor) are known only after scene_do op "
                    "compare has run since the file last changed. Plan {\"tool\": \"scene_do\", \"op\": \"compare\"}, "
                    "apply it, then plan the save. Nothing was sent to the Godot editor and nobody was asked.")
        blocks: list[str] = []
        losses: list[str] = []
        self._local.virtual = {"scene": None, "scenes": {}, "local": {}, "inst": {}, "files": {}, "scripts": {},
                               "texts": {}}
        try:
            self._local.active_steps = steps
            for index, step in enumerate(steps, 1):
                if "session_id" in step:
                    return error_result(
                        f"game-editor: plan_change step {index} has session_id. A plan runs on the active editor, "
                        f"which it checks and compares; switch editors first with a plan of its own (editor_do op "
                        f"activate_session). No plan was saved.")
                if step.get("tool") == "editor_do" and step.get("op") == "activate_session" and len(steps) > 1:
                    return error_result(
                        "game-editor: editor_do op activate_session must be a plan of its own: the other steps were "
                        "checked against the editor that is active now. No plan was saved.")
                tool_name = step.get("tool")
                if not isinstance(tool_name, str) or tool_name not in PLAN_TOOLS:
                    return error_result(
                        f"game-editor: plan_change step {index} needs tool, one of: {', '.join(PLAN_TOOLS)}. "
                        f"Nothing was sent to the Godot editor and no plan was saved.")
                tool = TOOLS[tool_name]
                raw_op = step.get("op")
                op = raw_op if isinstance(raw_op, str) and raw_op else tool.get("default_op")
                if tool["multi"] and not (isinstance(raw_op, str) and raw_op) and len(plan_step_ops(tool_name)) == 1:
                    op = plan_step_ops(tool_name)[0]
                    step["op"] = op
                elif tool["multi"] and not (isinstance(raw_op, str) and raw_op):
                    return error_result(
                        f"game-editor: plan_change step {index} ({tool_name}) needs op, one of: "
                        f"{', '.join(plan_step_ops(tool_name))}. Nothing was sent to the Godot editor and no "
                        f"plan was saved.")
                if "also" in step:
                    also = step.get("also")
                    if not isinstance(also, list) or not all(
                            isinstance(item, str) and item.strip().startswith("res://") for item in also):
                        return error_result(
                            f"game-editor: plan_change step {index} also takes a list of res:// paths this step may "
                            f"also write. No plan was saved.")
                    step["also"] = [item.strip() for item in also]
                    remain, why_also = self._also_remain_text(index, step["also"], root)
                    if why_also:
                        return error_result(why_also)
                else:
                    also = None
                    remain = ""
                args = {key: value for key, value in step.items() if key not in ("tool", "also")}
                if tool_name == "scene_do" and op == "compare":
                    extra = sorted(set(args) - {"op"})
                    if extra:
                        return error_result(f"game-editor: plan_change step {index}: scene_do op compare takes no "
                                            f"arguments. No plan was saved.")
                    line = (f"reads every node of the open scene in the editor and compares it with its file; the "
                            f"editor reads values through the nodes' scripts, so project code (@tool scripts, property "
                            f"getters) can run")
                    blocks.append(f"{index}. Godot: scene_do op compare (touches the editor)\n   ! runs project code: "
                                  f"{line}\n   The reply lists each difference with the file's value and the editor's "
                                  f"value; a later plan can save with overwrite only after this (both sides are then in "
                                  f"that plan).")
                    losses.append(f"step {index} runs project code: {line}")
                    continue
                if (tool_name, op) in READ_ALLOW:
                    # A direct call of scene_look reads the file. editor_look and asset_look are not direct calls.
                    # The same op as a plan step asks the editor, so it is a step even when no @tool script is in the file.
                    spec = tool["ops"].get(op) or {}
                    path_values = [value.strip() for value in args.values()
                                   if isinstance(value, str) and ("://" in value or value.strip()[:1] in ("'", '"', "&"))]
                    if path_values:
                        try:
                            self.check_paths(tool_name, path_values, root)
                        except Refused as exc:
                            return error_result(f"{exc} (plan_change step {index}; no plan was saved.)")
                    refusal = ""
                    if root:
                        refusal = self.read_tool_refusal(
                            tool_name, op, spec, {k: v for k, v in args.items() if k != "op"},
                            self.current_scene(), root)
                    shown = "\n".join(f"{k}: {v if isinstance(v, str) else json.dumps(v)}" for k, v in args.items()
                                      if k not in ("op", "session_id"))
                    if refusal:
                        line = refusal.split(": ", 2)[-1].split(" A read does not")[0]
                    else:
                        line = ("asks the editor; while it answers, Godot can run project code (a @tool script's "
                                "_get, _get_property_list or a property getter)")
                    if args.get("force_sync") is True:
                        line += "; force_sync rebuilds the navigation map in the editor"
                    blocks.append(f"{index}. Godot: {tool_name} op {op} (touches the editor)\n   " +
                                  (shown.replace("\n", "\n   ") + "\n   " if shown else "") + "! runs project code: " + line)
                    losses.append(f"step {index} runs project code: {line}")
                    continue
                try:
                    result = self._dispatch(tool_name, args, None, planning=True, public_read=False)
                except _Planned as planned:
                    blocks.append(f"{index}. " + planned.text.replace("\n", "\n   "))
                    losses.extend(f"step {index} {line}" for line in planned.losses)
                    if planned.arguments:
                        step.clear()
                        step.update({"tool": tool_name, **planned.arguments})
                    if also:
                        step["also"] = also
                    if remain:
                        blocks[-1] += "\n   " + remain.replace("\n", "\n   ")
                    self._virtual_after(tool_name, op, {k: v for k, v in step.items() if k != "tool"}, root)
                    continue
                except Refused as exc:
                    return error_result(f"{exc} (plan_change step {index}; no plan was saved.)")
                if isinstance(result, dict) and result.get("isError"):
                    content = list(result.get("content") or [{"type": "text", "text": ""}])
                    first = content[0].get("text", "") if isinstance(content[0], dict) else ""
                    content[0] = {"type": "text", "text": f"{first} (plan_change step {index}; no plan was saved.)"}
                    return {"content": content, "isError": True}
                return error_result("game-editor: plan_change did not stop before the editor. No plan was saved.")
            self._local.virtual = None
            if root and scene_now and (
                    self.plan_compares_current(steps) or any(self._changes_open_scene(step) for step in steps)):
                why = self._unparsed_scene_reason(scene_now, root)
                if why:
                    return error_result(
                        "game-editor: plan_change made no plan: " + why + ". The unsaved buffer was not parsed and "
                        "no note was added to let the plan through. Nothing was sent to the Godot editor.")
            try:
                fingerprint = self._fingerprint(steps, root, planned_diffs)
            except Refused as exc:
                return error_result(str(exc))
            if root and scene_now:
                # From the scene file, not from a live tree read. Naming the script is not a pass for an
                # unparsed buffer, and it does not call the editor while the plan is built.
                getters = [path for path, _script in self.scene_get_nodes(scene_now, root)]
                if getters and self.plan_compares_current(steps):
                    losses.append(
                        "runs project code: comparing the open scene with its file reads node names, and that "
                        f"runs the @tool _get of {', '.join(getters[:4])}")
                if getters and any(self._changes_open_scene(step) for step in steps):
                    losses.append(
                        "runs project code: when a step changes the open scene, the editor and Godot AI read its "
                        f"nodes again, and that runs the @tool _get of {', '.join(getters[:4])}")
        finally:
            self._local.planning = False
            self._local.virtual = None
            self._local.plan_diffs = None
            self._local.active_steps = None
        plan_id = secrets.token_hex(16)
        count = len(steps)
        losses = list(dict.fromkeys(losses))
        risky = [line for line in losses if risky_line(line)]
        lines = [f"game-editor plan: nothing has run yet. apply_plan runs "
                 f"{'this step' if count == 1 else f'these {count} steps in order and stops at the first error'}:"]
        lines += expansions
        lines += blocks
        if losses:
            lines.append("This plan deletes, replaces, discards or runs something:")
            lines += [f"- {line}" for line in losses]
        if risky:
            lines.append("Two approvals: this plan deletes, overwrites, discards unsaved work, writes outside the "
                         "project or runs code written in it. Hermes asks for apply_plan, which runs nothing yet: it "
                         "checks the editor and returns the list again with what would be lost. Then Hermes asks for "
                         "confirm_risky_plan, which runs the plan. If either answer is no, nothing runs.")
        elif losses:
            lines.append("Hermes asks once for apply_plan and its prompt names only the tool: read these lines "
                         "before you approve. If the answer is no, nothing runs. Playing and tests run the project's "
                         "own code.")
        lines.append("Only the newest plan can run, within 10 minutes. If the open scene, a file "
                     "named here, a file a step changes as a side effect, an export neighbor or a difference between "
                     "the editor and a scene this plan saves changes before apply_plan, nothing runs and a new plan "
                     "is needed.")
        lines.append(f"plan_id: {plan_id}")
        lines.append(f"fingerprint: {fingerprint}")
        body = escape_controls("\n".join(lines))
        if len(body.encode("utf-8")) > MAX_RESULT_BYTES:
            return error_result(
                "game-editor: the plan text is larger than 256 KiB, so it was not cut and no plan was saved. "
                "Split the change into smaller plans. Nothing was sent to the Godot editor.")
        if risky:
            summary = risky_summary(plan_id, risky, [line for line in losses if not risky_line(line)])
            if len(summary) > CONFIRM_LIMIT:
                return error_result(
                    f"game-editor: plan_change made no plan: what this plan deletes, overwrites, discards or runs takes "
                    f"{len(summary)} characters to list, more than the {CONFIRM_LIMIT} that the second approval shows "
                    f"whole in a chat gateway. Split it into smaller plans (fewer deletes or a save without overwrite "
                    f"first). Nothing was sent to the Godot editor.")
        with self._plans_lock:
            for old_id in list(self._plans):
                self._retired[old_id] = "replaced"
            self._plans.clear()
            while len(self._retired) > 64:
                self._retired.pop(next(iter(self._retired)))
            self._plans[plan_id] = {"steps": steps, "fingerprint": fingerprint, "root": root, "text": body,
                                    "checks": list(getattr(self._local, "place_checks", []) or []),
                                    "losses": losses, "risky": risky, "made": time.time(),
                                    "tree_skipped": False, "planned_diffs": planned_diffs,
                                    "deferred": list((found or {}).get("deferred") or []) if planned_diffs is not None
                                    else [],
                                    "save_mode": save_mode, "scene": scene_now, "state": "planned",
                                    "pending_buses": list(getattr(self._local, "pending_buses", []) or [])}
        return {"content": [{"type": "text", "text": body}], "isError": False}

    @staticmethod
    def _changes_open_scene(step: dict) -> bool:
        tool = TOOLS.get(step.get("tool") or "") or {}
        spec = (tool.get("ops") or {}).get(step.get("op") or "") or {}
        gname = spec.get("guard") or step.get("tool")
        if tool.get("tier") in (None, "read") or step.get("tool") in ("checks", "ship_build", "game_do"):
            return False
        return gname not in ("run_game", "stop_game", "editor_control", "import_files", "write_file", "delete_file",
                             "delete_file_permanently", "move_file", "create_script", "edit_script",
                             "edit_project_settings", "class_reference", "open_scene", "new_scene", "save_scene",
                             "screenshot", "read_logs")

    @staticmethod
    def plan_compares_current(steps: list[dict]) -> bool:
        """True when apply_plan compares the scene that is open now with its file: a step before the first
        open_scene, new_scene or revert_scene that edits or saves that scene."""
        for step in steps:
            tool = TOOLS.get(step.get("tool") or "") or {}
            spec = (tool.get("ops") or {}).get(step.get("op") or "") or {}
            gname = spec.get("guard") or step.get("tool")
            if gname in ("open_scene", "new_scene", "revert_scene"):
                return False
            if tool.get("tier") in (None, "read") or step.get("tool") in ("checks", "ship_build", "game_do"):
                continue
            if gname in ("run_game", "stop_game", "editor_control", "import_files", "write_file", "delete_file",
                         "delete_file_permanently", "move_file", "create_script", "edit_script",
                         "edit_project_settings", "class_reference"):
                if gname == "run_game" and step.get("autosave") is True:
                    return True
                continue
            return True
        return False

    def _take_plan(self, plan_id, tool: str) -> tuple[dict | None, dict | None]:
        """(plan, None) for a plan that may run now, or (None, error reply). The plan is claimed for this call: a
        second apply_plan or confirm_risky_plan for the same plan_id while this one works is refused."""
        with self._plans_lock:
            plan = self._plans.get(plan_id) if isinstance(plan_id, str) else None
            retired = self._retired.get(plan_id) if isinstance(plan_id, str) else None
            if plan is not None and plan.get("busy"):
                return None, error_result(
                    f"game-editor: {tool} is already working on this plan_id in another call. Nothing more was sent "
                    f"to the Godot editor.")
            if plan is not None:
                plan["busy"] = True
                self._local.claimed = plan_id
        if plan is None:
            if retired == "replaced":
                return None, error_result(
                    f"game-editor: a newer plan replaced this plan_id, and only the newest plan can run. Show the "
                    f"newest plan and call apply_plan with its plan_id, or call plan_change again. Nothing was sent "
                    f"to the Godot editor.")
            return None, error_result(
                f"game-editor: {tool} does not know this plan_id, or it was already used. Call plan_change again and "
                f"show the new plan. Nothing was sent to the Godot editor.")
        if time.time() - plan["made"] > PLAN_TTL:
            plan["busy"] = False
            self._retire(plan_id, "used")
            return None, error_result(
                f"game-editor: this plan is older than {int(PLAN_TTL // 60)} minutes, so it did not run. Call "
                f"plan_change again and show the new plan. Nothing was sent to the Godot editor.")
        return plan, None

    def _retire(self, plan_id: str, why: str) -> None:
        with self._plans_lock:
            self._plans.pop(plan_id, None)
            self._retired[plan_id] = why

    def _apply_plan(self, arguments, mid) -> dict:
        try:
            return self._apply_plan_claimed(arguments, mid)
        finally:
            self._release(arguments)

    def _confirm_risky_plan(self, arguments, mid) -> dict:
        try:
            return self._confirm_risky_plan_claimed(arguments, mid)
        finally:
            self._release(arguments)

    def _release(self, arguments) -> None:
        plan_id = arguments.get("plan_id") if isinstance(arguments, dict) else None
        if not isinstance(plan_id, str) or getattr(self._local, "claimed", None) != plan_id:
            return  # this call did not claim the plan (refused as busy, or no plan): leave it alone
        self._local.claimed = None
        with self._plans_lock:
            plan = self._plans.get(plan_id)
            if plan is not None:
                plan["busy"] = False

    def _apply_plan_claimed(self, arguments, mid) -> dict:
        if not isinstance(arguments, dict) or set(arguments) - {"plan_id"}:
            return error_result("game-editor: apply_plan takes plan_id only. Nothing was sent to the Godot editor.")
        plan_id = arguments.get("plan_id")
        plan, problem = self._take_plan(plan_id, "apply_plan")
        if problem is not None:
            return problem
        if plan.get("state") == "await_confirm":
            return {"content": [{"type": "text", "text": plan["confirm"]["text"]}], "isError": False}
        self.progress("checking the plan against the editor", 0, len(plan["steps"]) + 1)
        root, why = self._check_unchanged(plan)
        if why:
            self._retire(plan_id, "used")
            return error_result(why)
        apply_diffs: list[str] | None = None
        if self.plan_compares_current(plan["steps"]):
            why, apply_diffs = self._apply_baseline(plan, root)
            if why:
                self._retire(plan_id, "used")
                return error_result(why)
        baseline_done = True
        risky = list(plan.get("risky") or [])
        why, discard_lines, discard_diffs = self._name_discards(plan, root)
        if why:
            self._retire(plan_id, "used")
            return error_result(
                "game-editor: apply_plan did not run: " + why + " Nothing was sent to the Godot editor.")
        has_discard = bool(discard_diffs) or bool(discard_lines) or any(
            is_discard_placeholder(line) for line in risky)
        if has_discard:
            # The plan-time line is not a list. The compare above is. An empty compare drops the
            # placeholder and the step can run. A compare that could not name every value already returned.
            risky = [line for line in risky if not is_discard_placeholder(line)]
            for line in discard_lines:
                if line not in risky:
                    risky.append(line)
        if plan.get("save_mode") is True and apply_diffs is not None:
            # What apply_plan read replaces the plan's placeholder lines (the tree or the Control offsets it could not
            # read while the plan was made): each value the save drops, or nothing when nothing differs.
            number = next((i for i, step in enumerate(plan["steps"], 1) if self._step_spec(step)[0] == "save_scene"), 0)
            risky = [line for line in risky if f"drops whatever differs between the editor and {plan.get('scene')} on disk;"
                     not in line or "this plan opens or makes" in line]
            planned = plan.get("planned_diffs") or []
            risky += [f"step {number} drops the disk side of {plan.get('scene')}: {line}" for line in apply_diffs
                      if line not in planned]
        if risky:
            summary = risky_summary(plan_id, risky, [line for line in plan.get("losses") or [] if not risky_line(line)])
            if len(summary) > CONFIRM_LIMIT:
                self._retire(plan_id, "used")
                return error_result(
                    f"game-editor: apply_plan did not run this plan: what it would drop or delete takes {len(summary)} "
                    f"characters to list, more than the {CONFIRM_LIMIT} the second approval shows whole. Nothing was "
                    f"changed. Split the change, or save without overwrite after asking the user what to keep.")
            now = self._fingerprint(plan["steps"], root, plan.get("planned_diffs"))
            confirm = {"text": summary, "fingerprint": now, "diffs": apply_diffs, "at": time.time(),
                       "baseline_done": baseline_done}
            if has_discard:
                # Kept even when a scene had nothing to discard, so a later edit is not dropped unnamed.
                confirm["discard_diffs"] = discard_diffs
            with self._plans_lock:
                plan["state"] = "await_confirm"
                plan["confirm"] = confirm
            return {"content": [{"type": "text", "text": summary}], "isError": False}
        self._retire(plan_id, "used")
        return self._run_plan(plan, root, mid, baseline_done)

    def _confirm_risky_plan_claimed(self, arguments, mid) -> dict:
        if not isinstance(arguments, dict) or set(arguments) - {"plan_id"}:
            return error_result("game-editor: confirm_risky_plan takes plan_id only. Nothing was sent to the Godot "
                                "editor.")
        plan_id = arguments.get("plan_id")
        plan, problem = self._take_plan(plan_id, "confirm_risky_plan")
        if problem is not None:
            return problem
        if plan.get("state") != "await_confirm":
            return error_result(
                "game-editor: confirm_risky_plan runs a plan only after apply_plan was approved for it and returned "
                "its list of what is deleted, overwritten, discarded or run. Call apply_plan with this plan_id first. "
                "Nothing was sent to the Godot editor.")
        self.progress("checking the plan against the editor", 0, len(plan["steps"]) + 1)
        confirm = plan["confirm"]
        root, problem_text = self.project_root()
        if problem_text or root != plan["root"]:
            self._retire(plan_id, "used")
            return error_result("game-editor: confirm_risky_plan could not confirm the project folder, so the plan "
                                "did not run. Call plan_change again. Nothing was sent to the Godot editor.")
        try:
            self._local.planning = True
            now = self._fingerprint(plan["steps"], root, plan.get("planned_diffs"))
        except Refused as exc:
            self._retire(plan_id, "used")
            return error_result(str(exc))
        finally:
            self._local.planning = False
        if now != confirm["fingerprint"]:
            self._retire(plan_id, "used")
            return error_result(
                "game-editor: the project changed after apply_plan listed what this plan drops (the open scene, the "
                "scene tree or a file it names), so confirm_risky_plan did not run it. Call plan_change again and show "
                "the new plan. Nothing was sent to the Godot editor.")
        if confirm.get("diffs") is not None:
            scene = plan.get("scene") or ""
            # The list apply_plan showed came from an approved compare, which reads each editor-only node's
            # script. The same read here keeps that list. Making a plan still does not read the editor.
            self._local.compare_runs_code = True
            try:
                _compared, diffs, _why, _skipped = self.compare(scene, root, None)
            finally:
                self._local.compare_runs_code = False
            listed = [line for line in confirm["diffs"] if not line.startswith(UNCOMPARED)]
            if sorted(diffs) != sorted(listed):
                self._retire(plan_id, "used")
                return error_result(
                    f"game-editor: what the editor and {scene} differ in changed after apply_plan listed it, so "
                    f"confirm_risky_plan did not run the plan. Call plan_change again. Nothing was sent to the Godot "
                    f"editor.")
        if isinstance(confirm.get("discard_diffs"), dict):
            why, _lines, fresh = self._name_discards(plan, root)
            if why or sorted(fresh) != sorted(confirm["discard_diffs"]) or any(
                    sorted(fresh.get(one) or []) != sorted(confirm["discard_diffs"].get(one) or [])
                    for one in set(fresh) | set(confirm["discard_diffs"])):
                self._retire(plan_id, "used")
                detail = why or "what would be discarded changed after apply_plan listed it."
                return error_result(
                    "game-editor: confirm_risky_plan did not run: " + detail + " Call plan_change again. "
                    "Nothing was sent to the Godot editor.")
        self._retire(plan_id, "used")
        return self._run_plan(plan, root, mid, bool(confirm.get("baseline_done")))

    def _check_unchanged(self, plan: dict) -> tuple[str | None, str]:
        """(root, "") when nothing the plan was made against changed, else (root, why it does not run)."""
        reason = self.ensure_child()
        if reason:
            return None, reason
        root, problem = self.project_root()
        if problem:
            return None, ("game-editor: apply_plan could not confirm the project folder, so the plan did not run. "
                          "Call plan_change again. Nothing was sent to the Godot editor.")
        try:
            self._local.planning = True
            now = self._fingerprint(plan["steps"], root, plan.get("planned_diffs"))
        except Refused as exc:
            return root, str(exc)
        finally:
            self._local.planning = False
        if root != plan["root"] or now != plan["fingerprint"]:
            return root, ("game-editor: the project changed after this plan (the open scene, the scene tree, a file "
                          "it names, a file a step changes as a side effect or an export neighbor), so apply_plan did "
                          "not run it. Call plan_change again and show the new plan. Nothing was sent to the Godot "
                          "editor.")
        return root, ""

    def _res_rel(self, res: str) -> str | None:
        text = res.strip()
        if not text.lower().startswith("res://"):
            return None
        rel = text[6:].lstrip("/")
        if not rel or any(part in ("", ".", "..") for part in rel.split("/")):
            return None
        return rel

    def _capture_files(self, root: str | None) -> dict[str, tuple]:
        """Every file under the project except the .godot directory. A symlink is the link, not the target."""
        if not isinstance(root, str) or not root:
            return {}
        base = Path(root)
        out: dict[str, tuple] = {}
        if not base.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = [name for name in dirnames if name != ".godot"]
            for name in filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(base).as_posix()
                try:
                    if path.is_symlink():
                        out[rel] = ("link", os.readlink(path))
                    elif path.is_file():
                        out[rel] = ("file", path.read_bytes())
                except OSError:
                    continue
        return out

    def _put_back(self, base: Path, rel: str, old: tuple | None) -> None:
        path = base / rel
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.exists():
                return
        except OSError:
            return
        if old is None:
            return
        kind, payload = old
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "link":
            os.symlink(payload, path)
        elif kind == "file":
            path.write_bytes(payload)

    def _plan_script_sha(self, rel: str) -> str | None:
        """Hash this plan stored when it wrote this script, or None when this plan did not write it."""
        sha = self.written_scripts.get("res://" + rel)
        return sha if isinstance(sha, str) and sha else None

    @staticmethod
    def _payload_sha(payload: tuple | None) -> str | None:
        if not payload or payload[0] != "file" or not isinstance(payload[1], (bytes, bytearray)):
            return None
        return hashlib.sha256(payload[1]).hexdigest()

    @staticmethod
    def _payload_diff(before: tuple | None, after: tuple | None) -> str:
        """One short line: which line differed, or that the file appeared or disappeared."""
        old = before[1] if before and before[0] == "file" and isinstance(before[1], (bytes, bytearray)) else None
        new = after[1] if after and after[0] == "file" and isinstance(after[1], (bytes, bytearray)) else None
        if old is None and new is not None:
            return "the file was created"
        if new is None:
            return "the file was removed"
        try:
            old_lines = old.decode("utf-8").splitlines()
            new_lines = new.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            return f"the file size was {len(old)} bytes and became {len(new)} bytes"
        for index, (left, right) in enumerate(zip(old_lines, new_lines), 1):
            if left != right:
                return f"line {index} was {left[:40]!r} and became {right[:40]!r}"
        if len(old_lines) != len(new_lines):
            extra = min(len(old_lines), len(new_lines)) + 1
            return f"line {extra} was added or removed"
        return "the bytes differ"

    def _also_rels(self, step: dict) -> set[str]:
        """Project-relative paths a step's also list names. The plan said these bytes stay."""
        raw = step.get("also")
        if not isinstance(raw, list):
            return set()
        rels: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                rel = self._res_rel(item)
                if rel:
                    rels.add(rel)
        return rels

    def _also_remain_text(self, index: int, also: list[str], root: str | None) -> tuple[str, str]:
        """(plan lines, "") when every also path can be read. ("", why) when one cannot, and no plan is made."""
        if not root:
            named = also[0] if also else "a path"
            return "", (f"game-editor: plan_change made no plan: step {index} names {named} in also, and the "
                        f"project folder is not known, so the bytes that would remain are not known. "
                        f"No plan was saved.")
        chunks: list[str] = []
        for res in also:
            rel = self._res_rel(res)
            path = Path(root) / rel if rel else None
            if path is None or not path.is_file():
                return "", (f"game-editor: plan_change made no plan: step {index} names {res} in also, and that "
                            f"file is not on disk, so the bytes that would remain are not known. No plan was saved.")
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return "", (f"game-editor: plan_change made no plan: step {index} names {res} in also, and that "
                            f"file is not UTF-8 text, so the bytes that would remain cannot be shown. "
                            f"No plan was saved.")
            except OSError as exc:
                return "", (f"game-editor: plan_change made no plan: step {index} names {res} in also, and that "
                            f"file could not be read ({type(exc).__name__}), so the bytes that would remain are "
                            f"not known. No plan was saved.")
            shown = text if len(text) <= 400 else text[:400] + "\n…"
            chunks.append(f"also keeps {res}; these bytes remain on disk:\n{shown.rstrip()}")
        return "\n".join(chunks), ""

    def _plan_tres(self, root: str) -> list[str]:
        """ .tres paths a save may rewrite: ones the scene file references, and ones this plan names."""
        found: set[str] = set()
        for step in getattr(self._local, "active_steps", None) or []:
            _tres_mentions(step, found)
        scene = self.current_scene()
        if isinstance(scene, str) and scene.startswith("res://"):
            try:
                text = self.disk_path(scene, root).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for match in re.finditer(r'path="(res://[^"]+\.tres)"', text):
                found.add(match.group(1))
        return sorted(found)

    def _restore_strays(self, root: str | None, before: dict[str, tuple], declared: set[str],
                        pinned: set[str] | None = None) -> list[tuple[str, str]]:
        """Put back every file the step did not name.

        A script this plan wrote is not one of those. When the editor saves that same text, the bytes stay.
        When the editor saves an older tab over that text, the plan's text is put back and the step still succeeds.
        A path in also stays the bytes the plan showed. A .uid or .import file the editor writes beside a file
        that is already on disk stays when the new bytes have that shape. Any other file this plan did not write
        is put back, and the caller makes the step an error.
        Returns (project-relative path, how the bytes differed) for each file that was put back as an error.
        """
        self._beside_kept = []
        if not isinstance(root, str) or not root:
            return []
        pinned = pinned or set()
        after = self._capture_files(root)
        base = Path(root)
        restored: list[tuple[str, str]] = []

        def keep_planned(rel: str, before_payload: tuple | None, after_payload: tuple | None) -> bool:
            plan = self._plan_script_sha(rel)
            if not plan:
                return False
            if self._payload_sha(after_payload) == plan:
                return True
            if self._payload_sha(before_payload) == plan:
                self._put_back(base, rel, before_payload)
                return True
            return False

        def keep_pinned(rel: str, before_payload: tuple | None, after_payload: tuple | None) -> None:
            if before_payload != after_payload:
                self._put_back(base, rel, before_payload)

        pending: list[tuple[str, tuple, tuple | None]] = []
        for rel, payload in after.items():
            if rel in pinned:
                keep_pinned(rel, before.get(rel), payload)
                continue
            if rel in declared or before.get(rel) == payload:
                continue
            if keep_planned(rel, before.get(rel), payload):
                continue
            pending.append((rel, payload, before.get(rel)))
        # A new file this step did not name will be removed. A .uid beside that new file is not kept.
        removing = {rel for rel, _payload, old in pending
                    if old is None and not self._editor_file_beside(root, rel, _payload)}
        for rel, payload, old in pending:
            if self._editor_file_beside(root, rel, payload):
                body = rel[:-4] if rel.endswith(".uid") else rel[:-len(".import")]
                if body not in removing and (base / body).is_file():
                    self._beside_kept.append(rel)
                    continue
            restored.append((rel, self._payload_diff(old, payload)))
            self._put_back(base, rel, old)
        self._beside_kept.sort()
        for rel, payload in before.items():
            if rel in pinned:
                keep_pinned(rel, payload, after.get(rel))
                continue
            if rel in declared or rel in after:
                continue
            if keep_planned(rel, payload, None):
                continue
            restored.append((rel, self._payload_diff(payload, None)))
            self._put_back(base, rel, payload)
        restored.sort(key=lambda item: item[0])
        return restored

    def _editor_file_beside(self, root: str, rel: str, payload: tuple | None) -> bool:
        """True when rel is a .uid or .import next to an asset file, and the new bytes are that file's shape.

        The suffix alone does not decide it. The asset has to be a file on disk. A .uid is one uid:// line.
        An .import file has a [remap] section. Anything else, including a script, is not this.
        """
        if not payload or payload[0] != "file" or not isinstance(payload[1], (bytes, bytearray)):
            return False
        if rel.endswith(".uid"):
            body = rel[:-4]
            kind = "uid"
        elif rel.endswith(".import"):
            body = rel[:-len(".import")]
            kind = "import"
        else:
            return False
        if not body or body.endswith("/"):
            return False
        try:
            if not (Path(root) / body).is_file():
                return False
            text = payload[1].decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if kind == "uid":
            line = text[:-1] if text.endswith("\n") else text
            if line.endswith("\r"):
                line = line[:-1]
            if "\n" in line or "\r" in line or " " in line or "\t" in line:
                return False
            return line.startswith("uid://") and len(line) > len("uid://")
        return any(part.strip() == "[remap]" for part in text.splitlines())

    def _beside_note(self, rels: list[str]) -> str:
        """One line naming each editor file that stayed beside an asset already on disk."""
        bits = []
        for rel in rels:
            body = rel[:-4] if rel.endswith(".uid") else rel[:-len(".import")]
            bits.append(f"res://{rel} beside res://{body}")
        if len(bits) == 1:
            return f"game-editor: {bits[0]} was written by the editor and was kept."
        shown = bits[:12]
        extra = f" and {len(bits) - 12} more" if len(bits) > 12 else ""
        return "game-editor: " + ", ".join(shown) + extra + " were written by the editor and were kept."

    def _declared_rels(self, step: dict, root: str) -> set[str]:
        """Project-relative paths this step is allowed to change: the paths it names, also, and side-effect files."""
        mentioned: set[str] = set()
        _res_mentions(step, mentioned)
        mentioned |= self.side_effect_files(step, root)
        uids: set[str] = set()
        _uid_mentions(step, uids)
        for uid in uids:
            resolved = self.resolve_uid(uid, root)
            if resolved:
                mentioned.add(resolved)
        gname, gop, _spec = self._step_spec(step)
        if gname == "save_scene" and gop == "save":
            scene = self.current_scene()
            if isinstance(scene, str) and scene.startswith("res://"):
                mentioned.add(scene)
        if gname == "export_game":
            output = step.get("output")
            if isinstance(output, str) and output.strip() and "://" not in output:
                mentioned.add("res://" + output.strip().lstrip("/"))
        rels = set()
        for res in mentioned:
            rel = self._res_rel(res)
            if rel:
                rels.add(rel)
        return rels

    def _side_rels(self, step: dict, root: str) -> set[str]:
        """Project-relative side-effect paths this step names, such as the .uid beside a script it creates.

        Later steps of the same plan treat a Godot write of these paths as planned. The step's own file is not included.
        """
        rels: set[str] = set()
        for res in self.side_effect_files(step, root):
            rel = self._res_rel(res)
            if rel:
                rels.add(rel)
        return rels

    _IMPORT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".wav", ".ogg", ".mp3",
                        ".glb", ".gltf", ".fbx", ".ttf")

    def _step_may_import(self, step: dict) -> bool:
        gname, gop, _spec = self._step_spec(step)
        if gname == "import_files" or gop in ("reimport", "scan", "import"):
            return True
        blob = json.dumps(step, ensure_ascii=False).lower()
        return any(suffix in blob for suffix in self._IMPORT_SUFFIXES)

    def _import_wait(self) -> None:
        if any("fake_upstream" in part for part in self.argv):
            return
        self._sleep(0.25)

    def _run_compare(self, root: str) -> dict:
        """An approved scene_do op compare: read both sides, including @tool getters, and keep them for a later save."""
        scene = self.current_scene()
        if not isinstance(scene, str) or not scene.startswith("res://"):
            return error_result("game-editor: scene_do op compare found no open scene file. Nothing was compared.")
        self._local.compare_runs_code = True
        try:
            compared, diffs, why, skipped = self.compare(scene, root, None)
        finally:
            self._local.compare_runs_code = False
        sha = file_sha(self.disk_path(scene, root))
        previous = self.compare_snapshots.get(scene) or {}
        bytes_moved = bool(compared and not diffs and previous.get("sha") not in (None, sha))
        self.compare_snapshots[scene] = {"sha": sha, "diffs": list(diffs), "compared": bool(compared),
                                         "why": why, "skipped": list(skipped), "at": time.time(),
                                         "bytes_moved": bytes_moved}
        # This compare is the editor as it is now, including edits earlier in this plan.
        self._local.unsaved_edit = None
        if not compared:
            return error_result(
                f"game-editor: compare could not compare every value of {scene} "
                f"({why or 'the file could not be read'}). "
                f"A later save cannot name both sides. Save a copy with scene_do op save_as when the scene is too "
                f"large. Nothing was saved.")
        body = "\n".join(diffs) if diffs else "the editor and the file agree"
        return {"content": [{"type": "text", "text": f"game-editor: compared {scene}\n{body}"}], "isError": False}

    def _compile_oracle(self, step: dict, result: dict) -> dict | None:
        """A written script is not a success when the editor's diagnostics or the log after the write name a parse error."""
        if result.get("isError"):
            return None
        gname, _gop, _spec = self._step_spec(step)
        path = step.get("path") if isinstance(step.get("path"), str) else ""
        if gname not in ("create_script", "edit_script", "write_file") or not path.lower().endswith((".gd", ".cs")):
            return None
        data = _first_json(result)
        for item in data.get("diagnostics") or []:
            if isinstance(item, dict) and _diag_error(item):
                text = str(item.get("text") or item.get("message") or "parse error")
                line = item.get("line")
                return error_result(
                    f"game-editor: {path} was written and did not compile. upstream_quote: {path}:{line}: {text}")
        logs = self.upstream("logs_read", {"source": "editor", "count": 40})
        blob = json.dumps(logs.get("content") or "", ensure_ascii=False)
        name = path.rsplit("/", 1)[-1].lower()
        low = blob.lower()
        if name in low and any(word in low for word in ("parse error", "parser error", "script error")):
            return error_result(
                f"game-editor: {path} was written and the editor log after the step names a script error. "
                f"That is not success. upstream_quote: {_first_line(logs)[:300]}")
        return None

    def _run_plan(self, plan: dict, root: str, mid, baseline_done: bool) -> dict:
        if not baseline_done:
            why, _diffs = self._apply_baseline(plan, root)
            if why:
                return error_result(why)
        self._local.active_steps = plan["steps"]
        scripts_before = self.script_snapshot(root)
        soft_stop = None
        results = []
        total = len(plan["steps"])
        self._local.unsaved_edit = None
        self._local.plan_edits = []
        self._local.plan_instances = {}
        self._local.bus_seen = {}
        # Side-effect paths an earlier step of this plan already named (.uid beside a new script, and the like).
        # A later step may let Godot write those. The step's own file is not carried, so an older open tab is still put back.
        planned_side: set[str] = set()
        for index, step in enumerate(plan["steps"], 1):
            self.progress(f"step {index} of {total}: {step.get('tool')} {step.get('op') or ''}".strip(), index, total + 1)
            args = {key: value for key, value in step.items() if key not in ("tool", "also")}
            before = self.step_before(step, root)
            before_files = self._capture_files(root)
            declared = self._declared_rels(step, root) | planned_side
            try:
                if step.get("tool") == "scene_do" and step.get("op") == "compare":
                    result = self._run_compare(root)
                else:
                    result = self._dispatch(step["tool"], args, mid, planning=False, public_read=False)
            except Refused as exc:
                result = error_result(str(exc))
            stopped_here = bool(result.pop("stop_plan", False))
            if not result.get("isError"):
                result = self.step_checked(step, result, before, root, plan)
            compile_error = self._compile_oracle(step, result)
            if compile_error is not None:
                result = compile_error
            if self._step_may_import(step):
                self._import_wait()
            strays = self._restore_strays(root, before_files, declared, self._also_rels(step))
            planned_side |= self._side_rels(step, root)
            kept = list(getattr(self, "_beside_kept", []) or [])
            if strays:
                first, note = strays[0]
                listed = ", ".join(f"res://{rel}" for rel, _note in strays[:12])
                more = f" and {len(strays) - 12} more" if len(strays) > 12 else ""
                # The file and the difference come first. A reply cut to 2,048 characters still says why.
                # Listing the path in also keeps the bytes already on disk. It does not keep an older tab.
                stray_text = (f"game-editor: res://{first} changed and this step did not name it ({note}). "
                              f"It was put back."
                              + (f" Also {listed}{more}." if len(strays) > 1 else "")
                              + " The bytes on disk before this step were kept. To write a different version, "
                              "plan a step that shows the old text and the new text. Listing the path in also "
                              "keeps the bytes already on disk, and the plan shows those bytes. It does not "
                              "keep an older editor tab.")
                result = dict(result)
                result["isError"] = True
                content = list(result.get("content") or [])
                content.insert(0, {"type": "text", "text": stray_text})
                result["content"] = content
            if kept:
                result = dict(result)
                content = list(result.get("content") or [])
                content.append({"type": "text", "text": self._beside_note(kept)})
                result["content"] = content
            results.append(result)
            if result.get("isError"):
                break
            if stopped_here:
                soft_stop = index
                break
        placed = self.check_placed(plan, results, root) + self.scripts_changed_behind(plan["steps"], root, scripts_before)
        placed += self.note_pending_buses(plan, results, root)
        self.progress("done", total + 1, total + 1)
        self._forget_uncompared_edit()
        if total == 1 and not placed:
            return results[0]
        failed = next((i for i, result in enumerate(results, 1) if result.get("isError")), None)
        if failed is None and soft_stop is not None and soft_stop < total:
            lines = [f"game-editor: apply_plan stopped after step {soft_stop} of {total}: "
                     f"{_first_line(results[soft_stop - 1])[:400]}",
                     f"Steps {soft_stop + 1} to {total} were not sent." if soft_stop < total - 1 else
                     f"Step {total} was not sent."]
        elif failed is None and soft_stop is not None:
            lines = [f"game-editor: apply_plan ran all {total} steps; step {soft_stop} found problems: "
                     f"{_first_line(results[soft_stop - 1])[:400]}"]
        elif failed is None:
            lines = [f"game-editor: apply_plan ran all {total} steps."]
        else:
            # The step's conclusion comes first. A play report stays the play_session line even when a file note
            # was placed ahead of it. The reason, when it is a different line, follows immediately.
            conclusion = _step_conclusion(results[failed - 1])
            why = _first_line(results[failed - 1])
            ran = f"Steps 1 to {failed - 1} ran" if failed > 2 else ("Step 1 ran" if failed == 2 else "No step ran")
            rest = (f"; steps {failed + 1} to {total} were not sent" if failed < total - 1 else
                    (f"; step {total} was not sent" if failed == total - 1 else ""))
            lines = [f"game-editor: apply_plan step {failed} of {total} failed: {conclusion}"]
            if why and why not in lines[0]:
                lines.append(why)
            lines.append(f"{ran}{rest}.")
        lines += placed
        others: list[dict] = []
        for index, result in enumerate(results, 1):
            lines.append(f"step {index}:")
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    lines.append(str(item.get("text", "")))
                else:
                    others.append(item)
        # One text block: Hermes joins parts differently for errors and successes.
        self._local.active_steps = None
        text = "\n".join(lines)
        if failed is not None and len(text) > FAILED_REPLY_CHARS:
            return {"content": [{"type": "text", "text": text[:FAILED_REPLY_CHARS]}], "isError": True}
        return {"content": [{"type": "text", "text": text}] + others, "isError": failed is not None}

    # ---- read back each step before saying it is done (PLAYBOOK 16, H7 ②) ----
    def _step_spec(self, step: dict) -> tuple[str, str, dict]:
        tool = TOOLS.get(step.get("tool") or "") or {}
        op = step.get("op") or tool.get("default_op")
        spec = (tool.get("ops") or {}).get(op or "") or {}
        return spec.get("guard") or str(step.get("tool")), spec["guard_op"] if "guard_op" in spec else str(op), spec

    def step_before(self, step: dict, root: str) -> dict:
        """What a step's read-back compares against: the open scene, and the file the step writes, before it runs."""
        gname, _gop, _spec = self._step_spec(step)
        out: dict = {"scene": self.current_scene() if gname in ("save_scene", "add_node", "set_node_property",
                                                               "delete_node", "edit_level") else ""}
        path = step.get("path")
        if isinstance(path, str) and path.strip().lower().startswith("res://"):
            out["file"] = path.strip()
            out["sha"] = file_sha(self.disk_path(path.strip(), root))
        return out

    def _read_node(self, path: str, fields: list[str]) -> tuple[dict | None, str]:
        res = self.upstream("node_get_properties", {"path": path, "fields": fields})
        if res.get("isError") or "bridge_error" in res:
            return None, _first_line(res)
        body = _first_json(res)
        return {item.get("name"): item.get("value") for item in body.get("properties") or []
                if isinstance(item, dict)}, ""

    def step_checked(self, step: dict, result: dict, before: dict, root: str, plan: dict | None = None) -> dict:
        """The step's reply with what was read back after it ("game-editor checked: ..."), or an error reply when the
        state does not show what the step said it did. A read that would run a @tool script is not made; the line
        says so."""
        gname, gop, _spec = self._step_spec(step)
        keys = {k: v for k, v in step.items() if k not in ("tool", "op")}
        scene = before.get("scene") or ""
        lines: list[str] = []
        problem = ""
        placed = getattr(self._local, "plan_instances", None)
        if not isinstance(placed, dict):
            placed = {}
            self._local.plan_instances = placed

        def hooked(node_path: str) -> bool:
            """A node this plan placed from a scene is not in the file yet: its scripts come from that scene."""
            for made, made_scene in placed.items():
                if node_path == made or node_path.startswith(made + "/"):
                    return any(hooks is None or hooks & GET_HOOKS
                               for _p, _s, hooks in self.scene_tool_nodes(made_scene, root))
            return bool(scene) and self._node_get_hooked(node_path, scene, root)

        try:
            if gname == "add_node":
                data = _first_json(result)
                path = data.get("path") if isinstance(data.get("path"), str) else None
                if path is None:
                    parent = str(keys.get("parent_path") or "").rstrip("/")
                    path = f"{parent}/{keys.get('name')}" if keys.get("name") else None
                if path and isinstance(keys.get("scene_path"), str) and keys["scene_path"].strip():
                    placed[path] = self.check_paths("apply_plan", [keys["scene_path"].strip()], root)[0]
                if path and hooked(path):
                    lines.append(f"{path} not read back: its @tool script would run")
                elif path:
                    got, why = self._read_node(path, ["name"])
                    if got is None:
                        problem = f"Godot AI said it added {path}, but reading it back failed ({why[:160]})"
                    else:
                        lines.append(f"{path} is in the open scene")
                        self._local.plan_edits = list(getattr(self._local, "plan_edits", []) or []) + [path]
            elif gname == "set_node_property" and isinstance(keys.get("path"), str) and isinstance(keys.get("property"), str):
                path, prop = keys["path"], keys["property"]
                if hooked(path):
                    lines.append(f"{path} {prop} not read back: its @tool script would run")
                else:
                    got, why = self._read_node(path, [prop])
                    want = keys.get("value")
                    if got is None or prop not in got:
                        lines.append(f"{path} {prop} could not be read back ({why[:120] or 'not returned'})")
                    elif _plain_value(want) and not same_value(want, got[prop]):
                        problem = (f"{path} {prop} reads {json.dumps(got[prop], ensure_ascii=False)[:120]} after the "
                                   f"step, not {json.dumps(want, ensure_ascii=False)[:120]}; Godot did not keep the value")
                    else:
                        lines.append(f"{path} {prop} = {json.dumps(got[prop], ensure_ascii=False)[:120]}")
                        self._local.plan_edits = list(getattr(self._local, "plan_edits", []) or []) + [path]
            elif gname == "delete_node" and isinstance(keys.get("path"), str) and hooked(keys["path"]):
                lines.append(f"{keys['path']} not read back: its @tool script would run")
            elif gname == "delete_node" and isinstance(keys.get("path"), str):
                got, why = self._read_node(keys["path"], ["name"])
                if got is not None:
                    problem = f"{keys['path']} is still in the open scene after the delete"
                elif "not found" in why.lower():
                    lines.append(f"{keys['path']} is gone from the open scene")
                else:
                    lines.append(f"{keys['path']} could not be read back ({why[:120]})")
            elif gname == "save_scene":
                target = before.get("file") if gop == "save_as" and before.get("file") else self.current_scene()
                if not target.startswith("res://") or not self.disk_path(target, root).is_file():
                    problem = f"the scene file {target or '(none)'} is not on disk after the save"
                else:
                    text = self.disk_path(target, root).read_text(encoding="utf-8", errors="replace")
                    parsed = parse_tscn(text)
                    if not parsed.get("root"):
                        problem = f"{target} does not read as a scene after the save"
                    else:
                        touched = list(dict.fromkeys(p for p in getattr(self._local, "plan_edits", []) or []
                                                     if isinstance(p, str)))
                        focus = {rel_node_path(p, parsed["root"]) for p in touched}
                        focus = {rel for rel in focus if rel == "" or rel in (parsed.get("nodes") or {})}
                        missing = [p for p in touched if rel_node_path(p, parsed["root"]) not in
                                   ({""} | set(parsed.get("nodes") or {}))]
                        if missing:
                            problem = f"{target} was saved without {', '.join(missing[:4])}"
                        elif focus:
                            compared, diffs, why, skipped = self.compare(target, root, focus)
                            if diffs:
                                problem = f"after the save, {target} and the editor still differ: {'; '.join(diffs[:4])}"
                            elif not compared and (not skipped or not all(str(s).startswith("@tool script on")
                                                                          for s in skipped)):
                                lines.append(f"{target} saved; the nodes this plan changed were not read back ({why})")
                            else:
                                lines.append(f"{target} on disk matches the editor for {len(focus)} node(s) this plan "
                                             f"changed ({len(parsed.get('nodes') or {}) + 1} nodes in the file)")
                        else:
                            lines.append(f"{target} on disk reads as a scene of {len(parsed.get('nodes') or {}) + 1} nodes")
            elif gname in ("write_file", "create_script") and before.get("file"):
                res = before["file"]
                path = self.disk_path(res, root)
                if not path.is_file():
                    problem = f"{res} is not on disk after the write"
                else:
                    want = str(keys.get("content") or "")
                    have = path.read_text(encoding="utf-8", errors="replace")
                    if _norm_text(want) != _norm_text(have):
                        problem = f"{res} on disk is not the content this step wrote ({len(have)} characters, not {len(want)})"
                    else:
                        lines.append(f"{res} on disk has the planned content ({len(have)} characters)")
            elif gname == "edit_script" and before.get("file"):
                res = before["file"]
                have = self.disk_path(res, root).read_text(encoding="utf-8", errors="replace")
                new_text = keys.get("new_text")
                if isinstance(new_text, str) and new_text and new_text not in have:
                    problem = f"{res} on disk does not contain the new text after the patch"
                else:
                    lines.append(f"{res} on disk has the patch")
            elif gname in ("delete_file", "delete_file_permanently") and before.get("file"):
                if self.disk_path(before["file"], root).exists():
                    problem = f"{before['file']} is still on disk after the delete"
                else:
                    lines.append(f"{before['file']} is gone")
            elif gname == "open_scene" and isinstance(keys.get("path"), str):
                now = self.current_scene()
                wanted = keys["path"].strip()
                if wanted.startswith("uid://"):
                    wanted = self.resolve_uid(wanted, root) or wanted
                if now != wanted:
                    problem = f"the open scene is {now or 'none'}, not {keys['path']}, after open_scene"
                else:
                    lines.append(f"the open scene is {now}")
            elif gname == "new_scene" and isinstance(keys.get("path"), str):
                res = keys["path"].strip()
                res = res if res.endswith((".tscn", ".scn")) else res + ".tscn"
                if not self.disk_path(res, root).is_file():
                    problem = f"{res} is not on disk after new_scene"
                else:
                    lines.append(f"{res} is on disk and open")
            elif gname == "edit_project_settings" and gop == "set" and isinstance(keys.get("key"), str):
                value = project_setting(Path(root), keys["key"])
                lines.append(f"project.godot {keys['key']} = {value if value is not None else '(not written: Godot default)'}")
            elif gname == "edit_project_settings" and gop == "autoload_add" and isinstance(keys.get("name"), str):
                if project_setting(Path(root), f"autoload/{keys['name']}") is None:
                    problem = f"project.godot has no autoload {keys['name']} after the step"
                else:
                    lines.append(f"project.godot lists autoload {keys['name']}")
            elif gname == "edit_level" and gop == "audio_set_playback" and isinstance(keys.get("player_path"), str):
                fields = [k for k in ("bus", "volume_db", "autoplay") if k in keys]
                path = keys["player_path"]
                if fields and not hooked(path):
                    got, _why = self._read_node(path, fields)
                    placed = any(check.get("path") == path for check in (plan or {}).get("checks") or [])
                    if got is not None and "bus" in keys:
                        seen = getattr(self._local, "bus_seen", None)
                        if isinstance(seen, dict):
                            seen.setdefault(path, []).append((self.current_scene(), got.get("bus")))
                    if got is not None and "bus" in keys and got.get("bus") != keys["bus"] and not placed:
                        problem = (f"{path} bus reads {got.get('bus')} after the step, not {keys['bus']}: the open "
                                   f"editor has not loaded bus {keys['bus']}. Reopen the project (Project > Reload "
                                   f"Current Project), then plan this step again")
                    elif got is not None:
                        lines.append(f"{path} " + ", ".join(f"{k} = {json.dumps(got.get(k))}" for k in fields))
                        self._local.plan_edits = list(getattr(self._local, "plan_edits", []) or []) + [path]
        except (OSError, ValueError) as exc:
            lines.append(f"not read back ({type(exc).__name__})")
        if problem:
            return error_result(escape_controls(f"game-editor: read back after the step: {problem}. The later steps "
                                                f"were not sent."))
        if lines:
            result = dict(result)
            content = list(result.get("content") or [])
            at = len(content)
            # The bridge's own notes stay last; the read-back goes right after Godot AI's reply.
            while at > 0 and isinstance(content[at - 1], dict) and str(content[at - 1].get("text", "")).startswith(
                    ("game-editor note", "game-editor warning", "game-editor: overwrite", "game-editor did")):
                at -= 1
            content.insert(at, {"type": "text", "text": escape_controls("game-editor checked: " + "; ".join(lines))})
            result["content"] = content
        return result

    def scene_scripts(self, scene: str, root: str) -> set[str]:
        """res:// scripts the scene file and the scenes it instances use (two levels), from the files on disk."""
        found: set[str] = set()
        todo, seen = [(scene, 0)], set()
        while todo:
            ref, depth = todo.pop()
            if ref in seen or depth > 2 or not ref:
                continue
            seen.add(ref)
            parsed = self.read_scene(ref, root)
            for entry in ((parsed or {}).get("ext") or {}).values():
                target = (self.resolve_uid(entry["uid"], root) if entry.get("uid") else None) or entry.get("path") or ""
                if not target.startswith("res://"):
                    continue
                if entry.get("type") == "Script" and target.endswith((".gd", ".cs")):
                    found.add(target)
                elif entry.get("type") == "PackedScene":
                    todo.append((target, depth + 1))
        return found

    def script_snapshot(self, root: str | None) -> dict[str, str | None]:
        """Hashes of the scripts the open scene uses and the scripts this session wrote, before a plan runs."""
        if root is None:
            return {}
        names = set(self.written_scripts)
        scene = self.current_scene()
        if scene.startswith("res://"):
            names |= self.scene_scripts(scene, root)
        out = {}
        for res in names:
            path = self.disk_path(res, root)
            try:
                out[res] = (file_sha(path), path.read_text(encoding="utf-8") if path.stat().st_size <= MAX_READ_FILE_BYTES else None)
            except (OSError, UnicodeDecodeError):
                out[res] = (file_sha(path), None)
        return out

    def scripts_changed_behind(self, steps: list[dict], root: str | None,
                               before: dict[str, tuple] | None = None) -> list[str]:
        """Scripts whose file changed during this plan without a step of the plan writing them: the editor can
        save an older copy it holds (an open script tab) when the scene is saved or the game runs. Said, with
        the first line that differs; not written back (it could also be the user's own edit)."""
        if root is None:
            return []
        named: set[str] = set()
        for step in steps:
            value = step.get("path")
            if isinstance(value, str) and value.strip().lower().startswith(("res://", "uid://")):
                try:
                    named.add(self.check_paths("apply_plan", [value.strip()], root)[0].lower())
                except Refused:
                    named.add(value.strip().lower())
        out = []
        with self.state_lock:
            items: dict[str, tuple] = {res: (sha, None) for res, sha in self.written_scripts.items()}
        items.update(before or {})
        for res, (sha, text) in items.items():
            if res.lower() in named:
                continue
            path = self.disk_path(res, root)
            now = file_sha(path)
            if now == sha:
                continue
            with self.state_lock:
                self.written_scripts[res] = now
            changed = ""
            if text is not None:
                try:
                    after = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    after = []
                old_lines = text.splitlines()
                for number in range(max(len(old_lines), len(after))):
                    left = old_lines[number] if number < len(old_lines) else ""
                    right = after[number] if number < len(after) else ""
                    if left != right:
                        changed = (f" Line {number + 1} was {left.strip()[:80]!r} before this plan and is "
                                   f"{right.strip()[:80]!r} now.")
                        break
            out.append(f"game-editor warning: {res} changed while this plan ran, and no step of it wrote that file."
                       f"{changed} The editor may have saved an older copy it held in an open script tab, or "
                       f"someone edited it. Nothing was written back. Tell the user, and plan a fix only if they "
                       f"want the earlier text.")
        return out

    def _apply_baseline(self, plan: dict, root: str) -> tuple[str, list[str] | None]:
        """Before the first step: compare the open scene with its file when the plan saves it, so a save in this
        plan is judged against what was there before the plan, not against the plan's own edits.
        (why it does not run, or "", and the differences for a save with overwrite)."""
        scene = self.current_scene()
        if not scene.startswith("res://"):
            return "", None
        mode = self._plan_save(plan["steps"])
        saves = mode is not None
        snap = self.compare_snapshots.get(scene) or {}
        sha_now = file_sha(self.disk_path(scene, root))
        fresh = bool(snap.get("compared")) and snap.get("sha") == sha_now and self.disk_path(scene, root).is_file()
        if fresh and saves:
            diffs = list(snap.get("diffs") or [])
            blocking = self._without_own_nodes(scene, root, diffs)
            if mode is False and blocking:
                diffs = blocking
                listed = "; ".join(diffs[:8]) + (f"; and {len(diffs) - 8} more" if len(diffs) > 8 else "")
                return (escape_controls(
                    f"game-editor: apply_plan did not run: the last compare of {scene} found differences: {listed}. "
                    f"This plan's save would overwrite the disk side. Plan the save with overwrite: true so the plan "
                    f"names each value. Nothing was sent to the Godot editor."), None)
            if mode is False:
                return "", None
            planned = plan.get("planned_diffs")
            if planned is not None and sorted(diffs) != sorted(planned):
                new = [line for line in diffs if line not in planned]
                gone = [line for line in planned if line not in diffs]
                said = "; ".join((["now also: " + "; ".join(new[:4])] if new else []) +
                                 (["no longer: " + "; ".join(gone[:4])] if gone else []))
                return (escape_controls(
                    f"game-editor: apply_plan did not run: what the editor and {scene} differ in changed after this "
                    f"plan named what its save drops ({said}). Call plan_change again and show the new plan. Nothing "
                    f"was sent to the Godot editor."), None)
            return "", diffs
        with self.state_lock:
            record = self.scenes.get(scene)
            if mode is True:
                # An overwrite names what it drops: compare afresh, not from an earlier look.
                self.scenes.pop(scene, None)
                record = None
        if record is None or not record.get("structure") or (saves and not record.get("full")):
            self.progress(f"comparing {scene} with its file")
            self.observe(scene, root, set(), full=saves)
        if not saves:
            return "", None
        record = self.scenes.get(scene) or {}
        diffs = list(record.get("diffs") or [])
        if mode is True:
            planned = plan.get("planned_diffs")
            deferred = plan.get("deferred") or []
            later = [line for line in diffs if planned is not None and line not in planned and any(
                line.startswith(f"{path} {field}:") for path in deferred for field in LAYOUT_FIELDS)]
            if planned is not None and sorted(line for line in diffs if line not in later) != sorted(planned):
                new = [line for line in diffs if line not in planned and line not in later]
                gone = [line for line in planned if line not in diffs]
                said = "; ".join((["now also: " + "; ".join(new[:4])] if new else []) +
                                 (["no longer: " + "; ".join(gone[:4])] if gone else []))
                return (escape_controls(
                    f"game-editor: apply_plan did not run: what the editor and {scene} differ in changed after this "
                    f"plan named what its save drops ({said}). Call plan_change again and show the new plan. Nothing "
                    f"was sent to the Godot editor."), None)
            if record.get("why"):
                return (f"game-editor: apply_plan did not run: this plugin could not compare the editor with {scene} "
                        f"({record['why']}), so a save cannot name both sides. Nothing was sent to the Godot editor."), None
            return "", diffs
        sha_now = file_sha(self.disk_path(scene, root))
        if record.get("sha") is not None and sha_now != record.get("sha"):
            return (f"game-editor: apply_plan did not run: {scene} changed on disk after this session last saw the "
                    f"editor and the file agree (a teammate, git, another editor), and this plan saves it. Ask the "
                    f"user what to keep, then plan again: a save step with overwrite: true keeps the editor's "
                    f"version and names what it drops, or scene_do op revert_scene loads the disk version. Nothing "
                    f"was sent to the Godot editor."), None
        if diffs:
            listed = "; ".join(diffs[:8]) + (f"; and {len(diffs) - 8} more" if len(diffs) > 8 else "")
            return (escape_controls(
                f"game-editor: apply_plan did not run: before this plan, the editor and {scene} already "
                f"differed: {listed}. They are unsaved edits made in the editor before this session, or changes "
                f"made to the file on disk; this plugin cannot tell which, and this plan's save would overwrite "
                f"the disk side. Ask the user. To keep the editor's version, plan again with overwrite: true on "
                f"the save step (the plan then names what is dropped). Nothing was sent to the Godot editor."), None)
        if record.get("why"):
            return (f"game-editor: apply_plan did not run: this plugin could not compare the editor with {scene} "
                    f"({record['why']}), so it cannot rule out a change on disk that this plan's save would overwrite. "
                    f"Save a copy first with scene_do op save_as, or plan scene_do op compare and then the save. "
                    f"Nothing was sent to the Godot editor."), None
        return "", None

    def _unparsed_scene_reason(self, scene: str, root: str) -> str:
        """Why the open scene cannot be planned from its file, or "". Does not read the editor and does not branch
        on a @tool annotation."""
        if not isinstance(scene, str) or not scene.startswith("res://"):
            return ""
        virt = self._virtual()
        if virt is not None and scene in (virt.get("scenes") or {}):
            return ""
        path = self.disk_path(scene, root)
        if not path.is_file():
            return f"{scene} is not a file on disk, so its unsaved buffer cannot be parsed"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return f"{scene} could not be read ({type(exc).__name__})"
        if text.startswith("\ufeff"):
            return f"{scene} starts with a byte-order mark, so it was not parsed"
        parsed = parse_tscn(text)
        if not parsed.get("root"):
            return f"{scene} could not be parsed"
        if parsed.get("root_scene"):
            return f"{scene} inherits another scene, so its nodes are not all in this file"
        return ""

    def _file_tree_rows(self, scene: str, root: str | None) -> tuple[list, int | None]:
        """[path, type, child count] from the scene file, for a plan fingerprint. Empty when the file is not a
        plain scene. This is not _disk_tree: that one returns the parsed node map a disk read shows."""
        if not root or not isinstance(scene, str) or not scene.startswith("res://"):
            return [], None
        if self._unparsed_scene_reason(scene, root):
            return [], None
        parsed = self.read_scene(scene, root) or {}
        root_name = parsed.get("root") or ""
        if not root_name:
            return [], None
        kids: dict[str, int] = {"": 0}
        for rel in parsed.get("nodes") or {}:
            parent = _parent(rel)
            kids[parent] = kids.get(parent, 0) + 1
            kids.setdefault(rel, kids.get(rel, 0))
        rows = [[f"/{root_name}", parsed.get("root_type") or "", kids.get("", 0)]]
        for rel in sorted(parsed.get("nodes") or {}):
            info = parsed["nodes"][rel]
            rows.append([f"/{root_name}/{rel}", (info or {}).get("type") or "", kids.get(rel, 0)])
        return rows, len(rows)

    def _fingerprint(self, steps: list, root: str | None, diffs: list[str] | None = None) -> str:
        """The session table (current scene and play state), the scene file's nodes (path, type, child count),
        the files a plan touches (also the files its steps change as a side effect: project.godot, an asset's
        .import, a script's .uid, the .uid and .import beside a move), and the differences a save with overwrite
        would drop. The editor is not asked: no editor state, no open-scene list, no node values."""
        # A save names .tres this plan mentions. apply_plan checks this fingerprint before the run sets
        # active_steps, so the check has to see the same steps the plan was built from.
        previous = getattr(self._local, "active_steps", None)
        self._local.active_steps = steps
        try:
            return self._fingerprint_body(steps, root, diffs)
        finally:
            self._local.active_steps = previous

    def _fingerprint_body(self, steps: list, root: str | None, diffs: list[str] | None) -> str:
        data = self.registry_state()
        scene = data.get("current_scene") if isinstance(data.get("current_scene"), str) else ""
        nodes, total = self._file_tree_rows(scene, root)
        files = {}
        if root:
            mentioned: set[str] = set()
            uids: set[str] = set()
            for step in steps:
                _res_mentions(step, mentioned)
                _uid_mentions(step, uids)
                mentioned |= self.side_effect_files(step, root)
            for uid in uids:
                mentioned.add(self.resolve_uid(uid, root) or uid)
            for res in sorted(mentioned | {path for path in [scene] if isinstance(path, str) and path.startswith("res://")}):
                if not res.startswith("res://"):
                    continue
                digest = file_sha(self.disk_path(res, root))
                files[res] = digest if digest is not None else "absent"
        export = None
        if root:
            blobs = []
            for step in steps:
                if not isinstance(step, dict) or step.get("tool") != "ship_build":
                    continue
                keys = {key: value for key, value in step.items() if key not in ("tool", "op")}
                parsed, err = parse_export(keys)
                if parsed is None:
                    blobs.append({"error": err})
                    continue
                dest, dest_err = output_path(Path(root), parsed["output"])
                if dest is None:
                    blobs.append({"error": dest_err})
                    continue
                neighbors = []
                for path in export_neighbor_paths(dest):
                    if path.is_file():
                        sha = file_sha(path)
                    elif path.exists():
                        sha = "dir"
                    else:
                        sha = "absent"
                    neighbors.append({"name": path.name, "sha": sha})
                preset = Path(root) / "export_presets.cfg"
                blobs.append({"output": parsed["output"], "neighbors": neighbors,
                              "preset_sha": file_sha(preset) if preset.is_file() else "absent"})
            if blobs:
                export = blobs
        payload = {"current_scene": scene, "is_playing": data.get("is_playing"),
                   "nodes": nodes, "total_count": total, "files": files, "export": export,
                   "drops": sorted(diffs) if diffs is not None else None}
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def side_effect_files(self, step: dict, root: str) -> set[str]:
        """Files a step changes without naming them (H7, from the extracted side effects): project.godot for a
        project setting, autoload or input change, and the .import beside an asset that is reimported or scanned."""
        out: set[str] = set()
        tool = TOOLS.get(step.get("tool") or "") or {}
        spec = (tool.get("ops") or {}).get(step.get("op") or "") or {}
        side = upstream_side_effects(spec)
        if {"changes_project_settings", "saves_project_settings"} & side or step.get("tool") == "project_do":
            out.add("res://project.godot")
        paths = step.get("paths") if isinstance(step.get("paths"), list) else []
        if "rescans" in side:
            for value in paths + [step.get("path")]:
                if isinstance(value, str) and value.strip().lower().startswith("res://"):
                    out.add(value.strip() + ".import")
        gname, gop, _spec = self._step_spec(step)
        for value in (step.get("path"),):
            if isinstance(value, str) and value.strip().lower().endswith((".gd", ".cs")) and gname in (
                    "create_script", "edit_script", "write_file"):
                out.add(value.strip() + ".uid")
        for value in move_sidecars(gname, gop, step):
            out.add(value)
        if gname == "save_scene" and gop == "save" and root:
            for res in self._plan_tres(root):
                out.add(res)
                out.add(res + ".uid")
        return out

    def _dispatch(self, name, arguments, mid, planning=False, public_read=False) -> dict:
        tool = TOOLS.get(name) if isinstance(name, str) else None
        if tool is None:
            return error_result(f"game-editor: unknown tool {name!r}. Nothing was sent to the Godot editor.")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return error_result(f"game-editor: {name} takes an object of arguments. Nothing was sent.")
        keys = dict(arguments)
        if tool["multi"]:
            op = keys.pop("op", None) or tool["default_op"]
            if op is None:
                return error_result(f"game-editor: {name} needs op, one of: {', '.join(tool['ops'])}. "
                                    f"Nothing was sent to the Godot editor.")
            if op not in tool["ops"]:
                return error_result(f"game-editor: {name} has no op {op!r}; ops: {', '.join(tool['ops'])}. "
                                    f"Nothing was sent to the Godot editor.")
        else:
            op = tool["default_op"]
        if public_read and (name, op) not in READ_ALLOW:
            if name == "scene_look" and op in ("properties", "resource"):
                return error_result(
                    f"game-editor: {name} op {op} runs the node's script in the editor, so it is not a read. "
                    f"Call plan_change with tool scene_do op {op}. Nothing was sent to the Godot editor.")
            if name == "asset_look":
                return error_result(
                    f"game-editor: {name} op {op} changes the project, so it is not a read. "
                    f"Call plan_change with tool asset_look op {op}. Nothing was sent to the Godot editor.")
            return error_result(
                f"game-editor: {name} op {op} is not a published read. Nothing was sent to the Godot editor.")
        if public_read and name == "asset_look" and op == "nav_path" and keys.get("force_sync") is True:
            return error_result(
                "game-editor: asset_look op nav_path does not take force_sync true here: it rebuilds the "
                "navigation map in the editor. Omit force_sync. Nothing was sent to the Godot editor.")
        spec = tool["ops"][op]
        gname = spec.get("guard") or name
        gop = spec["guard_op"] if "guard_op" in spec else op
        effects = spec_effects(spec) | EFFECT_OVERRIDE.get((name, op), set())
        if "unresolved" in effects:
            return error_result(
                f"game-editor: {name} op {op} has no classified effect, so it was not run. Nothing was sent "
                f"to the Godot editor.")
        overwrite = False
        bridge_overwrite = bool(spec.get("file_create")) or (gname == "save_scene")
        if "overwrite" in keys and bridge_overwrite and not spec.get("upstream_overwrite"):
            overwrite = keys.pop("overwrite")
            if not isinstance(overwrite, bool):
                return error_result(f"game-editor: {name} overwrite must be true or false. Nothing was sent.")
        allowed = set(spec["keys"])
        if gname == "run_game":
            allowed.add("autosave")
        session_id = None
        if "session_id" in keys and spec["upstream"] not in ("@cli", "@local"):
            session_id = keys.pop("session_id")
            if session_id is not None and not isinstance(session_id, str):
                return error_result(f"game-editor: {name} session_id must be a string. Nothing was sent.")
            if session_id == "":
                session_id = None
        extra = sorted(k for k in keys if k not in allowed)
        if extra and not spec["any_keys"]:
            takes = ", ".join(sorted(allowed | ({"op"} if tool["multi"] else set()) |
                                     ({"overwrite"} if bridge_overwrite else set()) |
                                     ({"session_id"} if spec["upstream"] not in ("@cli", "@local") else set())
                                     )) or "no arguments"
            return error_result(f"game-editor: {name}{' op ' + op if tool['multi'] else ''} does not take "
                                f"{', '.join(extra)}; it takes {takes}. Nothing was sent to the "
                                f"Godot editor.")
        node_values = [keys.get(k) for k in NODE_KEYS if isinstance(keys.get(k), str)]
        for item in (keys.get("commands") or []) if isinstance(keys.get("commands"), list) else []:
            params = item.get("params") if isinstance(item, dict) and isinstance(item.get("params"), dict) else {}
            node_values += [params.get(k) for k in NODE_KEYS if isinstance(params.get(k), str)]
        for value in node_values:
            why = self.odd_node_path(value)
            if why:
                raise Refused(f"game-editor: {name} refused the node path {value!r}: {why}. Use the plain path from "
                              f"scene_look op scene_tree, such as /Main/Player. Nothing was sent to the Godot editor.")
        for value in path_key_strings(keys):
            why = ambiguous_path(value)
            if why:
                raise Refused(f"game-editor: {name} refused {value!r}: {why}. Nothing was sent to the "
                              f"Godot editor.")
        paths = scheme_strings(keys)
        for value in paths:
            if value[:8].lower().startswith(("res://", "user://")):
                self.check_paths(name, [value], None)
        if tool["tier"] != "read" and gname not in ("export_game",):
            for key in ("path", "new_path", "to", "dest", "destination", "output", "target_path"):
                value = keys.get(key)
                if isinstance(value, str) and value.strip() and "://" not in value and (
                        value.strip().startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value.strip())):
                    if gname in ("write_file", "create_script", "edit_script", "delete_file", "delete_file_permanently",
                                 "move_file", "create_resource", "import_files", "save_scene"):
                        raise Refused(f"game-editor: {name} refused {value!r}: it is outside the project. Godot AI "
                                      f"writes only res:// paths inside the project, so this plugin does not write "
                                      f"outside it. Nothing was sent to the Godot editor.")
        if spec["upstream"] != "@cli":
            reason = self.ensure_child()
            if reason:
                return error_result(reason)
            if self.available is not None and op not in self.available.get(name, []):
                return error_result(f"game-editor: {name} op {op} is not offered by the running Godot AI (this "
                                    f"plugin pins godot-ai {UPSTREAM_VERSION}). Nothing was sent.")
        else:
            reason = self.ensure_child()
            if reason:
                return error_result(reason)
        writes = tool["tier"] != "read" or (name, op) in EFFECT_OVERRIDE
        root = None
        if paths or writes or spec["upstream"] == "@cli":
            root, problem = self.project_root()
            if problem:
                return error_result(f"game-editor: {name} could not confirm the project folder, so the call was "
                                    f"not checked and nothing was sent to the Godot editor.")
        if root is not None and paths:
            self.check_paths(name, paths, root)
        scene = ""
        if root is not None and writes:
            scene = self.current_scene()
            # A plan does not compare: comparing reads node values, which can run a @tool getter. The
            # comparison runs at apply_plan, before the step's own call, as it did before plans existed.
            if (not planning and gname not in ("open_scene", "new_scene", "revert_scene")
                    and spec["upstream"] not in ("@cli", "@local")):
                self.observe(scene, root, self._call_focus(gname, keys, scene, root))
        if public_read and op_node_access(spec):
            if root is None:
                root, problem = self.project_root()
                if problem:
                    return error_result(f"game-editor: {name} could not confirm the project folder, so it could "
                                        f"not check the node's script. Nothing was sent to the Godot editor.")
            if root is not None:
                refusal = self.read_tool_refusal(name, op, spec, keys, self.current_scene(), root)
                if refusal:
                    raise Refused(refusal)
        if root is not None and gname in ("read_file", "read_script") and isinstance(keys.get("path"), str):
            target = self.check_paths(name, [keys["path"].strip()], root)[0]
            size = 0
            if target.startswith("res://"):
                try:
                    size = self.disk_path(target, root).stat().st_size
                except OSError:
                    size = 0
            if size > MAX_READ_FILE_BYTES:
                return error_result(f"game-editor: {name} did not read {keys['path']}: it is {size // 1024} KB, and "
                                    f"Godot AI fails on replies of about 1 MB. There is no partial read; use "
                                    f"file_look op files to find a smaller file, or ask the user for the part they need. "
                                    f"Nothing was sent to the Godot editor.")
        if root is not None and spec.get("file_create"):
            for key in spec["file_create"]:
                value = keys.get(key)
                if isinstance(value, str) and value.strip()[:6].lower() in ("res://", "uid://"):
                    res = self.check_paths(name, [value.strip()], root)[0]
                    virt = self._virtual()
                    made_here = virt is not None and res in virt.get("files", {})
                    if (self.disk_path(res, root).exists() or made_here) and not overwrite:
                        return error_result(f"game-editor: {name} refused {value}: the file already exists and "
                                            f"would be replaced. Pass overwrite: true to replace it, or pick another "
                                            f"path. Nothing was sent to the Godot editor.")
        if root is not None and spec.get("node_keys"):
            values = [keys[k] for k in spec["node_keys"] if isinstance(keys.get(k), str) and "://" not in keys[k]]
            self.check_nodes(name, values, scene, root)
        if gname == "batch_execute":
            self.check_batch(keys.get("commands"), scene, root)
        if root is not None and scene:
            if gname == "delete_node":
                self.check_structure(name, "delete", keys, scene, root)
            elif gname == "edit_node" and gop in ("reparent", "move", "add_to_group", "remove_from_group"):
                self.check_structure(name, gop, keys, scene, root)
            elif gname == "edit_node" and gop == "rename":
                self.check_rename(name, keys.get("path"), scene, root)
            elif gname == "connect_signal":
                self.check_connection(name, gop, keys, scene, root)
            elif gname == "batch_execute":
                for item in keys.get("commands") or []:
                    if not isinstance(item, dict):
                        continue
                    command = str(item.get("command") or "")
                    params = item.get("params") or {}
                    if not isinstance(params, dict):
                        params = {}
                    self.check_structure(name, command, params, scene, root)
                    if command == "rename_node":
                        self.check_rename(name, params.get("path"), scene, root)
                    if command in ("connect_signal", "disconnect_signal"):
                        self.check_connection(name, "disconnect" if command == "disconnect_signal" else "connect",
                                              params, scene, root)
                    if (not planning and command == "set_property" and isinstance(params.get("path"), str)
                            and isinstance(params.get("property"), str)):
                        self.check_stored_property(name, params["path"], params["property"], scene, root)
            elif (not planning and gname == "set_node_property" and isinstance(keys.get("path"), str)
                  and isinstance(keys.get("property"), str)):
                self.check_stored_property(name, keys["path"], keys["property"], scene, root)
        if gname == "edit_node" and gop == "duplicate" and root is not None and isinstance(keys.get("path"), str):
            self.check_duplicate(name, keys["path"], scene, root)
        if gname == "add_node" and root is not None and isinstance(keys.get("scene_path"), str):
            self.check_cycle(name, self.check_paths(name, [keys["scene_path"].strip()], root)[0], scene, root)
        if planning and root is not None and scene and gname in ("add_node", "batch_execute"):
            adds = [keys] if gname == "add_node" else [
                item.get("params") for item in keys.get("commands") or []
                if isinstance(item, dict) and item.get("command") == "create_node" and isinstance(item.get("params"), dict)]
            root_name = (self.read_scene(scene, root) or {}).get("root") or ""
            for params in adds:
                new_name = params.get("name") if isinstance(params.get("name"), str) and params.get("name") else None
                parent = params.get("parent_path") if isinstance(params.get("parent_path"), str) and params.get(
                    "parent_path") else f"/{root_name}"
                if new_name and self.node_taken(scene, parent, new_name, root):
                    raise Refused(f"game-editor: {name} refused to add {parent.rstrip('/')}/{new_name}: a node with that "
                                  f"name is already there. Godot would call the new one {new_name}2, and the steps after "
                                  f"it would change the existing node. Pick another name. Nothing was sent to the Godot "
                                  f"editor.")
        save_note = ""
        if root is not None and gname == "save_scene" and gop == "save":
            save_note = self.save_plan_note(scene, root, overwrite) if planning else self.check_save(
                name, scene, root, overwrite)
            if planning:
                tres = self._plan_tres(root)
                if tres:
                    shown_tres = ", ".join(tres[:12])
                    more = f" and {len(tres) - 12} more" if len(tres) > 12 else ""
                    save_note = (save_note + f" This step also names {shown_tres}{more}: Godot rewrites those "
                                 "resources when it saves the scene.").strip()
        if gname in ("run_game", "play_session"):
            keys.setdefault("autosave", False)
            if planning and keys.get("autosave") is True:
                save_note = ("autosave true saves every open scene before the run. At apply_plan each open scene "
                             "is compared with its file first; if one cannot be confirmed, nothing runs.")
            elif keys.get("autosave") is True and root is not None:
                self.check_save(name, scene, root, False)
                roots = _first_json(self.upstream("scene_manage", {"op": "get_roots"}))
                for item in roots.get("scenes") or roots.get("roots") or []:
                    other = item.get("path") if isinstance(item, dict) else item
                    if isinstance(other, str) and other != scene and other.startswith("res://"):
                        record = self.scenes.get(other)
                        if not record or not record["certain"] or file_sha(self.disk_path(other, root)) != record["sha"]:
                            raise Refused(f"game-editor: {name} refused autosave: {other} is open in the editor "
                                          f"and this plugin cannot confirm that saving it would not overwrite a "
                                          f"change on disk. Save the scenes you changed with scene_do op save, then run "
                                          f"with autosave false. Nothing was run.")
        step_changes: dict = {}
        if planning and gname == "play_session":
            # The same check play_session makes, so an approved plan does not fail on its arguments.
            parsed_play, play_err = parse_play(keys)
            if parsed_play is None:
                return error_result(play_err)
            added, help_note = self.play_watch_help(parsed_play, root)
            if added:
                keys["watch"] = list(parsed_play["watch"]) + added
                step_changes["watch"] = keys["watch"]
            if help_note:
                save_note = (save_note + "\n" + help_note).strip() if save_note else help_note
        if planning and gname == "run_tests" and gop in ("gut", "gdunit4"):
            why = self.tests_plan_check(gop, keys, root)
            if why:
                return error_result(why)
        if planning or (name, op) not in READ_ALLOW:
            if gname == "export_game":
                if not root:
                    return error_result("game-editor: export_game needs the Godot editor open on the project. "
                                        "Nothing was exported.")
                parsed, err = parse_export(keys)
                if parsed is None:
                    return error_result(err)
                dest, dest_err = output_path(Path(root), parsed["output"])
                if dest is None:
                    return error_result(dest_err)
                refusal = neighbor_refusal(dest, parsed["overwrite"])
                if refusal:
                    return error_result(refusal)
                if planning:
                    pre = self._export_preflight(parsed)
                    if "error" in pre:
                        return pre["error"]
            shown = dict(keys)
            if scene and "scene" not in shown and gname in ("save_scene", "batch_execute", "revert_scene"):
                shown["scene"] = scene
            if planning:
                losses = self.step_losses(name, op, gname, gop, keys, scene, root, overwrite)
                notes = self.step_changes_text(gname, gop, keys, scene, root)
                side = side_effect_sentence(spec, keys)
                if side:
                    notes.append(side)
                label = {e for e in effects if e in ("write", "send", "dynamic")}
                if effects - {"run_code"} == {"read"} and not losses:
                    label = {"read"}
                if any(line.startswith(("deletes", "replaces", "clears", "removes", "drops")) for line in losses):
                    label.add("delete")
                if any(line.startswith("runs") for line in losses):
                    label.add("run_code")
                if any(line.startswith("discards") for line in losses):
                    label.add("discard")
                planned_args = dict(arguments) if isinstance(arguments, dict) else {}
                planned_args.update(step_changes)
                if overwrite:
                    planned_args["overwrite"] = True
                raise _Planned(plan_sentence(name, op, shown, label, gname, keys, save_note, root, None, losses,
                                             notes), planned_args, losses)
        if spec["upstream"] == "@local":
            if gname == "play_session":
                return self.play_session(keys, root, mid)
            return self.export_game(keys, root)
        if spec["upstream"] == "@cli":
            return self.run_cli_tests(spec["op"], keys, root)
        if gname == "revert_scene":
            keys.setdefault("path", scene)
            if not keys.get("path"):
                return error_result("game-editor: scene_do op revert_scene found no open scene file. Nothing was done.")
        keys.update(spec.get("fixed") or {})
        if spec["op"] is None:
            up_args = dict(keys)
        else:
            up_args = {"op": spec["op"], "params": keys}
        if session_id:
            up_args["session_id"] = session_id
        timeout = TEST_TIMEOUT if gname == "run_tests" else CALL_TIMEOUT
        res = self.upstream(spec["upstream"], up_args, timeout, mid)
        if res.get("bridge_error") == "timeout":
            return error_result(f"game-editor: {name} did not answer within {int(timeout)} seconds. The editor may "
                                f"still be working; check editor_look before trying again.")
        if res.get("bridge_error") == "exited":
            child = self.child
            head = child.stderr_text() if child else ""
            return error_result(head or f"game-editor: Godot AI exited during {name}. The call may or may not "
                                        f"have run.")
        if not res["isError"] and root is not None:
            self.after(gname, gop, keys, res, scene, root)
        if gname in ("create_script", "edit_script") and not res.get("isError"):
            self._rewrite_script_result(keys, res)
        if (gname in ("create_script", "edit_script", "write_file") and not res.get("isError") and root is not None
                and isinstance(keys.get("path"), str) and keys["path"].strip().lower().endswith((".gd", ".cs"))):
            target = keys["path"].strip()
            with self.state_lock:
                self.written_scripts[target] = file_sha(self.disk_path(target, root))
        shot_note = ""
        if gname == "screenshot" and _viewport_not_3d(keys, res):
            res, shot_note = self._retry_screenshot_2d(keys, res, mid)
        if gname == "read_logs" and not res.get("isError"):
            self._rewrite_editor_log(keys, res)
        if gname == "run_game" and not res.get("isError") and not _session_live(res):
            return self._run_game_not_live(res)
        out = limit_result(res["content"], res["isError"])
        if res["isError"] and not writes and "Transport failed after dispatch" in json.dumps(res["content"]):
            out["content"] = list(out["content"]) + [{"type": "text", "text": (
                "game-editor note: this was a read, so nothing changed. The answer was probably too large (about 1 MB "
                "or more); ask for less (a smaller limit, fields or max_resolution, or a smaller file).")}]
        note = "" if res["isError"] else zero_result_note(gname, gop, keys, res["content"])
        if gname == "run_tests" and gop == "godot_ai" and not res["isError"]:
            data = _first_json(res)
            total, failed = data.get("total"), data.get("failed", 0)
            if isinstance(total, int):
                note = (f"game-editor note: tests_run={total}, failed={failed}, passed="
                        f"{'true' if total >= 1 and failed == 0 else 'false'}.")
            else:
                note = ("game-editor note: the test reply did not give a test count, so passed is false. "
                        "A success line from the tool is not the result.")
            if "passed=false" in note or "passed is false" in note:
                out["isError"] = True
        if gname == "run_tests" and "cannot instantiate" in json.dumps(res["content"]):
            note = ("game-editor note: a script in res://tests could not be loaded. Godot AI test suites run inside "
                    "the editor, so they must start with @tool and extend McpTestSuite, and they cannot call the "
                    "game's own (non-@tool) scripts. Test game logic with GUT or gdUnit4 (op gut or gdunit4).")
        if gname == "run_game" and not res["isError"] and keys.get("autosave") is False and _session_live(res):
            note = ("game-editor note: autosave was false, so the game ran the scene files on disk; unsaved editor "
                    "changes are not in this run. scene_do op save first, or pass autosave: true.")
        if gname == "read_file" and not res.get("isError"):
            path = keys.get("path") if isinstance(keys.get("path"), str) else ""
            if path.replace("\\", "/").rstrip("/").endswith("export_presets.cfg"):
                _redact_preset_keys(res)
        if gname == "set_node_property" and not res["isError"] and keys.get("property") == "position":
            control = self._control_position_note(keys, scene, root)
            if control:
                note = (note + " " + control).strip() if note else control
        if gname == "open_scene" and not res["isError"] and keys.get("force_reload") is True:
            done = ("game-editor did: scene_do op open_scene force_reload discarded unsaved edits and read the file from disk.")
            note = (note + " " + done).strip() if note else done
        if shot_note:
            note = (note + " " + shot_note).strip() if note else shot_note
        if save_note and not res["isError"]:
            note = (note + " " + save_note).strip() if note else save_note
        if note:
            out["content"] = list(out["content"]) + [{"type": "text", "text": note}]
        if gname in DID_TOOLS and not out["isError"]:
            out["content"] = [{"type": "text", "text": _did_line(gname, gop, keys)}] + list(out["content"])
        if not out["isError"] and tool["tier"] == "read" and any(
                isinstance(value, str) and value.strip()[:7].lower() == "user://" for value in paths):
            out["content"] = [{"type": "text", "text":
                               "game-editor note: this reply is player data from user://, outside the project."}] + list(
                out["content"])
        return out

    def _script_source(self, res_path: str, root: str) -> tuple[str | None, str, str]:
        """(source, language, resolved res path) of a script: a res:// or uid:// file, an embedded
        "<scene>#sub:<id>", or a file a step of this plan writes. language is "gd", "cs" or "" (unknown)."""
        virt = self._virtual()
        if res_path == "?":
            return None, "", res_path
        if virt is not None and res_path in virt.get("files", {}):
            return virt["files"][res_path], ("cs" if res_path.lower().endswith(".cs") else "gd"), res_path
        if "#sub:" in res_path:
            scene_ref, sid = res_path.split("#sub:", 1)
            parsed = self.read_scene(scene_ref, root) if scene_ref else None
            sub = ((parsed or {}).get("subs") or {}).get(sid)
            raw = ((sub or {}).get("props") or {}).get("script/source")
            if not isinstance(raw, str):
                return None, "", res_path
            value = parse_value(raw, {})
            source = value if isinstance(value, str) else None
            kind = "cs" if (sub or {}).get("type") == "CSharpScript" else "gd"
            return source, kind, res_path
        if res_path.startswith("uid://"):
            resolved = self.resolve_uid(res_path, root)
            if not resolved:
                return None, "", res_path
            res_path = resolved
        lower = res_path.lower()
        if not lower.endswith((".gd", ".cs")):
            return None, "", res_path
        try:
            raw_bytes = self.disk_path(res_path, root).read_bytes()
        except (OSError, ValueError):
            return None, "", res_path
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None, "", res_path
        return text, ("cs" if lower.endswith(".cs") else "gd"), res_path

    def _class_scripts(self, root: str) -> dict[str, str]:
        """class_name -> res:// script, from the project's .gd files (hidden folders skipped), cached by mtime."""
        base = Path(root)
        stamp = []
        found: dict[str, str] = {}
        cache = getattr(self, "_class_cache", None)
        if cache and cache[0][0] == root and time.time() - getattr(self, "_class_cache_at", 0.0) < 2.0:
            return cache[1]  # one request reads many scripts; the folder is walked once per two seconds
        files = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.endswith(".gd"):
                    path = Path(dirpath) / name
                    try:
                        stamp.append((str(path), path.stat().st_mtime_ns))
                    except OSError:
                        continue
                    files.append(path)
                if len(files) > 20000:
                    break
        key = (root, tuple(sorted(stamp)))
        if cache and cache[0] == key:
            self._class_cache_at = time.time()
            return cache[1]
        for path in files:
            try:
                head = path.read_bytes()[:65536].decode("utf-8", errors="ignore")
            except OSError:
                continue
            if "class_name" not in head:
                continue
            info = gdscript_header(head)
            if info and info.get("class_name"):
                rel = path.relative_to(base).as_posix()
                found.setdefault(info["class_name"], "res://" + rel)
        self._class_cache = (key, found)
        self._class_cache_at = time.time()
        return found

    def _script_info(self, res_path: str, root: str, depth: int = 0) -> dict | None:
        """{"tool": the script runs in the editor, "hooks": underscore functions it has, its bases' included,
        "chain": the scripts read}. None when this plugin cannot tell: treated as a @tool script with every hook.

        Godot's rule, measured on 4.7.2: a script runs in the editor only when its own file has @tool; a script
        that extends a @tool script without @tool of its own does not. A @tool script does run the functions it
        inherits, so _get, _get_property_list and the rest are collected along the extends chain (a res:// or
        relative path, a uid://, or a class_name found in the project's .gd files). A base this plugin cannot
        find leaves the hooks unknown.
        """
        if depth > 16:
            return None
        source, kind, resolved = self._script_source(res_path, root)
        if source is None:
            return None
        if kind == "cs":
            tool = csharp_is_tool(source)
            hooks = set()
            for name, hook in (("_Get", "_get"), ("_GetPropertyList", "_get_property_list"),
                               ("_ValidateProperty", "_validate_property"),
                               ("_PropertyGetRevert", "_property_get_revert"),
                               ("_PropertyCanRevert", "_property_can_revert"), ("_Ready", "_ready"),
                               ("_EnterTree", "_enter_tree"), ("_Process", "_process")):
                if re.search(r"\boverride\b[^;{]*\b" + name + r"\s*\(", source):
                    hooks.add(hook)
            if re.search(r"\bget\s*(?:\{|=>)|=>", _cs_code(source)):
                hooks.add("@getter")  # a property getter body runs on a read
            base = re.search(r"\bclass\s+\w+\s*:\s*([\w.]+)", _cs_code(source))
            base_name = base.group(1).rsplit(".", 1)[-1] if base else ""
            if tool and base_name and base_name not in _CLASS_PARENTS:
                return {"tool": True, "hooks": None, "chain": [resolved]}
            return {"tool": tool, "hooks": hooks, "chain": [resolved]}
        info = gdscript_header(source)
        if info is None:
            return None
        hooks: set[str] | None = {name for name in info["funcs"] if name.startswith(("_", "@"))}
        chain = [resolved]
        target = info.get("extends")
        base_ref = None
        if isinstance(target, str) and target:
            if target.startswith(("res://", "uid://")):
                base_ref = target
            elif target.endswith((".gd", ".cs")) or "/" in target:
                # Relative to the script's own folder, as Godot resolves it.
                folder = resolved[:resolved.rfind("/") + 1] if resolved.startswith("res://") else "res://"
                joined = posixpath.normpath(folder[len("res://"):] + target) if folder.startswith("res://") else ""
                base_ref = "res://" + joined.lstrip("./") if joined and not joined.startswith("..") else "?"
            else:
                head = target.split(".", 1)[0]
                if head not in _CLASS_PARENTS:
                    base_ref = self._class_scripts(root).get(head) or "?"
        if base_ref is not None:
            base_info = self._script_info(base_ref, root, depth + 1)
            if base_info is None or base_info.get("hooks") is None:
                hooks = None
            else:
                hooks |= base_info["hooks"]
            if base_info is not None:
                chain += base_info.get("chain") or []
        return {"tool": bool(info["tool"]), "hooks": hooks, "chain": chain}

    def _script_is_tool(self, res_path: str, root: str) -> bool:
        """True when a script would run in the editor, or when this plugin cannot tell (fail closed).

        GDScript: Godot's own header rule (gdscript_header): @tool among the leading annotations, after a BOM,
        a doc string, another annotation on the same line or a comment; a @tool anywhere else also counts. C#:
        a [Tool] attribute. A script embedded in a scene is read from that scene. A uid:// that does not
        resolve, a script that cannot be read or decoded, a type other than .gd or .cs, and a script value that
        could not be parsed ("?") count as @tool. An unsaved editor buffer is not on disk, so it is not visible.
        """
        info = self._script_info(res_path, root)
        return info is None or bool(info["tool"])

    def _node_get_hooked(self, path: str, scene: str, root: str) -> bool:
        """True when reading this node's values calls its @tool script (_get, _get_property_list,
        _validate_property, ...), or the script cannot be read. A @tool script without those hooks is not
        called by Object.get or get_property_list, so its values can be read and compared."""
        found = self._node_tool(path, scene, root)
        return bool(found) and (found[1] is None or bool(found[1] & GET_HOOKS))

    def _node_runs_tool(self, path: str, scene: str, root: str) -> bool:
        script = self._node_script_res(path, scene, root)
        return bool(script and self._script_is_tool(script, root))

    def _script_tool_hooks(self, res_path: str, root: str) -> set[str] | None:
        """The hooks a @tool script has, its bases' included; an empty set when the script is not @tool; None when
        this plugin cannot read it or a base of it (treated as a @tool script that defines every hook)."""
        info = self._script_info(res_path, root)
        if info is None:
            return None
        if not info["tool"]:
            return set()
        return None if info["hooks"] is None else set(info["hooks"])

    def _node_tool(self, path: str, scene: str, root: str) -> tuple[str, set[str] | None] | None:
        """(script, hooks) when the node at path has a @tool script, else None."""
        script = self._node_script_res(path, scene, root)
        if not script:
            return None
        hooks = self._script_tool_hooks(script, root)
        if hooks is not None and not hooks and not self._script_is_tool(script, root):
            return None
        return script, hooks

    def scene_tool_nodes(self, scene: str, root: str) -> list[tuple[str, str, set[str] | None]]:
        """Every node in the scene file (instanced scenes included) whose script is @tool:
        (absolute path, script, hooks or None when unreadable). From the files on disk."""
        out: list[tuple[str, str, set[str] | None]] = []
        parsed = self.read_scene(scene, root) if scene else None
        if not parsed:
            return out
        root_name = parsed.get("root") or ""

        def visit(scene_ref: str, prefix: str, depth: int, uid: str | None = None) -> None:
            inner = self._instanced(scene_ref, uid, root) if depth > 0 else self.read_scene(scene_ref, root)
            if depth > 8 or (not inner and depth > 0):
                out.append((prefix, "?", None))  # an instanced scene this plugin cannot read: unknown scripts
                return
            if not inner:
                return
            if not inner.get("root") and not (self._virtual() or {}).get("scenes", {}).get(scene_ref):
                out.append((prefix, "?", None))  # a binary .scn: unknown scripts
                return
            ext = inner.get("ext") or {}
            if inner.get("root_scene"):
                # An inherited scene: the base scene's root script and nodes are this scene's too.
                visit(str(inner["root_scene"]), prefix, depth + 1, inner.get("root_scene_uid"))
            rows = [("", inner.get("root_props") or {}, {"local": True})]
            rows += [(rel, info.get("props") or {}, info) for rel, info in (inner.get("nodes") or {}).items()]
            for rel, props, info in rows:
                script = self._script_from_props(props, ext, scene_ref)
                shown = prefix if not rel else f"{prefix}/{rel}"
                if script:
                    hooks = self._script_tool_hooks(script, root)
                    if hooks is None or hooks or self._script_is_tool(script, root):
                        out.append((shown, script, hooks))
                if info.get("instance") and info.get("scene"):
                    visit(str(info["scene"]), shown, depth + 1, info.get("scene_uid"))

        visit(scene, f"/{root_name}", 0)
        return out

    def scene_get_nodes(self, scene: str, root: str) -> list[tuple[str, str]]:
        """Nodes whose @tool script defines _get (or cannot be read): a name lookup runs that _get."""
        return [(path, script) for path, script, hooks in self.scene_tool_nodes(scene, root)
                if hooks is None or "_get" in hooks]

    # ---- what a plan step loses: deletes, overwrites, discards, code it runs (risky ones need confirm_risky_plan) ----
    def _tools_line(self, scene_ref: str, root: str) -> str:
        found = self.scene_tool_nodes(scene_ref, root)
        if not found:
            return ""
        listed = ", ".join(f"{path} ({script})" for path, script, _hooks in found[:4])
        return listed + (" and more" if len(found) > 4 else "")

    def step_changes_text(self, gname: str, gop: str, keys: dict, scene: str, root: str | None) -> list[str]:
        """The value a step replaces, next to the new one (old -> new), read while the plan is made without running
        a @tool script: a node property, an audio player's playback values, a project setting, or the lines of a
        file an overwrite replaces (for an .import file, each setting that changes)."""
        out: list[str] = []
        if root is None:
            return out
        try:
            if gname == "set_node_property" and isinstance(keys.get("path"), str) and isinstance(keys.get("property"), str):
                now = self._plan_node_values(keys["path"], [keys["property"]], scene, root)
                if now is not None and keys["property"] in now:
                    out.append(f"now: {keys['property']} = {json.dumps(now[keys['property']], ensure_ascii=False)[:120]} "
                               f"-> {json.dumps(keys.get('value'), ensure_ascii=False)[:120]}")
            elif gname == "edit_level" and gop == "audio_set_playback" and isinstance(keys.get("player_path"), str):
                fields = [k for k in ("bus", "volume_db", "pitch_scale", "autoplay") if k in keys]
                now = self._plan_node_values(keys["player_path"], fields, scene, root) if fields else None
                if now:
                    out.append("now: " + ", ".join(f"{k} = {json.dumps(now.get(k))} -> {json.dumps(keys.get(k))}"
                                                   for k in fields if k in now))
            elif gname == "edit_project_settings" and gop == "set" and isinstance(keys.get("key"), str):
                old = project_setting(Path(root), keys["key"])
                out.append(f"now in project.godot: {keys['key']} = {old if old is not None else '(not set; Godot default)'} "
                           f"-> {json.dumps(keys.get('value'), ensure_ascii=False)[:120]}")
            elif gname in ("write_file", "create_script") and isinstance(keys.get("path"), str):
                res = keys["path"].strip()
                virt = self._virtual()
                before = None
                if virt is not None and res in virt.get("files", {}):
                    before = virt["files"][res]
                elif res.lower().startswith("res://") and self.disk_path(res, root).is_file():
                    before = self.disk_path(res, root).read_text(encoding="utf-8", errors="replace")
                if before is not None:
                    out.append(describe_replacement(res, before, str(keys.get("content") or "")))
                if res.lower().endswith((".gd", ".cs")):
                    out.append(f"also writes {res}.uid beside the script")
            elif gname == "edit_script" and isinstance(keys.get("path"), str) and keys["path"].strip().lower().endswith(
                    (".gd", ".cs")):
                out.append(f"also writes {keys['path'].strip()}.uid beside the script")
            elif gname == "move_file":
                names = move_sidecars(gname, gop, keys)
                if names:
                    out.append("also moves " + ", ".join(names))
        except (OSError, ValueError, Refused):
            return out
        return [line for line in out if line]

    def _plan_node_values(self, path: str, fields: list[str], scene: str, root: str) -> dict | None:
        """Values to show in a plan. The file, or a value this session already set. The editor is not asked."""
        if not scene or not fields or "://" in path:
            return None
        virt = self._virtual()
        if virt is not None and virt.get("scene") not in (None, scene):
            return None
        parsed = self.read_scene(scene, root) or {}
        rel = rel_node_path(path, parsed.get("root"))
        if virt is not None and any(rel == r or rel.startswith(r + "/") for r in
                                    (virt.get("local", {}).get(scene, set()) | virt.get("inst", {}).get(scene, set()))):
            return None
        edits = self.edited_props.get(scene) or {}
        if rel == "":
            props = parsed.get("root_props") or {}
        else:
            props = (parsed.get("nodes") or {}).get(rel, {}).get("props") or {}
        ext = parsed.get("ext") or {}
        out: dict = {}
        for field in fields:
            record = edits.get((rel, field))
            if isinstance(record, dict) and "value" in record:
                out[field] = record["value"]
                continue
            raw = props.get(field) if isinstance(props, dict) else None
            if not isinstance(raw, str):
                continue
            value = parse_value(raw, ext)
            if value is None or value is _NULL or isinstance(value, tuple):
                continue
            out[field] = value
        return out or None

    def _disk_field_text(self, parsed: dict, rel: str, prop: str) -> str:
        if rel == "":
            props = parsed.get("root_props") or {}
        else:
            props = (parsed.get("nodes") or {}).get(rel, {}).get("props") or {}
        raw = props.get(prop) if isinstance(props, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            return "(not in the file)"
        return raw.strip()[:80]

    def _discard_targets(self, plan: dict) -> list[tuple[str, str]]:
        """(kind, scene) for quit, revert and force_reload. scene is "" for quit."""
        out: list[tuple[str, str]] = []
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            gname, _gop, _spec = self._step_spec(step)
            if gname == "editor_control" and step.get("op") == "quit":
                out.append(("quit", ""))
            elif gname == "revert_scene":
                target = step.get("path") if isinstance(step.get("path"), str) else ""
                out.append(("revert", target if target.startswith("res://") else ""))
            elif gname == "open_scene" and step.get("force_reload") is True:
                target = step.get("path") if isinstance(step.get("path"), str) else ""
                out.append(("reload", target if target.startswith("res://") else ""))
        return out

    def _open_scenes_named(self, allow_other_scenes: bool = False) -> tuple[list[str] | None, str, str]:
        """(open res:// scenes, the scene whose tree a compare reads, why).

        why is set when the list is not complete. An empty list with no why means nothing is open.
        A scene that is open and is not the one whose tree can be read cannot be named, so quit does not run.
        revert of the scene whose tree can be read still runs when another scene is open.
        """
        res = self.upstream("scene_manage", {"op": "get_roots"})
        if res.get("isError") or "bridge_error" in res:
            return None, "", (f"the open scenes could not be listed ({_first_line(res)}), so what would be "
                              "discarded cannot be named.")
        body = _first_json(res)
        raw = body.get("scenes") if isinstance(body, dict) else None
        if raw is None and isinstance(body, dict):
            raw = body.get("roots")
        if not isinstance(raw, list):
            return None, "", "the open scenes were not a list, so what would be discarded cannot be named."
        state = self.upstream("editor_state", {})
        if state.get("isError") or "bridge_error" in state:
            return None, "", (f"the open scene could not be read ({_first_line(state)}), so what would be "
                              "discarded cannot be named.")
        current = _first_json(state).get("current_scene")
        current = current.strip() if isinstance(current, str) else ""
        if current and not current.startswith("res://"):
            return None, "", (f"the open scene {current!r} is not a res:// file, so what would be discarded "
                              "cannot be named.")
        out: list[str] = []
        for item in raw:
            other = item.get("path") if isinstance(item, dict) else item
            if not isinstance(other, str) or not other.strip():
                return None, "", "an open scene had no path, so what would be discarded cannot be named."
            other = other.strip()
            if not other.startswith("res://"):
                return None, "", (f"open scene {other!r} is not a res:// file, so what would be discarded "
                                  "cannot be named.")
            if other not in out:
                out.append(other)
        if current and current not in out:
            out.append(current)
        others = [one for one in out if one != current]
        if others and not allow_other_scenes:
            shown = ", ".join(others[:4])
            return None, "", (f"{shown} is open, and the editor tree that can be read is "
                              f"{current or 'none'}, so what would be discarded in that other scene cannot be named.")
        return out, current, ""

    def _file_instance_defines(self, rel: str, nodes: dict, root: str) -> bool | None:
        """True when an instanced scene this file already has defines rel.

        False when rel is not under such an instance (the caller names it). None when that scene cannot be read.
        """
        if not rel or not _under_instance(rel, nodes):
            return False
        parts = rel.split("/")
        inst_rel = ""
        inst_scene = ""
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            info = nodes.get(prefix)
            if info and info.get("instance"):
                inst_rel = prefix
                inst_scene = str(info.get("scene") or "")
                break
        if not inst_rel:
            return False
        return self._scene_defines(inst_scene, rel[len(inst_rel) + 1:], root, 1)

    def _compare_discards(self, scene: str, root: str) -> tuple[str, list[str]]:
        """("", diffs) when every editor-only node, session node and unsaved value is named.

        A scene file that is missing, a compare that did not finish, or a node this session added that
        the editor tree did not list is a reason, and the caller does not run.
        """
        if not isinstance(scene, str) or not scene.startswith("res://"):
            return "the scene to discard is not a res:// path, so what would be discarded cannot be named.", []
        try:
            exists = self.disk_path(scene, root).is_file()
        except (OSError, ValueError):
            exists = False
        if not exists:
            return f"{scene} is not a file on disk, so what would be discarded cannot be named.", []
        self._local.compare_runs_code = True
        try:
            compared, diffs, why, _skipped = self.compare(scene, root, None)
        finally:
            self._local.compare_runs_code = False
        if not compared:
            detail = why or "the compare did not finish"
            return f"{scene} could not be compared ({detail}), so what would be discarded cannot be named.", []
        diffs = list(diffs)
        nodes, why_tree = self._hierarchy()
        if nodes is None:
            return f"{scene} could not be compared ({why_tree}), so what would be discarded cannot be named.", []
        parsed = self.read_scene(scene, root) or {}
        root_name = parsed.get("root") or ""
        file_nodes = parsed.get("nodes") or {}
        editor = {rel_node_path(str(node.get("path", "")), root_name) for node in nodes}
        file_rels = set(file_nodes)
        # compare already names a node the file does not have, except one under an instance: that path is
        # a second copy or it is skipped. A child the instanced scene defines is not unsaved work. A child
        # it does not define (a person added it, or this session did) is, and an unreadable scene is not a guess.
        extra: list[str] = []
        for rel in sorted(editor):
            if not rel or rel in file_rels:
                continue
            abs_path = f"/{root_name}/{rel}" if root_name else f"/{rel}"
            if any(abs_path in line for line in diffs):
                continue
            defined = self._file_instance_defines(rel, file_nodes, root)
            if defined is None:
                return (f"the instanced scene behind {abs_path} could not be read, so what would be discarded "
                        "cannot be named.", [])
            if defined:
                continue
            extra.append(f"in editor only: {abs_path}")
        if extra:
            diffs.extend(extra)
            self._name_editor_only_scripts(diffs)
        made = set(self.created_local.get(scene) or ()) | set(self.created_inst.get(scene) or ())
        missing: list[str] = []
        for rel in sorted(made):
            if not rel or rel in file_rels or rel in editor:
                continue
            abs_path = f"/{root_name}/{rel}" if root_name else f"/{rel}"
            missing.append(abs_path)
        if missing:
            shown = ", ".join(missing[:6])
            return (f"nodes this session added are not in the editor tree ({shown}), so what would be discarded "
                    "cannot be named.", [])
        return "", diffs

    def _name_discards(self, plan: dict, root: str) -> tuple[str, list[str], dict[str, list[str]]]:
        """(why, lines, {scene: diffs}) after the first approval, before anything is discarded.

        No discard step: ("", [], {}). A scene that is not open has no lines. The current scene is
        compared. edited_props is not added: a value the compare did not see is not a discard line.
        """
        targets = self._discard_targets(plan)
        if not targets:
            return "", [], {}
        # revert names the scene whose tree can be read. Another open scene is left as it is.
        # quit still needs every open scene, because it would discard all of them.
        allow_others = {kind for kind, _scene in targets} == {"revert"}
        open_scenes, current, why = self._open_scenes_named(allow_others)
        if why or open_scenes is None:
            return why or "the open scenes could not be named.", [], {}
        lines: list[str] = []
        by_scene: dict[str, list[str]] = {}
        for kind, scene in targets:
            if kind == "quit":
                wanted = list(open_scenes)
            else:
                scene = scene or current
                if not isinstance(scene, str) or not scene.startswith("res://"):
                    return "the scene to discard is not a res:// path, so what would be discarded cannot be named.", [], {}
                if scene not in open_scenes:
                    try:
                        exists = self.disk_path(scene, root).is_file()
                    except (OSError, ValueError):
                        exists = False
                    if not exists:
                        return f"{scene} is not a file on disk, so what would be discarded cannot be named.", [], {}
                    by_scene.setdefault(scene, [])
                    continue
                if scene != current:
                    return (f"{scene} is open, and the editor tree that can be read is {current or 'none'}, "
                            f"so what would be discarded in {scene} cannot be named.", [], {})
                wanted = [scene]
            for one in wanted:
                if one not in by_scene:
                    why_one, diffs = self._compare_discards(one, root)
                    if why_one:
                        return why_one, [], {}
                    by_scene[one] = diffs
                for diff in by_scene[one]:
                    line = f"discards {one}: {diff}"
                    if line not in lines:
                        lines.append(line)
        return "", lines, by_scene

    def _scene_discard_lines(self, scene: str, root: str) -> list[str]:
        """A placeholder. apply_plan replaces it with a full compare, or does not run.

        edited_props is not a list: it names one property this session recorded and drops an editor-only
        node, a node this session added, and an edit the person made in the editor.
        """
        del root
        where = scene if isinstance(scene, str) and scene.startswith("res://") else "the open scene"
        return [discard_placeholder(where)]

    def _whole_file_replacements(self, name: str, op: str, gname: str, gop: str, keys: dict, root: str,
                                 already: list) -> list[str]:
        """A replace line for any whole-file write onto a path the plan named that already exists.

        file_create is handled by the caller. This covers a resource save (theme, material, environment,
        any other saves_resource) and a move's destination. It does not look at the operation's name.
        A path the step only reads (a prefab's scene_path, a shader_path) is not a destination.
        """
        spec = (TOOLS.get(name, {}).get("ops", {}).get(op, {}) or {})
        side = upstream_side_effects(spec)
        destinations: list[str] = []
        if "saves_resource" in side:
            for key in ("path", "resource_path", "output", "save_to", "file_path"):
                if key in already:
                    continue
                value = keys.get(key)
                if isinstance(value, str) and value.strip().lower().startswith("res://"):
                    destinations.append(value.strip())
        if "moves_file" in side:
            dest = ""
            if gop == "rename":
                src = keys.get("path") if isinstance(keys.get("path"), str) else ""
                new_name = keys.get("new_name") if isinstance(keys.get("new_name"), str) else ""
                if src.lower().startswith("res://") and new_name and "/" not in new_name and "\\" not in new_name:
                    dest = src.rsplit("/", 1)[0] + "/" + new_name
            elif isinstance(keys.get("new_path"), str):
                dest = keys["new_path"]
            if dest.strip().lower().startswith("res://"):
                destinations.append(dest.strip())
        out: list[str] = []
        seen: set[str] = set()
        has_overwrite = "overwrite" in (spec.get("keys") or [])
        for path in destinations:
            if path in seen:
                continue
            seen.add(path)
            try:
                exists = self.disk_path(path, root).is_file()
            except (OSError, ValueError):
                exists = False
            if not exists:
                continue
            if has_overwrite and keys.get("overwrite") is not True:
                raise Refused(
                    f"game-editor: plan_change made no plan: {path} already exists. Pass overwrite: true to replace "
                    f"it. The plan would then show the file on disk and what this step writes, and ask a second time. "
                    f"Nothing was sent to the Godot editor.")
            try:
                before = self.disk_path(path, root).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise Refused(
                    f"game-editor: plan_change made no plan: {path} is not UTF-8 text, so the plan cannot name both "
                    f"the file on disk and the new file. Nothing was sent to the Godot editor.")
            except OSError as exc:
                raise Refused(
                    f"game-editor: plan_change made no plan: {path} could not be read ({type(exc).__name__}), so the "
                    f"overwrite cannot name both sides. Nothing was sent to the Godot editor.")
            described = {key: value for key, value in keys.items() if key not in ("op", "session_id", "overwrite")}
            if "moves_file" in side:
                after = "the file moved here from " + str(keys.get("path") or "")
            else:
                after = ("a new resource this step saves over this file (Godot writes the resource; the file that "
                         "is here is dropped). Arguments: " + json.dumps(described, ensure_ascii=False))
            out.append("replaces the existing file " + path + ": disk "
                       + json.dumps(before, ensure_ascii=False) + " -> " + json.dumps(after, ensure_ascii=False))
        return out

    def step_losses(self, name: str, op: str, gname: str, gop: str, keys: dict, scene: str,
                    root: str | None, overwrite: bool) -> list[str]:
        """Lines for what this step deletes or replaces, the project code it runs, the unsaved work it
        discards, and anything it writes outside the project. Empty for an ordinary edit."""
        out: list[str] = []
        if root is None:
            return out
        target = keys.get("path") if isinstance(keys.get("path"), str) else ""
        # deletes and replaces
        if gname == "delete_node":
            out.append(f"deletes node {target}")
        elif gname in ("delete_file", "delete_file_permanently"):
            out.append(f"deletes file {target}" + (" (not to the trash)" if gname == "delete_file_permanently" else ""))
        elif gname == "delete_asset":
            if gop == "animation":
                out.append(f"deletes animation {keys.get('animation_name')} from {keys.get('player_path')}")
            elif gop == "tilemap":
                out.append(f"clears every tile on {target}")
            elif gop == "gridmap":
                out.append(f"clears every cell of {target}")
        elif gname == "edit_project_settings" and gop in ("autoload_remove", "input_remove_action"):
            out.append(f"removes {'autoload' if gop == 'autoload_remove' else 'input action'} "
                       f"{keys.get('name') or keys.get('action')}")
        elif gname == "batch_execute":
            for item in keys.get("commands") or []:
                if not isinstance(item, dict):
                    continue
                params = item.get("params") if isinstance(item.get("params"), dict) else {}
                command = str(item.get("command") or "")
                if command == "delete_node":
                    out.append(f"deletes node {params.get('path')} (batch)")
                elif command in ("tilemap_clear", "gridmap_clear"):
                    out.append(f"clears {params.get('path')} (batch)")
                elif command in ("remove_input_action", "remove_autoload", "animation_delete"):
                    out.append(f"removes {params.get('name') or params.get('action') or params.get('animation_name')} (batch)")
                for key in NODE_KEYS:
                    value = params.get(key)
                    if isinstance(value, str) and "://" not in value and scene:
                        found = self._node_tool(value, scene, root)
                        if found:
                            out.append(f"runs the @tool script {found[0]} of {value} (batch {command})")
                            break
        spec_create = (TOOLS.get(name, {}).get("ops", {}).get(op, {}) or {}).get("file_create") or []
        for key in spec_create:
            value = keys.get(key)
            if isinstance(value, str) and value.strip().lower().startswith("res://") and overwrite:
                try:
                    exists = self.disk_path(value.strip(), root).exists()
                except (OSError, ValueError):
                    exists = True
                if exists:
                    path = value.strip()
                    try:
                        before = self.disk_path(path, root).read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        raise Refused(
                            f"game-editor: plan_change made no plan: {path} is not UTF-8 text, so the plan cannot "
                            f"name both the file on disk and the new text. Nothing was sent to the Godot editor.")
                    except OSError as exc:
                        raise Refused(
                            f"game-editor: plan_change made no plan: {path} could not be read ({type(exc).__name__}), "
                            f"so the overwrite cannot name both sides. Nothing was sent to the Godot editor.")
                    content = keys.get("content")
                    after = content if isinstance(content, str) else None
                    if after is not None and path.endswith(".import") and only_params_differ(before, after):
                        continue  # import settings only; the asset itself is not touched
                    if after is not None and only_ext_paths_differ(before, after) and self._is_relink(
                            before, after, path, root):
                        continue  # a .tres relinked to the file beside it; every old path pointed at nothing
                    if after is not None and before.strip() and after.startswith(before.rstrip("\n")):
                        continue  # lines are added; nothing in the file is lost
                    if after is None:
                        raise Refused(
                            f"game-editor: plan_change made no plan: replacing {path} needs content, the whole new "
                            f"text, so the plan can show the file on disk and the file it would write. Nothing was "
                            f"sent to the Godot editor.")
                    out.append("replaces the existing file " + path + ": disk "
                               + json.dumps(before, ensure_ascii=False) + " -> "
                               + json.dumps(after, ensure_ascii=False))
        out.extend(self._whole_file_replacements(name, op, gname, gop, keys, root, list(spec_create)))
        if gname == "save_scene" and scene:
            hooked = [path for path, _script, hooks in self.scene_tool_nodes(scene, root)
                      if hooks is None or hooks & GET_HOOKS]
            if hooked:
                out.append(f"runs project code: Godot reads every property when it saves {scene}, and that calls "
                           f"the @tool _get or _get_property_list of {', '.join(hooked[:4])}")
                out.append(f"replaces values on disk that are not compared first: reading {', '.join(hooked[:4])} "
                           f"would run its @tool script, so a change made to those nodes in the file outside this "
                           f"editor would be overwritten")
        if gname == "save_scene" and gop == "save" and overwrite:
            known = getattr(self._local, "plan_diffs", None)
            current = known is not None and known.get("scene") == scene and self._virtual_scene_is(scene)
            if current and not known.get("unknown") and getattr(self._local, "planning", False):
                for diff in known.get("diffs") or []:
                    out.append(f"drops the disk side of {scene}: {diff}")
            elif current or (self._virtual() or {}).get("scene") is None:
                why = f" (this plan could not compare them: {known.get('why')})" if current and known.get("why") else ""
                out.append(f"drops whatever differs between the editor and {scene or 'the open scene'} on disk{why}; "
                           f"apply_plan compares them and lists each value before the second approval")
            else:
                out.append(f"drops whatever differs between the editor and {scene or 'that scene'} on disk; this plan "
                           f"opens or makes that scene first, so it is compared at its save step and the reply names "
                           f"what was dropped")
        if gname == "export_game" and keys.get("overwrite") is True:
            out.append("replaces existing build files beside the output")
        # unsaved work — a placeholder now. apply_plan lists every value before anything is discarded.
        if gname == "editor_control" and gop == "quit":
            out.append(discard_placeholder("every open scene"))
        if gname == "revert_scene" or (gname == "open_scene" and keys.get("force_reload") is True):
            target_scene = target if isinstance(target, str) and target.startswith("res://") else scene
            out.extend(self._scene_discard_lines(target_scene or "", root))
        # project code
        if gname in ("run_game", "play_session"):
            out.append("runs project code: plays the game in a separate Godot process")
            out.append("a headless editor sets editor/run/main_run_args to include --headless in memory only, "
                       "writes res://ge_headless_arm.gd and res://ge_headless_arm.tscn, opens that scene so its "
                       "@tool script can set the argument, closes the scene, then removes the files. "
                       "project.godot is not left changed. A windowed editor is not given --headless")
        elif gname == "run_game_code":
            out.append("runs code written in this plan: evaluates this code inside the running game: "
                       + json.dumps(str(keys.get("code") or ""), ensure_ascii=False))
        elif gname == "run_tests":
            out.append(f"runs project code: the project's {gop} tests")
        elif gname == "export_game":
            out.append("runs project code: a headless Godot opens the project to export it (editor plugins and "
                       "@tool scripts load)")
        elif gname == "editor_control" and gop == "reload_plugin":
            out.append("runs project code: the Godot AI add-on in res://addons/godot_ai starts again")
        elif gname in ("open_scene", "revert_scene") and (target or scene):
            line = self._tools_line(target or scene, root)
            if line:
                out.append(f"runs project code: opening {target or scene} runs its @tool scripts: {line}")
        elif gname == "add_node" and isinstance(keys.get("scene_path"), str):
            line = self._tools_line(keys["scene_path"].strip(), root)
            if line:
                out.append(f"runs project code: placing {keys['scene_path']} runs its @tool scripts: {line}")
        elif gname == "edit_node" and gop == "attach_script" and isinstance(keys.get("script_path"), str):
            if self._script_is_tool(keys["script_path"].strip(), root):
                out.append(f"runs project code: {keys['script_path']} is a @tool script and runs in the editor once attached")
        elif (gname in ("create_script", "edit_script") and gop != "shader") or (
                gname == "write_file" and isinstance(keys.get("path"), str)
                and keys["path"].strip().lower().endswith((".gd", ".cs"))):
            path = (keys.get("path") if isinstance(keys.get("path"), str) else "").strip()
            after = self._script_text_after("create_script" if gname == "write_file" else gname, keys, path, root)
            if after is None or (csharp_is_tool(after) if path.lower().endswith(".cs") else
                                 (gdscript_header(after) or {"tool": True})["tool"]):
                funcs = [name for name in ((gdscript_header(after or "") or {}).get("funcs") or []) if name != "@getter"]
                shape = (f" ({len((after or '').splitlines())} lines; functions: {', '.join(funcs[:8]) or 'none'})"
                         if after is not None else "")
                out.append(f"runs code written in this plan: {path} is a @tool script{shape}, and Godot runs it in "
                           f"the editor when it loads it")
        if gname not in ("delete_node", "batch_execute", "run_game", "play_session") and scene:
            for key in NODE_KEYS:
                value = keys.get(key)
                if isinstance(value, str) and value and "://" not in value:
                    found = self._node_tool(value, scene, root)
                    if found:
                        out.append(f"runs project code: Godot calls the @tool script {found[0]} of {value} for this step")
                        break
            access = op_node_access((TOOLS.get(name, {}).get("ops", {}) or {}).get(op, {}) or {})
            if access and access.get("walks"):
                hits = self.access_hits(access, None, scene, root)
                if hits:
                    out.append(f"runs project code: Godot AI reads the scene's nodes for this step, and that runs the "
                               f"@tool script of {', '.join(hits[:4])}" + (" and more" if len(hits) > 4 else ""))
        if gname == "edit_project_settings" and gop == "autoload_add" and isinstance(keys.get("path"), str):
            target_script = keys["path"].strip()
            if target_script.lower().endswith((".gd", ".cs")) and self._script_is_tool(target_script, root):
                out.append(f"runs project code: {target_script} is a @tool script; as an autoload Godot runs it in the "
                           f"editor from the next time the project opens, and in every game run")
            elif target_script.lower().endswith((".tscn", ".scn")) and self._tools_line(target_script, root):
                out.append(f"runs project code: the editor creates the autoload {target_script} right away, and its "
                           f"@tool scripts run: {self._tools_line(target_script, root)}")
        if "send" in spec_effects(TOOLS.get(name, {}).get("ops", {}).get(op, {})) and gname != "editor_control":
            out.append("sends data out of this computer")
        return list(dict.fromkeys(out))

    def _script_text_after(self, gname: str, keys: dict, path: str, root: str) -> str | None:
        """The script's text after this step: create_script's content, or edit_script's patch applied to the file
        (or to what an earlier step of this plan wrote). None when it cannot be known."""
        if gname == "create_script":
            return str(keys.get("content") or "")
        virt = self._virtual()
        if virt is not None and path in virt.get("files", {}):
            before = virt["files"][path]
        else:
            try:
                before = self.disk_path(path, root).read_text(encoding="utf-8")
            except (OSError, ValueError, UnicodeDecodeError):
                return None
        old_text, new_text = keys.get("old_text"), keys.get("new_text")
        if isinstance(old_text, str) and isinstance(new_text, str) and old_text and old_text in before:
            return before.replace(old_text, new_text, 1 if not keys.get("replace_all") else -1)
        if isinstance(keys.get("content"), str):
            return keys["content"]
        return before

    def _main_scene(self, root: str) -> str:
        virt = self._virtual()
        if virt is not None and virt.get("main_scene"):
            return virt["main_scene"]
        try:
            text = (Path(root) / "project.godot").read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
        match = re.search(r'(?m)^run/main_scene\s*=\s*"([^"]+)"', text)
        if not match:
            return ""
        value = match.group(1)
        return (self.resolve_uid(value, root) or "") if value.startswith("uid://") else value

    def play_watch_help(self, parsed: dict, root: str | None) -> tuple[list[str], str]:
        """When the goal compares score, find the nodes that hold it in the scene that will play (a script with
        `var score`, or a Label whose text has `score:`), and add the ones not watched yet. (added, note)."""
        if root is None or _score_condition(parsed["goal"]) is None:
            return [], ""
        if parsed["mode"] == "custom":
            scene = str(parsed.get("scene") or "")
        elif parsed["mode"] == "current":
            scene = self.current_scene()
        else:
            scene = self._main_scene(root)
        virt = self._virtual()
        if virt is not None and scene in virt.get("scenes", {}):
            return self._play_watch_virtual(parsed, scene, root, virt)
        parsed_scene = self.read_scene(scene, root) if scene.startswith("res://") else None
        if not parsed_scene or not parsed_scene.get("root"):
            what = f"{scene} could not be read" if scene else "the project has no main scene (run/main_scene)"
            return [], (f"game-editor note: the goal compares score, but {what}, so no score holder was added to "
                        f"watch. Watch the node that has `var score` or the score Label.")
        root_name = parsed_scene["root"]
        found: list[tuple[str, str]] = []
        rows = [("", parsed_scene.get("root_props") or {}, "")]
        rows += [(rel, info.get("props") or {}, info.get("type") or "") for rel, info in (parsed_scene.get("nodes") or {}).items()]
        for rel, props, _kind in rows:
            runtime = f"/root/{root_name}" + (f"/{rel}" if rel else "")
            text = props.get("text")
            if isinstance(text, str) and re.search(r"(?i)score\s*:", text):
                found.append((runtime, f"its text is {text.strip()[:40]}"))
                continue
            script = self._script_from_props(props, parsed_scene.get("ext") or {}, scene)
            if script and script.startswith("res://") and script.endswith(".gd"):
                try:
                    source = self.disk_path(script, root).read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    source = ""
                if re.search(r"(?m)^\s*(?:@\w+(?:\([^)]*\))?\s+)*var\s+score\b", source):
                    found.append((runtime, f"{script} has var score"))
        if not found:
            return [], (f"game-editor note: the goal compares score, but {scene} has no node with `var score` in its "
                        f"script and no Label whose text has `score:`. The score will read as missing.")
        watched = set(parsed["watch"])
        room = max(0, 8 - len(watched))
        added = [path for path, _why in found if path not in watched][:room]
        if not added:
            return [], ""
        reasons = "; ".join(f"{path} ({why})" for path, why in found if path in added)
        return added, f"game-editor note: added to watch because the goal compares score: {reasons}."

    @staticmethod
    def _has_score(source: str) -> bool:
        return re.search(r"(?m)^\s*(?:@\w+(?:\([^)]*\))?\s+)*var\s+score\b", source) is not None

    def _play_watch_virtual(self, parsed: dict, scene: str, root: str, virt: dict) -> tuple[list[str], str]:
        """The score holders of a scene this plan makes: its root and the nodes the plan adds, with the scripts the plan
        attaches (or writes) and the Label text it sets."""
        root_name = (virt["scenes"][scene] or {}).get("root") or ""
        rels = [""] + sorted(virt.get("local", {}).get(scene, set()) | virt.get("inst", {}).get(scene, set()))
        scripts = virt.get("scripts", {}).get(scene, {})
        texts = virt.get("texts", {}).get(scene, {})
        found: list[tuple[str, str]] = []
        for rel in rels:
            runtime = f"/root/{root_name}" + (f"/{rel}" if rel else "")
            text = texts.get(rel)
            if isinstance(text, str) and re.search(r"(?i)score\s*:", text):
                found.append((runtime, f"this plan sets its text to {text.strip()[:40]}"))
                continue
            script = scripts.get(rel)
            if isinstance(script, str) and script.endswith(".gd"):
                source = virt.get("files", {}).get(script)
                if source is None:
                    try:
                        source = self.disk_path(script, root).read_text(encoding="utf-8", errors="ignore")
                    except (OSError, ValueError):
                        source = ""
                if self._has_score(source):
                    found.append((runtime, f"{script} has var score"))
        if not found:
            return [], (f"game-editor note: the goal compares score and {scene} is made by this plan, but no node this "
                        f"plan adds has a script with `var score` or a Label whose text has `score:`. The score will "
                        f"read as missing.")
        watched = set(parsed["watch"])
        room = max(0, 8 - len(watched))
        added = [path for path, _why in found if path not in watched][:room]
        if not added:
            return [], ""
        reasons = "; ".join(f"{path} ({why})" for path, why in found if path in added)
        return added, f"game-editor note: added to watch because the goal compares score: {reasons}."

    def tests_plan_check(self, framework: str, keys: dict, root: str | None) -> str:
        """The run_cli_tests refusals, at plan time, so a plan never asks for a run that cannot start."""
        if root is None:
            return ""
        if not self.godot_binary():
            return ("game-editor: run_tests could not find the Godot binary (Godot.app or Godot_mono.app in "
                    "/Applications or ~/Applications, Steam, or godot on PATH). No plan was saved.")
        runner = "res://addons/gut/gut_cmdln.gd" if framework == "gut" else "res://addons/gdUnit4/bin/GdUnitCmdTool.gd"
        if not self.disk_path(runner, root).is_file():
            name = "GUT" if framework == "gut" else "gdUnit4"
            return (f"game-editor: {name} is not installed in this project ({runner[len('res://'):]} is missing), so "
                    f"there is nothing to run. Install {name} from the Godot Asset Library, then run "
                    f"`godot --headless --import` once. No plan was saved and nothing was run.")
        dirs = keys.get("dirs") or [d for d in ("res://test", "res://tests") if self.disk_path(d, root).is_dir()]
        if not dirs and not (framework == "gut" and (Path(root) / ".gutconfig.json").is_file()):
            return ("game-editor: there is no res://test or res://tests folder" +
                    (" and no .gutconfig.json" if framework == "gut" else "") +
                    "; pass dirs with the test folder. No plan was saved and nothing was run.")
        if framework == "gut" and not (Path(root) / ".godot" / "global_script_class_cache.cfg").is_file():
            return ("game-editor: GUT's class names have not been imported yet (.godot/global_script_class_cache.cfg "
                    "is missing). Run `godot --headless --import` in the project once. No plan was saved.")
        return ""

    def _node_known(self, path: str, scene: str, root: str) -> bool:
        """True when the node is in the scene file (its own nodes, or inside an instanced scene) or this session
        added it."""
        parsed = self.read_scene(scene, root) or {}
        rel = rel_node_path(path, parsed.get("root"))
        nodes = parsed.get("nodes") or {}
        if not rel or rel in nodes or rel in self._created(scene, False):
            return True
        if any(rel == made or rel.startswith(made + "/") for made in self._created(scene, True)):
            return True
        cur = rel
        while "/" in cur:
            cur = _parent(cur)
            info = nodes.get(cur)
            if info and info.get("instance"):
                return self._scene_defines(str(info.get("scene") or ""), rel[len(cur) + 1:], root, 1) is not False
        return False

    @staticmethod
    def odd_node_path(value: str) -> str:
        """Why a node path is not a plain path, or "". Godot resolves ".", "..", "%Name" and doubled slashes to
        a node, so a check made on the plain spelling could be skipped by another one."""
        if not isinstance(value, str) or "://" in value:
            return ""
        parts = value.strip().split("/")
        if value.strip().startswith("/"):
            parts = parts[1:]
        if "%" in value:
            return "it uses %UniqueName"
        if any(part in ("", ".", "..") for part in parts):
            return "it has an empty part, '.' or '..'"
        return ""

    def editor_rels(self, scene: str, root: str) -> set[str] | None:
        """Relative paths from the scene file plus nodes this plan added. The live tree is not read."""
        cached = getattr(self._local, "tree_rels", None)
        if isinstance(cached, dict) and scene in cached:
            return cached[scene]
        parsed = self.read_scene(scene, root) if scene else None
        if not parsed or not parsed.get("root"):
            rels = None
        else:
            rels = set(parsed.get("nodes") or {})
            virt = self._virtual()
            if virt is not None:
                rels |= set(virt.get("local", {}).get(scene, set()))
                rels |= set(virt.get("inst", {}).get(scene, set()))
            rels |= self._created(scene, False) | self._created(scene, True)
        if not isinstance(cached, dict):
            cached = {}
            self._local.tree_rels = cached
        cached[scene] = rels
        return rels

    def node_taken(self, scene: str, parent: str, name: str, root: str) -> bool:
        """True when parent/name already exists in the editor's scene, the scene file, or earlier in the plan."""
        parsed = self.read_scene(scene, root) or {}
        root_name = parsed.get("root")
        rel = rel_node_path(parent.rstrip("/") + "/" + name, root_name)
        if rel in (parsed.get("nodes") or {}) or rel in self._created(scene, False) or rel in self._created(scene, True):
            return True
        rels = self.editor_rels(scene, root) if scene in (self.current_scene(),) and not (
            self._virtual() or {}).get("scenes", {}).get(scene) else None
        return bool(rels and rel in rels)

    def access_hits(self, access: dict, target: str | None, scene: str, root: str) -> list[str]:
        """The @tool scripts a Godot AI handler runs when it reads nodes the way effects.json says it does (the
        same rule for reads, the tree shape, comparisons and plan steps): "/Path (res://script.gd ...)".

        A value read (Object.get, get_property_list) calls _get, _get_property_list, _validate_property and a
        property getter; a name read calls only _get. The hooks are collected along the script's extends chain.
        """
        hits: list[str] = []
        if "values" in access["reads"] and target:
            found = self._node_tool(target, scene, root)
            if found and (found[1] is None or found[1] & GET_HOOKS):
                hits.append(f"{target} ({found[0]})")
        if access["walks"] or not target:
            if "values" in access["reads"] and not target:
                hits += [f"{path} ({script})" for path, script, hooks in self.scene_tool_nodes(scene, root)
                         if hooks is None or hooks & GET_HOOKS]
            hits += [f"{path} ({script}, which defines _get)" for path, script in self.scene_get_nodes(scene, root)]
        elif "name" in access["reads"] and target:
            found = self._node_tool(target, scene, root)
            if found and (found[1] is None or "_get" in found[1]):
                hits.append(f"{target} ({found[0]}, which defines _get)")
        by_path: dict[str, str] = {}
        for hit in hits:
            key = hit.split(" (", 1)[0]
            if key not in by_path or len(hit) > len(by_path[key]):
                by_path[key] = hit
        return list(by_path.values())

    def read_tool_refusal(self, name: str, op: str, spec: dict, keys: dict, scene: str, root: str) -> str:
        """Why this read would run a @tool script in the editor, or "".

        The extracted node_access says what the Godot AI handler reads: a node's name (only a script _get
        runs on it) or its values (get, get_property_list: _get, _get_property_list and _validate_property
        run). A walk reads every node of the scene. A value read is refused for any @tool script on the
        node it reads; a name read only when the script defines _get.
        """
        access = op_node_access(spec)
        if not access:
            return ""
        if not scene:
            state = _first_json(self.upstream("editor_state", {}))
            if state.get("readiness") == "no_scene" or not state:
                return ""
            return (f"game-editor: {name} op {op} refused: the open scene has no file yet, so this plugin cannot see "
                    f"whether its nodes have @tool scripts that this read would run. Save the scene first (plan "
                    f"scene_do op save_as). Nothing was sent to the Godot editor.")
        target = next((keys[k] for k in ("node_path", "path", "camera_path", "player_path")
                       if isinstance(keys.get(k), str) and keys[k] and "://" not in keys[k]), None)
        if "values" in access["reads"] and target and not self._node_tool(target, scene, root) \
                and not self._node_known(target, scene, root):
            return (f"game-editor: {name} op {op} refused {target}: that node is not in {scene} on disk and this "
                    f"session did not add it, so its script cannot be checked before the read. Save the scene, "
                    f"or put {name} op {op} in plan_change as a step. Nothing was sent to the Godot editor.")
        unique = self.access_hits(access, target, scene, root)
        if not unique:
            return ""
        what = "values" if "values" in access["reads"] else "names"
        step_tool = "scene_do" if name == "scene_look" and op in plan_step_ops("scene_do") else name
        return (f"game-editor: {name} op {op} refused: Godot AI reads node {what} here, and that runs the @tool "
                f"script of {', '.join(unique[:4])}{' and more' if len(unique) > 4 else ''} in the editor. A read "
                f"does not run project code. Put {step_tool} op {op} in plan_change as a step: the plan says which "
                f"script runs and asks before it does. Nothing was sent to the Godot editor.")

    def _script_from_props(self, props: dict, ext: dict, scene_ref: str = "") -> str | None:
        """The node's script: a res:// or uid:// path, "<scene>#sub:<id>" for an embedded one, "?" when unclear."""
        script = props.get("script") if isinstance(props, dict) else None
        if not isinstance(script, str) or not script.strip() or script.strip() == "null":
            return None
        value = parse_value(script, ext)
        if isinstance(value, str) and value.startswith(("res://", "uid://")):
            return value
        if isinstance(value, tuple) and len(value) == 2 and value[0] == "sub":
            return f"{scene_ref}#sub:{value[1]}"
        if value is _NULL:
            return None
        return "?"

    def _node_script_res(self, path: str, scene: str, root: str) -> str | None:
        parsed = self.read_scene(scene, root) if scene else None
        rel = rel_node_path(path, (parsed or {}).get("root"))
        return self._script_at(scene, rel, root, 0)

    def _instanced(self, ref: str, uid: str | None, root: str) -> dict | None:
        """An instanced scene, found the way Godot finds it: its uid first, then its path. None when neither reads as
        a text scene (a moved file, a binary .scn): the caller treats its scripts as unknown."""
        for candidate in ([uid] if uid else []) + [ref]:
            parsed = self.read_scene(candidate, root) if candidate else None
            if parsed and parsed.get("root"):
                return parsed
        return None

    def _script_at(self, scene_ref: str, rel: str, root: str, depth: int, uid: str | None = None) -> str | None:
        if depth > 8 or not scene_ref:
            return "?" if depth > 8 else None
        parsed = self._instanced(scene_ref, uid, root) if depth > 0 else self.read_scene(scene_ref, root)
        if not parsed:
            return "?" if depth > 0 else None
        if not parsed.get("root") and not (self._virtual() or {}).get("scenes", {}).get(scene_ref):
            return "?"  # not a text scene (a binary .scn): its scripts cannot be read
        ext = parsed.get("ext") or {}
        base = parsed.get("root_scene")
        if not rel:
            found = self._script_from_props(parsed.get("root_props") or {}, ext, scene_ref)
            if found or not base:
                return found
            return self._script_at(str(base), "", root, depth + 1, parsed.get("root_scene_uid"))
        nodes = parsed.get("nodes") or {}
        info = nodes.get(rel) or {}
        found = self._script_from_props(info.get("props") or {}, ext, scene_ref)
        if found:
            return found
        if info.get("local") and not info.get("instance"):
            return None
        if info.get("instance") and info.get("scene"):
            return self._script_at(str(info["scene"]), "", root, depth + 1, info.get("scene_uid"))
        cur = rel
        while cur:
            par = _parent(cur)
            pinfo = nodes.get(par) if par else None
            if par and pinfo and pinfo.get("instance"):
                return self._script_at(pinfo.get("scene") or "", rel[len(par) + 1:], root, depth + 1,
                                       pinfo.get("scene_uid"))
            if not par:
                break
            cur = par
        if base and not info.get("local"):
            return self._script_at(str(base), rel, root, depth + 1, parsed.get("root_scene_uid"))
        return None

    def _forward_resource(self, method: str, params, mid) -> dict:
        """Proxy one MCP resource call. Secret paths are refused. Preset passwords are blanked."""
        params = params if isinstance(params, dict) else {}
        uri = params.get("uri") if isinstance(params.get("uri"), str) else ""
        file_uris = resource_file_uris(uri) if method == "resources/read" else []
        node_paths = node_property_paths(uri) if method == "resources/read" else []
        if method == "resources/read" and node_paths:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message":
                    f"game-editor: resources/read refused {uri}: reading node properties can run the node's "
                    f"script in the editor. Put scene_do op properties in plan_change and approve apply_plan. "
                    f"Nothing was sent."}}
        if method == "resources/read" and not resource_read_allowed(uri):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message":
                    f"game-editor: resources/read refused {uri}: it is not one of the resources this plugin "
                    f"reads without approval (editor state, scene, sessions, selection, performance, input map, "
                    f"project, logs, materials, class, node children or groups, script text). Nothing was sent."}}
        reason = self.ensure_child()
        if reason:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": reason}}
        if method == "resources/read" and (file_uris or node_paths):
            root, problem = self.project_root()
            if problem or not root:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message":
                        "game-editor: resources/read could not confirm the project folder, so the call was "
                        "not checked and nothing was sent to the Godot editor."}}
            if file_uris:
                try:
                    self.check_paths("resources/read", file_uris, root)
                except Refused as exc:
                    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": str(exc)}}
        child = self.child
        assert child is not None
        reply = child.request(method, params, CALL_TIMEOUT)
        if reply.get("bridge_error"):
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32000,
                                                           "message": f"game-editor: {reply['bridge_error']}"}}
        if "error" in reply:
            err = reply.get("error") or {"code": -32603, "message": "upstream error"}
            return {"jsonrpc": "2.0", "id": mid, "error": err}
        result = reply.get("result")
        if isinstance(result, dict):
            self._drop_secret_resources(result)
            _redact_tree(result)
        return {"jsonrpc": "2.0", "id": mid, "result": result if result is not None else {}}

    def _drop_secret_resources(self, result: dict) -> None:
        for key in ("resources", "resourceTemplates"):
            items = result.get(key)
            if not isinstance(items, list):
                continue
            kept = []
            for item in items:
                uri = item.get("uri") or item.get("uriTemplate") if isinstance(item, dict) else ""
                if not isinstance(uri, str) or not resource_read_allowed(re.sub(r"\{[^}]*\}", "x", uri)):
                    continue
                if self._uri_refused(uri):
                    continue
                kept.append(item)
            result[key] = kept

    def _uri_refused(self, uri: str) -> bool:
        paths = resource_file_uris(uri)
        if not paths:
            return False
        root, _problem = self.project_root()
        for found in paths:
            try:
                self.check_paths("resources/list", [found], root)
            except Refused:
                return True
        return False

    def _rewrite_script_result(self, keys: dict, res: dict) -> None:
        """Point diagnostics at the script that was written, and say committed is not compiled."""
        data = _first_json(res)
        diags = data.get("diagnostics")
        if not isinstance(diags, list):
            return
        path = keys.get("path") if isinstance(keys.get("path"), str) else ""
        fixed = []
        errors = []
        for item in diags:
            if not isinstance(item, dict):
                fixed.append(item)
                continue
            copy = dict(item)
            details = copy.get("details") if isinstance(copy.get("details"), dict) else {}
            source = details.get("source") if isinstance(details.get("source"), dict) else {}
            use_path = source.get("path") or copy.get("path") or path
            use_line = source.get("line") if source.get("line") is not None else copy.get("line")
            if isinstance(use_path, str) and use_path:
                copy["path"] = use_path
                copy["resolved"] = {"path": use_path, "line": use_line}
            if use_line is not None:
                copy["line"] = use_line
            fixed.append(copy)
            if _diag_error(copy):
                errors.append(copy)
        data["diagnostics"] = fixed
        self.last_script_diagnostics = errors
        if not errors:
            return
        first = errors[0]
        text = str(first.get("text") or first.get("message") or "parse error")
        spath = first.get("path") or path
        head = (f"game-editor: {spath} was written and did not compile. "
                f"upstream_quote: {spath}:{first.get('line')}: {text}")
        res["isError"] = True
        res["content"] = [{"type": "text", "text": head},
                          {"type": "text", "text": json.dumps(data, ensure_ascii=False)}]

    def _rewrite_editor_log(self, keys: dict, res: dict) -> None:
        if keys.get("source") != "editor":
            return
        data = _first_json(res)
        lines = data.get("lines")
        if not isinstance(lines, list) or lines:
            return
        if self.last_script_diagnostics:
            first = self.last_script_diagnostics[0]
            shown = f"{first.get('path')}:{first.get('line')}: {first.get('text') or first.get('message')}"
            data["lines"] = [{"source": "script_diagnostics", "level": "error", "text": shown}]
            data["from_script_diagnostics"] = True
            head = ("game-editor: editor log lines are empty. Showing the last script diagnostics instead. "
                    + shown)
        else:
            head = ("game-editor note: editor log lines are empty. A GDScript parse error is in the diagnostics "
                    "of create_script or edit_script, not in this log.")
        res["content"] = [{"type": "text", "text": head},
                          {"type": "text", "text": json.dumps(data, ensure_ascii=False)}]

    def _retry_screenshot_2d(self, keys: dict, res: dict, mid) -> tuple[dict, str]:
        retry = dict(keys)
        retry["source"] = "viewport_2d"
        second = self.upstream("editor_screenshot", retry, CALL_TIMEOUT, mid)
        note = ("game-editor note: source viewport is the 3D view. This scene has no 3D node, so viewport_2d "
                "was used.")
        if second.get("bridge_error") or second.get("isError"):
            empty = "viewport_empty" in json.dumps(second.get("content", "")) or "EDITOR_VIEWPORT_EMPTY" in json.dumps(
                second.get("content", ""))
            note += " The 2D view did not return a picture."
            if empty:
                note += " The 2D view is empty (headless editors often have nothing drawn)."
            if second.get("bridge_error"):
                return res, note
            return second, note
        return second, note

    def _run_game_not_live(self, res: dict) -> dict:
        data = _first_json(res)
        status = data.get("game_status") if isinstance(data.get("game_status"), dict) else {}
        status = dict(status)
        status["not_live"] = True
        status["helper_live"] = False
        if not status.get("status"):
            status["status"] = "not_live"
        data["game_status"] = status
        data["helper_live"] = False
        data["not_live"] = True
        reason = status.get("status") or "not_live"
        head = ("game-editor: run_game not_live. The game was not played "
                f"({reason}). game_status.not_live is true. A headless editor usually stops here; do not say it "
                "ran. A separate Godot process can show a floor and a score when it really played.")
        return {"content": [{"type": "text", "text": head},
                            {"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "isError": True}

    def _control_position_note(self, keys: dict, scene: str = "", root: str | None = None) -> str:
        path = keys.get("path")
        if not isinstance(path, str) or not path:
            return ""
        if scene and root and self._node_runs_tool(path, scene, root):
            # Reading its offsets would run its @tool getter, which the plan did not name.
            return ""
        res = self.upstream("node_get_properties", {"path": path, "fields": list(OFFSET_FIELDS)})
        if res.get("bridge_error") or res.get("isError"):
            return ""
        got = {item.get("name"): item.get("value") for item in (_first_json(res).get("properties") or [])
               if isinstance(item, dict)}
        if not all(name in got for name in OFFSET_FIELDS):
            return ""

        def _num(value):
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        left, right, top, bottom = (_num(got[name]) for name in OFFSET_FIELDS)
        if None in (left, right, top, bottom):
            return ""
        if abs(right - left) < 2 or abs(bottom - top) < 2:
            return ("game-editor note: position on this Control left a near-zero size "
                    f"(offset_left={left}, offset_right={right}, offset_top={top}, offset_bottom={bottom}). "
                    "Set the offsets (or use edit_ui) instead of position.")
        return ""

    def _track(self, scene: str, path: str, root_name: str | None, instance: bool) -> None:
        if not isinstance(path, str) or "://" in path:
            return
        rel = rel_node_path(path, root_name)
        with self.state_lock:
            (self.created_inst if instance else self.created_local).setdefault(scene, set()).add(rel)

    def _moved(self, scene: str, old: str, new: str, root_name: str | None) -> None:
        old_rel, new_rel = rel_node_path(old, root_name), rel_node_path(new, root_name)
        with self.state_lock:
            for table in (self.created_local, self.created_inst):
                entries = table.get(scene, set())
                for rel in list(entries):
                    if rel == old_rel or rel.startswith(old_rel + "/"):
                        entries.discard(rel)
                        entries.add(new_rel + rel[len(old_rel):])

    def _is_instance(self, scene: str, value: str, root: str, root_name: str | None) -> bool:
        rel = rel_node_path(value, root_name)
        if rel in self._created(scene, True):
            return True
        info = (self.read_scene(scene, root) or {"nodes": {}})["nodes"].get(rel)
        return bool(info and info["instance"])

    def after(self, name: str, op: str, keys: dict, res: dict, scene: str, root: str) -> None:
        """Bookkeeping after a successful call."""
        data = _first_json(res)
        if name in ("open_scene", "revert_scene", "new_scene") or (name == "save_scene" and op == "save"):
            target = data.get("path") if isinstance(data.get("path"), str) else ""
            if name == "save_scene":
                target = target or scene
            if name == "new_scene" and not target and isinstance(keys.get("path"), str):
                target = keys["path"].strip()
            if not target:
                return
            if name == "open_scene":
                # Right after opening, before later steps edit it: a scene that was already open may hold
                # unsaved edits, and a later save in this plan is judged against this look.
                self.observe(target, root, set(), full=True)
            else:
                self.in_step(target, root)
            return
        if not scene:
            return
        root_name = (self.read_scene(scene, root) or {}).get("root")
        if name == "set_node_property" and isinstance(keys.get("path"), str) and isinstance(keys.get("property"), str):
            rel = rel_node_path(keys["path"], root_name)
            self.note_property_edit(scene, rel, keys["property"],
                                    self._had_prior_diff(scene, root, rel, keys["property"]),
                                    keys.get("value"), True)
        if name == "edit_node" and op == "attach_script" and isinstance(keys.get("path"), str):
            rel = rel_node_path(keys["path"], root_name)
            self.note_property_edit(scene, rel, "script",
                                    self._had_prior_diff(scene, root, rel, "script"),
                                    keys.get("script_path"), True)
        if name == "delete_node" and isinstance(keys.get("path"), str):
            with self.state_lock:
                self.deleted_nodes.setdefault(scene, set()).add(rel_node_path(keys["path"], root_name))
        if name == "add_node":
            self._track(scene, data.get("path"), root_name, bool(keys.get("scene_path")))
        elif name == "edit_node" and op == "duplicate":
            self._track(scene, data.get("path"), root_name, self._is_instance(scene, keys.get("path", ""), root,
                                                                              root_name))
        elif name == "edit_node" and op in ("rename", "reparent") and isinstance(data.get("path"), str):
            self._moved(scene, keys.get("path", ""), data["path"], root_name)
        elif "create" in op or op == "build_layout":
            self._track(scene, data.get("path"), root_name, False)
        elif name == "batch_execute":
            results = data.get("results") or []
            for item, result in zip(keys.get("commands") or [], results):
                if not isinstance(result, dict) or result.get("status") != "ok":
                    continue
                rdata = result.get("data") or {}
                params = item.get("params") or {}
                command = item.get("command")
                if command == "set_property" and isinstance(params.get("path"), str) and isinstance(params.get("property"), str):
                    rel = rel_node_path(params["path"], root_name)
                    self.note_property_edit(scene, rel, params["property"],
                                            self._had_prior_diff(scene, root, rel, params["property"]),
                                            params.get("value"), True)
                if command == "attach_script" and isinstance(params.get("path"), str):
                    rel = rel_node_path(params["path"], root_name)
                    self.note_property_edit(scene, rel, "script",
                                            self._had_prior_diff(scene, root, rel, "script"),
                                            params.get("script_path"), True)
                if command == "delete_node" and isinstance(params.get("path"), str):
                    with self.state_lock:
                        self.deleted_nodes.setdefault(scene, set()).add(rel_node_path(params["path"], root_name))
                if command == "create_node":
                    self._track(scene, rdata.get("path"), root_name, bool(params.get("scene_path")))
                elif command == "duplicate_node":
                    self._track(scene, rdata.get("path"), root_name,
                                self._is_instance(scene, params.get("path", ""), root, root_name))
                elif command in ("rename_node", "reparent_node") and isinstance(rdata.get("path"), str):
                    self._moved(scene, params.get("path", ""), rdata["path"], root_name)
                elif command.endswith("_create"):
                    self._track(scene, rdata.get("path"), root_name, False)
        if scene and name in ("set_node_property", "add_node", "delete_node", "edit_node", "batch_execute",
                              "edit_level", "connect_signal"):
            # The editor no longer matches the last compare. Dropping that compare waits until the plan ends,
            # so a save later in this same plan still sees the agreement from before these edits.
            self._local.unsaved_edit = scene

    def _forget_uncompared_edit(self) -> None:
        """An edit that was not compared or saved must not leave an older 'they agree' snapshot in place."""
        scene = getattr(self._local, "unsaved_edit", None)
        if not isinstance(scene, str) or not scene:
            return
        self.compare_snapshots.pop(scene, None)
        self._local.unsaved_edit = None

    # ---- play session and local export (not Godot AI operations) ----
    def export_template_root(self) -> Path:
        """Where this Godot looks for export templates. Godot reads HOME; tests replace this method."""
        return Path(os.path.expanduser("~")) / "Library/Application Support/Godot/export_templates"

    def find_android_sdk(self, given: str | None) -> tuple[Path | None, str]:
        """A folder that contains platform-tools, or (None, a short label). Does not download."""
        if given:
            path = Path(os.path.expanduser(given))
            if not path.is_absolute():
                return None, "not-absolute"
            label = "the android_sdk path"
        else:
            path = Path(os.path.expanduser("~/Library/Android/sdk"))
            label = "the usual Android SDK folder"
        if (path / "platform-tools").is_dir():
            return path, ""
        return None, label

    def stop_web_server(self) -> None:
        """Stop the page this bridge started, by its process id. Does not signal other processes."""
        with self.web_server_lock:
            proc = self.web_server
            self.web_server = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except OSError:
                pass

    def start_web_server(self, directory: Path) -> dict:
        """Serve one build folder on 127.0.0.1. Replaces a page this bridge already started."""
        self.stop_web_server()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        finally:
            sock.close()
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=str(directory), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        threading.Thread(target=self._drain_server, args=(proc,), daemon=True).start()
        deadline = time.time() + 3
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    up = True
                    break
            except OSError:
                self._sleep(0.05)
        if not up:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            return {"served": False, "url": None,
                    "note": "the local page did not answer within 3 seconds. The build is on disk."}
        with self.web_server_lock:
            self.web_server = proc
        return {"served": True, "url": f"http://127.0.0.1:{port}/",
                "note": "local only, on 127.0.0.1. Nothing was uploaded."}

    @staticmethod
    def _drain_server(proc: subprocess.Popen) -> None:
        if proc.stderr is None:
            return
        try:
            while proc.stderr.read(4096):
                pass
        except (OSError, ValueError):
            return

    def _play_call(self, name: str, arguments: dict, mid) -> tuple[dict, bool]:
        res = self.upstream(name, arguments, PLAY_SAMPLE_TIMEOUT, mid)
        return res, res.get("bridge_error") == "timeout"

    @staticmethod
    def _add_error(sample: dict, line: str) -> None:
        text = " ".join(str(line).split())[:240]
        if text and text not in sample["errors"] and len(sample["errors"]) < 3:
            sample["errors"].append(text)

    _ARM_GD = "res://ge_headless_arm.gd"
    _ARM_TSCN = "res://ge_headless_arm.tscn"
    _ARM_SCRIPT = (
        "@tool\n"
        "extends Node\n"
        "func _enter_tree() -> void:\n"
        "\tif not Engine.is_editor_hint():\n"
        "\t\treturn\n"
        "\tvar key := \"editor/run/main_run_args\"\n"
        "\tvar prior := str(ProjectSettings.get_setting(key, \"\"))\n"
        "\tvar parts := prior.split(\" \", false)\n"
        "\tif \"--headless\" not in parts:\n"
        "\t\tProjectSettings.set_setting(key, (prior + \" --headless\").strip_edges())\n"
        "\tvar iface := Engine.get_singleton(\"EditorInterface\")\n"
        "\tif iface == null or iface.get_base_control() == null:\n"
        "\t\treturn\n"
        "\tiface.get_base_control().get_tree().process_frame.connect(_close_arm_tab, CONNECT_ONE_SHOT)\n"
        "func _close_arm_tab() -> void:\n"
        "\tvar iface := Engine.get_singleton(\"EditorInterface\")\n"
        "\tif iface == null:\n"
        "\t\treturn\n"
        "\tvar arm := \"res://ge_headless_arm.tscn\"\n"
        "\tvar root: Node = iface.get_edited_scene_root()\n"
        "\tvar was := \"\"\n"
        "\tif root != null:\n"
        "\t\twas = str(root.scene_file_path)\n"
        "\tif arm not in iface.get_open_scenes():\n"
        "\t\treturn\n"
        "\tif was != arm:\n"
        "\t\tiface.open_scene_from_path(arm)\n"
        "\tiface.close_scene()\n"
        "\tif was != \"\" and was != arm:\n"
        "\t\tiface.open_scene_from_path(was)\n"
    )
    _ARM_SCENE = (
        "[gd_scene load_steps=2 format=3]\n\n"
        "[ext_resource type=\"Script\" path=\"res://ge_headless_arm.gd\" id=\"1_arm\"]\n\n"
        "[node name=\"GeHeadlessArm\" type=\"Node\"]\n"
        "script = ExtResource(\"1_arm\")\n"
    )

    def _ws_port(self) -> int | None:
        argv = list(self.argv or [])
        if "--ws-port" not in argv:
            return None
        index = argv.index("--ws-port")
        if index + 1 >= len(argv) or not str(argv[index + 1]).isdigit():
            return None
        return int(argv[index + 1])

    @staticmethod
    def _process_command(pid: int) -> str:
        """The process command line, or "" when it cannot be read.

        ps is preferred. When it is not permitted, macOS sysctl KERN_PROCARGS2 still returns the argv.
        A windowed editor must not be classified as headless just because ps failed.
        """
        try:
            listed = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=2, check=False)
        except (OSError, subprocess.TimeoutExpired):
            listed = None
        if listed is not None and listed.returncode == 0 and (listed.stdout or "").strip():
            return listed.stdout.strip()
        try:
            import ctypes
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, int(pid))  # CTL_KERN, KERN_PROCARGS2
            size = ctypes.c_size_t(0)
            if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
                return ""
            buf = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
                return ""
            argv: list[str] = []
            for token in buf.raw[: size.value].split(b"\0"):
                if not token:
                    continue
                text = token.decode("utf-8", "replace")
                # Environment pairs follow argv. Flags we care about have no '='.
                if argv and "=" in text and text.split("=", 1)[0].replace("_", "").isupper():
                    break
                argv.append(text)
            return " ".join(argv)
        except (OSError, AttributeError, ValueError):
            return ""

    def _editor_headless(self) -> bool | None:
        """True when this session's editor was started with --headless. False for a windowed editor, a fake
        upstream, or a bridge with no websocket port. None when the port is set but the editor command cannot be read."""
        if any("fake_upstream" in str(part) for part in (self.argv or [])):
            return False
        port = self._ws_port()
        if not port:
            return False
        try:
            listed = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:ESTABLISHED", "-Fp"],
                capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        pids = [int(line[1:]) for line in (listed.stdout or "").splitlines()
                if line.startswith("p") and line[1:].isdigit()]
        if not pids:
            return None
        saw_editor = False
        for pid in pids:
            command = self._process_command(pid)
            tokens = command.split()
            if "--editor" not in tokens:
                continue
            if "Godot" not in command and "godot" not in command:
                continue
            saw_editor = True
            if "--headless" in tokens:
                return True
        return False if saw_editor else None

    def _remove_arm_files(self, root: str) -> None:
        for res in (self._ARM_GD, self._ARM_TSCN, self._ARM_GD + ".uid", self._ARM_TSCN + ".uid"):
            path = self.disk_path(res, root)
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except OSError:
                continue

    def _remember_project_snapshot(self, root: str) -> None:
        if self._project_snapshot and self._project_snapshot[0] == root:
            return
        path = Path(root) / "project.godot"
        try:
            blob = path.read_bytes() if path.is_file() else b""
        except OSError:
            blob = b""
        self._project_snapshot = (root, blob)

    def _restore_project_snapshot(self) -> None:
        snap = self._project_snapshot
        self._project_snapshot = None
        if not snap:
            return
        root, blob = snap
        self._remove_arm_files(root)
        path = Path(root) / "project.godot"
        try:
            if blob and path.is_file() and path.read_bytes() != blob:
                path.write_bytes(blob)
        except OSError:
            return

    def _unsaved_blocks_arm(self) -> bool:
        scene = self.current_scene()
        if not isinstance(scene, str) or not scene.startswith("res://"):
            return False
        if self.edited_props.get(scene):
            return True
        if (self.compare_snapshots.get(scene) or {}).get("diffs"):
            return True
        if (self.scenes.get(scene) or {}).get("diffs"):
            return True
        return False

    def _settings_have_headless(self, mid) -> bool:
        got = self.upstream(
            "project_manage", {"op": "settings_get", "params": {"key": "editor/run/main_run_args"}}, 20, mid)
        return "--headless" in json.dumps(got.get("content") or "")

    def _arm_headless_child(self, root: str, mid) -> bool:
        """Set editor/run/main_run_args in memory so the game child is headless. Temporary files are removed
        before this returns. project.godot is put back if the editor saved it. A windowed editor is not armed."""
        if getattr(self, "_headless_armed", False) or self._settings_have_headless(mid):
            self._headless_armed = True
            return True
        if self._unsaved_blocks_arm():
            return False
        self._remember_project_snapshot(root)
        previous = self.current_scene()
        try:
            self.disk_path(self._ARM_GD, root).write_text(self._ARM_SCRIPT, encoding="utf-8")
            self.disk_path(self._ARM_TSCN, root).write_text(self._ARM_SCENE, encoding="utf-8")
            scan = self.upstream("filesystem_manage", {"op": "scan", "params": {}}, 30, mid)
            if scan.get("isError") or scan.get("bridge_error"):
                return False
            opened = self.upstream("scene_open", {"path": self._ARM_TSCN}, 20, mid)
            if opened.get("isError") or opened.get("bridge_error"):
                return False
            # The scene closes itself on the next editor frame. Wait before deleting the file, or the tab stays
            # open on a path quit cannot name.
            deadline = time.time() + 3
            while time.time() < deadline and self._ARM_TSCN in self.open_scene_paths():
                self._sleep(0.1)
            armed = self._settings_have_headless(mid)
            if isinstance(previous, str) and previous.startswith("res://") and previous != self._ARM_TSCN:
                self.upstream("scene_open", {"path": previous}, 20, mid)
            if armed:
                self._headless_armed = True
            return armed
        except OSError:
            return False
        finally:
            self._remove_arm_files(root)
            snap = self._project_snapshot
            if snap and snap[0] == root and snap[1]:
                path = Path(root) / "project.godot"
                try:
                    if path.is_file() and path.read_bytes() != snap[1]:
                        path.write_bytes(snap[1])
                except OSError:
                    pass

    def _log_snapshot(self, source: str, mid) -> list:
        logs = self.upstream("logs_read", {"source": source, "count": 80}, 15, mid)
        if logs.get("bridge_error") or logs.get("isError"):
            return []
        return _log_items(_first_json(logs))

    def _errors_since(self, source: str, logs: dict, prior: dict, dead: bool) -> list[str]:
        fresh = {"lines": _fresh_log_items(prior.get(source) or [], _log_items(_first_json(logs)))}
        return _dead_run_errors(fresh) if dead else _log_errors(fresh)

    def play_session(self, keys: dict, root: str | None, mid) -> dict:
        """Stop, play the steps, record what happened, stop. One approval covers the whole session."""
        if root is None:
            return error_result("game-editor: play_session needs the Godot editor open on the project. "
                                "Nothing was run.")
        parsed, err = parse_play(keys)
        if parsed is None:
            return error_result(err)
        stopped_first = self.upstream("project_manage", {"op": "stop", "params": {}}, 15, mid)
        if stopped_first.get("bridge_error"):
            return error_result("game-editor: play_session could not stop the game, so it did not start a new one. "
                                "Nothing was run.")
        run_args = {"mode": parsed["mode"], "autosave": parsed["autosave"]}
        if parsed["mode"] == "custom":
            run_args["scene"] = parsed["scene"]
        headless = self._editor_headless()
        armed = None
        if headless is True:
            armed = self._arm_headless_child(root, mid)
        # Lines already in the log belong to an earlier run. Only lines after this read are this play.
        prior_logs = {source: self._log_snapshot(source, mid) for source in ("editor", "game")}
        started = self.upstream("project_run", run_args, CALL_TIMEOUT, mid)
        live = (not started.get("isError") and not started.get("bridge_error")
                and _session_live(started))
        if not live and headless is None:
            logs = self.upstream("logs_read", {"source": "editor", "count": 40}, 15, mid)
            if "Failed to execute" in json.dumps(logs.get("content") or ""):
                if self._arm_headless_child(root, mid):
                    prior_logs = {source: self._log_snapshot(source, mid) for source in ("editor", "game")}
                    started = self.upstream("project_run", run_args, CALL_TIMEOUT, mid)
                    live = (not started.get("isError") and not started.get("bridge_error")
                            and _session_live(started))
        if not live:
            logged: list[str] = []
            for source in ("editor", "game"):
                logs = self.upstream("logs_read", {"source": source, "count": 80}, 15, mid)
                if not logs.get("bridge_error") and not logs.get("isError"):
                    logged.extend(self._errors_since(source, logs, prior_logs, True))
            script_lines = [line for line in logged if re.search(r"\.(?:gd|cs):\d+", line)]
            other_lines = [line for line in logged if line not in script_lines]
            logged = (script_lines + other_lines)[:3]
            self._restore_project_snapshot()
            self.upstream("project_manage", {"op": "stop", "params": {}}, 15, mid)
            if logged:
                samples = [{"step": 0, "op": "wait", "seconds": parsed["steps"][0].get("seconds") or 0,
                            "timed_out": False, "process_frames": None, "nodes": {}, "errors": logged}]
                report = build_play_report(
                    parsed["goal"], parsed["steps"], samples,
                    {"skipped": True, "note": "the game never went live"}, True,
                    parsed["idle_seconds"], parsed["fall_dy"], bool(parsed["watch"]), None,
                    frames_before=None, require_frames=False, session_live=False)
                report["editor_headless"] = headless
                report["headless_arm"] = armed
                report["oracle"]["playing"] = False
                report["ok"] = False
                report["failure"] = ("error" if any(item.get("kind") == "error" for item in report["findings"])
                                     else "not_live")
                report["upstream_quote"] = _first_line(started)[:240]
                self.locate_errors(report, root)
                named = next((item for item in report["findings"] if item.get("file")), None)
                where = f" {named['file']}:{named['line']}" if named else ""
                kinds = sorted({item["kind"] for item in report["findings"]})
                summary = (f"game-editor: play_session ok=false failure={report['failure']}{where} "
                           f"findings={report['findings_total']} kinds={','.join(kinds) or 'none'}")
                result = json_result(summary + "\n", report, False)
                result["stop_plan"] = True
                return result
            reason = str(started.get("bridge_error") or _first_line(started))[:300]
            payload = {"ok": False, "failure": "not_live", "editor_headless": headless,
                       "headless_arm": armed, "truncated": False, "findings_total": 0,
                       "samples_total": 0, "source": "godot game session",
                       "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "reason": reason, "findings": [], "autosave": parsed["autosave"],
                       "oracle": {"playing": False, "frames_before": None, "frames_after": None, "advanced": False},
                       "upstream_quote": reason}
            return json_result("game-editor: play_session ok=false failure=not_live. Nothing was played.",
                               payload, True)
        samples: list[dict] = []
        halt = False
        # Where the watched nodes are before the first step, so the first move is judged like the others.
        start_nodes: dict = {}
        for path in parsed["watch"]:
            info, timed = self._play_call("game_manage", {"op": "get_node_info", "params": {
                "path": path, "include_properties": True}}, mid)
            if timed:
                break
            data = _first_json(info)
            found = data.get("found")
            if found is None:
                found = not info.get("isError") and not info.get("bridge_error")
            start_nodes[path] = {"name": data.get("name") or path.rsplit("/", 1)[-1], "found": bool(found),
                                 "props": _kept_props(data)}
        total_steps = len(parsed["steps"])
        step_shots: dict[int, dict] = {}
        shot_notes: dict[int, str] = {}
        for index, step in enumerate(parsed["steps"]):
            self.progress(f"play step {index + 1} of {total_steps}: {step['op']} "
                          f"{step.get('action') or step.get('key') or ''}".strip(), index + 1, total_steps + 1)
            sample = {"step": index, "op": step["op"], "action": step.get("action"), "key": step.get("key"),
                      "pressed": step.get("pressed", True), "seconds": step["seconds"], "answered": True,
                      "timed_out": False, "process_frames": None, "nodes": {}, "errors": []}
            pressed_sent = False
            if step["op"] != "wait":
                if step["op"] == "action":
                    params = {"action": step["action"], "pressed": step["pressed"], "strength": 1.0}
                    sent, timed = self._play_call("game_manage", {"op": "input_action", "params": params}, mid)
                else:
                    params = {"key": step["key"], "pressed": step["pressed"]}
                    sent, timed = self._play_call("game_manage", {"op": "input_key", "params": params}, mid)
                if timed:
                    sample["timed_out"] = True
                    sample["answered"] = False
                    samples.append(sample)
                    halt = True
                    break
                if sent.get("isError") or sent.get("bridge_error"):
                    sample["answered"] = False
                    self._add_error(sample, _first_line(sent))
                elif step.get("pressed", True):
                    pressed_sent = True
            if step["seconds"]:
                self._sleep(step["seconds"])
            debug, timed = self._play_call("game_manage", {"op": "debug_status", "params": {}}, mid)
            if timed:
                sample["timed_out"] = True
                sample["answered"] = False
                samples.append(sample)
                halt = True
                break
            sample["process_frames"] = _frames_from_debug(_first_json(debug))
            for path in parsed["watch"]:
                info, timed = self._play_call("game_manage", {"op": "get_node_info", "params": {
                    "path": path, "include_properties": True}}, mid)
                if timed:
                    sample["timed_out"] = True
                    sample["answered"] = False
                    halt = True
                    break
                data = _first_json(info)
                found = data.get("found")
                if found is None:
                    found = not info.get("isError") and not info.get("bridge_error")
                sample["nodes"][path] = {"name": data.get("name") or path.rsplit("/", 1)[-1],
                                         "found": bool(found), "props": _kept_props(data)}
            if halt:
                samples.append(sample)
                break
            for source in ("game", "editor"):
                logs, timed = self._play_call("logs_read", {"source": source, "count": 40}, mid)
                if timed:
                    sample["timed_out"] = True
                    sample["answered"] = False
                    halt = True
                    break
                for line in self._errors_since(source, logs, prior_logs, False):
                    self._add_error(sample, line)
            if not halt:
                # A small picture of the game after this step, kept for the findings that point at this step.
                shot, timed = self._play_call("editor_screenshot", {
                    "source": "game", "include_image": True, "max_resolution": 240}, mid)
                images = [item for item in (shot.get("content") or []) if isinstance(item, dict)
                          and item.get("type") == "image"] if not timed and not shot.get("isError") else []
                if images:
                    step_shots[index] = images[0]
                else:
                    shot_notes[index] = ("timed out" if timed else _first_line(shot)[:120] if shot.get("isError")
                                         else "the editor returned no picture")
            if pressed_sent:
                if step["op"] == "action":
                    release = {"action": step["action"], "pressed": False, "strength": 1.0}
                    released, timed = self._play_call("game_manage", {"op": "input_action", "params": release}, mid)
                else:
                    release = {"key": step["key"], "pressed": False}
                    released, timed = self._play_call("game_manage", {"op": "input_key", "params": release}, mid)
                if timed:
                    sample["timed_out"] = True
                    sample["answered"] = False
                    samples.append(sample)
                    halt = True
                    break
                if released.get("isError") or released.get("bridge_error"):
                    self._add_error(sample, _first_line(released))
            samples.append(sample)
            if halt:
                break
        final_image = None
        if halt:
            screenshot = {"skipped": True, "note": "skipped because the game stopped answering"}
        else:
            shot, timed = self._play_call("editor_screenshot", {
                "source": "game", "include_image": True, "max_resolution": 320}, mid)
            final_image = None
            if timed:
                screenshot = {"skipped": True, "note": "screenshot timed out"}
            elif shot.get("isError") or shot.get("bridge_error"):
                screenshot = {"ok": False, "note": _first_line(shot)[:200]}
            else:
                images = [item for item in shot.get("content") or [] if isinstance(item, dict) and item.get("type") == "image"]
                final_image = images[0] if images else None
                screenshot = {"ok": True, "note": "the game at the end of the run; the picture is attached below the "
                                                  "report" if final_image else "the editor returned no picture"}
        frames_seen = [item["process_frames"] for item in samples if isinstance(item.get("process_frames"), int)]
        frames_before = frames_seen[0] if frames_seen else None
        frames_after = frames_before
        if not halt:
            end_debug, end_timed = self._play_call("game_manage", {"op": "debug_status", "params": {}}, mid)
            if not end_timed:
                got = _frames_from_debug(_first_json(end_debug))
                if isinstance(got, int):
                    frames_after = got
        stopped_res = self.upstream("project_manage", {"op": "stop", "params": {}}, 15, mid)
        stopped = not stopped_res.get("bridge_error") and not stopped_res.get("isError")
        report = build_play_report(parsed["goal"], parsed["steps"], samples, screenshot, stopped,
                                   parsed["idle_seconds"], parsed["fall_dy"], bool(parsed["watch"]), start_nodes,
                                   frames_before=frames_before, require_frames=False, session_live=True)
        report["upstream_quote"] = _first_line(started)[:240]
        advanced = isinstance(frames_before, int) and isinstance(frames_after, int) and frames_after > frames_before
        report["oracle"]["frames_after"] = frames_after if isinstance(frames_after, int) else None
        report["oracle"]["advanced"] = advanced
        report["oracle"]["playing"] = True
        if isinstance(frames_before, int) and isinstance(frames_after, int) and not advanced:
            report["ok"] = False
            report["failure"] = "frames_not_advanced"
        self.locate_errors(report, root)
        if True:
            head = ({"custom": f"Run {parsed['scene']} (mode custom)", "current": "Run the scene open in the editor "
                     "(mode current)"}.get(parsed["mode"], "Run the main scene") +
                    f" with autosave {str(parsed['autosave']).lower()}.")
            for holder in [report] + list(report.get("findings") or []):
                if isinstance(holder.get("repro"), list) and holder["repro"]:
                    holder["repro"][0] = head
        report["replay"] = {"tool": "game_do", "op": "play_session", "goal": parsed["goal"],
                            "steps": [{k: v for k, v in step.items() if k in ("op", "action", "key", "seconds", "pressed")}
                                      for step in parsed["steps"]],
                            "watch": list(parsed["watch"]), "mode": parsed["mode"], "autosave": parsed["autosave"],
                            **({"scene": parsed["scene"]} if parsed["mode"] == "custom" else {})}
        attached: list[dict] = []
        for finding in report["findings"]:
            evidence = finding.get("evidence")
            step_no = finding.get("step") if isinstance(finding.get("step"), int) else None
            if not isinstance(evidence, dict) or step_no is None:
                continue
            if step_no in step_shots:
                if step_no not in [item["step"] for item in attached] and len(attached) < 3:
                    attached.append({"step": step_no, "image": step_shots[step_no]})
                if any(item["step"] == step_no for item in attached):
                    evidence["screenshot"] = (f"image {[item['step'] for item in attached].index(step_no) + 1} of this "
                                              f"reply (the game after step {step_no + 1})")
                else:
                    evidence["screenshot"] = "taken, not attached (three pictures per reply)"
            else:
                evidence["screenshot"] = "not available: " + shot_notes.get(step_no, "the game stopped answering")
        report["autosave"] = parsed["autosave"]
        encoded = json.dumps(report, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            report["samples"] = [{"step": item["step"], "timed_out": item["timed_out"],
                                  "process_frames": item["process_frames"], "errors": item["errors"]}
                                 for item in samples]
            report["truncated"] = True
        kinds = sorted({item["kind"] for item in report["findings"]})
        ok_word = "true" if report["ok"] else "false"
        failure = f" failure={report['failure']}" if report.get("failure") else ""
        summary = (f"game-editor: play_session ok={ok_word}{failure} goal_checked="
                   f"{'true' if report['goal_checked'] else 'false'} findings={report['findings_total']} "
                   f"kinds={','.join(kinds) or 'none'} samples={report['samples_total']} "
                   f"stopped={'true' if stopped else 'false'}")
        if report.get("failure") == "goal_not_checked":
            summary += (". The goal was not judged: only an English score comparison (score >= 3) or a node "
                        "path (/Main/Player) is checked. The game did play; quote the findings, and say the "
                        "goal itself was not checked.")
        # The play ran, so this is a report, not an error: Hermes cuts an error reply to 2,048 characters, which
        # would drop the findings' evidence. A plan still stops after a play that was not ok (stop_plan).
        result = json_result(summary + "\n", report, False)
        for number, item in enumerate(attached, 1):
            result["content"].append({"type": "text", "text": f"image {number}: the game after step "
                                      f"{item['step'] + 1}"})
            result["content"].append(item["image"])
        if final_image is not None and len(attached) < 3:
            result["content"].append({"type": "text", "text": f"image {len(attached) + 1}: the game at the end of the run"})
            result["content"].append(final_image)
        elif final_image is not None:
            report_note = "the final picture was taken but not attached (three pictures per reply)"
            result["content"][0]["text"] = result["content"][0]["text"].replace(
                "the picture is attached below the report", report_note)
        result = limit_result(result["content"], False)
        result["stop_plan"] = not report["ok"]
        self._restore_project_snapshot()
        return result

    _ERROR_AT = re.compile(r"((?:res://)?[A-Za-z0-9_./-]+\.(?:gd|cs)):(\d+)")

    def locate_errors(self, report: dict, root: str | None) -> None:
        """Give each error finding the res:// file, the line number and that line's text, when the message
        names a script line (Godot writes main.gd:7 @ func()), so the fix can be planned from it."""
        if root is None:
            return
        index: dict[str, list[str]] | None = None
        for finding in report.get("findings") or []:
            if finding.get("kind") != "error":
                continue
            match = self._ERROR_AT.search(str(finding.get("detail") or ""))
            if not match:
                continue
            raw, line_no = match.group(1), int(match.group(2))
            res = raw if raw.startswith("res://") else None
            if res is None:
                if index is None:
                    index = {}
                    base = Path(root)
                    for path in base.rglob("*"):
                        rel = path.relative_to(base)
                        if any(part.startswith(".") for part in rel.parts) or rel.parts[:2] == ("addons", "godot_ai"):
                            continue
                        if path.suffix in (".gd", ".cs") and path.is_file():
                            index.setdefault(path.name, []).append("res://" + rel.as_posix())
                tail = raw.lstrip("./")
                options = [cand for cand in index.get(tail.rsplit("/", 1)[-1], []) if cand.endswith("/" + tail)
                           or cand == "res://" + tail]
                res = options[0] if len(options) == 1 else None
            if res is None:
                continue
            try:
                lines = self.disk_path(res, root).read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            finding["file"] = res
            finding["line"] = line_no
            if 1 <= line_no <= len(lines):
                finding["code"] = lines[line_no - 1]
            finding["next"] = (f"Fix {res}:{line_no}: plan_change with script_do op script, path {res}, old_text the "
                               f"exact text of that line (code), new_text the fix; then play_session again.")

    def _export_preflight(self, parsed: dict) -> dict:
        """Local checks before an export: the Godot binary, installed templates, an Android SDK.

        {"godot": path} or {"error": result}. plan_change runs this too, so a plan does not ask for an
        export that cannot run.
        """
        godot = self.godot_binary()
        if not godot:
            return {"error": error_result(
                "game-editor: export_game could not find the Godot binary (Godot.app or Godot_mono.app in "
                "/Applications or ~/Applications, Steam, or godot on PATH). Nothing was exported.")}
        version = godot_version_text(godot)
        need = template_need(version, self.export_template_root(), parsed["platform"])
        if need is not None:
            need["source"] = "local export templates"
            need["as_of"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if version is None:
                need["note"] = ("Godot's version could not be read, so the templates were not treated as "
                                "installed. This plugin does not download them. Nothing was written.")
            return {"error": json_result("game-editor: export_templates_missing. Nothing was downloaded and "
                                         "nothing was written.", need, True)}
        if parsed["platform"] == "android":
            sdk, label = self.find_android_sdk(parsed["android_sdk"])
            if label == "not-absolute":
                return {"error": error_result("game-editor: export_game android_sdk must be an absolute path. "
                                              "Nothing was exported.")}
            if sdk is None:
                payload = {"ok": False, "failure": "android_sdk_missing", "truncated": False,
                           "platform": "android", "downloaded": False, "sdk": label,
                           "files_total": 0, "source": "local android sdk",
                           "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           "note": "Pass android_sdk as an absolute path to a folder that contains "
                                   "platform-tools. Nothing was downloaded."}
                return {"error": json_result("game-editor: android_sdk_missing. Nothing was downloaded and "
                                             "nothing was exported.", payload, True)}
        return {"godot": godot}

    def export_game(self, keys: dict, root: str | None) -> dict:
        """Export locally, or stop before any download or upload when templates are missing."""
        parsed, err = parse_export(keys)
        if parsed is None:
            return error_result(err)
        if root is None:
            return error_result("game-editor: export_game needs the Godot editor open on the project. "
                                "Nothing was exported.")
        dest, dest_err = output_path(Path(root), parsed["output"])
        if dest is None:
            return error_result(dest_err)
        refusal = neighbor_refusal(dest, parsed["overwrite"])
        if refusal:
            return error_result(refusal)
        pre = self._export_preflight(parsed)
        if "error" in pre:
            return pre["error"]
        godot = pre["godot"]
        preset, wrote, preset_err = ensure_preset(Path(root), parsed["platform"])
        if preset is None:
            return error_result(preset_err)
        self.stop_web_server()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return error_result(f"game-editor: export_game could not create the output folder "
                                f"({type(exc).__name__}). Nothing was exported.")
        flag = "--export-debug" if parsed["debug"] else "--export-release"
        self.progress(f"exporting {parsed['platform']} with Godot (up to {int(EXPORT_TIMEOUT)} s)")
        argv = [godot, "--headless", "--path", root, flag, preset, str(dest)]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=EXPORT_TIMEOUT, cwd=root)
            code: int | None = proc.returncode
            err_bytes = proc.stderr or b""
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            code = None
            err_bytes = exc.stderr or b""
            timed_out = True
        stderr, cut = _tail_bytes(err_bytes)
        command = [os.path.basename(godot), "--headless", flag, preset, dest.name]
        rel = dest.name
        try:
            rel = str(dest.resolve().relative_to(Path(root).resolve()))
        except (OSError, ValueError):
            rel = dest.name
        base = {"ok": False, "truncated": cut, "platform": parsed["platform"], "preset": preset,
                "preset_written": wrote, "output": rel, "command": command, "exit_code": code,
                "downloaded": False, "served": False, "url": None, "stderr": stderr,
                "files_total": 1 if dest.exists() else 0, "source": "godot export",
                "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if timed_out:
            base["failure"] = "timed_out"
            return json_result("game-editor: export_game ok=false failure=timed_out. The export did not finish. "
                               "Nothing was uploaded.", base, True)
        if code != 0 or not dest.exists():
            base["failure"] = "export_failed"
            note = export_failure_note(parsed["platform"], stderr)
            if note:
                base["note"] = note
            elif code == 0:
                base["note"] = "Godot exited 0 but the output is not there."
            summary = (f"game-editor: export_game ok=false failure=export_failed exit={code}"
                       f"{' truncated=true' if cut else ''}. "
                       f"{note or 'Nothing was uploaded.'}")
            return json_result(summary, base, True)
        page = {"served": False, "url": None, "note": ""}
        if parsed["serve"]:
            page = self.start_web_server(dest.parent)
        base.update({"ok": True, "failure": None, "served": page["served"], "url": page["url"]})
        if page.get("note"):
            base["note"] = page["note"]
        summary = (f"game-editor: export_game ok=true platform={parsed['platform']} "
                   f"output={dest.name} served={'true' if page['served'] else 'false'}"
                   f"{' truncated=true' if cut else ''}")
        return json_result(summary, base, False)

    def shutdown(self) -> None:
        self.stop_web_server()
        if self.child is not None:
            self.child.stop()

    def serve(self) -> int:
        for raw in iter(sys.stdin.buffer.readline, b""):
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.write({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            if not isinstance(msg, dict):
                continue
            if "method" not in msg and "id" in msg:
                continue  # a reply to nothing: this bridge sends no requests to the client
            reply = self.handle(msg)
            if reply is not None:
                self.write(reply)
        deadline = time.time() + SHUTDOWN_GRACE
        for worker in self.calls:
            worker.join(max(0.0, deadline - time.time()))
        self.shutdown()
        return 0


def main() -> int:
    bridge = Bridge()

    def _stop(_signum, _frame):
        bridge.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    return bridge.serve()


if __name__ == "__main__":
    sys.exit(main())
