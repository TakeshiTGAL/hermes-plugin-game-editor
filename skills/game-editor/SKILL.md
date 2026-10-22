---
name: game-editor
description: "Work on the Godot 4 project open in the editor. Build, playtest, fix and export, one plan and one approval at a time, and put GameForge assets into the open scene."
version: 1.2.3
author: TakeshiTGAL
license: MIT
platforms: [macos]
metadata:
  tags: "godot, game-engine, gamedev, gdscript, scene, prefab, signal, animation, tilemap, sprite, audio, gameforge, gut, gdunit4, mcp, playtest, export, web, android, ios"
---

# Game editor (Godot 4)

## When to use

The user's real project is open in the Godot 4 editor with the Godot AI add-on enabled, and they want to look at it, change it, play it, fix it or export it. That includes sprite sheets, tile sets, sound effects and music loops made by GameForge (or any files in the same formats) that should go into the open scene. Answer in the user's language; keep `tool_search` queries in English.

## Search

`tool_search` matches English words only. Put `godot` in the query: `godot scene`, `godot add node`, `godot prefab instance`, `godot script`, `godot signal`, `godot animation`, `godot ui label`, `godot input map`, `godot tilemap`, `godot sprite sheet`, `godot sound`, `godot music`, `godot gameforge assets`, `godot run game`, `godot playtest`, `godot tests`, `godot logs`, `godot export web`.

Tools:

- Read from disk, no approval: `mcp__game_editor__scene_look`, `mcp__game_editor__script_look`, `mcp__game_editor__file_look`, `mcp__game_editor__project_look`, and `mcp__game_editor__change_guide` (local help for plan steps). `scene_look`, `script_look`, `file_look` and `project_look` read the project files. `editor_look` and `asset_look` ask the editor, so they are plan steps.
- Change: `mcp__game_editor__plan_change` (writes nothing), `mcp__game_editor__apply_plan` (Hermes asks first) and `mcp__game_editor__confirm_risky_plan` (the second approval, only for a plan that deletes, overwrites, discards unsaved work, writes outside the project or runs code written in it).
- GameForge's playtest report: `file_look` op `gameforge_report` (no approval).

A plan step's `tool` is `editor_do`, `scene_do`, `script_do`, `file_do`, `project_do`, `asset_do`, `game_do`, `checks`, `batch`, `ship_build` or `place_assets` (also `scene_look` or `asset_look` when a read was refused because of a @tool script). These are step names, not tools; Hermes does not list them.

## Procedure

The daily loop: look → plan → apply → play → fix from the file and line → save.

1. Start with `scene_look` op `scene_tree` on the scene file. It reads disk and does not ask. Unsaved nodes that exist only in the editor are not in that file. The editor's open-scene name is `editor_look` op `state`, which is a plan step because it asks the editor.
2. Read before you change: `scene_look` op `scene_tree`, `children`, `properties`, `signals`. Node paths are `/Main/Player` (scene root first), never `/root/...` in the editor. A read never runs project code: when the read would call a @tool script's `_get`, `_get_property_list`, `_validate_property` or a property getter (its base scripts and an inherited scene's root included), the read is refused and the reply names the script. Then use that read as a `plan_change` step (`scene_do` op `properties`, or `scene_look`/`asset_look` with the same op); the plan says the script runs.
3. One feature is one `plan_change`. Put every edit for it in `steps`, including the save: `{"tool": "scene_do", "op": "add_node", "type": "Node2D", "name": "Coin", "parent_path": "/Main"}`. `change_guide` with `tool` and `op` gives the arguments. Show the returned plan text to the user unchanged (it has each value now -> new, and what Godot AI does inside each step), then call `apply_plan` once with its `plan_id`. Each step is read back before the reply says it ran; `game-editor checked:` lines say what was read.
4. Building notes:
   - a reusable object (coin, enemy) is its own scene: `scene_do` op `new_scene`, its nodes and script, `save`; then place it with `add_node` and `scene_path`. A new scene and the nodes placed in it can be one plan.
   - collision: `CollisionShape2D`, then `asset_do` op `edit_resources_create` with `type` RectangleShape2D or CircleShape2D, `path` the shape node, `property` `shape`.
   - scripts: `script_do` op `create_script`, `scene_do` op `attach_script`; fix with `script_do` op `script` (`old_text` exact, `new_text`). The plan names the `.uid` Godot writes beside a new or edited `.gd` or `.cs` file. Read `diagnostics` in the reply.
   - signals: `scene_do` op `connect` (`path`, `signal`, `target`, `method`). Input: `project_do` op `input_ensure_action` and `input_ensure_binding`.
   - HUD: a CanvasLayer and a Label; set `offset_right`/`offset_bottom` or `size`, or it is 0×0.
   - a prefab's own child: the plan refuses changes Godot would not save (delete, rename, move, connect without Editable Children). Open the prefab scene instead.
5. GameForge assets: one `place_assets` step each. The plan shows every Godot AI step it becomes.
   - `{"tool": "place_assets", "op": "sprite_sheet", "sheet": "res://assets/sprites/hero.png", "parent_path": "/Main/Player", "animation": "walk_right"}` → AnimatedSprite2D with `hero_frames.tres` (or one written from `hero.json`). GameForge pixel art (32 px or smaller) gets `texture_filter` Nearest unless the project already defaults to Nearest.
   - `{"tool": "place_assets", "op": "tileset", "tileset": "res://assets/tiles/water_grass.tres", "map": ["~~~~~~", "~....~", "~~~~~~"]}` → TileMapLayer with GameForge's dual grid: `~` is water, any other character land (or a 0/1 mask); the edges get the Wang tiles and open ground gets fill variants, as in GameForge's Godot template. Without `map`, `fill` `{x, y, w, h}` fills one tile.
   - `{"tool": "place_assets", "op": "sfx", "stream": "res://assets/sfx/coin.wav", "name": "CoinSound"}` → AudioStreamPlayer on bus SFX.
   - `{"tool": "place_assets", "op": "music", "stream": "res://assets/music/theme.wav"}` → looping music on bus Music, autoplay. A file that already loops keeps its loop points.
   A `.tres` that points at another project's `res://` path (GameForge writes its own folder's paths) is rewritten to the file of the same name beside it; the plan says why. A bus the project lacks is added to its bus layout file, but the open editor only reads that file when the project opens, so Godot keeps the player on Master and the reply says it is waiting. Tell the user to reopen the project (Project > Reload Current Project); then one plan with `{"tool": "place_assets", "op": "buses"}` sets every waiting player and saves. `editor_look` lists what is waiting.
6. Save with `scene_do` op `save` in the same plan only after both sides are known. `plan_change` does not read editor state, the open scenes, the scene tree or node values. Apply `{"tool": "scene_do", "op": "compare"}` first (one approval; it reads the editor and can run `@tool` code), or put that compare before the save in the same plan. If the last compare differs, a plain save makes no plan and nobody is asked; the reply lists each difference, including a node or a script that is in the editor and not in the file. Ask the user. To keep the editor's version, plan again with `"overwrite": true` on the save step: the plan names every disk value it drops, and it needs the second approval. If compare has not run since the file changed, the save makes no plan.
7. Play: one `game_do` op `play_session` step (it plays the files on disk: save first, or `autosave: true`). On a headless editor the plan says it writes `res://ge_headless_arm.gd` and `res://ge_headless_arm.tscn`, opens that scene so `--headless` is added to the run arguments in memory, closes the scene and removes the files. `project.godot` is not left changed. A windowed editor is not given `--headless`. `not_live` means the game did not stay up with frames advancing; `ok` true still needs the goal met. `goal` is `score >= 3` or a node path; `steps` are `{"op": "action", "action": "ui_right", "seconds": 2}`, `key` or `wait`, 12 seconds in all; `watch` lists runtime paths like `/root/Main/Player`. For a score goal the plan adds the nodes that hold score to `watch`. `ok` true means the game was playing, the frame counter advanced, positions were watched, and the goal was judged and met. A script error, a parse error, a line that says `ERROR:`, a line that names a script and a line (`level.gd:7 @`), a line that says `on a null instance`, or a line that says `node not found` is a failure. When that line names a file and a line, the finding carries `file`, `line` and `code` (the text of that line). Other logged lines are `log` and do not by themselves make `ok` false. idle and frame_drop can still be listed. A missed goal stays `ok` false even when frames advanced. Each finding carries `evidence`: the step it follows, the game's frame counter, the watched nodes' state, the log lines and, when the editor gives one, a picture attached to the reply. Quote `kind`, `repro` and that evidence. `replay` in the reply is the exact step to play the same script again.
8. Fix: an `error` finding carries `file`, `line` and `code` (the line's text). Plan `script_do` op `script` with `path` that file and `old_text` that code, plus the same `play_session`, and apply once. For a GameForge playtest, `file_look` op `gameforge_report` reads its `report.json` (the newest under `res://playtests/` when `path` is left out) and returns `fix_plan.steps`: write `new_text` for each line, set the play step's goal and input, then `plan_change`.
9. Export: one `ship_build` step, op `export_game`, `platform` `web`, `macos`, `android` or `ios`. It never downloads templates or uploads; `export_templates_missing` → quote file, source and size and stop.
10. Tests: `checks` op `gut` or `gdunit4` (the plan refuses when the framework or the test folder is missing), or `godot_ai`. A pass needs `"passed": true`.

## Approval

Reads, `change_guide` and `plan_change` do not ask. Hermes asks for `apply_plan`, and its prompt names only the tool, so show the plan text first. A plan that deletes, overwrites (a save with `overwrite`, a replaced file), discards unsaved work, writes outside the project or runs code written in it (`run_game_code`, a new `@tool` script) needs two approvals: `apply_plan` runs nothing yet and returns the list of what is lost (each dropped value, each deleted node); show it, and if the user agrees call `mcp__game_editor__confirm_risky_plan` with the same `plan_id`, which Hermes asks about too. Playing and tests run the project's own code and ask once. Only the newest plan can run, for 10 minutes, once; a newer plan replaces it. If the open scene, the scene file's nodes, a named file, a file a step changes as a side effect (including a script's `.uid`), or what a save drops changes after the plan, nothing runs. Quit, revert and a reload return a plan that says unsaved work would be discarded. The plan does not list those values: `plan_change` does not read the editor. `apply_plan` lists every editor-only node, every node this session added and every unsaved value before anything is discarded, or it does not run. In cron, `-q` and other unattended sessions nobody can answer, so nothing runs. In a gateway chat, anyone who can message Hermes can ask and approve. If the user declines, say it did not run and do not do the same change another way (terminal, `patch`, `write_file`). Do not edit `.tscn`, `.gd` or `project.godot` with built-in file tools, and do not start `godot` in the terminal.

## When a call fails

Quote the first line of the reply. Do not describe values the tools did not return. Do not repeat the same failing call: three errors in a row pause these tools for about 60 seconds.

- `PLUGIN_DISCONNECTED`, or `exited with code` → open the project in Godot with the Godot AI add-on enabled, then try once.
- `plugin disabled in headless mode` → set `GODOT_AI_ALLOW_HEADLESS=1` and open the editor again.
- `PORT_OCCUPIED` → another program holds the port the reply names; ask the user to stop it. Later calls answer at once while it stays taken.
- `NODE_NOT_FOUND` → `scene_look` op `scene_tree` once, then use a path from it.
- `PROPERTY_NOT_ON_CLASS` → `editor_look` op `class` with that class once (or `scene_look` op `properties` on the node) and use an exact name.
- `old_text not found` → `script_look` op `source` once and copy the exact text, tabs included.
- `runs the @tool script` on a read → use it as a plan step, as the reply says.
- `a newer plan replaced this plan_id`, `does not know this plan_id`, `older than 10 minutes`, or `the project changed after this plan` → call `plan_change` again and show the new plan. Nothing ran.
- `already differ` or `changed on disk` → nothing ran; ask the user what to keep (see step 6).
- `what would be discarded cannot be named` → `apply_plan` did not run quit, revert or reload. Nothing was sent. The reply says which scene or node could not be named.
- `both sides` and `scene_do op compare` on a save → no plan yet. Apply compare, then plan the save. A script only in the editor is a difference, not an agreement, unless the node's base scene already has that script. A node only in the editor is a difference too, including one this session added.
- `discards unsaved work` on quit, revert or reload → the plan does not list the values yet. `apply_plan` lists them, or it does not run. A stored empty compare is not that list.
- `needs a second approval` → nothing ran yet; show the list, and call `confirm_risky_plan` only if the user agrees.
- `read back after the step` → Godot did not keep what the step asked; later steps were not sent. Quote it.
- `was not accepted` or `did not approve` → the user declined. Nothing ran. Do not retry another way.
- `part of the instanced scene`, `comes back`, `second copy`, `a cycle`, `refused to duplicate` → Godot would not keep that change on a prefab; open the prefab scene the reply names.
- `did not compile` or `Parse Error` in `diagnostics` → quote the line and offer a fix plan.
- `changed while this plan ran` on a script → the editor saved an old copy from an open script tab, or someone edited it. Quote the line, ask the user, and patch only if they want.
- `already there` → pick another node name. `plain path` → use the path from `scene_tree`.
- `not_live` → the game did not play; say so and read `editor_look` op `read_logs` with `source` `game`.
- `goal_not_checked` → the goal named no score comparison or node path; restate it as `score >= 3`.
- `goal_missed` with `score is missing` → watch the node with `var score` or the score Label.
- `stall` → the game stopped answering (an infinite loop looks like this); quote `repro`.
- `GUT is not installed` / `gdUnit4 is not installed` → ask the user to install it from the Asset Library; do not report a pass.
- `refused res://` or `outside the project` → hidden folder, key-like name, a link or a path out of the project. Do not reach it another way.
- `truncated` → ask for less (a smaller `limit` or `fields`).
- `RESOURCE_NOT_FOUND` → that scene or file is not in the project. Read `scene_look` op `scene_tree` or `file_look` once, then use a path it returns.
- `had_previous_script` → the reply does not prove the script file exists. Read `script_look` op `source` on that path. If the file is missing, plan `create_script` before attaching again.
- `Unknown op` → the reply lists the valid ops. Pick one of those.
- `Missing required argument` → the reply names the argument. Send it and call once more.
- `Path must start with res://` → send a `res://` path inside the project.
- `path traversal not allowed` → the path contains `..`. Use a path from `scene_tree` or `file_look`.
- `EDITOR_NOT_READY` → the 2D viewport drew nothing. Open the editor window, let it draw a frame, then take the screenshot once.
- `viewport_2d was used` → the 2D viewport was empty. Let the editor draw a frame, then try the screenshot once.
- `No test suites found` → do not call the run a pass. If the reply says preloaded scripts may be stale, restart the editor and run once.
- `Unknown Godot class` → use a class the reply suggests, or read `editor_look` op `class`.
- `Cannot delete the scene root` → delete a child, not the root.
- `NEW_CLIENT_SESSION_REQUIRED` → a different Godot AI backend is already running. Close that backend, then try once. Nothing was sent.
- `export_templates_missing` → install that version's export templates in the editor. This plugin does not download them. Nothing was written.
- `refused to save` → the editor and the file already differed. Apply `scene_do` op `compare`, then plan the save. Nothing was written.
- `overwrite keeps the editor` → the save kept the editor copy. Read the reply's list before another save.
- `script diagnostics` → the editor log was empty and the reply shows the last diagnostics. Quote the file and line, then plan a fix.
- `Editable Children` → that node belongs to an instanced scene. Open the prefab scene the reply names.
- `does not store` → Godot will not keep that property on the instance. Open the prefab scene the reply names and set it there.
- `not an editor-visible` → Godot does not save that property on a child of an instance. Open the prefab scene the reply names.
- `position_unwatched` → positions were not watched, so it is not a pass. Name the nodes in `watch` and play once more.
- `Nothing was run` → the step was not `action`, `key` or `wait`. Mouse, gamepad and sequence are `game_input`. Plan those separately.
- `import_etc2_astc` → enable Rendering > Textures > VRAM Compression > Import ETC2 ASTC, then export again. Nothing was exported.
- `application/bundle_identifier` → set that key in the macOS preset in `export_presets.cfg`, then export again.
- `0 results` → the class name did not match. Use an exact Godot class name from `scene_tree`.
- `inside the instance` → moving a child the instance defines leaves a second copy. Open the prefab scene the reply names.
- `old name comes back` → that name is how the instance tells the node apart. Rename it in the prefab scene the reply names.
- `does not save a connection` → Godot does not save a signal from a child of an instance. Connect it in the prefab scene the reply names.
- `key.properties` → that file is refused. Do not read it another way.
- `gradle.properties` → that file is refused. Do not read it another way.
- `brings it back` → the instance defines that connection and Godot restores it. Change it in the prefab scene the reply names.
- `sibling order` → Godot does not save sibling order for a child the instance defines. Change it in the prefab scene the reply names.
- `does not write` → `user://` is outside the project. Nothing was sent. Keep the path under `res://`.
- `a path with '..'` → the path was refused. Nothing was sent. Use a path from `scene_tree` or `file_look`.
- `contents were not read` → the scene was too large to compare, so the save did not run. Nothing was written.
- `cannot name the path` → unsaved edits would be discarded and the plan cannot name them. Apply `scene_do` op `compare` first. No plan was saved.

## Limits

It works on the project open in the editor. It does not download export templates, upload builds, sign in to stores, read or write `user://` player data, or generate art or sound (GameForge or another tool makes the files; this places them). Say that in one sentence and stop.

Never suggest pressing Configure for Hermes in the Godot AI dock, or `hermes mcp add` for godot-ai: that adds a second server, named `godot-ai`, whose tools run with no approval and none of these checks. Ask the user to remove it (Remove in the dock, or `hermes mcp remove godot-ai`).
