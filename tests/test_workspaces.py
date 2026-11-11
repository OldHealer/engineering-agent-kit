import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kit.py"
SPEC = importlib.util.spec_from_file_location("kit", SCRIPT)
kit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kit)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="engineering-kit-test-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project with spaces"
        self.project.mkdir()
        self.backend = self.make_repo("backend")
        self.frontend = self.make_repo("frontend")
        self.run_cli("init", "--repo", "backend", "--repo", "frontend")

    def git(self, repo, *args):
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def make_repo(self, name):
        repo = self.project / name
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.email", "kit-test@example.invalid")
        self.git(repo, "config", "user.name", "Kit test")
        (repo / "app.txt").write_text("base\n")
        (repo / ".gitignore").write_text("cache/\n")
        self.git(repo, "add", "app.txt", ".gitignore")
        self.git(repo, "commit", "-m", "base")
        self.git(repo, "checkout", "-b", "feature")
        (repo / "app.txt").write_text("feature\n")
        self.git(repo, "commit", "-am", "feature")
        self.git(repo, "checkout", "main")
        return repo

    def run_cli(self, command, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), command, "--project", str(self.project), *args],
            capture_output=True, text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stderr

    def prepare(self, alias="backend", mode="review", task_id="review-1", head="feature"):
        return self.run_cli("prepare", "--id", task_id, "--repo", alias,
                            "--mode", mode, "--base", "main", "--head", head)

    def preview(self, task_id="review-1"):
        return self.run_cli("cleanup", "--id", task_id)

    def remove(self, preview):
        return self.run_cli("cleanup", "--id", preview["id"], "--confirm",
                            preview["confirmation_token"], "--idle-confirmed")

    def test_review_snapshot_preserves_original_dirty_checkout(self):
        (self.backend / "app.txt").write_text("user work\n")
        before = self.git(self.backend, "status", "--porcelain")
        prepared = self.prepare()
        worktree = Path(prepared["path"])
        self.assertEqual((worktree / "app.txt").read_text(), "feature\n")
        self.assertEqual(self.git(worktree, "rev-parse", "HEAD"), self.git(self.backend, "rev-parse", "feature"))
        self.assertEqual(self.git(self.backend, "status", "--porcelain"), before)
        self.assertEqual(self.git(self.backend, "branch", "--show-current"), "main")
        self.assertEqual(self.git(self.backend, "diff", f'{prepared["merge_base"]}..{prepared["head"]}', "--stat"),
                         "app.txt | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)")

    def test_multi_repo_cleanup_preserves_report_refs_and_originals(self):
        back = self.prepare()
        front = self.prepare("frontend")
        report = Path(back["results"]) / "report.md"
        report.write_text("review result\n")
        preview = self.preview()
        self.assertTrue(preview["can_remove"])
        self.assertTrue(Path(back["path"]).exists())
        self.remove(preview)
        self.assertFalse(Path(back["path"]).exists())
        self.assertFalse(Path(front["path"]).exists())
        self.assertEqual(report.read_text(), "review result\n")
        self.assertTrue((self.backend / "app.txt").exists())
        self.assertEqual(self.git(self.backend, "rev-parse", "refs/engineering-kit/review-1/backend/head"), back["head"])
        state = self.run_cli("status", "--id", "review-1")
        self.assertTrue(all(entry["removed"] for entry in state["entries"]))

    def test_dirty_repo_blocks_whole_set(self):
        back = self.prepare()
        front = self.prepare("frontend")
        (Path(front["path"]) / "new.txt").write_text("not saved\n")
        preview = self.preview()
        self.assertFalse(preview["can_remove"])
        self.run_cli("cleanup", "--id", "review-1", "--confirm",
                     preview["confirmation_token"], "--idle-confirmed", success=False)
        self.assertTrue(Path(back["path"]).exists())
        self.assertTrue(Path(front["path"]).exists())

    def test_ignored_files_block_cleanup(self):
        prepared = self.prepare()
        cache = Path(prepared["path"]) / "cache"
        cache.mkdir()
        (cache / "local.txt").write_text("local data\n")
        self.assertFalse(self.preview()["can_remove"])

    def test_staged_changes_block_cleanup(self):
        prepared = self.prepare()
        worktree = Path(prepared["path"])
        (worktree / "app.txt").write_text("staged\n")
        self.git(worktree, "add", "app.txt")
        self.assertFalse(self.preview()["can_remove"])

    def test_detached_commit_blocks_cleanup_even_when_clean(self):
        prepared = self.prepare()
        worktree = Path(prepared["path"])
        (worktree / "app.txt").write_text("detached work\n")
        self.git(worktree, "commit", "-am", "local detached commit")
        self.assertEqual(self.git(worktree, "status", "--porcelain"), "")
        self.assertFalse(self.preview()["can_remove"])

    def test_feature_branch_survives_cleanup(self):
        prepared = self.prepare(mode="feature", task_id="csv", head="main")
        worktree = Path(prepared["path"])
        (worktree / "app.txt").write_text("implemented\n")
        self.git(worktree, "commit", "-am", "implementation")
        final_sha = self.git(worktree, "rev-parse", "HEAD")
        self.remove(self.preview("csv"))
        self.assertEqual(self.git(self.backend, "rev-parse", "kit/csv"), final_sha)
        self.assertEqual(self.git(self.backend, "show", "kit/csv:app.txt"), "implemented")

    def test_missing_base_fails_without_worktree(self):
        self.run_cli("prepare", "--id", "bad", "--repo", "backend", "--mode", "review",
                     "--base", "missing", "--head", "feature", success=False)
        self.assertFalse((self.project / ".engineering-kit/workspaces/bad/backend").exists())

    def test_rejects_traversal_and_mode_collision(self):
        self.run_cli("prepare", "--id", "../outside", "--repo", "backend", "--mode", "review",
                     "--base", "main", success=False)
        self.prepare()
        self.run_cli("prepare", "--id", "review-1", "--repo", "frontend", "--mode", "feature",
                     "--base", "main", success=False)

    def test_cleanup_requires_confirmation_and_idle_assertion(self):
        prepared = self.prepare()
        preview = self.preview()
        self.run_cli("cleanup", "--id", "review-1", "--confirm", "wrong", "--idle-confirmed", success=False)
        self.run_cli("cleanup", "--id", "review-1", "--confirm", preview["confirmation_token"], success=False)
        self.assertTrue(Path(prepared["path"]).exists())

    def test_confirmation_changes_with_workspace_head(self):
        prepared = self.prepare(mode="feature", task_id="csv", head="main")
        preview = self.preview("csv")
        worktree = Path(prepared["path"])
        (worktree / "app.txt").write_text("another commit\n")
        self.git(worktree, "commit", "-am", "another commit")
        self.run_cli("cleanup", "--id", "csv", "--confirm", preview["confirmation_token"],
                     "--idle-confirmed", success=False)
        self.assertTrue(worktree.exists())

    def test_source_and_path_tampering_are_rejected(self):
        prepared = self.prepare()
        path = Path(prepared["results"]) / "state.json"
        state = json.loads(path.read_text())
        state["repos"]["backend"]["path"] = str(self.backend)
        path.write_text(json.dumps(state))
        self.run_cli("cleanup", "--id", "review-1", success=False)
        self.assertTrue((self.backend / "app.txt").exists())

    def test_real_fetch_from_local_remote(self):
        remote = Path(self.temporary.name) / "remote.git"
        result = subprocess.run(["git", "clone", "--bare", str(self.backend), str(remote)], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.git(self.backend, "remote", "add", "origin", str(remote))
        prepared = self.run_cli("prepare", "--id", "fetched", "--repo", "backend", "--mode", "review",
                                "--base", "origin/main", "--head", "origin/feature", "--fetch")
        self.assertEqual(prepared["head"], self.git(self.backend, "rev-parse", "feature"))

    def test_skill_install_is_idempotent_and_does_not_overwrite(self):
        destination = Path(self.temporary.name) / "skills"
        args = type("Args", (), {"dest": str(destination)})()
        destination.mkdir()
        for name in ("review-pr", "plan-feature", "investigate-bug", "workspace-cleanup"):
            (destination / name).symlink_to(kit.KIT_ROOT / "skills" / name, target_is_directory=True)
        installed = kit.install_skills(args)
        self.assertEqual(kit.install_skills(args), installed)
        expected = {"review-pr", "plan-feature", "investigate-bug", "workspace-cleanup",
                    "feature-hub", "verify-feature", "library-docs"}
        self.assertEqual(set(installed["installed"]), expected)
        self.assertEqual({path.name for path in destination.iterdir()}, expected)
        for name in expected:
            source = kit.KIT_ROOT / "skills" / name
            self.assertEqual((destination / name).resolve(), source)
            self.assertTrue((destination / name / "SKILL.md").is_file())
        other = Path(self.temporary.name) / "existing"
        (other / "verify-feature").mkdir(parents=True)
        args.dest = str(other)
        with self.assertRaises(kit.KitError):
            kit.install_skills(args)
        self.assertFalse((other / "plan-feature").exists())
        self.assertFalse((other / "feature-hub").exists())


if __name__ == "__main__":
    unittest.main()
