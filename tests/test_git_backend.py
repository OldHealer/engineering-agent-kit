import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("git_worker", SCRIPTS / "git_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                            env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    return result.stdout.decode().strip()


class GitBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-git-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.project / "backend"
        self.source.mkdir()
        git(self.source, "init", "-q", "-b", "main")
        git(self.source, "config", "user.name", "Kit fixture")
        git(self.source, "config", "user.email", "kit@example.invalid")
        (self.source / "app.py").write_text("value = 1\n")
        git(self.source, "add", "app.py")
        git(self.source, "commit", "-q", "-m", "base")
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.source), str(self.remote)], check=True)
        git(self.source, "remote", "add", "origin", str(self.remote))
        self.backend = worker.WorkspaceGit(self.project, local_test=True)
        self.addCleanup(setattr, worker.kit, "GIT_EXECUTOR", None)
        self.call("init")
        self.author = {"name": "Kit fixture", "email": "kit@example.invalid"}

    def call(self, action, **parameters):
        return self.backend.dispatch({"action": action, "confirm": True, **parameters})

    def prepare(self, **parameters):
        result = self.call("prepare", id="APP-1", repo="backend", **parameters)
        return Path(result["path"])

    def commit(self, repo, message="Implement fixture"):
        (repo / "app.py").write_text("value = 2\n")
        request = {"id": "APP-1", "repo": "backend", "files": ["app.py"], "message": message, "author": self.author}
        preview = self.call("commit_preview", **request)
        return self.call("commit", **request, confirmation_token=preview["confirmation_token"])

    def test_local_worktree_commit_push_cleanup_preserves_source_and_branch(self):
        (self.source / "app.py").write_text("original dirty checkout\n")
        repo = self.prepare()
        result = self.commit(repo)
        self.assertEqual(git(repo, "status", "--porcelain"), "")
        preview = self.call("push_preview", id="APP-1", repo="backend")
        pushed = self.call("push", id="APP-1", repo="backend", confirmation_token=preview["confirmation_token"])
        self.assertFalse(pushed["pull_request_created"])
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/kit/APP-1"), result["commit"])
        remote_state = self.call("remote_check", repo="backend", id="APP-1")
        self.assertEqual(remote_state["sha"], result["commit"])
        preview = self.call("cleanup_preview", id="APP-1")
        with self.assertRaises(worker.kit.KitError):
            self.call("cleanup", id="APP-1", confirmation_token=preview["confirmation_token"], idle_confirmed=False)
        self.call("cleanup", id="APP-1", confirmation_token=preview["confirmation_token"], idle_confirmed=True)
        self.assertFalse(repo.exists())
        self.assertEqual(git(self.source, "rev-parse", "kit/APP-1"), result["commit"])
        self.assertEqual((self.source / "app.py").read_text(), "original dirty checkout\n")
        self.assertTrue((self.project / ".engineering-kit/runs/APP-1/state.json").is_file())

    def test_changed_files_message_and_head_invalidate_commit_confirmation(self):
        repo = self.prepare()
        request = {"id": "APP-1", "repo": "backend", "files": ["app.py"], "message": "Approved", "author": self.author}
        (repo / "app.py").write_text("value = 2\n")
        preview = self.call("commit_preview", **request)
        (repo / "app.py").write_text("value = 3\n")
        with self.assertRaises(ValueError):
            self.call("commit", **request, confirmation_token=preview["confirmation_token"])
        (repo / "app.py").write_text("value = 2\n")
        with self.assertRaises(ValueError):
            self.call("commit", **{**request, "message": "Different"}, confirmation_token=preview["confirmation_token"])
        self.assertEqual(git(repo, "log", "-1", "--format=%s"), "base")

    def test_sensitive_paths_and_existing_staged_changes_block_commit(self):
        repo = self.prepare()
        for name in (".env", "private.key", "credentials.json"):
            (repo / name).write_text("fixture only")
            with self.assertRaises(ValueError):
                self.call("commit_preview", id="APP-1", repo="backend", files=[name], message="Refused", author=self.author)
        (repo / "app.py").write_text("value = 2\n")
        git(repo, "add", "app.py")
        with self.assertRaises(ValueError):
            self.call("commit_preview", id="APP-1", repo="backend", files=["app.py"], message="Refused", author=self.author)

    def test_missing_confirmation_and_arbitrary_actions_are_refused(self):
        with self.assertRaises(ValueError):
            self.backend.dispatch({"action": "prepare", "id": "APP-1", "repo": "backend"})
        for action in ("exec", "create_pr", "force_push"):
            with self.assertRaises(ValueError):
                self.call(action)

    def test_private_artifacts_cannot_be_selected_for_commit(self):
        repo = self.prepare()
        for name in (".engineering-kit/runs/local/report.md", "notes/.engineering-kit/plan.md"):
            path = repo / name
            path.parent.mkdir(parents=True)
            path.write_text("personal fixture notes\n")
            with self.assertRaises(ValueError):
                self.call("commit_preview", id="APP-1", repo="backend", files=[name],
                          message="Refused", author=self.author)
        self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "")

    def test_already_tracked_private_artifacts_block_commit_and_push(self):
        repo = self.prepare()
        path = repo / ".engineering-kit/report.md"
        path.parent.mkdir()
        path.write_text("personal fixture notes\n")
        git(repo, "add", "--force", ".engineering-kit/report.md")
        git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-q", "-m", "accidental personal notes")
        with self.assertRaisesRegex(ValueError, "уже отслеживаются"):
            self.call("commit_preview", id="APP-1", repo="backend", files=["app.py"],
                      message="Refused", author=self.author)
        with self.assertRaisesRegex(ValueError, "уже отслеживаются"):
            self.call("push_preview", id="APP-1", repo="backend")

    def test_removed_private_artifacts_in_history_still_block_push(self):
        repo = self.prepare()
        path = repo / ".engineering-kit/report.md"
        path.parent.mkdir()
        path.write_text("personal fixture notes\n")
        git(repo, "add", "--force", ".engineering-kit/report.md")
        for message in ("accidental personal notes", "remove personal notes"):
            git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-q", "-m", message)
            if path.exists():
                git(repo, "rm", "-q", ".engineering-kit/report.md")
        self.assertFalse(path.exists())
        with self.assertRaises(ValueError):
            self.call("push_preview", id="APP-1", repo="backend")

    def test_git_hooks_are_not_executed_with_backend_privileges(self):
        marker = self.root / "hook-ran"
        for name in ("post-checkout", "pre-commit", "commit-msg", "pre-push"):
            hook = self.source / ".git/hooks" / name
            hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
            hook.chmod(0o755)
        repo = self.prepare()
        self.commit(repo)
        preview = self.call("push_preview", id="APP-1", repo="backend")
        self.call("push", id="APP-1", repo="backend", confirmation_token=preview["confirmation_token"])
        self.assertFalse(marker.exists())

    def test_custom_hooks_path_allows_review_fetch_and_feature_operations_without_running_hooks(self):
        hooks = self.root / "custom-hooks"
        hooks.mkdir()
        marker = self.root / "custom-hook-ran"
        for name in ("post-checkout", "reference-transaction", "post-index-change", "pre-commit", "commit-msg", "pre-push"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
            hook.chmod(0o755)
        git(self.source, "config", "core.hooksPath", str(hooks))
        config_before = (self.source / ".git/config").read_bytes()
        # Контроль: обычный Git действительно находит и запускает этот hook.
        with self.assertRaises(subprocess.CalledProcessError):
            git(self.source, "hook", "run", "post-checkout", "--", "fixture", "fixture", "0")
        self.assertTrue(marker.exists())
        marker.unlink()

        git(self.remote, "update-ref", "refs/heads/fix/fixture", "refs/heads/main")
        review = self.call("prepare", id="REVIEW-1", repo="backend", mode="review", workspace="worktree",
                           base="origin/main", head="origin/fix/fixture", fetch=True)
        self.assertIsNone(review["branch"])
        self.assertTrue(Path(review["path"]).is_dir())
        self.assertFalse(marker.exists())
        preview = self.call("cleanup_preview", id="REVIEW-1")
        self.call("cleanup", id="REVIEW-1", confirmation_token=preview["confirmation_token"], idle_confirmed=True)

        repo = self.prepare(base="origin/main", fetch=True)
        committed = self.commit(repo)
        preview = self.call("push_preview", id="APP-1", repo="backend")
        self.call("push", id="APP-1", repo="backend", confirmation_token=preview["confirmation_token"])
        self.assertEqual(git(self.remote, "rev-parse", "refs/heads/kit/APP-1"), committed["commit"])
        preview = self.call("cleanup_preview", id="APP-1")
        self.call("cleanup", id="APP-1", confirmation_token=preview["confirmation_token"], idle_confirmed=True)
        self.assertFalse(marker.exists())
        self.assertTrue((self.source / ".git/config").read_bytes() == config_before,
                        "Конфигурация тестового репозитория неожиданно изменилась")

    def test_executable_git_configuration_is_rejected(self):
        git(self.source, "config", "core.hooksPath", str(self.root / "custom-hooks"))
        for key in ("filter.fixture.clean", "filter.fixture.process", "core.fsmonitor",
                    "core.sshCommand", "credential.helper", "include.path"):
            with self.subTest(key=key):
                git(self.source, "config", key, "fixture-command")
                try:
                    with self.assertRaisesRegex(worker.GitError, "настройки вне безопасного списка"):
                        self.prepare()
                finally:
                    git(self.source, "config", "--unset-all", key)

    def test_registry_cannot_redirect_to_another_project_or_symlink(self):
        config_path = self.project / ".engineering-kit/project.json"
        data = json.loads(config_path.read_text())
        data["repos"]["backend"] = str(self.remote)
        config_path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.prepare()

    def test_unselected_legacy_worktree_does_not_block_another_repository(self):
        other = self.project / "legacy-worktree"
        git(self.source, "worktree", "add", "-q", "--detach", str(other), "HEAD")
        path = self.project / ".engineering-kit/project.json"
        data = json.loads(path.read_text())
        data["repos"]["legacy-worktree"] = str(other)
        path.write_text(json.dumps(data))
        self.assertTrue(self.prepare().is_dir())

    def test_review_refreshes_rewritten_remote_branch_preserving_previous_snapshot(self):
        git(self.source, "checkout", "-q", "-b", "feature")
        (self.source / "app.py").write_text("first PR version\n")
        git(self.source, "add", "app.py")
        git(self.source, "commit", "-q", "-m", "first PR version")
        first = git(self.source, "rev-parse", "HEAD")
        git(self.source, "push", "-q", "origin", "feature")
        git(self.source, "checkout", "-q", "main")
        review = self.call("prepare", id="review-1", repo="backend", mode="review", head="origin/feature")
        git(self.source, "checkout", "-q", "-b", "replacement")
        (self.source / "app.py").write_text("rewritten PR version\n")
        git(self.source, "add", "app.py")
        git(self.source, "commit", "-q", "-m", "rewritten PR version")
        second = git(self.source, "rev-parse", "HEAD")
        git(self.source, "push", "-q", "origin", "replacement")
        git(self.remote, "update-ref", "refs/heads/feature", second)
        refreshed = self.call("prepare", id="review-2", repo="backend", mode="review", head="origin/feature")
        self.assertEqual(review["head"], first)
        self.assertEqual(refreshed["head"], second)
        self.assertEqual(git(Path(review["path"]), "rev-parse", "HEAD"), first)

    def test_state_symlink_is_refused_before_reading_it(self):
        self.prepare()
        state = self.project / ".engineering-kit/runs/APP-1/state.json"
        destination = self.root / "fixture.json"
        destination.write_text('{"fixture": true}')
        state.unlink()
        state.symlink_to(destination)
        with self.assertRaises(ValueError):
            self.call("status", id="APP-1")

    def test_preplanted_temporary_symlink_cannot_overwrite_an_external_file(self):
        storage = self.project / ".engineering-kit"
        (storage / "project.json").unlink()
        outside = self.root / "outside.fixture"
        outside.write_text("preserve fixture")
        (storage / "project.tmp").symlink_to(outside)
        self.call("init")
        self.assertEqual(outside.read_text(), "preserve fixture")
        self.assertTrue((storage / "project.json").is_file())

    def test_push_checks_sensitive_files_in_intermediate_commits(self):
        repo = self.prepare()
        (repo / ".env.fixture").write_text("DUMMY=not-a-real-token")
        git(repo, "add", ".env.fixture")
        git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "refused intermediate")
        git(repo, "rm", "-q", ".env.fixture")
        git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "remove")
        with self.assertRaises(ValueError):
            self.call("push_preview", id="APP-1", repo="backend")

    def test_new_commit_invalidates_push_confirmation(self):
        repo = self.prepare()
        self.commit(repo)
        preview = self.call("push_preview", id="APP-1", repo="backend")
        (repo / "app.py").write_text("value = 3\n")
        git(repo, "add", "app.py")
        git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "later")
        with self.assertRaises(ValueError):
            self.call("push", id="APP-1", repo="backend", confirmation_token=preview["confirmation_token"])

    def test_authenticated_smart_http_uses_memory_only_header_and_preserves_origin(self):
        git(self.remote, "config", "http.receivepack", "true")
        expected = "Bearer synthetic-token-for-git-test"
        records = []
        root = self.root

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.run_git()

            def do_POST(self):
                self.run_git()

            def log_message(self, *args):
                pass

            def run_git(self):
                authorized = self.headers.get("Authorization") == expected
                records.append({"authorized": authorized, "method": self.command})
                if not authorized:
                    self.send_response(401)
                    self.end_headers()
                    return
                parsed = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                env = {"PATH": os.defpath, "GIT_PROJECT_ROOT": str(root), "GIT_HTTP_EXPORT_ALL": "1",
                       "REQUEST_METHOD": self.command, "PATH_INFO": parsed.path, "QUERY_STRING": parsed.query,
                       "CONTENT_TYPE": self.headers.get("Content-Type", ""), "CONTENT_LENGTH": str(len(body)),
                       "REMOTE_USER": "fixture", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
                result = subprocess.run(["git", "http-backend"], input=body, capture_output=True, env=env, check=True)
                headers, content = result.stdout.split(b"\r\n\r\n", 1)
                self.send_response(200)
                for line in headers.decode().split("\r\n"):
                    name, value = line.split(":", 1)
                    if name.lower() != "status":
                        self.send_header(name, value.strip())
                self.end_headers()
                self.wfile.write(content)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/remote.git"
        git(self.source, "remote", "set-url", "origin", url)
        self.backend.connections = {"bitbucket": {"token": expected.removeprefix("Bearer ")}}
        self.call("remote_check", repo="backend")
        before = git(self.source, "rev-parse", "HEAD")
        fetched = self.call("fetch", repo="backend", branch="main")
        self.assertEqual(fetched["sha"], before)
        self.assertEqual(git(self.source, "rev-parse", "HEAD"), before)
        self.assertFalse((self.project / ".engineering-kit/workspaces").exists())
        repo = self.prepare()
        result = self.commit(repo)
        preview = self.call("push_preview", id="APP-1", repo="backend")
        self.call("push", id="APP-1", repo="backend", confirmation_token=preview["confirmation_token"])
        self.assertEqual(git(self.remote, "rev-parse", "kit/APP-1"), result["commit"])
        self.assertTrue(records and all(item["authorized"] for item in records))
        self.assertTrue(any(item["method"] == "POST" for item in records))
        self.assertEqual(git(self.source, "remote", "get-url", "origin"), url)
        for path in (self.source / ".git/config", self.project / ".engineering-kit/project.json",
                     self.project / ".engineering-kit/runs/APP-1/state.json"):
            self.assertNotIn(expected.removeprefix("Bearer "), path.read_text())

    def configure_current(self):
        git(self.source, "branch", "master", "main")
        git(self.source, "checkout", "-q", "-b", "dev")
        git(self.source, "push", "-q", "origin", "master", "dev")
        return self.call("configure", workspace="current", development_branch="dev", target_branch="master")

    def test_current_commit_and_push_use_dev_without_creating_worktree(self):
        self.configure_current()
        prepared = self.call("prepare", id="APP-1", repo="backend")
        self.assertEqual(prepared["path"], str(self.source))
        self.assertFalse(prepared["worktree_created"])
        self.assertEqual(self.call("discover")["workspace"]["mode"], "current")
        target = git(self.remote, "rev-parse", "master")
        result = self.commit(self.source)
        preview = self.call("push_preview", id="APP-1", repo="backend")
        self.assertEqual(preview["branch"], "dev")
        self.assertEqual(preview["remote_head"], target)
        self.call("push", id="APP-1", repo="backend", confirmation_token=preview["confirmation_token"])
        self.assertEqual(git(self.remote, "rev-parse", "dev"), result["commit"])
        self.assertEqual(git(self.remote, "rev-parse", "master"), target)
        self.assertEqual(self.call("remote_check", id="APP-1", repo="backend")["reference"], "refs/heads/dev")
        self.assertTrue(self.call("push_preview", id="APP-1", repo="backend")["up_to_date"])
        self.assertFalse((self.project / ".engineering-kit/workspaces").exists())
        self.assertEqual(git(self.source, "branch", "--show-current"), "dev")

    def test_current_cleanup_never_removes_original_checkout(self):
        self.configure_current()
        self.call("prepare", id="APP-1", repo="backend")
        preview = self.call("cleanup_preview", id="APP-1")
        self.assertFalse(preview["can_remove"])
        self.assertEqual(preview["entries"][0]["workspace"], "current")
        with self.assertRaises(worker.kit.KitError):
            self.call("cleanup", id="APP-1", confirmation_token=preview["confirmation_token"], idle_confirmed=True)
        self.assertTrue((self.source / "app.py").is_file())
        self.assertTrue((self.source / ".git").is_dir())

    def test_current_configuration_does_not_switch_branch_and_requires_consent(self):
        with self.assertRaises(ValueError):
            self.backend.dispatch({"action": "configure", "workspace": "current", "development_branch": "dev", "target_branch": "master"})
        self.call("configure", workspace="current", development_branch="dev", target_branch="master")
        self.assertEqual(git(self.source, "branch", "--show-current"), "main")
        with self.assertRaises(ValueError):
            self.call("prepare", id="APP-1", repo="backend")
        git(self.source, "branch", "dev")
        self.call("prepare", id="APP-1", repo="backend", switch_branch=True)
        self.assertEqual(git(self.source, "branch", "--show-current"), "dev")

    def test_current_preserves_existing_changes_until_their_inclusion_is_approved(self):
        self.configure_current()
        (self.source / "app.py").write_text("preserve existing edits\n")
        with self.assertRaises(ValueError):
            self.call("prepare", id="APP-1", repo="backend")
        self.call("prepare", id="APP-1", repo="backend", include_changes=True)
        self.assertEqual((self.source / "app.py").read_text(), "preserve existing edits\n")
        self.assertEqual(git(self.source, "stash", "list"), "")

    def test_dirty_current_branch_is_not_switched_even_with_switch_consent(self):
        self.configure_current()
        git(self.source, "checkout", "-q", "main")
        (self.source / "app.py").write_text("preserve edits on main\n")
        with self.assertRaises(ValueError):
            self.call("prepare", id="APP-1", repo="backend", switch_branch=True, include_changes=True)
        self.assertEqual(git(self.source, "branch", "--show-current"), "main")
        self.assertEqual((self.source / "app.py").read_text(), "preserve edits on main\n")

    def test_current_forbids_protected_and_target_branches(self):
        for branch, target in (("main", "release"), ("master", "release"), ("dev", "dev")):
            with self.assertRaises(ValueError):
                self.call("configure", workspace="current", development_branch=branch, target_branch=target)
        self.assertEqual(self.call("discover")["workspace"]["mode"], "worktree")

    def test_changed_current_settings_or_branch_invalidate_commit(self):
        self.configure_current()
        self.call("prepare", id="APP-1", repo="backend")
        (self.source / "app.py").write_text("value = 2\n")
        request = {"id": "APP-1", "repo": "backend", "files": ["app.py"], "message": "Approved", "author": self.author}
        preview = self.call("commit_preview", **request)
        self.call("configure", workspace="current", development_branch="dev", target_branch="release")
        with self.assertRaises(ValueError):
            self.call("commit", **request, confirmation_token=preview["confirmation_token"])
        self.call("configure", workspace="current", development_branch="dev", target_branch="master")
        git(self.source, "checkout", "-q", "main")
        with self.assertRaises(ValueError):
            self.call("commit", **request, confirmation_token=preview["confirmation_token"])
        self.assertEqual(git(self.source, "diff", "--cached", "--name-only"), "")

    def test_worktree_override_and_current_tasks_do_not_mix(self):
        self.configure_current()
        worktree = self.prepare(workspace="worktree", base="origin/dev")
        self.assertNotEqual(worktree, self.source)
        self.assertEqual(git(worktree, "branch", "--show-current"), "kit/APP-1")
        with self.assertRaises(ValueError):
            self.call("prepare", id="APP-1", repo="backend", workspace="current")
        self.call("prepare", id="APP-2", repo="backend")
        with self.assertRaises(worker.kit.KitError):
            self.call("prepare", id="APP-2", repo="backend", workspace="worktree", base="origin/dev")

    def test_current_push_checks_commits_that_predate_task_registration(self):
        self.configure_current()
        (self.source / ".env.fixture").write_text("DUMMY=fixture-only")
        git(self.source, "add", ".env.fixture")
        git(self.source, "commit", "-q", "-m", "refused pending file")
        git(self.source, "rm", "-q", ".env.fixture")
        git(self.source, "commit", "-q", "-m", "remove pending file")
        self.call("prepare", id="APP-1", repo="backend")
        self.commit(self.source)
        with self.assertRaises(ValueError):
            self.call("push_preview", id="APP-1", repo="backend")
        self.assertEqual(git(self.remote, "rev-parse", "dev"), git(self.remote, "rev-parse", "master"))

    def test_single_repository_current_mode_keeps_project_metadata_out_of_commits(self):
        self.configure_current()
        self.backend = worker.WorkspaceGit(self.source, local_test=True)
        self.call("init")
        self.call("configure", workspace="current", development_branch="dev", target_branch="master")
        self.call("prepare", id="APP-1", repo="backend")
        result = self.commit(self.source)
        note = self.source / ".engineering-kit/features/local/hub.md"
        note.parent.mkdir(parents=True)
        note.write_text("personal fixture notes\n")
        self.assertEqual(git(self.source, "check-ignore", ".engineering-kit/features/local/hub.md"),
                         ".engineering-kit/features/local/hub.md")
        git(self.source, "add", "--all")
        self.assertEqual(git(self.source, "diff", "--cached", "--name-only"), "")
        self.assertEqual(git(self.source, "diff-tree", "--no-commit-id", "--name-only", "-r", result["commit"]), "app.py")
        self.assertEqual(git(self.source, "status", "--porcelain"), "")

    def test_private_artifacts_in_root_commit_block_new_current_branch_push(self):
        note = self.source / ".engineering-kit/report.md"
        note.parent.mkdir()
        note.write_text("personal fixture notes\n")
        git(self.source, "add", "--force", ".engineering-kit/report.md")
        git(self.source, "commit", "--amend", "-q", "--no-edit")
        git(self.source, "rm", "-q", ".engineering-kit/report.md")
        git(self.source, "commit", "-q", "-m", "remove personal notes")
        git(self.source, "checkout", "-q", "-b", "dev")
        self.call("configure", workspace="current", development_branch="dev", target_branch="main")
        self.call("prepare", id="APP-1", repo="backend")
        with self.assertRaises(ValueError):
            self.call("push_preview", id="APP-1", repo="backend")
        self.assertEqual(git(self.remote, "branch", "--list", "dev"), "")


if __name__ == "__main__":
    unittest.main()
