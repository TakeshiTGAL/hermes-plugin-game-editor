"""A stand-in for `godot-ai attach` used by the offline tests. Standard library only.

    python fake_upstream.py <calls.jsonl> [mode]

It offers every upstream tool and op that bridge/tools.json maps to. mode:
  normal      answers editor_state, session_manage list, scene_get_hierarchy (paged), node_get_properties and
              scene_manage get_roots from FAKE_SCENE, FAKE_PROJECT_PATH, FAKE_NODES (a JSON list of node paths)
              and FAKE_PROPS ({path: {property: value}}); node_create and batch_execute return paths; echoes others
  drop_op     like normal, but script_manage has no find_symbols op
  port_busy   prints Godot AI's PORT_OCCUPIED message to stderr and exits 1
  big         every tools/call answers with 300 KB of text
  slow        tools/call never answers (except the bridge's own lookups)
Every tools/call it receives is appended to calls.jsonl.
State, so the bridge's read-back after each step sees what a real editor would: node_set_property and
audio player_set_playback values are remembered and read back by node_get_properties; node_create makes a node that
reads back; node_manage delete makes it NODE_NOT_FOUND; filesystem_manage write_text, script_create and script_patch
write the file under FAKE_PROJECT_PATH, and filesystem_manage remove deletes it. The open scene follows scene_open.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CALLS = sys.argv[1]
MODE = sys.argv[2] if len(sys.argv) > 2 else "normal"
TABLE = json.loads((Path(__file__).resolve().parents[1] / "bridge" / "tools.json").read_text())

PORT_BUSY = (
    "godot-ai attach [PORT_OCCUPIED]: Port 8000 is occupied by a process the bridge cannot adopt: "
    "listener did not answer the godot-ai status probe.\n"
)
_LAYOUT = {
    "offset_left", "offset_top", "offset_right", "offset_bottom",
    "anchor_left", "anchor_top", "anchor_right", "anchor_bottom",
}
NODE_NOT_FOUND = (
    "NODE_NOT_FOUND: Node not found: /Main/NoSuchNode. Paths are relative to the edited scene root "
    "(e.g. \"/Main/Child\"), not runtime /root/... paths. Scene root is \"/Main\"."
)


def tools() -> list[dict]:
    ops: dict[str, set] = {}
    flat: set = set()
    for tool in TABLE["tools"]:
        for spec in tool["ops"].values():
            if spec["upstream"] in ("@cli", "@local"):
                continue
            if spec["op"] is None:
                flat.add(spec["upstream"])
            else:
                ops.setdefault(spec["upstream"], set()).add(spec["op"])
    ops.setdefault("session_manage", set()).add("list")
    ops.setdefault("scene_manage", set()).add("get_roots")
    flat.add("editor_state")
    flat.add("scene_get_hierarchy")
    out = [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}} for n in sorted(flat)]
    for name, names in sorted(ops.items()):
        if MODE == "drop_op" and name == "script_manage":
            names = names - {"find_symbols"}
        out.append({"name": name, "description": name, "inputSchema": {
            "type": "object", "properties": {"op": {"enum": sorted(names), "type": "string"}}, "required": ["op"]}})
    return out


def reply(mid, result) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def text(mid, payload, is_error=False) -> None:
    reply(mid, {"content": [{"type": "text", "text": payload if isinstance(payload, str) else json.dumps(payload)}],
                "isError": is_error})


STATE: dict = {"props": None, "made": set(), "gone": set(), "scene": None}


def _disk(res: str) -> Path | None:
    root = os.environ.get("FAKE_PROJECT_PATH", "")
    if not root or not isinstance(res, str) or not res.startswith("res://") or ".." in res:
        return None
    return Path(root) / res[len("res://"):]


def _props() -> dict:
    if STATE["props"] is None:
        STATE["props"] = json.loads(os.environ.get("FAKE_PROPS", "{}"))
    return STATE["props"]


def main() -> int:
    if MODE == "port_busy":
        sys.stderr.write(PORT_BUSY)
        sys.stderr.flush()
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            reply(mid, {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-godot-ai", "version": "4.3.0"}})
        elif method == "tools/list":
            reply(mid, {"tools": tools()})
        elif method == "resources/list":
            reply(mid, {"resources": [{"uri": "godot://editor/state", "name": "editor state"}]})
        elif method == "resources/templates/list":
            reply(mid, {"resourceTemplates": []})
        elif method == "resources/read":
            uri = (msg.get("params") or {}).get("uri", "")
            reply(mid, {"contents": [{"uri": uri, "text": "ok"}]})
        elif method == "tools/call":
            params = msg["params"]
            name, args = params["name"], params.get("arguments") or {}
            with open(CALLS, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(params) + "\n")
            if name == "session_manage":
                root = os.environ.get("FAKE_PROJECT_PATH", "")
                scene = STATE["scene"] or os.environ.get("FAKE_SCENE", "")
                text(mid, {"sessions": [{"project_path": root, "is_active": True, "current_scene": scene}] if root else []})
            elif name == "editor_state":
                text(mid, {"current_scene": STATE["scene"] or os.environ.get("FAKE_SCENE", "")})
            elif name == "node_get_properties" and args.get("path") in STATE["gone"]:
                text(mid, NODE_NOT_FOUND.replace("/Main/NoSuchNode", str(args.get("path"))), True)
            elif name == "node_get_properties" and args.get("path") in STATE["made"] and args.get("path") not in _props():
                fields = args.get("fields") or []
                text(mid, {"properties": [{"name": key, "value": (str(args["path"]).rsplit("/", 1)[-1]
                                                                  if key == "name" else None)} for key in fields]})
            elif name == "scene_get_hierarchy" and MODE != "big":
                nodes = json.loads(os.environ.get("FAKE_NODES", "[]"))
                nodes = [n for n in nodes if n not in STATE["gone"]] + sorted(
                    n for n in STATE["made"] - STATE["gone"] if n not in nodes)
                off, lim = int(args.get("offset", 0)), int(args.get("limit", 100))
                page = nodes[off:off + lim]
                text(mid, {"nodes": [{"path": n} for n in page], "has_more": off + lim < len(nodes),
                           "total_count": len(nodes)})
            elif name == "node_get_properties" and args.get("path") in _props():
                values = _props()[args["path"]]
                fields = args.get("fields") or []
                listed = []
                unknown = []
                layout_on = bool(os.environ.get("FAKE_LAYOUT"))
                for key in fields:
                    if key in values:
                        listed.append({"name": key, "value": values[key]})
                    elif layout_on and key in _LAYOUT:
                        unknown.append(key)
                    else:
                        # The editor lists a real property even when the scene file does not override it.
                        listed.append({"name": key, "value": None})
                payload = {"properties": listed}
                node_type = json.loads(os.environ.get("FAKE_NODE_TYPES") or "{}").get(args.get("path"))
                if isinstance(node_type, str) and node_type:
                    payload["node_type"] = node_type
                if unknown:
                    payload["unknown_fields"] = unknown
                text(mid, payload)
            elif name == "batch_execute" and os.environ.get("FAKE_LAYOUT"):
                layout = json.loads(os.environ["FAKE_LAYOUT"])
                results = []
                stopped = None
                for index, item in enumerate(args.get("commands") or []):
                    prm = item.get("params") or {}
                    if not prm.get("property"):
                        results.append({"command": item.get("command"), "status": "error", "error": {
                            "code": "MISSING_REQUIRED_PARAM", "message": "Missing required param: property"}})
                        stopped = index
                        break
                    path = prm.get("path") or ""
                    prop = prm.get("property")
                    old = (layout.get(path) or {}).get(prop, prm.get("value"))
                    results.append({"command": item.get("command"), "status": "ok", "data": {
                        "path": path, "property": prop, "old_value": old, "value": prm.get("value"), "undoable": True}})
                text(mid, {"results": results, "rolled_back": stopped is not None, "stopped_at": stopped,
                           "undo": bool(args.get("undo", True))})
            elif name == "node_create":
                parent = args.get("parent_path") or "/Main"
                made = parent + "/" + (args.get("name") or "Node")
                STATE["made"].add(made)
                STATE["gone"].discard(made)
                text(mid, {"path": made, "scene_path": args.get("scene_path", "")})
            elif name == "node_set_property" and isinstance(args.get("path"), str):
                _props().setdefault(args["path"], {})[str(args.get("property"))] = args.get("value")
                text(mid, {"echo": params})
            elif name == "audio_manage" and args.get("op") == "player_create":
                prm = args.get("params") or {}
                made = (prm.get("parent_path") or "/Main").rstrip("/") + "/" + (prm.get("name") or "AudioStreamPlayer")
                STATE["made"].add(made)
                text(mid, {"path": made, "undoable": True})
            elif name == "audio_manage" and args.get("op") == "player_set_playback":
                prm = args.get("params") or {}
                bucket = _props().setdefault(str(prm.get("player_path")), {})
                for key in ("bus", "volume_db", "autoplay", "pitch_scale"):
                    if key in prm:
                        bucket[key] = prm[key]
                text(mid, {"echo": params})
            elif name == "node_manage" and args.get("op") == "delete":
                gone = str((args.get("params") or {}).get("path") or "")
                STATE["gone"].add(gone)
                text(mid, {"path": gone, "undoable": True})
            elif (name == "script_create" and "func broken(" not in str(args.get("content") or "")) or (
                    name == "filesystem_manage" and args.get("op") == "write_text"):
                prm = (args.get("params") if name == "filesystem_manage" else args) or {}
                target = _disk(prm.get("path"))
                if target is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(str(prm.get("content") or ""), encoding="utf-8")
                text(mid, {"path": prm.get("path"), "size": len(str(prm.get("content") or ""))})
            elif name == "script_patch":
                target = _disk(args.get("path"))
                if target is not None and target.is_file() and isinstance(args.get("old_text"), str):
                    body = target.read_text(encoding="utf-8")
                    if args["old_text"] in body:
                        target.write_text(body.replace(args["old_text"], str(args.get("new_text") or ""), 1),
                                          encoding="utf-8")
                text(mid, {"echo": params})
            elif name == "filesystem_manage" and args.get("op") == "remove":
                target = _disk((args.get("params") or {}).get("path"))
                if target is not None and target.is_file():
                    target.unlink()
                text(mid, {"echo": params})
            elif name == "scene_save" and _disk(STATE["scene"] or os.environ.get("FAKE_SCENE", "")) is not None \
                    and _disk(STATE["scene"] or os.environ.get("FAKE_SCENE", "")).is_file():
                # What the editor's save writes, roughly: the nodes made here are added, deleted ones removed.
                target = _disk(STATE["scene"] or os.environ.get("FAKE_SCENE", ""))
                body = target.read_text(encoding="utf-8")
                for made in sorted(STATE["made"] - STATE["gone"]):
                    parts = [p for p in made.split("/") if p]
                    if len(parts) < 2:
                        continue
                    parent = "." if len(parts) == 2 else "/".join(parts[1:-1])
                    head = f'[node name="{parts[-1]}" type="Node" parent="{parent}"]'
                    if head not in body:
                        body = body.rstrip("\n") + "\n\n" + head + "\n"
                for gone in STATE["gone"]:
                    parts = [p for p in gone.split("/") if p]
                    if len(parts) >= 2:
                        parent = "." if len(parts) == 2 else "/".join(parts[1:-1])
                        body = body.replace(f'[node name="{parts[-1]}" type="Node" parent="{parent}"]\n', "")
                target.write_text(body, encoding="utf-8")
                # The open script tab can save an older copy while the scene is saved.
                for res, content in json.loads(os.environ.get("FAKE_SAVE_CLOBBER") or "{}").items():
                    script = _disk(res)
                    if script is not None and isinstance(content, str):
                        script.parent.mkdir(parents=True, exist_ok=True)
                        script.write_text(content, encoding="utf-8")
                text(mid, {"path": STATE["scene"] or os.environ.get("FAKE_SCENE", ""), "undoable": False})
            elif name == "scene_open":
                STATE["scene"] = args.get("path") or STATE["scene"]
                text(mid, {"path": args.get("path"), "switched": True})
            elif name == "batch_execute":
                results = []
                for item in args.get("commands") or []:
                    prm = item.get("params") or {}
                    data = {"path": (prm.get("parent_path") or "/Main") + "/" + (prm.get("name") or "Node")}
                    results.append({"command": item.get("command"), "data": data, "status": "ok"})
                text(mid, {"results": results})
            elif name == "scene_manage" and args.get("op") == "get_roots":
                raw = os.environ.get("FAKE_OPEN_SCENES")
                scenes = [{"path": path} for path in json.loads(raw)] if raw else []
                text(mid, {"scenes": scenes})
            elif MODE == "slow":
                continue
            elif MODE == "big":
                text(mid, "x" * 300_000)
            elif name == "node_manage" and args.get("op") == "get_groups":
                path = (args.get("params") or {}).get("path", "")
                groups = json.loads(os.environ.get("FAKE_GROUPS") or "{}")
                text(mid, {"groups": groups.get(path, [])})
            elif name == "signal_manage" and args.get("op") == "list":
                path = (args.get("params") or {}).get("path", "")
                signals = json.loads(os.environ.get("FAKE_SIGNALS") or "{}")
                text(mid, {"connections": signals.get(path, [])})
            elif name == "resource_manage" and args.get("op") == "inspect":
                params = args.get("params") or {}
                key = f"{params.get('node_path', '')} {params.get('property', '')}"
                resources = json.loads(os.environ.get("FAKE_RESOURCES") or "{}")
                text(mid, resources.get(key) or {"type": "", "properties": {}})
            elif name == "project_run" and os.environ.get("FAKE_PLAY_TOUCH"):
                # A later step of a plan can write a file the running game creates.
                # A string path is a script. An object {"path", "text"} writes those bytes.
                for res in json.loads(os.environ["FAKE_PLAY_TOUCH"]):
                    if isinstance(res, dict):
                        path = res.get("path")
                        body = res.get("text") if isinstance(res.get("text"), str) else ""
                    else:
                        path, body = res, "extends Node\n"
                    target = _disk(path if isinstance(path, str) and path.startswith("res://")
                                   else "res://" + str(path).lstrip("/"))
                    if target is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(body, encoding="utf-8")
                if os.environ.get("FAKE_GAME_STATUS"):
                    status = os.environ["FAKE_GAME_STATUS"]
                    live = status == "live"
                    text(mid, {"helper_live": live, "game_status": {"status": status, "helper_live": live}})
                else:
                    text(mid, "game did not start", True)
            elif name == "project_run" and os.environ.get("FAKE_GAME_STATUS"):
                status = os.environ["FAKE_GAME_STATUS"]
                live = status == "live"
                text(mid, {"helper_live": live, "game_status": {"status": status, "helper_live": live}})
            elif name == "editor_screenshot" and os.environ.get("FAKE_SCREENSHOT") == "2d":
                source = args.get("source") or "viewport"
                if source == "viewport":
                    text(mid, {"error": "EDITOR_VIEWPORT_NOT_3D",
                               "data": {"editor_state": "viewport_not_3d"}}, True)
                elif source == "viewport_2d" and os.environ.get("FAKE_SCREENSHOT_2D") == "empty":
                    text(mid, {"error": "EDITOR_VIEWPORT_EMPTY",
                               "data": {"editor_state": "viewport_empty"}}, True)
                else:
                    text(mid, {"ok": True, "source": source})
            elif name == "script_create" and "func broken(" in str(args.get("content") or ""):
                path = args.get("path") or "res://bad.gd"
                target = _disk(path)
                if target is not None:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(str(args.get("content") or ""), encoding="utf-8")
                text(mid, {"committed": True, "path": path, "diagnostics_status": "checked", "diagnostics": [{
                    "path": path, "line": 2,
                    "text": 'Parse Error: Expected closing ")" after function parameters.',
                    "resolved": {"path": "res://addons/godot_ai/handlers/script_handler.gd", "line": 375},
                    "details": {"source": {"path": path, "line": 2}},
                    "frames": [{"path": "res://addons/godot_ai/handlers/script_handler.gd", "line": 375}],
                }]})
            elif name == "logs_read" and args.get("source") == "editor":
                text(mid, {"lines": [], "total_count": 0, "source": "editor"})
            elif name == "node_get_properties" and args.get("path") == "/Main/NoSuchNode":
                text(mid, NODE_NOT_FOUND, True)
            elif name == "node_get_properties":
                fields = args.get("fields") or []
                text(mid, {"properties": [{"name": key, "value": None} for key in fields]})
            elif name == "node_find":
                text(mid, {"nodes": [], "total_count": 0})
            else:
                text(mid, {"echo": params})
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": -32601, "message": "Method not found"}}) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
