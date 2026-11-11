import importlib.util
from pathlib import Path
import tempfile
import sys
import tomllib
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_mcp.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("configure_mcp", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class McpConfigTests(unittest.TestCase):
    def test_registration_preserves_other_settings_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="kit-mcp-config-test-") as directory:
            path = Path(directory) / "config.toml"
            initial = 'model = "example"\n[mcp_servers.other]\ncommand = "example"\n'
            path.write_text(initial)
            result = module.configure(path, Path("/example/ai-box"), "/example/node")
            self.assertEqual(len(result["added"]), 5)
            self.assertTrue(path.read_text().startswith(initial))
            config = tomllib.loads(path.read_text())
            self.assertEqual(config["mcp_servers"]["kit-jira"]["default_tools_approval_mode"], "writes")
            self.assertNotIn("env", config["mcp_servers"]["kit-jira"])
            self.assertFalse(config["mcp_servers"]["kit-git"]["enabled"])
            before = path.read_bytes()
            self.assertEqual(module.configure(path, Path("/example/ai-box"), "/example/node")["added"], [])
            self.assertEqual(path.read_bytes(), before)

    def test_conflicting_server_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="kit-mcp-config-test-") as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[mcp_servers.kit-jira]\ncommand = "existing"\n')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                module.configure(path, Path("/example/ai-box"), "/example/node")
            self.assertEqual(path.read_bytes(), before)

    def test_context7_is_opt_in_and_existing_connections_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="kit-mcp-config-test-") as directory:
            path = Path(directory) / "config.toml"
            module.configure(path, Path("/example/ai-box"), "/example/node")
            before = path.read_bytes()
            previous = tomllib.loads(before.decode())
            self.assertNotIn("context7", previous["mcp_servers"])
            result = module.configure(path, Path("/example/ai-box"), "/example/node", with_context7=True)
            self.assertEqual(result["added"], ["context7"])
            self.assertTrue(path.read_bytes().startswith(before))
            config = tomllib.loads(path.read_text())
            context7 = config["mcp_servers"].pop("context7")
            self.assertEqual(config, previous)
            self.assertEqual(context7["url"], "https://mcp.context7.com/mcp")
            self.assertEqual(set(context7["enabled_tools"]), {"resolve-library-id", "query-docs"})
            self.assertNotIn("command", context7)
            self.assertNotIn("env", context7)
            self.assertNotIn("http_headers", context7)
            updated = path.read_bytes()
            self.assertEqual(module.configure(path, Path("/example/ai-box"), "/example/node",
                                              with_context7=True)["added"], [])
            self.assertEqual(path.read_bytes(), updated)

    def test_context7_conflict_leaves_the_whole_configuration_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="kit-mcp-config-test-") as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[mcp_servers.context7]\nurl = "https://custom.example.invalid/mcp"\n')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                module.configure(path, Path("/example/ai-box"), "/example/node", with_context7=True)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(set(tomllib.loads(path.read_text())["mcp_servers"]), {"context7"})
