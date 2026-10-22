"""Offline checks for the package files. Standard library only."""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Words that must not appear in the published tree (split so this file does not match itself).
# Compared without case. The lower-case folder name below is compared as written, because the
# publisher's name is spelled with capitals in the author field and the license.
CASE_SENSITIVE = ("takeshi" + "tgal",)
INTERNAL = (
    "ship" + "/",
    ".har" + "ness",
    "this " + "book",
    "この" + "本",
    "関" + "門",
    "監査" + "役",
    "執行" + "部",
    "作り" + "手",
    "試験" + "台",
    "cursor-" + "agent",
    "sessions-" + "kit",
    "fixtures/" + "proj",
)


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def _skill_text() -> str:
    return (ROOT / "skills" / "game-editor" / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        raise AssertionError("skill frontmatter missing")
    return text.split("---\n", 2)[1]


class ManifestTest(unittest.TestCase):
    def test_mcp_runs_the_bridge_on_pinned_godot_ai(self):
        server = _load("mcp.json")["mcpServers"]["game-editor"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "uvx")
        args = server["args"]
        self.assertEqual(args[-5:], ["--from", "godot-ai==4.3.0", "python", "-I",
                                     "${PLUGIN_ROOT}/bridge/game_editor_bridge.py"])
        for flag in ("--isolated", "--no-config", "--no-env-file", "--no-sources", "--no-build"):
            self.assertIn(flag, args)
        self.assertNotIn("--exclude-domains", args)
        self.assertEqual(server["env"], {"GODOT_AI_DISABLE_TELEMETRY": "true", "FASTMCP_CHECK_FOR_UPDATES": "off"})
        self.assertEqual(server["cwd"], "${PLUGIN_DATA}/work")
        self.assertTrue((ROOT / "bridge" / "game_editor_bridge.py").is_file())

    def test_plugin_trust_and_app(self):
        plugin = _load("plugin.json")
        self.assertEqual(plugin["name"], "game-editor")
        self.assertEqual(plugin["version"], "1.2.3")
        # Hermes v0.22.2 validate warns "ignored unknown top-level field: requires_hermes"; the catalog entry keeps it.
        self.assertNotIn("requires_hermes", plugin)
        self.assertEqual(plugin["license"], "MIT")
        server = plugin["extensions"]["com.nousresearch.hermes"]["servers"]["game-editor"]
        self.assertEqual(server["trust"], "untrusted")
        self.assertEqual(list(server["app"]), ["darwin"])
        darwin = server["app"]["darwin"]
        self.assertEqual(darwin["presence"], "bundle")
        self.assertEqual(darwin["location"][:2], ["/Applications/Godot.app", "~/Applications/Godot.app"])
        self.assertEqual(darwin["version"]["kind"], "plist")
        self.assertTrue(server["requires"]["app"])
        self.assertEqual(server["requires"]["min_version"], "4.7")
        self.assertNotIn("python_dependencies", plugin)  # Hermes ignores it in plugin.json; pyproject.toml carries it
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dependencies = ["mcp>=2.0.0,<3"]', project)

    def test_bridge_uses_only_the_standard_library(self):
        import ast
        import sys
        tree = ast.parse((ROOT / "bridge" / "game_editor_bridge.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
        if not hasattr(sys, "stdlib_module_names"):
            self.skipTest("Python 3.10+ is needed for sys.stdlib_module_names")
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        self.assertEqual(names - stdlib, set())

    def test_skill_frontmatter_and_sections(self):
        text = _skill_text()
        body = _frontmatter(text)
        self.assertNotIn("requires_toolsets", body)
        self.assertNotIn("requires_tools", body)
        desc = ""
        for line in body.splitlines():
            if line.startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip('"')
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("Godot", desc[:60])
        self.assertIn('\n  tags: "', body)
        self.assertNotIn("\n  hermes:", body)
        for heading in ("## When to use", "## Search", "## Procedure", "## Approval", "## When a call fails",
                        "## Limits"):
            self.assertIn(heading, text)
        self.assertIn("Configure", text.split("## Limits", 1)[1])

    def test_skill_names_every_tool(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("b", ROOT / "bridge" / "game_editor_bridge.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = _skill_text()
        published = [tool["name"] for tool in module.tool_definitions()]
        for name in published:
            self.assertIn("mcp__game_editor__" + name, text)
        for name in module.PLAN_TOOLS:
            # Step names are written plainly; only published tools carry the mcp__ prefix.
            self.assertIn("`" + name + "`", text)
            if name not in published:
                self.assertNotIn("mcp__game_editor__" + name, text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in list(module.TOOLS) + published:
            self.assertIn("`" + name + "`", readme)

    def test_public_tree_has_no_internal_words(self):
        hits = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".md", ".json", ".toml", ".yaml", ".yml", ".txt"} and path.name != "LICENSE":
                continue
            raw = path.read_text(encoding="utf-8", errors="ignore")
            text = raw.lower()
            for word in INTERNAL:
                if word.lower() in text:
                    hits.append(path.name + ":" + word)
            for word in CASE_SENSITIVE:
                if word in raw:
                    hits.append(path.name + ":" + word)
        self.assertEqual(hits, [])

    def test_no_local_paths(self):
        hits = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search("/Us" + "ers/|/private/" + "tmp|/home/[a-z]", text):
                hits.append(path.name)
        self.assertEqual(hits, [])

    def test_hermes_source_when_present(self):
        src = os.environ.get("HERMES_SRC")
        if not src:
            self.skipTest("HERMES_SRC is not set")
        agent = Path(src) / "hermes_cli" / "agent_plugins.py"
        if not agent.is_file():
            self.skipTest("agent_plugins.py is not in HERMES_SRC")
        text = agent.read_text(encoding="utf-8", errors="replace")
        self.assertIn("_server_trust", text)
        self.assertIn("untrusted", text)


if __name__ == "__main__":
    unittest.main()
