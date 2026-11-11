import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("kit_launcher", SCRIPTS / "launch.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_install_preserves_configuration_is_idempotent_and_installs_working_command(self):
        with tempfile.TemporaryDirectory(prefix="kit-launch-test-") as root:
            root = Path(root)
            settings = root / "codex"
            settings.mkdir()
            base = settings / "config.toml"
            original = '[mcp_servers.other]\ncommand="fixture"\n'
            base.write_text(original)
            args = SimpleNamespace(codex_dir=settings, bin_dir=root / "bin", ai_box=launcher.permissions.ai_box(),
                                   with_command_rules=False)
            result = launcher.install(args)
            self.assertFalse(result["activated"])
            self.assertEqual(len(result["mcp_added"]), 5)
            self.assertTrue(base.read_text().startswith(original))
            before = base.read_bytes()
            self.assertEqual(launcher.install(args)["mcp_added"], [])
            self.assertEqual(base.read_bytes(), before)
            help_result = subprocess.run([str(root / "bin/kit"), "--help"], capture_output=True, text=True)
            self.assertEqual(help_result.returncode, 0)
            self.assertIn("install", help_result.stdout)
            self.assertTrue((settings / "skills/plan-feature").is_symlink())
            self.assertEqual((settings / "skills/feature-hub").resolve(), launcher.kit.KIT_ROOT / "skills/feature-hub")
            self.assertEqual((settings / "skills/verify-feature").resolve(), launcher.kit.KIT_ROOT / "skills/verify-feature")
            self.assertEqual((settings / "skills/library-docs").resolve(), launcher.kit.KIT_ROOT / "skills/library-docs")
            args.with_context7 = True
            self.assertEqual(launcher.install(args)["mcp_added"], ["context7"])
            updated = base.read_bytes()
            self.assertEqual(launcher.install(args)["mcp_added"], [])
            self.assertEqual(base.read_bytes(), updated)

    def test_existing_command_is_not_overwritten_or_partially_installed(self):
        with tempfile.TemporaryDirectory(prefix="kit-launch-test-") as root:
            root = Path(root)
            (root / "bin").mkdir()
            command = root / "bin/kit"
            command.write_text("fixture user command")
            args = SimpleNamespace(codex_dir=root / "codex", bin_dir=root / "bin", ai_box=launcher.permissions.ai_box(),
                                   with_command_rules=False)
            with self.assertRaises(ValueError):
                launcher.install(args)
            self.assertEqual(command.read_text(), "fixture user command")
            self.assertFalse((root / "codex").exists())
