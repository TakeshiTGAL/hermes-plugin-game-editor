#!/usr/bin/env python3
"""Build bridge/tools.json from the v06 surface and bridge/effects.json.

Each public op keeps the old tool as guard and the old op as guard_op.
A pure read stays on a look tool. Anything that runs code, writes, deletes
or sends, and the inspect ops that exist for @tool nodes, go on a do tool.
custom_manage.invoke stays out: its body is not in the upstream source.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EFFECTS = json.loads((ROOT / "bridge" / "effects.json").read_text(encoding="utf-8"))
BY_TOOL = EFFECTS["by_tool"]

# Old tools, in the order a person meets them. Look and do are split by effects.
GROUPS = [
    ("editor", ["editor_status", "read_logs", "screenshot", "class_reference", "editor_control"]),
    ("scene", ["scene_tree", "find_nodes", "node_properties", "inspect_node", "open_scene", "add_node",
               "set_node_property", "edit_node", "delete_node", "save_scene", "new_scene", "revert_scene",
               "connect_signal"]),
    ("script", ["read_script", "create_script", "edit_script"]),
    ("file", ["read_file", "search_files", "write_file", "delete_file", "delete_file_permanently",
              "move_file", "import_files"]),
    ("project", ["project_setting", "edit_project_settings", "run_game", "stop_game"]),
    ("asset", ["read_assets", "edit_animation", "edit_ui", "edit_material", "edit_particles", "edit_camera",
               "edit_csg", "edit_level", "edit_resources", "delete_asset"]),
    ("game", ["read_game", "game_input", "run_game_code", "play_session"]),
    ("checks", ["run_tests"]),
    ("batch", ["batch_execute"]),
    ("ship", ["export_game"]),
]

# These bundles are one tool, not a look/do pair.
SOLO = {"checks": "checks", "batch": "batch", "ship": "ship_build"}

FIRST = {
    "editor_look": "Godot editor state, logs and class info.",
    "editor_do": "Quit, reload, select or screenshot in the Godot editor.",
    "scene_look": "Godot scene tree, node properties and node search.",
    "scene_do": "Edit the open Godot scene: nodes, signals and save.",
    "script_look": "Read a Godot GDScript file and its outline.",
    "script_do": "Write or patch a Godot GDScript or shader file.",
    "file_look": "Read or search files in the open Godot project.",
    "file_do": "Write, move, delete or reimport a Godot project file.",
    "project_look": "Read Godot project settings, input map and autoloads.",
    "project_do": "Change Godot project settings or run and stop the game.",
    "asset_look": "Read Godot materials, animations, cameras and tiles.",
    "asset_do": "Edit Godot materials, UI, animation, tiles and resources.",
    "game_look": "Read the running Godot game's nodes and input state.",
    "game_do": "Play a Godot game, send input, or run GDScript in it.",
    "checks": "Run Godot GUT, gdUnit4 or Godot AI test suites.",
    "batch": "Apply several Godot scene edits in one approval.",
    "ship_build": "Export a local Godot build for macOS, Web, Android or iOS.",
}

LOOK_DEFAULT = {
    "editor_look": "state",
    "scene_look": "scene_tree",
    "script_look": "source",
    "file_look": "text",
    "project_look": "get",
    "game_look": "scene_tree",
}

DANGER = {"run_code", "write", "delete", "send", "unresolved"}
ASK = "Asks the user for approval every time."


def effect_key(old_tool: str, spec: dict) -> str | None:
    up, uop = spec["upstream"], spec.get("op")
    if up == "@local":
        if old_tool == "play_session":
            return "@local.play_session"
        if old_tool == "export_game":
            return "@local.export_game"
        return None
    if up == "@cli":
        return f"@cli.{uop}"
    if uop:
        return f"{up}.{uop}"
    return up


def effects_of(key: str) -> set[str]:
    row = BY_TOOL.get(key)
    if row is None:
        return {"unresolved"}
    return set(row.get("effects") or [])


def load_v06() -> dict:
    raw = subprocess.check_output(
        ["git", "show", "v06-20261020:bridge/tools.json"], cwd=ROOT, text=True
    )
    return json.loads(raw)


def public_name(group: str, do: bool) -> str:
    if group in SOLO:
        return SOLO[group]
    return f"{group}_{'do' if do else 'look'}"


def build() -> tuple[dict, dict]:
    old = load_v06()
    by_name = {tool["name"]: tool for tool in old["tools"]}
    # (group, do, old_tool, old_op, spec, effects)
    rows = []
    missing = []
    for group, names in GROUPS:
        for old_tool in names:
            tool = by_name[old_tool]
            single = len(tool["ops"]) == 1
            for old_op, spec in tool["ops"].items():
                key = effect_key(old_tool, spec)
                effects = effects_of(key) if key else {"unresolved"}
                if "unresolved" in effects or key is None:
                    missing.append((old_tool, old_op, key, sorted(effects)))
                    continue
                do = bool(effects & DANGER) or old_tool == "inspect_node" or group in SOLO
                copied = dict(spec)
                copied["guard"] = old_tool
                copied["guard_op"] = old_op
                copied["effect_key"] = key
                if old_tool == "inspect_node":
                    copied["elicit"] = True
                rows.append({
                    "group": group,
                    "do": do,
                    "old_tool": old_tool,
                    "old_op": old_op,
                    "single": single,
                    "spec": copied,
                    "effects": effects,
                    "props": tool.get("properties") or {},
                })
    if missing:
        raise SystemExit("unclassified ops:\n" + "\n".join(str(m) for m in missing))

    # Public op names. One-op tools use the old tool name. Collisions get a prefix.
    buckets: dict[str, list] = {}
    for row in rows:
        buckets.setdefault(public_name(row["group"], row["do"]), []).append(row)
    call_map = {}
    tools = []
    for name, members in buckets.items():
        used: set[str] = set()
        ops = {}
        props: dict = {}
        effects: set[str] = set()
        for row in members:
            if row["single"]:
                public_op = row["old_tool"]
            else:
                public_op = row["old_op"]
            if public_op in used:
                public_op = f"{row['old_tool']}_{row['old_op']}"
            if public_op in used:
                raise SystemExit(f"op collision on {name}: {public_op}")
            used.add(public_op)
            ops[public_op] = row["spec"]
            effects |= row["effects"]
            call_map[f"{row['old_tool']}\t{row['old_op']}"] = {"tool": name, "op": public_op}
            for key, schema in row["props"].items():
                if key in ("op", "session_id"):
                    continue
                if key not in props:
                    props[key] = json.loads(json.dumps(schema))
        if "delete" in effects or name == "batch":
            tier = "destructive"
        elif effects & DANGER or any(spec.get("elicit") for spec in ops.values()):
            tier = "change"
        else:
            tier = "read"
        op_list = ", ".join(ops)
        first = FIRST[name]
        if len(first) > 60 or "Godot" not in first:
            raise SystemExit(f"bad first sentence for {name}: {first!r}")
        if tier == "read":
            desc = f"{first} Does not change the project. Ops: {op_list}."
        else:
            desc = f"{first} {ASK} Ops: {op_list}."
        if len(desc) > 1024:
            if tier == "read":
                desc = f"{first} Does not change the project."
            else:
                desc = f"{first} {ASK}"
        default = None
        required: list[str] = []
        if tier == "read":
            want = LOOK_DEFAULT.get(name)
            default = want if want in ops else next(iter(ops))
        else:
            required = ["op"]
        props = {"op": {"type": "string", "enum": list(ops), "description": f"Operation. One of: {op_list}."}, **props}
        if len(props["op"]["description"]) > 1024:
            props["op"]["description"] = "Operation name. The enum lists every op."
        # batch: keep the command catalogue phrase the tests and the model need.
        if name == "batch":
            for spec in ops.values():
                doc = spec.get("doc") or ""
                stale = ("This bridge does not run delete_node or detach_script in the batch; "
                         "call those tools on their own so the approval names the operation.")
                if stale in doc:
                    spec["doc"] = doc.replace(
                        stale,
                        "This bridge runs delete_node and detach_script in the batch and shows every "
                        "command in the one approval. A command that writes a file is refused here.",
                    )
            props["commands"]["description"] = (
                "List of {\"command\": name, \"params\": {...}} shown together in one approval. "
                "Scene edits include create_node, set_property, delete_node and detach_script. "
                "A command that writes a file (write_file and anything else that touches disk) "
                "is refused here; call file_do or script_do so the overwrite check runs. "
                "An unknown command is refused. undo true (default) rolls the scene back if one edit fails."
            )
            desc = (
                "Apply several Godot scene edits in one approval. "
                f"{ASK} The approval shows every command and its values. "
                "delete_node and detach_script are included. File writes are not."
            )
        tools.append({
            "name": name,
            "tier": tier,
            "description": desc,
            "default_op": default,
            "multi": True,
            "ops": ops,
            "properties": props,
            "required": required,
        })

    # Stable order follows GROUPS, look then do.
    order = []
    for group, _names in GROUPS:
        if group in SOLO:
            order.append(SOLO[group])
        else:
            order.append(f"{group}_look")
            order.append(f"{group}_do")
    tools.sort(key=lambda tool: order.index(tool["name"]))

    left = list(old["left_out"])
    for item in left:
        if item.get("upstream") == "editor_manage" and item.get("op") == "state":
            item["why"] = "Same handler as editor_state, which editor_look op state reaches."
    left.append({
        "upstream": "custom_manage",
        "op": "invoke",
        "why": ("The add-on tool body is not in the Godot AI source, so this plugin cannot tell whether "
                "the call runs code, writes, deletes or sends data. It is not offered."),
    })
    unique = {(spec["upstream"], spec.get("op")) for tool in tools for spec in tool["ops"].values()}
    payload = {
        "upstream": old["upstream"],
        "tools": tools,
        "left_out": left,
        "counts": {
            "tools": len(tools),
            "upstream_ops": old["counts"]["upstream_ops"],
            "upstream_single_call_tools": old["counts"]["upstream_single_call_tools"],
            "reachable": len(unique),
        },
    }
    return payload, call_map


def _dict_op(node: ast.Dict):
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "op" and isinstance(value, ast.Constant):
            return value.value
    return None


def _has_op(node: ast.Dict) -> bool:
    return any(isinstance(key, ast.Constant) and key.value == "op" for key in node.keys)


def rewrite_tests(call_map: dict, defaults: dict) -> int:
    """Point bridge.call sites at the new tool and op. Leaves other formatting."""
    path = ROOT / "tests" / "test_bridge.py"
    source = path.read_text(encoding="utf-8")
    if 'bridge.call("editor_status"' not in source and 'bridge.call("scene_tree"' not in source:
        print("tests already rewritten")
        return 0
    tree = ast.parse(source)
    edits = []
    skipped = []

    class Finder(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            self.generic_visit(node)
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "call":
                return
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                if node.args and isinstance(node.args[0], ast.Name):
                    skipped.append((node.lineno, "variable"))
                return
            old = node.args[0].value
            if old not in defaults and not any(k.startswith(old + "\t") for k in call_map):
                return
            old_op = None
            dict_node = node.args[1] if len(node.args) > 1 and isinstance(node.args[1], ast.Dict) else None
            if dict_node is not None:
                old_op = _dict_op(dict_node)
            if old_op is None:
                old_op = defaults.get(old)
            mapped = call_map.get(f"{old}\t{old_op}")
            if mapped is None:
                skipped.append((node.lineno, f"{old} op {old_op}"))
                return
            edits.append((node, old, mapped, dict_node))

    Finder().visit(tree)
    # Apply from the end of the file so slices stay valid.
    pieces = source
    # Later calls first, and within a call the dict (to the right) before the tool name,
    # so every span still matches the original line and column.
    for node, old, mapped, dict_node in sorted(edits, key=lambda item: item[0].lineno, reverse=True):
        if dict_node is not None and _has_op(dict_node):
            for key, value in zip(dict_node.keys, dict_node.values):
                if isinstance(key, ast.Constant) and key.value == "op" and isinstance(value, ast.Constant):
                    if value.value != mapped["op"]:
                        va = _offset(pieces, value.lineno, value.col_offset)
                        vb = _offset(pieces, value.end_lineno, value.end_col_offset)
                        pieces = pieces[:va] + json.dumps(mapped["op"]) + pieces[vb:]
        elif dict_node is not None:
            brace = _offset(pieces, dict_node.lineno, dict_node.col_offset)
            if pieces[brace] != "{":
                skipped.append((node.lineno, "no brace"))
            elif pieces[brace:brace + 2] == "{}":
                insert = '{"op": ' + json.dumps(mapped["op"]) + "}"
                pieces = pieces[:brace] + insert + pieces[brace + 2:]
            else:
                pieces = pieces[:brace + 1] + '"op": ' + json.dumps(mapped["op"]) + ", " + pieces[brace + 1:]
        arg = node.args[0]
        a = _offset(pieces, arg.lineno, arg.col_offset)
        b = _offset(pieces, arg.end_lineno, arg.end_col_offset)
        quoted = pieces[a:b]
        if quoted.strip("\"'") != old:
            skipped.append((node.lineno, f"span {quoted!r}"))
            continue
        pieces = pieces[:a] + json.dumps(mapped["tool"]) + pieces[b:]
    path.write_text(pieces, encoding="utf-8")
    print(f"rewrote {len(edits)} calls, skipped {len(skipped)}")
    for item in skipped:
        print(" skip", item)
    return len(edits)


def _offset(source: str, line: int, col: int) -> int:
    lines = source.splitlines(keepends=True)
    return sum(len(lines[i]) for i in range(line - 1)) + col


def main() -> None:
    payload, call_map = build()
    (ROOT / "bridge" / "tools.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (ROOT / "tools" / "call_map.json").write_text(
        json.dumps(call_map, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    old = load_v06()
    defaults = {tool["name"]: tool.get("default_op") for tool in old["tools"]}
    print(f"tools {payload['counts']['tools']} reachable {payload['counts']['reachable']} "
          f"left_out {len(payload['left_out'])}")
    for tool in payload["tools"]:
        print(f"  {tool['name']:16} {tool['tier']:12} ops {len(tool['ops'])}")
    rewrite_tests(call_map, defaults)


if __name__ == "__main__":
    main()
