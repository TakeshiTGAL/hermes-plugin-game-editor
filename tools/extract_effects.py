#!/usr/bin/env python3
"""Classify each Godot AI 4.3.0 operation from the upstream source.

Does not import godot_ai. Reads plugin.gd, the GDScript handlers it registers,
and the Python tools/handlers with the ast module. An operation is marked
run_code, write, delete, or send only when the handler body (and the helpers
it calls) contains that call. A command whose body cannot be found is
unresolved, which the bridge treats as fail closed.

It also records node_access for each command: whether the handler reads a
value of a node held in a Node-typed variable (`node.name`, `node.get(...)`,
`node.get_property_list()`), and whether it walks children. A @tool script's
_get, _get_property_list and _validate_property run on those reads, so the
bridge refuses such a read when the node it touches has a @tool script.

Runs from this repository alone, given a checkout of the upstream source:

    git clone --depth 1 --branch v4.3.0 https://github.com/hi-godot/godot-ai /tmp/godot-ai
    python3 tools/extract_effects.py --upstream /tmp/godot-ai          # writes bridge/effects.json
    python3 tools/extract_effects.py --upstream /tmp/godot-ai --check  # exit 1 if it would change

--upstream may also come from the GODOT_AI_SRC environment variable. The checkout's
pyproject.toml must say version 4.3.0 (the version the bridge pins).
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINNED = "4.3.0"
OUT = ROOT / "bridge" / "effects.json"
# Set by configure() from --upstream.
UPSTREAM = Path()
PLUGIN = Path()
HANDLERS_GD = Path()
UTILS_GD = Path()
PY_HANDLERS = Path()
PY_TOOLS = Path()


def configure(upstream: Path) -> str:
    """Point the module at an upstream checkout. Returns "" or why it cannot be used."""
    global UPSTREAM, PLUGIN, HANDLERS_GD, UTILS_GD, PY_HANDLERS, PY_TOOLS
    UPSTREAM = upstream.resolve()
    PLUGIN = UPSTREAM / "plugin" / "addons" / "godot_ai"
    HANDLERS_GD = PLUGIN / "handlers"
    UTILS_GD = PLUGIN / "utils"
    PY_HANDLERS = UPSTREAM / "src" / "godot_ai" / "handlers"
    PY_TOOLS = UPSTREAM / "src" / "godot_ai" / "tools"
    if not PLUGIN.is_dir() or not PY_TOOLS.is_dir():
        return f"{UPSTREAM} is not a godot-ai checkout (plugin/addons/godot_ai and src/godot_ai/tools are missing)"
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (UPSTREAM / "pyproject.toml").read_text(encoding="utf-8")
                        if (UPSTREAM / "pyproject.toml").is_file() else "")
    if not version or version.group(1) != PINNED:
        return f"{UPSTREAM} is godot-ai {version.group(1) if version else 'of unknown version'}, not {PINNED}"
    return ""


# Call shapes. Comments are stripped first; string literals stay, because
# undo calls the real method by name ("remove_child", "queue_free").
RUN_CODE = (
    r"ResourceLoader\s*\.\s*load\b",
    r"(?<![\w.])load\s*\(",
    r"\.instantiate\s*\(",
    r"GDScript\s*\.\s*new\b",
    r"Expression\s*\.\s*new\b",
    r"\.reload\s*\(",
    r"(?<![\w.])eval\s*\(",
    r"change_scene\w*\s*\(",
    r"ClassDB\s*\.\s*instantiate\b",
    r"EditorInterface\s*\.\s*play",
    r"EditorInterface\s*\.\s*stop_playing_scene\s*\(",
    r"player\.play\s*\(",
    r"player\.stop\s*\(",
    r"EditorInterface\s*\.\s*open_scene_from_path\s*\(",
    r"EditorInterface\s*\.\s*reload_scene_from_path\s*\(",
    r"EditorInterface\s*\.\s*set_plugin_enabled\s*\(",
    r'"set_script"',
    r"OS\s*\.\s*execute\s*\(",
    r"OS\s*\.\s*create_process\s*\(",
    r"OS\s*\.\s*shell_open\s*\(",
    r"\bgame_eval\b",
    r"request_game_eval\s*\(",
    r"request_game_command\s*\(",
    r"request_game_debug_control\s*\(",
    r"call_deferred\(\s*\"quit\"\s*\)",
    r"\.set_script\s*\(",
    r"\.scan\s*\(",
)
WRITE = (
    r"FileAccess\s*\.\s*WRITE\b",
    r"FileAccess\s*\.\s*READ_WRITE\b",
    r"FileAccess\s*\.\s*WRITE_READ\b",
    r"\.store_string\s*\(",
    r"\.store_buffer\s*\(",
    r"\.store_line\s*\(",
    r"ResourceSaver\s*\.\s*save\b",
    r"\.save\s*\(",
    r"ProjectSettings\s*\.\s*set_setting\b",
    r"ProjectSettings\s*\.\s*save\b",
    r"\.set_setting\s*\(",
    r"EditorInterface\s*\.\s*set_plugin_enabled\s*\(",
    r"selection\.clear\s*\(",
    r"selection\.add_node\s*\(",
    r"\.update_file\s*\(",
    r"add_do_property\s*\(",
    r"add_do_method\s*\(",
    r"EditorInterface\s*\.\s*save_scene(?:_as)?\s*\(",
    r"DirAccess\s*\.\s*(make_dir|rename|copy)\b",
    r"\.store_var\s*\(",
)
# Disk writes and deletes. A batch of scene edits does not apply the file
# overwrite check, so a command with disk=true is refused inside a batch.
DISK = (
    r"FileAccess\s*\.\s*WRITE\b",
    r"FileAccess\s*\.\s*READ_WRITE\b",
    r"FileAccess\s*\.\s*WRITE_READ\b",
    r"\.store_string\s*\(",
    r"\.store_buffer\s*\(",
    r"\.store_line\s*\(",
    r"\.store_var\s*\(",
    r"ResourceSaver\s*\.\s*save\b",
    r"DirAccess\s*\.\s*(make_dir|rename|copy|remove)\b",
    r"\.remove_absolute\s*\(",
    r"move_to_trash",
    r"ProjectSettings\s*\.\s*save\b",
    r"EditorInterface\s*\.\s*save_scene(?:_as)?\s*\(",
    r"\.set_setting\s*\(",
)
DELETE = (
    r"queue_free\s*\(",
    r"_log_buffer\.clear\s*\(",
    r"clear_debugger_error_trees\s*\(",
    r"remove_child",
    r"move_to_trash",
    r"DirAccess\s*\.\s*remove",
    r"\.remove_absolute\s*\(",
    r"\bfree\s*\(",
)
SEND = (
    r"\bHTTPRequest\b",
    r"\bHTTPClient\b",
    r"a\.run\.app",
    r"\.request\s*\(\s*[\"']https?://",
    r"send_event\s*\(",
    r"EditorInterface\s*\.\s*set_plugin_enabled\s*\(",
)
# H7: the side effects a handler causes inside the operation, beyond its own name. Each is a call shape in the
# handler or a helper it calls (followed the same way as the effects above). The bridge puts them in the plan
# text, the second approval and the plan fingerprint, so a step is described by what it really does.
SIDE_EFFECTS = (
    ("saves_scene", (r"EditorInterface\s*\.\s*save_scene(?:_as)?\s*\(", r"\bsave_all_scenes\s*\(")),
    ("saves_resource", (r"ResourceSaver\s*\.\s*save\b",)),
    ("writes_file", (r"FileAccess\s*\.\s*(?:WRITE|READ_WRITE|WRITE_READ)\b", r"\.store_(?:string|buffer|line|var)\s*\(",
                     r"\bwrite_text_to_disk\s*\(")),
    ("deletes_file", (r"DirAccess\s*\.\s*remove", r"\.remove_absolute\s*\(", r"move_to_trash")),
    ("moves_file", (r"DirAccess\s*\.\s*(?:rename|copy)\b", r"\.(?:rename|copy)_absolute\s*\(")),
    ("reimports", (r"\.reimport_files\s*\(",)),
    ("rescans", (r"\.update_file\s*\(", r"\.scan\s*\(", r"\.scan_sources\s*\(")),
    ("saves_project_settings", (r"ProjectSettings\s*\.\s*save\b",)),
    ("changes_project_settings", (r"ProjectSettings\s*\.\s*set_setting\b", r"ProjectSettings\s*\.\s*clear\b")),
    ("changes_editor_settings", (r"get_editor_settings\s*\(\s*\)\s*\.\s*set",)),
    ("loads_resource", (r"ResourceLoader\s*\.\s*load\b", r"(?<![\w.])load\s*\(")),
    ("instantiates_scene", (r"\.instantiate\s*\(",)),
    ("adds_to_tree", (r"\.add_child\s*\(", r'"add_child"')),
    ("sets_script", (r"\.set_script\s*\(", r'"set_script"')),
    ("removes_nodes", (r"queue_free\s*\(", r"remove_child")),
    ("opens_scene", (r"open_scene_from_path\s*\(",)),
    ("reloads_scene_from_disk", (r"reload_scene_from_path\s*\(",)),
    ("plays_game", (r"EditorInterface\s*\.\s*play", r"\bplay_main_scene\s*\(", r"\bplay_current_scene\s*\(")),
    ("stops_game", (r"stop_playing_scene\s*\(",)),
    ("evaluates_code", (r"Expression\s*\.\s*new\b", r"GDScript\s*\.\s*new\b", r"\bgame_eval\b", r"request_game_eval\s*\(")),
    ("starts_process", (r"OS\s*\.\s*(?:execute|create_process)\s*\(",)),
    ("opens_with_system", (r"OS\s*\.\s*shell_open\s*\(",)),
    ("network", (r"\bHTTPRequest\b", r"\bHTTPClient\b")),
    ("toggles_plugin", (r"set_plugin_enabled\s*\(",)),
    ("clears_logs", (r"_log_buffer\.clear\s*\(", r"clear_debugger_error_trees\s*\(")),
    ("quits_editor", (r'call_deferred\(\s*"quit"\s*\)',)),
    ("changes_selection", (r"selection\.(?:clear|add_node)\s*\(",)),
)


def side_effects_in(body: str) -> set[str]:
    return {tag for tag, patterns in SIDE_EFFECTS if any(re.search(pat, body) for pat in patterns)}


DEFER_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.(?:call_deferred|bind)\s*\(")
LOAD_GD_RE = re.compile(r'load\(\s*"(res://addons/godot_ai/[^"]+\.gd)"\s*\)')

MEMBER_NEW_RE = re.compile(r"(?m)^\s*(?:var\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[A-Za-z_][A-Za-z0-9_]*\s*)?:?=\s*([A-Za-z_][A-Za-z0-9_]*)\.new\s*\(")
FUNC_RE = re.compile(r"(?m)^(?:static\s+)?func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
CLASS_RE = re.compile(r"(?m)^class_name\s+([A-Za-z_][A-Za-z0-9_]*)")
PRELOAD_RE = re.compile(
    r'(?m)^const\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::=\s*|\s*=\s*)preload\(\s*"([^"]+\.gd)"\s*\)'
)
CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\s*\(")
DOT_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
COMMENT_RE = re.compile(r"(?m)^\s*##.*$|#.*$")
KEYWORDS = {
    "if", "elif", "else", "for", "while", "match", "return", "func", "class",
    "and", "or", "not", "await", "print", "push_error", "push_warning", "str",
    "int", "float", "bool", "len", "range", "min", "max", "abs", "clamp",
    "typeof", "preload", "super", "self", "assert", "breakpoint", "in", "is",
    "as", "not", "pass", "var", "const", "enum", "signal", "static",
}


def strip_comments(text: str) -> str:
    return COMMENT_RE.sub("", text)


def gd_functions(text: str) -> dict[str, str]:
    """Map function name to its comment-stripped body, including nested funcs."""
    clean = strip_comments(text)
    matches = list(FUNC_RE.finditer(clean))
    out: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
        out[match.group(1)] = clean[start:end]
    return out


def gd_path(spec: str) -> Path | None:
    if spec.startswith("res://addons/godot_ai/"):
        rel = spec[len("res://addons/godot_ai/"):]
        path = PLUGIN / rel
        return path if path.is_file() else None
    return None


def effects_in(body: str) -> set[str]:
    found = set()
    if any(re.search(pat, body) for pat in RUN_CODE):
        found.add("run_code")
    if any(re.search(pat, body) for pat in WRITE):
        found.add("write")
    if any(re.search(pat, body) for pat in DELETE):
        found.add("delete")
    if any(re.search(pat, body) for pat in SEND):
        found.add("send")
    if any(re.search(pat, body) for pat in DISK):
        found.add("disk")
    return found


def load_gd_index() -> tuple[dict[Path, dict], dict[str, Path]]:
    index: dict[Path, dict] = {}
    classes: dict[str, Path] = {}
    files = [path for path in PLUGIN.rglob("*.gd") if path.is_file()]
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        preloads = {}
        for name, spec in PRELOAD_RE.findall(text):
            target = gd_path(spec)
            if target is not None:
                preloads[name] = target
        # Members set from a constructor (`_values = AnimationValues.new(self)`): calls on them are followed.
        members = {name: kind for name, kind in MEMBER_NEW_RE.findall(strip_comments(text))}
        index[path] = {"funcs": gd_functions(text), "preloads": preloads, "members": members}
        for match in CLASS_RE.findall(text):
            classes[match] = path
    return index, classes


def resolve_target(info: dict, classes: dict[str, Path], const: str) -> Path | None:
    """The script a name refers to: a preload constant, a class_name, or a member built with .new()."""
    target = info["preloads"].get(const) or classes.get(const)
    if target is None and const in info.get("members", {}):
        kind = info["members"][const]
        target = info["preloads"].get(kind) or classes.get(kind)
    return target


def walk_gd(index: dict, classes: dict[str, Path], path: Path, func: str,
            seen: set[tuple[Path, str]], scan=None) -> set[str]:
    scan = scan or effects_in
    key = (path, func)
    if key in seen:
        return set()
    seen.add(key)
    info = index.get(path)
    if not info or func not in info["funcs"]:
        return {"unresolved"} if scan is effects_in else set()
    body = info["funcs"][func]
    found = scan(body)
    for name in CALL_RE.findall(body):
        if name in KEYWORDS or name not in info["funcs"]:
            continue
        found |= walk_gd(index, classes, path, name, seen, scan)
    for const, method in DOT_RE.findall(body):
        # Godot constructors are not methods. Follow _init when the script
        # has one; a built-in Node3D.new() has no script and no effect.
        if method == "new":
            target = info["preloads"].get(const) or classes.get(const)
            if target is not None and "_init" in index.get(target, {}).get("funcs", {}):
                found |= walk_gd(index, classes, target, "_init", seen, scan)
            continue
        target = resolve_target(info, classes, const)
        if target is None:
            continue
        followed = walk_gd(index, classes, target, method, seen, scan)
        # A missing helper hides the real call. Fail closed.
        found |= followed
    for name in DEFER_RE.findall(body):
        if name in info["funcs"]:
            found |= walk_gd(index, classes, path, name, seen, scan)
    # load("res://...gd").new() then job.run(): the method is on that script.
    for spec in LOAD_GD_RE.findall(body):
        target = gd_path(spec)
        funcs = index.get(target, {}).get("funcs", {}) if target is not None else {}
        for method in funcs:
            if re.search(r"\." + re.escape(method) + r"\s*\(", body):
                found |= walk_gd(index, classes, target, method, seen, scan)
    return found


# ---- node access: which handlers read values of a node (a @tool script's _get runs on those reads) ----

def node_classes() -> set[str]:
    """Node and every engine class below it, from the ClassDB table the bridge already ships."""
    parents = json.loads((ROOT / "bridge" / "property_usage.json").read_text(encoding="utf-8")).get("parents") or {}
    out = {"Node"}
    for name in parents:
        cur, seen = name, set()
        while cur in parents and cur not in seen:
            seen.add(cur)
            if parents[cur] == "Node":
                out.add(name)
                break
            cur = parents[cur]
    return out


NODE_CLASSES: set[str] = set()
TYPED_VAR = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Z][A-Za-z0-9_]*)\b")
AS_VAR = re.compile(r"\bvar\s+([A-Za-z_][A-Za-z0-9_]*)\s*:?=[^\n]*\bas\s+([A-Z][A-Za-z0-9_]*)")
FOR_CHILD = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+[^\n]*get_children\s*\(")
# Reads that reach the script instance: Object.get and its friends, and a property read.
VALUE_CALLS = ("get", "get_indexed", "get_property_list", "get_meta_list")


def node_vars(body: str) -> set[str]:
    found = {name for name, kind in TYPED_VAR.findall(body) if kind in NODE_CLASSES}
    found |= {name for name, kind in AS_VAR.findall(body) if kind in NODE_CLASSES}
    found |= set(FOR_CHILD.findall(body))
    return found


def access_in(body: str) -> set[str]:
    """"name" for X.name, "values" for X.get(...), X.get_property_list() or another property read, where X
    is a Node-typed variable. An assignment (X.prop = v) is a write, not a read, and is left out."""
    found: set[str] = set()
    for var in node_vars(body):
        for match in re.finditer(r"(?<![\w.])" + re.escape(var) + r"\.([A-Za-z_][A-Za-z0-9_]*)(\s*\()?", body):
            attr, call = match.group(1), match.group(2)
            if call:
                if attr in VALUE_CALLS:
                    found.add("values")
                continue
            rest = body[match.end():match.end() + 3]
            if re.match(r"\s*=[^=]", rest):
                continue
            found.add("name" if attr == "name" else "values")
    return found


def walk_access(index: dict, classes: dict[str, Path], path: Path, func: str,
                seen: set[tuple[Path, str]]) -> tuple[set[str], bool]:
    """(access kinds, walks children) for a handler and the helpers it calls (same call rules as walk_gd)."""
    key = (path, func)
    if key in seen:
        return set(), False
    seen.add(key)
    info = index.get(path)
    if not info or func not in info["funcs"]:
        return set(), False
    body = info["funcs"][func]
    found = access_in(body)
    walks = bool(re.search(r"\bget_children\s*\(|\bfind_children\s*\(", body))
    for name in CALL_RE.findall(body):
        if name in KEYWORDS or name not in info["funcs"]:
            continue
        more, deep = walk_access(index, classes, path, name, seen)
        found |= more
        walks = walks or deep
    for const, method in DOT_RE.findall(body):
        target = resolve_target(info, classes, const)
        if target is None or method == "new":
            continue
        more, deep = walk_access(index, classes, target, method, seen)
        found |= more
        walks = walks or deep
    return found, walks


def parse_plugin(index: dict, classes: dict[str, Path]) -> dict[str, dict]:
    text = (PLUGIN / "plugin.gd").read_text(encoding="utf-8", errors="replace")
    handlers: dict[str, Path] = {}
    for key, filename in re.findall(
        r'register_lazy_handler\(\s*"([^"]+)"\s*,\s*HANDLERS_DIR\s*\+\s*"([^"]+)"', text
    ):
        path = HANDLERS_GD / filename
        if path.is_file():
            handlers[key] = path
    commands: dict[str, dict] = {}
    for command, handler_key, method in re.findall(
        r'register_lazy\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*&"([^"]+)"\s*\)', text
    ):
        path = handlers.get(handler_key)
        if path is None:
            commands[command] = {
                "effects": ["unresolved"],
                "why": f"no handler file for {handler_key}",
                "evidence": [],
            }
            continue
        effects = walk_gd(index, classes, path, method, set())
        access, walks = walk_access(index, classes, path, method, set())
        rel = str(path.relative_to(UPSTREAM))
        disk = "disk" in effects
        effects.discard("disk")
        commands[command] = {
            "effects": sorted(effects) or ["read"],
            "disk": disk,
            "handler": f"{rel}:{method}",
            "evidence": [f"{rel}:{method}"],
        }
        if access:
            commands[command]["node_access"] = {"reads": sorted(access), "walks": walks}
        side = walk_gd(index, classes, path, method, set(), side_effects_in)
        if side:
            commands[command]["side_effects"] = sorted(side)
    return commands


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def py_functions(path: Path) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def commands_of_function(funcs: dict[str, ast.AST], name: str, seen: set[str]) -> tuple[set[str], bool]:
    if name in seen or name not in funcs:
        return set(), name not in funcs and name not in seen
    seen.add(name)
    commands: set[str] = set()
    unresolved = False
    for node in ast.walk(funcs[name]):
        if not isinstance(node, ast.Call):
            continue
        called = _call_name(node.func)
        if called.endswith("send_command") or called == "send_command":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                commands.add(node.args[0].value)
            else:
                unresolved = True
            continue
        leaf = called.split(".")[-1]
        if leaf in funcs and leaf not in seen:
            nested, bad = commands_of_function(funcs, leaf, seen)
            commands |= nested
            unresolved = unresolved or bad
    return commands, unresolved


def handler_index() -> dict[str, dict[str, ast.AST]]:
    """Map module file stem to its functions. `node` → handlers/node.py."""
    out = {}
    for path in PY_HANDLERS.glob("*.py"):
        if path.name == "__init__.py":
            continue
        out[path.stem] = py_functions(path)
    return out


def resolve_handler(alias: str, imports: dict[str, str], handlers: dict) -> tuple[str, str] | None:
    """Return (module stem, function) for alias.func or a bare imported function."""
    if "." in alias:
        module_alias, func = alias.split(".", 1)
        stem = imports.get(module_alias)
        if stem and func:
            return stem, func
    stem = imports.get(alias)
    if stem and alias in handlers.get(stem, {}):
        return stem, alias
    return None


def tool_ops(handlers: dict[str, dict[str, ast.AST]]) -> dict[str, dict]:
    """MCP tool name (+ .op) → plugin commands."""
    rows: dict[str, dict] = {}
    for path in sorted(PY_TOOLS.glob("*.py")):
        if path.name.startswith("_"):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        imports: dict[str, str] = {}
        for node in tree.body:
            if not (isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("godot_ai.handlers")):
                continue
            if node.module == "godot_ai.handlers":
                for alias in node.names:
                    imports[alias.asname or alias.name] = alias.name
            else:
                stem = node.module.split(".")[-1]
                for alias in node.names:
                    imports[alias.asname or alias.name] = stem
        local_funcs = {
            n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        # Named @mcp.tool functions and register_manage_tool ops, anywhere.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                is_tool = False
                for deco in node.decorator_list:
                    called = deco.func if isinstance(deco, ast.Call) else deco
                    if _call_name(called).endswith("tool"):
                        is_tool = True
                if not is_tool:
                    continue
                commands, unresolved, local = _py_calls_to_commands(node, imports, handlers)
                rows[node.name] = _row(commands, unresolved, f"src/godot_ai/tools/{path.name}:{node.name}", local)
            if isinstance(node, ast.Call) and _call_name(node.func).endswith("register_manage_tool"):
                tool_name = ""
                ops = None
                for kw in node.keywords:
                    if kw.arg == "tool_name" and isinstance(kw.value, ast.Constant):
                        tool_name = str(kw.value.value)
                    if kw.arg == "ops":
                        ops = kw.value
                if not tool_name or not isinstance(ops, ast.Dict):
                    continue
                for key, value in zip(ops.keys, ops.values):
                    if not isinstance(key, ast.Constant):
                        continue
                    op = str(key.value)
                    target = _call_name(value)
                    commands, unresolved, local = set(), True, set()
                    if "." not in target and target in local_funcs:
                        commands, unresolved, local = _py_calls_to_commands(local_funcs[target], imports, handlers)
                    else:
                        resolved = resolve_handler(target, imports, handlers)
                        if resolved:
                            stem, func = resolved
                            commands, unresolved = commands_of_function(handlers[stem], func, set())
                            func_node = handlers.get(stem, {}).get(func)
                            if func_node is not None:
                                local = py_local_effects(func_node)
                    rows[f"{tool_name}.{op}"] = _row(
                        commands, unresolved, f"src/godot_ai/tools/{path.name}:{tool_name}.{op}", local
                    )
        del local_funcs, source
    return rows


# Python mutations that never send a plugin command. The call name is the
# evidence; a session switch writes which editor the next call uses.
PY_WRITE_CALLS = {"set_active_session"}


def py_local_effects(node: ast.AST) -> set[str]:
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and _call_name(child.func).split(".")[-1] in PY_WRITE_CALLS:
            found.add("write")
    return found


def _py_calls_to_commands(node: ast.AST, imports: dict[str, str], handlers: dict) -> tuple[set[str], bool, set[str]]:
    commands: set[str] = set()
    local = py_local_effects(node)
    unresolved = False
    saw = False
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        called = _call_name(child.func)
        if called.endswith("send_command"):
            saw = True
            if child.args and isinstance(child.args[0], ast.Constant) and isinstance(child.args[0].value, str):
                commands.add(child.args[0].value)
            else:
                unresolved = True
            continue
        resolved = resolve_handler(called, imports, handlers)
        if resolved:
            saw = True
            stem, func = resolved
            nested, bad = commands_of_function(handlers[stem], func, set())
            commands |= nested
            unresolved = unresolved or bad
            func_node = handlers.get(stem, {}).get(func)
            if func_node is not None:
                local |= py_local_effects(func_node)
    if not saw and not local:
        unresolved = True
    return commands, unresolved, local


def _row(commands: set[str], unresolved: bool, evidence: str, local: set[str] | None = None) -> dict:
    return {
        "commands": sorted(commands),
        "unresolved_command": unresolved,
        "local_effects": sorted(local or []),
        "evidence": [evidence],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upstream", default=os.environ.get("GODOT_AI_SRC", ""),
                        help=f"a checkout of hi-godot/godot-ai at v{PINNED} (or set GODOT_AI_SRC)")
    parser.add_argument("--out", default=str(OUT), help="where to write effects.json")
    parser.add_argument("--check", action="store_true", help="do not write; exit 1 when the output would change")
    args = parser.parse_args(argv)
    if not args.upstream:
        print("no upstream source: pass --upstream DIR (or GODOT_AI_SRC). Get it with\n"
              f"  git clone --depth 1 --branch v{PINNED} https://github.com/hi-godot/godot-ai DIR", file=sys.stderr)
        return 2
    problem = configure(Path(args.upstream).expanduser())
    if problem:
        print(problem, file=sys.stderr)
        return 2
    NODE_CLASSES.update(node_classes())
    out_path = Path(args.out)
    index, classes = load_gd_index()
    commands = parse_plugin(index, classes)
    handlers = handler_index()
    tools = tool_ops(handlers)
    by_tool: dict[str, dict] = {}
    for key, row in tools.items():
        effects: set[str] = set()
        evidence = list(row["evidence"])
        disk = False
        if row["unresolved_command"]:
            effects.add("unresolved")
        effects.update(row.get("local_effects") or [])
        for command in row["commands"]:
            info = commands.get(command)
            if info is None:
                effects.add("unresolved")
                evidence.append(f"command {command} is not in plugin.gd")
                continue
            effects.update(e for e in info["effects"] if e != "read")
            disk = disk or bool(info.get("disk"))
            evidence.extend(info.get("evidence") or [])
        if effects - {"read"}:
            effects.discard("read")
        if not effects:
            effects.add("read")
        side: set[str] = set()
        for command in row["commands"]:
            side.update((commands.get(command) or {}).get("side_effects") or [])
        by_tool[key] = {
            "effects": sorted(effects),
            "disk": disk,
            "commands": row["commands"],
            "evidence": evidence,
        }
        if side:
            by_tool[key]["side_effects"] = sorted(side)

    # batch_execute runs whatever plugin commands the caller lists.
    if "batch_execute" in commands:
        commands["batch_execute"]["dynamic"] = True
        commands["batch_execute"]["effects"] = ["dynamic"]
    if "batch_execute" in by_tool:
        by_tool["batch_execute"]["effects"] = ["dynamic"]
        by_tool["batch_execute"]["dynamic"] = True

    # Bridge-local operations. They are not plugin commands. The evidence is
    # the bridge function that runs them. family_hint is removed before the file is written.
    # disk_read is only the four tools that read project files and do not ask the editor.
    # play, export and the two test runners are the envelope: they run the project.
    local = {
        "@local.play_session": {
            "effects": ["run_code"],
            "evidence": ["bridge/game_editor_bridge.py:play_session"],
            "commands": [],
            "family_hint": "envelope",
        },
        "@local.export_game": {
            "effects": ["run_code", "write"],
            "evidence": ["bridge/game_editor_bridge.py:export_game"],
            "commands": [],
            "family_hint": "envelope",
        },
        "@cli.gut": {
            "effects": ["run_code"],
            "evidence": ["bridge/game_editor_bridge.py:run_cli_tests"],
            "commands": [],
            "family_hint": "envelope",
        },
        "@cli.gdunit4": {
            "effects": ["run_code"],
            "evidence": ["bridge/game_editor_bridge.py:run_cli_tests"],
            "commands": [],
            "family_hint": "envelope",
        },
        "@local.scene_look": {
            "effects": ["read"],
            "disk": False,
            "evidence": ["bridge/game_editor_bridge.py:disk_read"],
            "commands": [],
            "family_hint": "disk_read",
        },
        "@local.script_look": {
            "effects": ["read"],
            "disk": False,
            "evidence": ["bridge/game_editor_bridge.py:disk_read"],
            "commands": [],
            "family_hint": "disk_read",
        },
        "@local.file_look": {
            "effects": ["read"],
            "disk": False,
            "evidence": ["bridge/game_editor_bridge.py:disk_read"],
            "commands": [],
            "family_hint": "disk_read",
        },
        "@local.project_look": {
            "effects": ["read"],
            "disk": False,
            "evidence": ["bridge/game_editor_bridge.py:disk_read"],
            "commands": [],
            "family_hint": "disk_read",
        },
    }
    by_tool.update(local)

    def assign_family(row: dict) -> str:
        """One of the four families. Taken from effects and side_effects already extracted.
        A write, a delete, a dynamic command or a disk write is a mutation. Playing, stopping,
        reimporting or evaluating code is an envelope. Anything else that reaches the editor,
        including an unresolved command, is editor contact. disk_read is only the hinted local tools."""
        hinted = row.pop("family_hint", None)
        if hinted in ("disk_read", "envelope"):
            return hinted
        effects = set(row.get("effects") or [])
        side = set(row.get("side_effects") or [])
        if effects & {"write", "delete", "dynamic"} or row.get("disk"):
            return "mutation"
        if side & {"plays_game", "stops_game", "reimports", "evaluates_code"}:
            return "envelope"
        return "editor_contact"

    for table in (commands, by_tool):
        for row in table.values():
            row["family"] = assign_family(row)

    payload = {
        "upstream": "godot-ai==4.3.0",
        "source": "godot-ai 4.3.0 plugin and Python tools",
        "by_command": commands,
        "by_tool": by_tool,
    }
    text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
    if args.check:
        same = out_path.is_file() and out_path.read_text(encoding="utf-8") == text
        print(f"{'unchanged' if same else 'WOULD CHANGE'}: {out_path}")
        return 0 if same else 1
    out_path.write_text(text, encoding="utf-8")
    unresolved_commands = [k for k, v in commands.items() if "unresolved" in v["effects"]]
    unresolved_tools = [k for k, v in by_tool.items() if "unresolved" in v["effects"]]
    print(f"commands {len(commands)} unresolved {len(unresolved_commands)}")
    counts: dict[str, int] = {}
    for info in commands.values():
        for tag in info.get("side_effects") or []:
            counts[tag] = counts.get(tag, 0) + 1
    with_side = sum(1 for info in commands.values() if info.get("side_effects"))
    print(f"side effects: {with_side} of {len(commands)} commands; " +
          ", ".join(f"{tag} {n}" for tag, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))))
    print(f"tools {len(by_tool)} unresolved {len(unresolved_tools)}")
    if unresolved_commands:
        print("unresolved commands:", ", ".join(sorted(unresolved_commands)[:40]))
    if unresolved_tools:
        print("unresolved tools:", ", ".join(sorted(unresolved_tools)[:60]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
