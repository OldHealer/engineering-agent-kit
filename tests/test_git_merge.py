"""Интеграция merge на временных локальных репозиториях, без сети и моков."""

import unittest

import test_git_backend as fixtures


git = fixtures.git
worker = fixtures.worker


class GitMergeTests(unittest.TestCase):
    setUp = fixtures.GitBackendTests.setUp
    call = fixtures.GitBackendTests.call
    prepare = fixtures.GitBackendTests.prepare
    commit = fixtures.GitBackendTests.commit

    def update_origin(self, files=None):
        for name, content in (files or {'incoming.py': 'incoming = True\n'}).items():
            path = self.source/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(self.source, 'add', '-A')
        git(self.source, 'commit', '-q', '-m', 'upstream change')
        git(self.source, 'push', '-q', 'origin', 'main')
        return git(self.source, 'rev-parse', 'HEAD')

    def fetch(self):
        return self.call('fetch', repo='backend', branch='main')

    def preview(self):
        return self.call('merge_preview', id='APP-1', repo='backend', revision='origin/main')

    def merge(self, preview=None):
        preview = preview or self.preview()
        return self.call('merge', id='APP-1', repo='backend', revision='origin/main',
                         confirmation_token=preview['confirmation_token'])

    def finish(self, resolved=None):
        request = {'id': 'APP-1', 'repo': 'backend', 'mode': 'merge', 'message': 'Merge current main', 'author': self.author}
        if resolved is not None:
            request['resolved_files'] = resolved
        preview = self.call('commit_preview', **request)
        result = self.call('commit', **request, confirmation_token=preview['confirmation_token'])
        return preview, result

    def conflict(self):
        repo = self.prepare()
        self.commit(repo)
        self.update_origin({'app.py': 'value = 3\n'})
        self.fetch()
        result = self.merge()
        self.assertEqual(result['outcome'], 'conflicts')
        self.assertEqual(result['conflicts'], ['app.py'])
        return repo

    def test_fast_forward_stops_head_until_confirmed_two_parent_commit(self):
        repo = self.prepare()
        original = git(repo, 'rev-parse', 'HEAD')
        incoming = self.update_origin()
        fetched = self.fetch()
        self.assertEqual(fetched['sha'], incoming)
        before = self.backend.merge_index_hash(repo)
        preview = self.preview()
        self.assertEqual(preview['scenario'], 'fast_forward_possible')
        self.assertEqual(self.backend.merge_index_hash(repo), before)
        self.assertFalse(self.backend.git_path(repo, 'MERGE_HEAD').exists())
        objects_before = git(repo, 'count-objects', '-v')
        self.preview()
        self.assertEqual(git(repo, 'count-objects', '-v'), objects_before)
        result = self.merge(preview)
        self.assertEqual(result['outcome'], 'merged_pending_commit')
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), original)
        self.assertEqual((repo/'incoming.py').read_text(), 'incoming = True\n')
        status = self.call('status', id='APP-1')['entries'][0]['merge']
        self.assertEqual(status['parents'], [original, incoming])
        self.assertTrue(status['ready_to_complete'])
        shown, completed = self.finish()
        self.assertEqual(shown['parents'], [original, incoming])
        self.assertEqual(git(repo, 'show', '-s', '--format=%P', 'HEAD').split(), [original, incoming])
        self.assertEqual(git(repo, 'rev-parse', 'HEAD^{tree}'), git(repo, 'rev-parse', incoming+'^{tree}'))
        self.assertEqual(git(repo, 'status', '--porcelain'), '')
        self.assertTrue(completed['merge_complete'])
        self.assertFalse(self.call('status', id='APP-1')['entries'][0]['merge']['in_progress'])
        # Push всё ещё требует отдельного вызова и своего токена.
        self.assertNotEqual(git(self.remote, 'rev-parse', 'main'), completed['commit'])
        pushed = self.call('push_preview', id='APP-1', repo='backend')
        self.call('push', id='APP-1', repo='backend', confirmation_token=pushed['confirmation_token'])
        self.assertEqual(git(self.remote, 'rev-parse', 'kit/APP-1'), completed['commit'])

    def test_diverged_clean_merge_preserves_both_sides(self):
        repo = self.prepare()
        self.commit(repo)
        head = git(repo, 'rev-parse', 'HEAD')
        source = self.update_origin()
        self.fetch()
        self.assertEqual(self.preview()['scenario'], 'merge')
        self.assertEqual(self.merge()['outcome'], 'merged_pending_commit')
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
        self.finish()
        self.assertEqual(git(repo, 'show', '-s', '--format=%P', 'HEAD').split(), [head, source])
        self.assertEqual((repo/'app.py').read_text(), 'value = 2\n')
        self.assertTrue((repo/'incoming.py').exists())

    def test_already_up_to_date_is_noop(self):
        repo = self.prepare()
        self.fetch()
        head = git(repo, 'rev-parse', 'HEAD')
        self.assertEqual(self.preview()['scenario'], 'already_up_to_date')
        self.assertEqual(self.merge()['outcome'], 'already_up_to_date')
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.backend.merge_head(repo), [])

    def test_conflict_fix_without_shell_staging_and_finish(self):
        repo = self.conflict()
        head = git(repo, 'rev-parse', 'HEAD')
        status = self.call('status', id='APP-1')['entries'][0]['merge']
        self.assertEqual(status['conflicts'], ['app.py'])
        self.assertFalse(status['ready_to_complete'])
        with self.assertRaises(ValueError):
            self.finish(['app.py'])
        (repo/'app.py').write_text('value = 4\n')
        with self.assertRaises(ValueError):
            self.finish()
        preview, result = self.finish(['app.py'])
        self.assertEqual([item['path'] for item in preview['files']], ['app.py'])
        self.assertEqual(result['parents'][0], head)
        self.assertEqual(git(repo, 'show', 'HEAD:app.py'), 'value = 4')
        self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_fetch_without_task_or_checkout_and_with_dirty_files(self):
        before = git(self.source, 'rev-parse', 'HEAD')
        (self.source/'app.py').write_text('local change\n')
        fetched = self.fetch()
        self.assertFalse(fetched['worktree_created'])
        self.assertEqual(git(self.source, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.source/'app.py').read_text(), 'local change\n')
        self.assertFalse((self.project/'.engineering-kit/workspaces').exists())

    def test_fetch_rewritten_remote_preserves_tracking_ref(self):
        self.fetch()
        before = git(self.source, 'rev-parse', 'origin/main')
        self.update_origin()
        self.fetch()
        latest = git(self.source, 'rev-parse', 'origin/main')
        # Только временный bare remote: моделируем уже переписанный сервер без force push.
        git(self.remote, 'update-ref', 'refs/heads/main', before, latest)
        with self.assertRaisesRegex(ValueError, 'переписал'):
            self.fetch()
        self.assertEqual(git(self.source, 'rev-parse', 'origin/main'), latest)

    def test_fetch_is_required_and_stale_remote_confirmation_rejected(self):
        repo = self.prepare()
        with self.assertRaisesRegex(ValueError, 'fetch'):
            self.preview()
        self.fetch()
        preview = self.preview()
        self.update_origin()
        self.fetch()
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.merge(preview)
        self.assertEqual(self.backend.merge_head(repo), [])

    def test_dirty_staged_unstaged_and_untracked_rejected(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        clean = self.preview()
        for kind in ('unstaged', 'staged', 'untracked'):
            with self.subTest(kind=kind):
                name = 'extra.py' if kind == 'untracked' else 'app.py'
                (repo/name).write_text('foreign change\n')
                if kind == 'staged':
                    git(repo, 'add', name)
                with self.assertRaises(ValueError):
                    self.merge(clean)
                blocked = self.preview()
                self.assertTrue(blocked['blockers'])
                with self.assertRaises(ValueError):
                    self.merge(blocked)
                if kind == 'untracked':
                    (repo/name).unlink()
                else:
                    git(repo, 'restore', '--source=HEAD', '--staged', '--worktree', name)
        self.assertEqual(self.backend.merge_head(repo), [])

    def test_protected_target_detached_and_repeat_rejected(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        self.call('configure', workspace='worktree', development_branch='main', target_branch='kit/APP-1')
        with self.assertRaisesRegex(ValueError, 'защищённую'):
            self.preview()
        self.call('configure', workspace='worktree', development_branch='main', target_branch='main')
        head = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'switch', '--detach', '-q', head)
        with self.assertRaises(ValueError):
            self.preview()
        git(repo, 'switch', '-q', 'kit/APP-1')
        self.merge()
        repeat = self.preview()
        self.assertTrue(repeat['blockers'])
        with self.assertRaises(ValueError):
            self.merge(repeat)

    def test_current_uses_selected_dev_without_switching(self):
        git(self.source, 'switch', '-q', '-c', 'dev')
        self.call('configure', workspace='current', development_branch='dev', target_branch='main')
        repo = self.prepare()
        self.assertEqual(repo, self.source)
        # Создаём серверный commit через отдельную временную копию, не переключая dev.
        upstream = self.root/'upstream'
        fixtures.subprocess.run(['git', 'clone', '-q', str(self.remote), str(upstream)], check=True)
        git(upstream, 'config', 'user.name', 'Fixture')
        git(upstream, 'config', 'user.email', 'fixture@example.invalid')
        (upstream/'incoming.py').write_text('new\n')
        git(upstream, 'add', 'incoming.py')
        git(upstream, 'commit', '-q', '-m', 'upstream')
        git(upstream, 'push', '-q', 'origin', 'main')
        self.fetch()
        self.merge()
        self.finish()
        self.assertEqual(git(repo, 'branch', '--show-current'), 'dev')
        self.assertEqual(len(git(repo, 'show', '-s', '--format=%P', 'HEAD').split()), 2)
        self.assertEqual(git(repo, 'rev-parse', 'main'), git(repo, 'rev-parse', 'HEAD^1'))

    def test_native_abort_discards_only_shown_conflict_resolution(self):
        repo = self.conflict()
        head = git(repo, 'rev-parse', 'HEAD')
        (repo/'app.py').write_text('resolved but abandoned\n')
        preview = self.call('merge_abort_preview', id='APP-1', repo='backend')
        self.assertEqual(preview['will_discard'], ['app.py'])
        result = self.call('merge_abort', id='APP-1', repo='backend', confirmation_token=preview['confirmation_token'])
        self.assertTrue(result['aborted'])
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual((repo/'app.py').read_text(), 'value = 2\n')
        self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_abort_stale_and_foreign_untracked_staged_unstaged_protected(self):
        repo = self.conflict()
        preview = self.call('merge_abort_preview', id='APP-1', repo='backend')
        (repo/'app.py').write_text('resolved\n')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.call('merge_abort', id='APP-1', repo='backend', confirmation_token=preview['confirmation_token'])
        for staged in (False, True):
            (repo/'foreign.py').write_text('keep this\n')
            if staged:
                git(repo, 'add', 'foreign.py')
            with self.assertRaisesRegex(ValueError, 'посторонние'):
                self.call('merge_abort_preview', id='APP-1', repo='backend')
            self.assertEqual((repo/'foreign.py').read_text(), 'keep this\n')
            if not staged:
                (repo/'foreign.py').unlink()
        self.assertEqual(self.backend.merge_head(repo), [git(repo, 'rev-parse', 'origin/main')])

    def test_forbidden_incoming_files_block_without_opening_them(self):
        repo = self.prepare()
        self.update_origin({'.env': 'synthetic fixture only\n', '.engineering-kit/report.md': 'private fixture\n'})
        self.fetch()
        preview = self.preview()
        self.assertEqual(preview['blocked_paths'], ['.engineering-kit/report.md', '.env'])
        with self.assertRaises(ValueError):
            self.merge(preview)
        self.assertFalse((repo/'.env').exists())
        self.assertFalse((repo/'.engineering-kit/report.md').exists())

    def test_foreign_changes_during_merge_cannot_enter_commit(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        self.merge()
        for staged in (False, True):
            (repo/'foreign.py').write_text('keep\n')
            if staged:
                git(repo, 'add', 'foreign.py')
            with self.assertRaisesRegex(ValueError, 'посторонние'):
                self.finish()
            if not staged:
                (repo/'foreign.py').unlink()
        self.assertNotEqual(self.backend.merge_head(repo), [])

    def test_ordinary_commit_cannot_silently_complete_merge(self):
        repo = self.conflict()
        (repo/'app.py').write_text('resolved\n')
        with self.assertRaisesRegex(ValueError, 'mode=merge'):
            self.call('commit_preview', id='APP-1', repo='backend', files=['app.py'], message='unsafe', author=self.author)
        with self.assertRaisesRegex(ValueError, 'частичный'):
            self.call('commit_preview', id='APP-1', repo='backend', mode='merge', files=['app.py'],
                      resolved_files=['app.py'], message='unsafe', author=self.author)

    def test_commit_preview_is_read_only_and_all_state_changes_revoke_token(self):
        repo = self.conflict()
        (repo/'app.py').write_text('resolved\n')
        request = {'id': 'APP-1', 'repo': 'backend', 'mode': 'merge', 'resolved_files': ['app.py'],
                   'message': 'Approved merge', 'author': self.author}
        before = self.backend.merge_index_hash(repo)
        preview = self.call('commit_preview', **request)
        self.assertEqual(self.backend.merge_index_hash(repo), before)
        token = preview['confirmation_token']
        for key, value in [('message', 'Changed'), ('author', {'name': 'Other', 'email': 'other@example.invalid'})]:
            with self.assertRaises(ValueError):
                self.call('commit', **{**request, key: value}, confirmation_token=token)
        (repo/'app.py').write_text('different resolution\n')
        with self.assertRaises(ValueError):
            self.call('commit', **request, confirmation_token=token)
        (repo/'app.py').write_text('resolved\n')
        git(repo, 'add', 'app.py')
        with self.assertRaises(ValueError):
            self.call('commit', **request, confirmation_token=token)
        self.assertEqual(len(self.backend.merge_head(repo)), 1)

    def test_merge_lock_and_other_operation_prevent_start(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        for name in ('index.lock', 'CHERRY_PICK_HEAD'):
            path = self.backend.git_path(repo, name)
            path.write_text(git(repo, 'rev-parse', 'HEAD')+'\n')
            preview = self.preview()
            self.assertTrue(preview['blockers'])
            with self.assertRaises(ValueError):
                self.merge(preview)
            path.unlink()

    def test_merge_without_git_identity_still_stops_before_commit(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        git(self.source, 'config', '--unset', 'user.name')
        git(self.source, 'config', '--unset', 'user.email')
        self.assertEqual(self.merge()['outcome'], 'merged_pending_commit')
        self.finish()
        self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_unmanaged_conflict_status_is_reported_without_file_content(self):
        repo = self.prepare()
        self.commit(repo)
        incoming = self.update_origin({'app.py': 'value = 3\n'})
        self.fetch()
        self.backend.git(repo, 'merge', '--no-commit', '--no-ff', incoming, check=False)
        status = self.call('status', id='APP-1')['entries'][0]['merge']
        self.assertTrue(status['in_progress'])
        self.assertFalse(status['managed'])
        self.assertEqual(status['conflicts'], ['app.py'])
        self.assertFalse(status['ready_to_complete'])
        with self.assertRaises(ValueError):
            self.call('merge_abort_preview', id='APP-1', repo='backend')

    def prepare_interrupted_completion(self, before_cas=False):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        self.merge()
        preview, result = self.finish()
        request = {'id': 'APP-1', 'repo': 'backend', 'mode': 'merge', 'message': 'Merge current main', 'author': self.author}
        record = self.backend.merge_record(request)
        record['phase'] = 'committing'
        self.backend.save_merge(request, record)
        self.backend.git_path(repo, 'MERGE_HEAD').write_text(record['source_sha']+'\n')
        if before_cas:
            # Воспроизводим журнал/индекс до CAS на реальных Git-объектах, без моков.
            git(repo, 'update-ref', 'refs/heads/kit/APP-1', record['original_head'], result['commit'])
        return repo, request, preview, result

    def test_recovery_after_commit_object_before_cas_reuses_exact_commit(self):
        repo, request, preview, result = self.prepare_interrupted_completion(before_cas=True)
        recovered = self.call('commit', **request, confirmation_token=preview['confirmation_token'])
        self.assertEqual(recovered['commit'], result['commit'])
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), result['commit'])
        self.assertEqual(self.backend.merge_head(repo), [])

    def test_recovery_after_cas_only_cleans_up_and_rejects_new_data(self):
        repo, request, preview, result = self.prepare_interrupted_completion()
        status = self.call('status', id='APP-1')['entries'][0]['merge']
        self.assertTrue(status['cleanup_required'])
        (repo/'foreign.py').write_text('preserve\n')
        with self.assertRaisesRegex(ValueError, 'посторонние'):
            self.call('commit', **request, confirmation_token=preview['confirmation_token'])
        self.assertEqual((repo/'foreign.py').read_text(), 'preserve\n')
        (repo/'foreign.py').unlink()
        recovered = self.call('commit', **request, confirmation_token=preview['confirmation_token'])
        self.assertEqual(recovered['commit'], result['commit'])
        self.assertEqual(self.backend.merge_head(repo), [])
        self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_conflict_resolution_can_delete_file_and_include_automatic_changes(self):
        repo = self.prepare()
        self.commit(repo)
        self.update_origin({'app.py': 'value = 3\n', 'incoming.py': 'automatic\n'})
        self.fetch()
        self.assertEqual(self.merge()['outcome'], 'conflicts')
        (repo/'app.py').unlink()
        preview, _ = self.finish(['app.py'])
        self.assertEqual([item['path'] for item in preview['files']], ['app.py', 'incoming.py'])
        self.assertEqual(preview['files'][0]['git_blob'], None)
        self.assertEqual(git(repo, 'ls-files'), 'incoming.py')
        self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_equal_result_tree_still_has_two_parents(self):
        repo = self.prepare()
        self.commit(repo)
        self.update_origin({'app.py': 'value = 2\n'})
        self.fetch()
        self.assertEqual(self.merge()['outcome'], 'merged_pending_commit')
        self.assertEqual(git(repo, 'status', '--porcelain'), '')
        cleanup = self.call('cleanup_preview', id='APP-1')
        self.assertFalse(cleanup['can_remove'])
        with self.assertRaises(worker.kit.KitError):
            self.call('cleanup', id='APP-1', confirmation_token=cleanup['confirmation_token'], idle_confirmed=True)
        self.assertTrue(repo.exists())
        preview, result = self.finish()
        self.assertEqual(preview['files'], [])
        self.assertEqual(len(result['parents']), 2)
        self.assertEqual(git(repo, 'rev-parse', 'HEAD^{tree}'), git(repo, 'rev-parse', 'HEAD^1^{tree}'))

    def test_ignored_destination_is_not_overwritten(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        exclude = self.backend.git_path(repo, 'info/exclude')
        with exclude.open('a') as stream:
            stream.write('\nincoming.py\n')
        (repo/'incoming.py').write_text('preserve ignored data\n')
        self.assertEqual(git(repo, 'status', '--porcelain'), '')
        preview = self.preview()
        self.assertIn('Новый merge-путь занят локальными данными', preview['blockers'])
        with self.assertRaises(ValueError):
            self.merge(preview)
        self.assertEqual((repo/'incoming.py').read_text(), 'preserve ignored data\n')

    def test_incoming_symlink_is_blocked_by_metadata(self):
        repo = self.prepare()
        (self.source/'alias.py').symlink_to('app.py')
        git(self.source, 'add', 'alias.py')
        git(self.source, 'commit', '-q', '-m', 'symlink fixture')
        git(self.source, 'push', '-q', 'origin', 'main')
        self.fetch()
        preview = self.preview()
        self.assertEqual(preview['blocked_paths'], ['alias.py'])
        with self.assertRaises(ValueError):
            self.merge(preview)
        self.assertFalse((repo/'alias.py').exists())

    def test_hidden_index_edits_are_not_treated_as_a_clean_worktree(self):
        repo = self.prepare()
        self.update_origin({'app.py': 'value = 3\n'})
        self.fetch()
        git(repo, 'update-index', '--assume-unchanged', 'app.py')
        (repo/'app.py').write_text('hidden user edit\n')
        self.assertEqual(git(repo, 'status', '--porcelain'), '')
        preview = self.preview()
        self.assertEqual(preview['hidden_index_paths'], ['app.py'])
        with self.assertRaises(ValueError):
            self.merge(preview)
        self.assertEqual((repo/'app.py').read_text(), 'hidden user edit\n')

    def test_merge_token_is_stale_after_head_change(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        preview = self.preview()
        self.commit(repo)
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.merge(preview)
        self.assertEqual(self.backend.merge_head(repo), [])

    def test_merge_head_change_revokes_commit_confirmation(self):
        repo = self.prepare()
        self.update_origin()
        self.fetch()
        self.merge()
        request = {'id': 'APP-1', 'repo': 'backend', 'mode': 'merge', 'message': 'Approve', 'author': self.author}
        preview = self.call('commit_preview', **request)
        self.backend.git_path(repo, 'MERGE_HEAD').write_text(git(repo, 'rev-parse', 'HEAD')+'\n')
        with self.assertRaisesRegex(ValueError, 'MERGE_HEAD'):
            self.call('commit', **request, confirmation_token=preview['confirmation_token'])
        self.assertEqual(len(git(repo, 'show', '-s', '--format=%P', 'HEAD').split()), 0)

    def test_foreign_unstaged_tracked_edit_blocks_abort(self):
        (self.source/'other.py').write_text('original\n')
        git(self.source, 'add', 'other.py')
        git(self.source, 'commit', '-q', '-m', 'second tracked file')
        git(self.source, 'push', '-q', 'origin', 'main')
        repo = self.conflict()
        (repo/'other.py').write_text('foreign edit\n')
        with self.assertRaisesRegex(ValueError, 'посторонние'):
            self.call('merge_abort_preview', id='APP-1', repo='backend')
        self.assertEqual((repo/'other.py').read_text(), 'foreign edit\n')

    def test_aborted_pre_cas_completion_cannot_be_resumed(self):
        repo, request, preview, _ = self.prepare_interrupted_completion(before_cas=True)
        abort = self.call('merge_abort_preview', id='APP-1', repo='backend')
        result = self.call('merge_abort', id='APP-1', repo='backend', confirmation_token=abort['confirmation_token'])
        self.assertTrue(result['aborted'])
        with self.assertRaisesRegex(ValueError, 'отменено'):
            self.call('commit', **request, confirmation_token=preview['confirmation_token'])
        self.assertFalse((repo/'incoming.py').exists())

    def test_incoming_gitlink_is_blocked_by_metadata(self):
        repo = self.prepare()
        head = git(self.source, 'rev-parse', 'HEAD')
        git(self.source, 'update-index', '--add', '--cacheinfo', f'160000,{head},vendor')
        git(self.source, 'commit', '-q', '-m', 'gitlink fixture')
        git(self.source, 'push', '-q', 'origin', 'main')
        self.fetch()
        preview = self.preview()
        self.assertEqual(preview['blocked_paths'], ['vendor'])
        with self.assertRaises(ValueError):
            self.merge(preview)
        self.assertFalse((repo/'vendor').exists())

    def test_forbidden_unmanaged_conflict_reports_block_without_content(self):
        repo = self.prepare()
        blob = git(repo, 'rev-parse', 'HEAD:app.py')
        records = ''.join(f'100644 {blob} {stage}\t.env\n' for stage in (1, 2, 3))
        fixtures.subprocess.run(['git', '-C', str(repo), 'update-index', '--index-info'],
                               input=records.encode(), check=True)
        self.backend.git_path(repo, 'MERGE_HEAD').write_text(git(repo, 'rev-parse', 'HEAD')+'\n')
        status = self.call('status', id='APP-1')['entries'][0]['merge']
        self.assertEqual(status['blocked_paths'], ['.env'])
        self.assertFalse(status['ready_to_complete'])
        self.assertFalse((repo/'.env').exists())

    def test_filemode_false_preserves_git_mode_of_conflict_resolution(self):
        repo = self.conflict()
        git(self.source, 'config', 'core.filemode', 'false')
        (repo/'app.py').write_text('resolved\n')
        (repo/'app.py').chmod(0o755)
        preview, _ = self.finish(['app.py'])
        self.assertEqual(preview['files'][0]['mode'], '100644')
        self.assertTrue(git(repo, 'ls-files', '--stage', 'app.py').startswith('100644 '))
        self.assertEqual(git(repo, 'status', '--porcelain'), '')

    def test_missing_confirmation_denies_all_new_mutations(self):
        for action in ('fetch', 'merge', 'merge_abort'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'согласие'):
                self.backend.dispatch({'action': action, 'id': 'APP-1', 'repo': 'backend'})


if __name__ == '__main__':
    unittest.main()
