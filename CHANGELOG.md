# Changelog

## 1.2.3

- A `.uid` or `.import` file the editor writes beside a file that is already on disk stays, when the new bytes are that file's shape: one `uid://` line, or an import file with a `[remap]` section. The reply names each kept path on one line. It does not matter which plan, or which person, created the file beside it. Any other file the step did not name, including a `.uid` whose bytes are not that shape or whose file is not on disk, is still put back, and that step is an error.

## 1.2.2

- A file an earlier step of the same plan already names, such as the .uid beside a script that step creates, stays when a later step writes it. A file no step names is still put back, and that step is an error.
- A failed step's reply starts with that step's number and what the step concluded. A play report stays `play_session ok=…`, with the reason and the file name, and the reply is at most 2,048 characters.
- Compare does not add an editor-only script line for a node that has no script. When that node is in the editor and in the file, the reply still says the editor and the file agree. A node that is only in the editor is still listed.

## 1.2.1

- A scene save no longer fails when the editor writes an older open script tab over a script this plan already wrote. The file stays the text the plan wrote. When the editor writes that same text, it is kept. A file this plan did not write is still put back, and the step is an error.
- That error names the file and the differing line in the first line of the reply.
- One plan holds up to 64 steps, so a level of about forty edits and a save is one plan.
- A path in a step's also list is named in the plan with the text that stays on disk. If that file cannot be read, no plan is made. After the step, that text is what remains, including when an open script tab saves an older copy. The error for an unnamed file no longer tells you to add the path to also.
- A scene save names each .tres the scene or the plan already names. Godot's rewrite of that resource is part of the save.
- A play report uses log lines from this run only. An error left in the log by an earlier run is not a finding.
- revert of the current scene lists that scene's unsaved values and runs when another scene is also open. The other scene is not opened or saved. quit still does not run while another scene cannot be named.

## 1.2.0

- `plan_change` sends Godot AI only the session list. It does not read editor state, the open scenes, the scene tree or node values.
- Quit, revert and `open_scene` with `force_reload: true` return a plan that says unsaved work would be discarded. `apply_plan` compares before anything is discarded and lists every editor-only node, every node this session added and every unsaved value, or it does not run. A stored empty compare is not that list.
- A script the node's base scene already has is not a difference. A script attached in this session and absent from the file still is.
- Any write onto a file the plan named that already exists is a replace line with both contents and the second approval. That includes a theme, a material, an environment, another saved resource, and a move's destination. `overwrite: true` is required when the operation has that key.
- A play finding is an error when the line names a script and a line (`level.gd:7 @`), says `on a null instance`, or says `node not found`. When the line names a file and a line, the finding carries `file`, `line`, `code` (that line of the file) and `next`. A game that never stays up still reads the editor log. The entry's script path and line (Godot's Output prints them as `at: GDScript::reload (res://scripts/boom.gd:3)`) are that same finding.
- `change_guide` for `place_assets` op `buses` returns the step. It does not require a path.
- A player left waiting for a bus is still recorded when a later step of the same plan fails.
- `file_do` op `move` names the `.uid` and `.import` beside the source and the destination, so those files are not put back as unnamed changes.
- After each step, a file the step did not name is put back and the step is an error that names it. A script that still differs after that is named and is not written back.
- Published tools: 8. `pyproject.toml`, `plugin.json` and this package are 1.2.0.

## 1.1.0

- `plan_change` sends Godot AI only the editor state, the open-scene list and the session list. It does not read the scene tree or node values. Values in the plan come from the scene file and from values this session already set.
- A save is planned only after `scene_do` op `compare` has been applied since the file last changed, or that compare is an earlier step of the same plan. A difference blocks a plain save. `overwrite: true` is planned only when both sides are already named. A script that is on the node in the editor and absent from the file is a difference.
- Quit, revert and `open_scene` with `force_reload: true` name the path, the field and the editor's value before they return a `plan_id`. When that value is not known, no plan is made.
- `create_script`, `edit_script` and a script `write_file` name the `.uid` written beside the `.gd` or `.cs` file. That `.uid` is part of the plan fingerprint, so the plan is not rolled back for it.
- A scene file that cannot be parsed (no file, a byte-order mark, an inherited scene, or a parse failure) makes no plan when the plan would change that open scene. No note is added to let the plan through. The fingerprint is the scene file's nodes (path, type, child count). The editor's live tree is not read while the plan is made.
- On a headless editor, `play_session` and `run_game` write `res://ge_headless_arm.gd` and `res://ge_headless_arm.tscn`, open that scene so its `@tool` script adds `--headless` to `editor/run/main_run_args` in memory, close the scene and remove the files. `project.godot` is not left changed. A windowed editor is not given `--headless`. The game is playing only when the session is live and the frame counter advances. A missed goal stays `ok` false.
- After a save, a new scene or a reload, the file sha is kept and the snapshot's `compared` stays false. That snapshot is not an agreement, so quit, revert and reload do not treat it as proof that nothing would be discarded. Those plans name a path, a field and the editor's value, or they make no plan. The editor is not read while the plan is made. The scene record does not store `compared` false, so a later save of the file this session just wrote is not refused for an empty difference list. A node this session added is still named by compare, and a later plain save of that node is not blocked by that line alone.
- A node that is in the editor and not in the file is a difference, including one this session added. An approved compare also names that node's script. The reply does not say the editor and the file agree while such a node exists.
- `play_session` `ok` is true when the game is playing, the frame counter advanced, positions were watched, and the goal was judged and met. A script error, a parse error, or a line that says `ERROR:` fails the run. Other logged lines are `log` findings and do not. A missed goal stays `ok` false.
- `bridge/effects.json` gives every command and tool one family: `disk_read` (the four file reads), `editor_contact`, `mutation` or `envelope`. The family comes from the effects and side effects already extracted.

## 1.0.0

- `@tool` is decided the way Godot 4.7.2 decides it, measured with a marker file per variant: `@tool` among the annotations at the top (after a UTF-8 BOM, a doc string, a comment, CRLF line ends, or on one line with `@icon(...)` or `@static_unload`); a `@tool` anywhere else (a parse error in Godot) is counted too. A script that `extends` a `@tool` script without `@tool` of its own does not run in the editor (measured by path, by `class_name` and with an empty body), but a `@tool` script runs the functions it inherits, so `_get`, `_get_property_list`, `_validate_property`, the revert hooks, `_to_string` and property getters are collected along the `extends` chain (a path, a relative path, `uid://`, or a `class_name` found in the project). An inherited scene's root and an instanced scene's root take the base scene's script. A script or base that cannot be read counts as `@tool` with every hook. Reads, tree reads, the comparison before a save and plan steps use this one rule. A value read of a `@tool` node with none of those hooks is allowed: it runs none of its code.
- Side effects inside each Godot AI operation are extracted from the upstream source (`side_effects` in `bridge/effects.json`: saving a scene or a resource, writing, deleting or moving a file, rescanning and reimporting, saving `project.godot`, loading resources, instancing a scene, adding nodes to the tree, setting a script, evaluating code and more; 92 of 164 commands). Each plan step lists them. Files a step changes as a side effect (`project.godot` for a setting, autoload or input change, an asset's `.import` when it is rescanned) are in the plan's fingerprint.
- Each plan step shows the value it replaces next to the new one: a node property, an audio player's bus and volume, a project setting, each changed setting of an `.import`, the buses of a bus layout, the first changed line of a replaced file. Values are read while the plan is made only from nodes whose `@tool` script would not run.
- A plan that saves the open scene compares it with its file while the plan is made. If they differ and the save has no `overwrite`, no plan is made and nobody is asked; the reply lists each difference. With `overwrite: true` the plan names every disk value it drops, the second approval lists them again, and the reply after the save names them; a difference that appears after the plan stops it. This covers a change made on disk before the session started. Control offsets, which Godot AI returns only through a set it rolls back, are compared at `apply_plan` and listed there when they differ.
- `confirm_risky_plan` (`readOnlyHint: false`) is the second approval. A plan that deletes, overwrites (a save with `overwrite` that drops something, a replaced file), discards unsaved work, writes outside the project or runs code written in it (`run_game_code`, a new or edited `@tool` script) does not run at `apply_plan`: the reply lists what is lost first, in at most 1,700 characters with control and bidirectional characters escaped, and `confirm_risky_plan` with the same `plan_id` runs it after Hermes asks again. Playing and tests run the project's own code and keep one approval. MCP elicitation is not used any more (`GAME_EDITOR_ELICIT` is gone).
- Each step is read back before the reply says it ran (`game-editor checked:`): a node added or deleted, a property's value, a written or patched file, a deleted file, the open scene after `open_scene`, a new scene file, a project setting or autoload, an audio player's bus, and after a save the nodes this plan changed in the file. A value Godot did not keep stops the plan with an error.
- GameForge pixel art (an atlas whose `meta.app` is `hermes-gameforge`, or a tile set with GameForge's layout, 32 px or smaller) gets `texture_filter` Nearest on its node, unless the project already defaults to Nearest.
- `place_assets` op `tileset` takes `map`, rows of text as in GameForge's `level.json` (`~` water, any other character land, or a 0/1 mask), and lays GameForge's dual grid with the same formula as its Godot template: the Wang edge tiles where the terrains meet and fill variants scattered over open ground, up to 64×64 cells.
- A `.tres` that points at a `res://` path this project does not have (GameForge writes its own project folder's paths) is rewritten to the file of the same name beside it; the plan says why. Only those `[ext_resource]` lines change.
- A bus the open editor had not loaded is remembered in the plugin's data folder (`pending_buses.json`). `editor_look` lists what is waiting, and after the project is opened again one `place_assets` op `buses` plan sets every waiting player and saves, then reads the bus back.
- Music that already loops keeps its loop mode and its `loop_begin`/`loop_end`; a WAV that did not loop gets Forward and keeps the loop points the user set.
- `file_look` op `gameforge_report` reads a GameForge playtest `report.json` (the newest under `res://playtests/` when `path` is left out) and returns each finding with the `res://` file, line and that line's code, and `fix_plan.steps` for `plan_change`.
- A write to a path outside the project (`/tmp/…`) is refused when the plan is made; Godot AI writes only `res://`.
- `autoload_add` of a `@tool` script says the script runs in the editor from the next time the project opens.
- A score goal in a scene the plan makes watches the nodes the plan gives a script with `var score` or a Label showing `score:`.
- The play report's final picture is attached (it was "captured without the image").
- `plan_change`, `apply_plan` and `file_look` name tilemap, playtest, UI and Label, so `tool_search` finds them for `godot tilemap`, `godot playtest` and `godot ui label`.

## 0.9.0

- Works on the scene that was open before the session. apply_plan compares the open scene with its file before the first step (the whole scene when the plan saves it), so a node the plan adds or deletes is no longer reported as a difference "before this session" and the save goes through. If the editor and the file already differed, nothing runs and the reply lists the differences; `overwrite: true` on the save step keeps the editor's version and names what is dropped.
- Only the newest plan can run, once, within 10 minutes. An older plan_id is refused with "a newer plan replaced this plan_id".
- A plan that deletes or replaces something, discards unsaved work, runs project code (playing, tests, export, a `@tool` script) or sends data lists those lines under "This plan deletes, replaces, discards or runs something"; batch commands get their own lines. Hermes's one approval of apply_plan stays the only question. The bridge can also show the whole plan again as an MCP elicitation (`GAME_EDITOR_ELICIT=1`), but that is off: in Hermes v0.22.2 the elicitation is answered on a thread without the approval panel and falls back to a plain text prompt.
- Steps of one plan see the earlier steps: a scene made or opened earlier is the scene later steps check (cycle, prefab and save checks), nodes it adds count as this session's, files it writes count as existing, and a main scene it sets is the one play_session reads.
- Reads never run a `@tool` script. Which Godot AI handlers read a node's name or values is extracted from the upstream source (`node_access` in `bridge/effects.json`); a value read is refused for a node with a `@tool` script, and a name read (the scene tree, node search, children) for a `@tool` script that defines `_get`. Such a read can be a plan step, which says the script runs. `scene_look` op `properties` and `resource` are reads again for other nodes. When the open scene has a `_get` script, plan_change does not read the tree shape and says so.
- `place_assets` plan steps put GameForge-format files into the open scene: `sprite_sheet` (AnimatedSprite2D with `<name>_frames.tres`, or a SpriteFrames written from the TexturePacker JSON), `tileset` (TileMapLayer with the TileSet .tres and filled cells), `sfx` and `music` (AudioStreamPlayer on a bus; music loops through its `.import` settings and a reimport). After the plan runs the reply reads back what Godot kept, including the bus.
- play_session: where the watched nodes start is read before the first step, so the first move is judged for idle; a score goal adds the nodes that hold score to `watch` when the plan is made; an error finding names the res:// file, the line and that line's text, for the fix plan.
- play_session findings carry evidence from the build: the step, the frame counter, the watched nodes' state, the log lines, and a picture of the game after that step (attached, up to three). `replay` is the step to play the same script again. The report is no longer an error reply (Hermes cuts those to 2,048 characters); a plan still stops after a play that was not ok.
- GUT and gdUnit4 plans are refused when the framework, the test folder or the imported class names are missing, before anyone is asked.
- Long steps (apply_plan steps, play, tests, export) send `notifications/progress` when the call carries a progressToken, and nothing otherwise.
- A Godot AI start that failed with `PORT_OCCUPIED` is answered at once while that port stays taken.
- Saving compares TileMapLayer cells (`tile_map_data`) and other stored values the editor does not list (such as AnimatedSprite2D `autoplay`) instead of refusing the save.
- The scripts the open scene uses are read before each plan. One that changes during a plan without a step writing it (the editor can write back an older copy from an open script tab when the scene is saved) is named with its first changed line; it is not written back.
- A plan that saves always compares the whole scene first, also when an earlier plan only edited it; a scene opened by the plan is compared right after it opens. A `@tool` node whose script has no `_get`-like hook is compared; one that has is named in the save step as not compared.
- Node paths with `.`, `..`, `%Name` or doubled slashes, a name already taken under the parent, a node not in the scene file for a value read, `session_id` in a plan step, and a binary bus layout are refused.
- `screenshot` plans no longer say "deletes"; labels name only what the step really deletes, runs or discards.
- `plugin.json` no longer has `requires_hermes` (Hermes v0.22.2 warned that it ignores it). `tools/extract_effects.py` runs from this repository given `--upstream` (a v4.3.0 checkout) and has `--check`.

## 0.8.0

- Nine tools: the 6 reads, `change_guide`, `plan_change` and `apply_plan`. `readOnlyHint` is true for the first eight and false for `apply_plan`, so Hermes's `trust: untrusted` gate asks once per `apply_plan`. The bridge no longer sends `elicitation/create`.
- `plan_change` returns the whole change as text (operation, target, values, files it would replace, unsaved work it would discard), a `plan_id` and a fingerprint. While it builds a plan the bridge refuses every Godot AI call except the editor state, the open scenes, the session list and the tree shape: it does not compare node values, read Control layout through an undone write, or run a `@tool` getter. Those checks run at `apply_plan`, before each step's own call.
- `apply_plan` runs only that plan, once, in order, and stops at the first error. If the open scene, the tree shape, the open scenes' files, a file a step names, or an export's neighbors changed after the plan, it sends nothing.
- `change_guide` lists the plan tools' operations, and for one operation its arguments, effects and an example step. It does not contact the editor.
- Reads are an explicit list. `scene_look` does not offer `properties` or `resource`; `asset_look` does not offer `preset_fade`, `preset_slide`, `preset_shake`, `preset_pulse` or `particle_restart` (they are plan steps labelled with what they do). `asset_look` op `nav_path` refuses `force_sync: true`. `resources/read` is allowed only for a fixed list of `godot://` forms.
- A node whose script is embedded in the scene, a `uid://` script that does not resolve, or a script the bridge cannot read counts as `@tool`: the comparison does not read its values, and a save says it was not compared. The offset note after setting `position` is skipped for such a node.
- `export_game` refuses every existing file that shares the output name (`game.js` next to `game.html`) unless `overwrite` is true, and refuses a source file beside the output even then. `editor_do` op `quit`'s plan names the open scenes whose unsaved edits would be lost.
- A plan is not cut at 3,000 characters. A plan over 256 KiB is refused whole and is not saved.
- `play_session` with a goal that is not judged (any goal without an English score comparison or a node path) is `ok` false with `goal_not_checked`; the game still plays and findings are listed.
- Ports follow the Godot AI 4.3.0 add-on: `godot_ai/v4_endpoint_ports` first, then each legacy key with its own default. A malformed override starts nothing and says so.

## 0.7.0

- Sixteen tools. Which operations ask is decided from the upstream source: a call that runs code, writes, deletes or sends data asks. Six tools only read.
- A write, a delete or a call that runs code shows the operation, the target and the values in one elicitation. Declining sends nothing. A message longer than 3,000 characters is refused and is not cut. A batch shows every command in that one elicitation, including `delete_node` and `detach_script`. A command that writes a file is refused in the batch.
- `editor_do` op `clear_logs` deletes the log buffer, op `select` replaces the selection, and op `activate_session` switches the editor session, so each asks. `screenshot` asks because the capture frees a temporary camera. Live game reads are on `game_do` because they go through the game command path. There is no separate game-read tool.
- This plugin does not read the Hermes home. A second `godot-ai` server is not detected, and writes are not refused because that server is listed. Do not press Configure for Hermes in the Godot AI dock.
- `requires_hermes` is `>=0.21.6`.

## 0.6.3

- `play_session` `ok` is true only when positions were watched and the report has no error, fall, stall or uncollected finding, and a judged goal was not missed. A fall is continued downward motion, and only when a judged goal failed: 2D down is +y, and a position that contains z is 3D where down is −y. One reversal, including a jump, is not a fall. Nodes within 40 pixels are not called uncollected. An idle names the node that moved farthest. An empty `watch` stays `position_unwatched`. A goal is judged only for an English score comparison or a `/Path`. A Japanese goal is not judged.
- `batch_execute` does not run `delete_node` or `detach_script`. Call those tools on their own so the approval button names them. The daily path is one named tool per edit.
- A persistent group on a prefab child is allowed when Editable Children is on and refused when it is off. Godot 4.7.2 writes `groups=["hurt"]` on that instance.
- A name missing from a `properties` list is refused even when `unknown_fields` is absent. A reply with neither list is still not a verdict.
- Before a write, including `set_node_property`, the bridge compares that node and its parent and records the file fingerprint. A diff this session created does not block a later save. A diff that was already on disk does. If the file bytes change after that fingerprint, save refuses with `changed after`. A save that has not touched nodes walks the tree shape up to 5,000 nodes and does not getter-read untouched nodes. `@tool` getters are not run. `@tool` matches any non-comment line, and C# `[Tool]`. An unsaved script buffer is not visible.
- `resources/read` checks `godot://script/`, `godot://shader/` and `godot://visual_shader/` with the same path rules, and refuses `godot://node/.../properties` on an `@tool` node before anything is sent.

## 0.6.2

- Tool descriptions no longer paste Godot AI's essays. `tools/list` stays under 80 KB. Each operation lists its signature. `open_scene` takes `force_reload`; true discards unsaved edits and the reply says so. To discard by name, call `revert_scene`.
- `node_properties` is a read tool and refuses a node whose script starts with `@tool`. `inspect_node` asks and then reads the same operations. 50 tools: 13 read, 21 change, 16 destructive.
- Control `position` and `size` are sent. Godot stores them as four offsets and keeps an existing size. A result near 0×0 is named after the call.
- `parse_value` reads `Transform3D`, `Rect2`, `Transform2D`, `Basis`, `AABB`, `Quaternion`, `Plane` and multiline dictionaries, including animation `keys` and `AnimationLibrary._data`. A scene those values match can save without `overwrite`.
- A sibling move, `add_to_group` and `remove_from_group` on a prefab child are refused. `delete_node` is a batch command and rolls back with the rest. The daily path is one `batch_execute`.
- Talking tools take `session_id` at the top level and forward it beside `op`. Omit it to use the active editor. A batch command's params still must not contain it.
- A read of an ordinary `user://` file is allowed and the reply says it is player data. Writes, key-like names and hidden names stay refused.
- Compare caps are 5,000 nodes. Over the cap the reply names `save_as` and says the contents were not read.
- `resources/list`, `resources/templates/list` and `resources/read` are forwarded under the same path checks. Prompts are still not offered. Secret URIs are refused.
- ClassDB drop adds `GLTFState.handle_binary_image`, `LightmapGIData.light_textures`, `RDPipelineSpecializationConstant.value` and `VisualShaderNodeVec4Constant.constant_v4`.
- `edit_script` patches the file on disk and does not update an unsaved script-editor tab. Saving that tab can overwrite the patch.

## 0.6.1

- A prefab-defined node is not reparented, including a move that stays inside the instance. Renaming it is refused. A connection from a child without Editable Children, and disconnecting a connection the prefab defines, are refused before Godot AI reports success. A save flags an editor-only node under an instance when the prefab does not define that path but does define the same name.
- Stored properties on a prefab child come from `bridge/property_usage.json` (Godot 4.7.2 ClassDB) and `node_type`. A `usage` field on the reply is ignored. `playing`, `current_animation`, and Control `position` and `size` are refused on that child. `frame_coords` and a Node3D `position`, `rotation` or `scale` are allowed, because Godot writes them as `frame` or `transform`. An `@export` the table does not list stays allowed. `@export_storage` on the defining scene's script is allowed.
- Setting `position` on a Control, or a subclass, is refused before the call. Use the four offsets or `edit_ui`.
- `play_session` judges a goal only when it names a score comparison or a node path. A miss is `goal_missed`. A fall is a vertical move. Sideways movement is named and does not cancel it.
- `read_file` refuses `key.properties` and `gradle.properties` by name. A reply for `export_presets.cfg` blanks `password` and `keystore/` values and does not change the file.
- Attach reads `godot_ai/http_port` and `godot_ai/ws_port` from the editor settings for this Godot version. The fallback is 8000 and 9500.
- While `config.yaml` lists a `godot-ai` server without `trust: untrusted`, `editor_status` says so on the first line and every write tool is refused. The check reads `HERMES_HOME` only.
- The daily path is one named tool per edit. `batch_execute` remains, and its `game-editor did:` line names each command, path and property.
- `edit_script` patches the file on disk and then writes `source_code` over the script editor buffer. godot-ai 4.3.0 does not show that buffer, so an unsaved script editor cannot be refused first.
- A one-test GUT 9.6.1 suite and a one-test gdUnit4 6.2.2 suite each passed under Godot 4.7.2 headless after `godot --headless --import`. If GUT's class names are not imported, `passed` stays false and the reply says to import. Android and iOS export were not run.

## 0.6.0

- Deleting a child that a prefab defines, or moving that child out of the instance, is refused even when Editable Children is on. The reply names the prefab scene to open. Adding a node directly under the instance root is still allowed. A nested instance uses the inner scene's Editable Children flag.
- A property that Godot will not store on a prefab child (not editor-visible, or not `PROPERTY_USAGE_STORAGE`, or `PROPERTY_USAGE_NO_INSTANCE_STATE`) is refused before the editor is told it succeeded.
- `parent_path` and `new_parent` are checked on add, reparent, layout, particles, cameras, CSG, audio and animation players. `delete_node` is not a `batch_execute` command.
- `edit_visuals` is now `edit_material`, `edit_particles`, `edit_camera` and `edit_csg`. Each asks on its own. `node_properties` asks every time, because reading a `@tool` property runs its getter.
- `set_node_property` does not compare the whole scene first. The comparison runs on the next other edit or on save, and properties this session set are not treated as a disk conflict.
- `play_session` releases each held action or key at the end of the step. An empty `watch` is `ok` false with `position_unwatched`. A fall is a large vertical move of a watched node, not a name and not a slope. Mouse, gamepad and sequence stay on `game_input`.
- `export_game` accepts `macos` (default `build/macos/Game.app`). When Godot rejects it for ETC2/ASTC or a missing bundle identifier, the reply names that setting and does not change the project. `read_file` of `export_presets.cfg` blanks `script_encryption_key` in the reply and leaves the file as it is.
- `godot-ai.lock` (836 hashes, Python 3.11) is passed to `uvx`. `mcp.json` still ends with `--from godot-ai==4.3.0 python -I` the bridge.
- 49 tools: 12 read, 21 change, 16 destructive.

## 0.5.0

- A save now compares connections, groups, subresources, packed arrays and property names that contain `/`. An unparsed value, or a group, connection or subresource the editor did not return, refuses the save instead of counting as a match. Control offsets and anchors are not in the editor property list; they are read with a write that is undone, and the save is refused if that undo does not land. Connections that belong to an instanced scene, and engine methods whose names contain `::`, are not counted as edits of this file. An overwrite save says which of those the disk side loses. Nodes that exist only in the editor count, except children inside an instanced scene.
- `run_game` is an error with `not_live` when the game helper is not live, including a headless editor. It does not say the game ran. The start error for a headless editor names `GODOT_AI_ALLOW_HEADLESS=1`.
- `create_script` and `edit_script` put the script path and line first when the file was written but did not compile, and point `resolved` at that script. `read_logs` source `editor` shows those diagnostics when the editor log is empty.
- `screenshot` retries the 2D view once when the default 3D view reports `viewport_not_3d`. Setting `position` on a Control reports a near-zero size and the current offsets.
- Replies from `save_scene`, `delete_node`, `delete_file`, `delete_file_permanently`, `delete_asset`, `batch_execute`, `run_game` and `run_game_code` start with `game-editor did:` and the target. Hermes still asks with the tool name only.
- The skill answers in the user's language, tells `tool_search` to stay in English, and places a HUD with offsets rather than `position`.

## 0.4.0

- `play_session` plays an input script in the open Godot project and reports falls, overlapping coins that do not score, script errors, stalls, frame drops and spots where a move does nothing, each with steps to reproduce. One approval. It stops a game that is already running, plays the files on disk unless `autosave` is true, and stops again. The picture is not attached.
- `export_game` writes a local Web, Android or iOS build. If that Godot version's export templates are not installed, it names the file, the source and the size and downloads nothing. It does not upload, sign in to a store, or run Gradle or altool. `serve` true serves a web build on `127.0.0.1` only. Android needs a local SDK folder with `platform-tools` and does not download one.

## 0.3.0

- 44 tools: permanent file deletion is its own tool, `delete_file_permanently`, apart from `delete_file` (which moves files to the system trash); `client_manage` status is left out, so no reading tool reports other AI clients' settings.
- Godot AI now starts on the first tool call; listing the tools comes from `bridge/tools.json`, so a session that never calls a tool does not start it or use ports 8000 and 9500.
- Prefab guard: also covers instances two levels down (Editable Children needed at each level), instances added in the session with `add_node` or `batch_execute`, nodes created inside `batch_execute`, and refuses to duplicate an instance (Godot AI saves its children twice). When it cannot tell, it refuses.
- Disk-change guard: compares each scene's nodes and saved values with the editor before the session's first change to it, refuses to save when the file changed afterwards or when they already differed (listing the differences), refuses when it cannot compare, and does not count the session's own deletions as disk changes.
- `tool_describe` shows each operation's arguments, required keys and Godot AI's own text.
- `run_tests` reports `passed` only when at least one test ran and none failed, and returns `tests_run`.
- `revert_scene` always reloads from disk.
- Large `read_file` and `read_script` requests are refused with the size instead of failing in transport.
- The skill shows how to group scene edits into one `batch_execute` for one approval; the README explains the trust setting and why it is not recommended.
- Path checks handle Windows-style paths; `data/tokens.json` and similar asset names stay readable.
- The app declaration also finds `Godot_mono.app` and the Steam install.
- README: disclosures for cached screenshots, the telemetry host, `@tool` getters run by reads, the add-on's `_mcp_game_helper` autoload, and how to pin every dependency with hashes.

## 0.2.0

- Reaches every Godot AI 4.3.0 operation except configuring other AI clients: 43 tools in three tiers named by what they do (13 read without approval; 17 change and 13 delete, overwrite, settings, project-code or editor-control tools that ask every time). `bridge/tools.json` maps each tool and operation to the Godot AI call.
- New for game work: prefab instances (`add_node` with `scene_path`), signals, animation, UI and themes, input map and autoloads, materials and shaders, particles, cameras, tile maps and grid maps, resources and collision shapes, the running game's tree and input, `revert_scene`, and `run_tests` with GUT or gdUnit4 in a headless Godot.
- Guards that Godot AI does not have: refuses changes to children of an instanced scene that Godot would not save, refuses to overwrite existing files without `overwrite: true`, refuses a save that would overwrite a change made on disk, refuses a scene that would instance itself, and `run_game` no longer saves every open scene unless `autosave: true`.
- Path checks now cover every `res://`, `uid://` (resolved) and `user://` value at any depth and after leading spaces, and a wider list of key-like file names, while Godot assets such as `credentials_screen.tscn` stay readable.
- Fixes the empty 0-result note.
- `python_dependencies` is no longer in `plugin.json`; `pyproject.toml` declares `mcp>=2.0.0,<3`.

## 0.1.0

- First release. Pins `godot-ai==4.3.0` and starts it through a small standard-library bridge that offers 25 Godot editor tools: 12 that read (`readOnlyHint: true`, no approval) and 13 that open a scene (which runs its `@tool` scripts), change the scene, write scripts, save, or run the game or its tests (each asks every time).
- Refuses, before any call, `res://` paths in hidden folders such as `.godot/`, files with key-like names (`export_credentials.cfg`, `*.keystore`, `.env`, `*.pem` and similar), and paths that lead outside the project through a symbolic link.
- Adds a one-line note when `find_nodes` or `search_files` finds nothing (exact class names; rescan for new files).
- Leaves out the upstream tools that configure other AI clients, run code inside the game, delete or move files, change startup settings, or bundle several writes into one call.
- Turns off Godot AI's usage telemetry and FastMCP's update check for the server this plugin starts.
- Declares `mcp>=2.0.0,<3` in `pyproject.toml` so Hermes chat can load the MCP client.
