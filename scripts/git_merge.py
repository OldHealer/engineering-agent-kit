"""Слияние по подтверждённому SHA; рабочая ветка меняется только при commit."""

import hashlib
from pathlib import Path
import re
import stat
import time

import kit


class MergeOperations:
    def merge_require(self, condition, message):
        if not condition:
            # Тип отказа совпадает с остальным backend, без циклического импорта.
            raise self.error_type(message)

    def git_path(self, repo, name):
        value = Path(self.text(repo, 'rev-parse', '--git-path', name))
        return self.inside(value if value.is_absolute() else repo/value)

    def merge_storage(self, request):
        folder = self.inside(self.project/'.engineering-kit/runs'/kit.valid_name(request['id'])/'merges')
        return folder/self.merge_alias(request['repo'])

    def merge_alias(self, alias):
        return kit.valid_name(alias)+'.json'

    def merge_record(self, request):
        path = self.inside(self.merge_storage(request))
        if not path.exists():
            return None
        record = kit.load(path)
        self.merge_require(record.get('version') == 1 and record.get('id') == request['id']
                           and record.get('repo') == request['repo'], 'Некорректный журнал слияния')
        self.merge_require(record.get('phase') in {'starting', 'pending', 'committing', 'complete', 'aborted', 'failed'},
                           'Неизвестная стадия слияния')
        for key in ('original_head', 'source_sha'):
            self.merge_require(isinstance(record.get(key), str) and re.fullmatch(r'[a-f0-9]{40,64}', record[key]),
                               'Некорректные родители слияния')
        for name in record.get('owned', []):
            if name not in record.get('blocked_paths', []):
                self.safe_file(name)
        return record

    def save_merge(self, request, record):
        path = self.inside(self.merge_storage(request))
        path.parent.mkdir(exist_ok=True)
        kit.save(path, record)

    def merge_flags(self, repo):
        return [name for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'REBASE_HEAD',
                                  'rebase-merge', 'rebase-apply', 'sequencer', 'MERGE_AUTOSTASH', 'BISECT_LOG')
                if self.git_path(repo, name).exists()]

    def merge_locks(self, repo, branch):
        common = Path(self.text(repo, 'rev-parse', '--git-common-dir'))
        common = self.inside(common if common.is_absolute() else repo/common)
        paths = [self.git_path(repo, name) for name in ('index.lock', 'HEAD.lock')]
        paths += [self.inside(common/name) for name in ('packed-refs.lock', f'refs/heads/{branch}.lock')]
        return [str(path.relative_to(self.project)) for path in paths if path.exists()]

    def merge_status_paths(self, repo):
        raw = self.git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--no-renames').stdout
        return {item[3:].decode(): item[:2].decode() for item in raw.split(b'\0') if item}

    def merge_index(self, repo):
        result = {}
        for item in self.git(repo, 'ls-files', '--stage', '-z').stdout.split(b'\0'):
            if item:
                prefix, name = item.split(b'\t', 1)
                mode, blob, stage = prefix.decode().split()
                result.setdefault(name.decode(), {})[stage] = [mode, blob]
        return result

    def hidden_index_paths(self, repo):
        # status не показывает часть локальных правок при этих флагах индекса.
        return [item[2:].decode() for item in self.git(repo, 'ls-files', '-v', '-z').stdout.split(b'\0')
                if item and (item[:1].islower() or item[:1] == b'S')]

    def merge_index_hash(self, repo):
        path = self.git_path(repo, 'index')
        if not path.exists():
            return None
        with path.open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def merge_head(self, repo):
        path = self.git_path(repo, 'MERGE_HEAD')
        if not path.exists():
            return []
        values = path.read_text().splitlines()
        self.merge_require(all(re.fullmatch(r'[a-f0-9]{40,64}', value) for value in values),
                           'Некорректный MERGE_HEAD')
        return values

    def merge_file(self, repo, name, markers=False):
        path = self.inside(repo/self.safe_file(name))
        if not path.exists():
            return {'path': name, 'sha256': None, 'mode': None, 'git_blob': None}
        self.merge_require(path.is_file() and not path.is_symlink(), 'Слияние поддерживает только обычные файлы')
        with path.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            if markers:
                stream.seek(0)
                self.merge_require(not any(re.match(br'^(<<<<<<< |>>>>>>> )', line) for line in stream),
                                   'В разрешённом файле остались маркеры конфликта')
        filemode = stat.S_IMODE(path.stat().st_mode)
        mode = '100755' if filemode & 0o111 else '100644'
        trust_mode = self.git(repo, 'config', '--bool', '--local', '--no-includes', '--get', 'core.filemode', check=False).stdout.strip()
        if trust_mode == b'false':
            stages = self.merge_index(repo).get(name, {})
            previous = next((stages[key] for key in ('0', '2', '3', '1') if key in stages), None)
            mode = previous[0] if previous else '100644'
        blob = self.text(repo, 'hash-object', f'--path={name}', '--', name)
        return {'path': name, 'sha256': checksum, 'mode': mode, 'filesystem_mode': filemode, 'git_blob': blob}

    def merge_tree_entries(self, repo, revision):
        result = {}
        for item in self.git(repo, 'ls-tree', '-r', '-z', revision).stdout.split(b'\0'):
            if item:
                prefix, name = item.split(b'\t', 1)
                mode, _, blob = prefix.decode().split()
                result[name.decode()] = [mode, blob]
        return result

    def fetch_receipt_path(self, alias, revision):
        folder = self.inside(self.project/'.engineering-kit/fetches')
        return folder/(self.state_digest([alias, revision])+'.json')

    def fetch_revision(self, request):
        _, config = self.validate_registry()
        alias = kit.valid_name(request['repo'])
        self.merge_require(alias in config['repos'], 'Репозиторий не зарегистрирован')
        repo = self.metadata(Path(config['repos'][alias]))
        branch = self.branch_name(request.get('branch', 'main'))
        revision = f'origin/{branch}'
        target = f'refs/remotes/{revision}'
        self.merge_require(not self.merge_locks(repo, branch), 'Git заблокирован другой операцией')
        before = self.git(repo, 'rev-parse', '--verify', target, check=False).stdout.decode().strip() or None
        # Сначала только FETCH_HEAD: non-fast-forward не должен изменить даже tracking ref.
        self.git(repo, 'fetch', '--no-tags', 'origin', f'refs/heads/{branch}')
        sha = self.text(repo, 'rev-parse', '--verify', 'FETCH_HEAD^{commit}')
        self.merge_require(not before or self.git(repo, 'merge-base', '--is-ancestor', before, sha, check=False).returncode == 0,
                           'Origin переписал историю; tracking ref сохранён, требуется отдельное решение')
        self.git(repo, 'update-ref', target, sha, before or '0'*len(sha))
        origin = self.text(repo, 'config', '--local', '--no-includes', '--get', 'remote.origin.url')
        receipt = {'repo': alias, 'revision': revision, 'sha': sha, 'origin_digest': self.state_digest(origin),
                   'fetched_at_ns': time.time_ns()}
        path = self.inside(self.fetch_receipt_path(alias, revision))
        path.parent.mkdir(exist_ok=True)
        kit.save(path, receipt)
        return {'repo': alias, 'path': str(repo), 'revision': revision, 'before': before, 'sha': sha,
                'updated': before != sha, 'working_files_changed': False, 'worktree_created': False}

    def merge_preview(self, request):
        repo, entry = self.entry(request['id'], request['repo'])
        _, config = self.validate_registry()
        target = self.workspace_settings(config)['target_branch']
        self.merge_require(entry['branch'] not in {'main', 'master', target}, 'Слияние в защищённую или целевую ветку запрещено')
        revision = request.get('revision', 'origin/main')
        self.merge_require(isinstance(revision, str) and revision.startswith('origin/'), 'Нужна явная ссылка origin/ветка')
        branch = self.branch_name(revision[7:])
        ref = f'refs/remotes/origin/{branch}'
        sha = self.text(repo, 'rev-parse', '--verify', ref+'^{commit}')
        receipt_path = self.inside(self.fetch_receipt_path(request['repo'], revision))
        self.merge_require(receipt_path.is_file(), 'Сначала выполните отдельный kit_git_fetch для этой ветки')
        receipt = kit.load(receipt_path)
        origin = self.text(Path(entry['source']), 'config', '--local', '--no-includes', '--get', 'remote.origin.url')
        self.merge_require(receipt.get('sha') == sha and receipt.get('origin_digest') == self.state_digest(origin),
                           'Remote-ссылка или origin изменились; повторите kit_git_fetch')
        head = self.text(repo, 'rev-parse', 'HEAD')
        changes = self.merge_status_paths(repo)
        flags = self.merge_flags(repo)
        locks = self.merge_locks(repo, entry['branch'])
        record = self.merge_record(request)
        self.check_private_artifacts(repo)
        blockers = []
        hidden = self.hidden_index_paths(repo)
        if hidden:
            blockers.append('Индекс содержит assume-unchanged/skip-worktree; status недостаточен для безопасного merge')
        if changes:
            blockers.append('Есть staged, unstaged или untracked изменения')
        if flags:
            blockers.append('Есть незавершённая Git-операция')
        if locks:
            blockers.append('Git заблокирован другой операцией')
        if record and record['phase'] in {'starting', 'pending', 'committing', 'failed'}:
            blockers.append('Предыдущее слияние kit требует проверки status')
        bases = self.text(repo, 'merge-base', '--all', head, sha).splitlines()
        self.merge_require(len(bases) == 1, 'Нужна одна общая база; unrelated/criss-cross merge не поддерживается')
        scenario = ('already_up_to_date' if self.git(repo, 'merge-base', '--is-ancestor', sha, head, check=False).returncode == 0
                    else 'fast_forward_possible' if self.git(repo, 'merge-base', '--is-ancestor', head, sha, check=False).returncode == 0
                    else 'merge')
        paths = set()
        incoming = set()
        for tip in (head, sha):
            names = {p.decode() for p in self.git(repo, 'diff', '--no-renames', '--name-only', '-z', bases[0], tip).stdout.split(b'\0') if p}
            paths.update(names)
            if tip == sha:
                incoming.update(names)
        unsafe = []
        for name in sorted(paths):
            try:
                self.safe_file(name)
                path = self.inside(repo/name)
                self.merge_require(not path.is_symlink(), 'Симлинк не поддерживается')
            except (ValueError, kit.KitError):
                unsafe.append(name)
        for tip in (head, sha, bases[0]):
            for name, (mode, _) in self.merge_tree_entries(repo, tip).items():
                if name in paths and mode not in {'100644', '100755'}:
                    unsafe.append(name)
        if unsafe:
            blockers.append('Затрагиваются запрещённые пути, симлинки или submodules')
        for name in incoming:
            path = repo/name
            if path.exists() and self.git(repo, 'ls-files', '--error-unmatch', '--', name, check=False).returncode:
                blockers.append('Новый merge-путь занят локальными данными')
                break
        return {'id': request['id'], 'repo': request['repo'], 'path': str(repo), 'branch': entry['branch'],
                'workspace': entry.get('workspace', 'worktree'), 'head': head, 'revision': revision, 'source_sha': sha,
                'fetch_receipt': receipt, 'index_sha256': self.merge_index_hash(repo), 'changes': changes,
                'operations': flags, 'locks': locks, 'hidden_index_paths': hidden, 'scenario': scenario, 'base': bases[0],
                'affected_paths': sorted(paths), 'incoming_paths': sorted(incoming), 'blocked_paths': sorted(set(unsafe)),
                'blockers': blockers, 'conflict_prediction': 'not_performed',
                'prediction_reason': 'Preview не выполняет merge и не создаёт Git-объекты'}

    def merge_start(self, request):
        preview = self.merge_preview(request)
        if request['action'] == 'merge_preview':
            return {**preview, 'confirmation_token': self.state_digest(preview)}
        self.merge_require(request.get('confirmation_token') == self.state_digest(preview),
                           'Состояние или SHA изменились; повторите merge_preview')
        self.merge_require(not preview['blockers'], 'Слияние заблокировано: '+ '; '.join(preview['blockers']))
        if preview['scenario'] == 'already_up_to_date':
            return {**preview, 'outcome': 'already_up_to_date', 'head_changed': False}
        repo = Path(preview['path'])
        record = {'version': 1, 'id': request['id'], 'repo': request['repo'], 'phase': 'starting',
                  'original_head': preview['head'], 'source_sha': preview['source_sha'], 'branch': preview['branch'],
                  'revision': preview['revision'], 'owned': preview['incoming_paths']}
        self.save_merge(request, record)
        result = self.git(repo, '-c', 'user.name=Engineering Agent Kit', '-c', 'user.email=kit@example.invalid',
                          'merge', '--no-commit', '--no-ff', '--no-edit', '--no-autostash',
                          '--no-overwrite-ignore', '--no-rerere-autoupdate', preview['source_sha'], check=False)
        head = self.text(repo, 'rev-parse', 'HEAD')
        parents = self.merge_head(repo)
        changes = self.merge_status_paths(repo)
        record['owned'] = sorted(set(record['owned']) | set(changes))
        # Проверка имён предшествует любому чтению рабочего содержимого.
        blocked = []
        for name in record['owned']:
            try:
                self.safe_file(name)
            except ValueError:
                blocked.append(name)
        record['blocked_paths'] = blocked
        record['phase'] = 'pending' if head == preview['head'] and parents == [preview['source_sha']] else 'failed'
        self.save_merge(request, record)
        conflicts = [name for name, stages in self.merge_index(repo).items() if set(stages) != {'0'}]
        return {'id': request['id'], 'repo': request['repo'], 'head': head, 'parents': [record['original_head'], *parents],
                'outcome': 'blocked' if blocked else 'conflicts' if record['phase'] == 'pending' and conflicts
                else 'merged_pending_commit' if record['phase'] == 'pending' and result.returncode == 0 else 'failed',
                'conflicts': conflicts, 'blocked_paths': blocked, 'head_changed': head != preview['head'],
                'git_exit_code': result.returncode}

    def pending_merge(self, request):
        repo, entry = self.entry(request['id'], request['repo'])
        _, config = self.validate_registry()
        self.merge_require(entry['branch'] not in {'main', 'master', self.workspace_settings(config)['target_branch']},
                           'Слияние в защищённую или целевую ветку запрещено')
        record = self.merge_record(request)
        self.merge_require(record and record['phase'] in {'pending', 'committing'}, 'Нет управляемого незавершённого merge')
        self.merge_require(not record.get('blocked_paths'), 'Merge затрагивает запрещённый файл; требуется отдельное решение')
        self.merge_require(record['branch'] == entry['branch'], 'Контекст ветки merge изменился')
        self.merge_require(self.text(repo, 'rev-parse', 'HEAD') == record['original_head'], 'HEAD merge изменился')
        self.merge_require(self.merge_head(repo) == [record['source_sha']], 'MERGE_HEAD изменился')
        self.merge_require(self.text(repo, 'rev-parse', 'ORIG_HEAD') == record['original_head'], 'ORIG_HEAD изменился')
        self.merge_require(self.merge_flags(repo) == ['MERGE_HEAD'], 'Обнаружена другая незавершённая операция')
        self.merge_require(not self.merge_locks(repo, entry['branch']), 'Git заблокирован другой операцией')
        self.merge_require(not self.hidden_index_paths(repo), 'Индекс скрывает локальные изменения; завершение/отмена merge заблокированы')
        changes = self.merge_status_paths(repo)
        self.merge_require(set(changes).issubset(record['owned']), 'Есть посторонние изменения вне merge; сначала сохраните их отдельно')
        return repo, entry, record, changes

    def merge_commit_snapshot(self, request):
        self.merge_require('files' not in request, 'В mode=merge частичный files запрещён; показывается весь результат')
        repo, entry, record, changes = self.pending_merge(request)
        self.check_private_artifacts(repo)
        index = self.merge_index(repo)
        unmerged = {name for name, stages in index.items() if set(stages) != {'0'}}
        resolved = request.get('resolved_files', [])
        self.merge_require(isinstance(resolved, list) and len(resolved) == len(set(resolved)) and set(resolved) == unmerged,
                           'resolved_files должен явно перечислять все текущие unmerged пути')
        entries = {name: stages['0'] for name, stages in index.items() if set(stages) == {'0'}}
        snapshots = {name: self.merge_file(repo, name, markers=True) for name in record['owned']}
        for name, snap in snapshots.items():
            if name in changes or name in unmerged:
                if snap['git_blob'] is None:
                    entries.pop(name, None)
                else:
                    entries[name] = [snap['mode'], snap['git_blob']]
        original = self.merge_tree_entries(repo, record['original_head'])
        names = sorted(name for name in set(original) | set(entries) if original.get(name) != entries.get(name))
        self.merge_require(set(names).issubset(record['owned']), 'Полный индекс содержит посторонние файлы')
        for name in names:
            self.safe_file(name)
        self.merge_require(len(names) <= 1000, 'Результат merge слишком велик для безопасного предпросмотра')
        author, message = self.commit_identity(repo, request)
        payload = {'mode': 'merge', 'id': request['id'], 'repo': request['repo'], 'path': str(repo),
                   'workspace': entry.get('workspace', 'worktree'), 'branch': entry['branch'],
                   'head': record['original_head'], 'parents': [record['original_head'], record['source_sha']],
                   'files': [snapshots[name] for name in names], 'resolved_files': sorted(resolved),
                   'message': message, 'author': author, 'index_sha256': self.merge_index_hash(repo),
                   'index_manifest_sha256': self.state_digest(entries), 'working_files': list(snapshots.values())}
        stage = sorted(name for name, xy in changes.items() if xy[1] != ' ' or name in unmerged)
        return payload, entries, stage

    def finish_merge_cleanup(self, request, record, repo):
        self.merge_require(self.text(repo, 'rev-parse', 'HEAD') == record['commit'], 'HEAD завершённого merge изменился')
        parents = self.text(repo, 'show', '-s', '--format=%P', record['commit']).split()
        self.merge_require(parents == [record['original_head'], record['source_sha']], 'Родители завершённого merge не совпадают')
        self.merge_require(self.merge_flags(repo) in ([], ['MERGE_HEAD'])
                           and not self.merge_locks(repo, record['branch']), 'Другая операция блокирует очистку merge')
        current = self.merge_head(repo)
        self.merge_require(current in ([], [record['source_sha']]), 'MERGE_HEAD принадлежит другой операции')
        if current:
            self.git(repo, 'merge', '--quit')
        record['phase'] = 'complete'
        self.save_merge(request, record)
        return {'commit': record['commit'], 'branch': record['branch'], 'parents': parents,
                'tree': record['tree'], 'mode': 'merge', 'merge_complete': True}

    def merge_commit(self, request):
        record = self.merge_record(request)
        if request['action'] == 'commit' and record and record.get('commit'):
            self.merge_require(record['phase'] in {'committing', 'complete'}, 'Слияние отменено или журнал требует отдельной проверки')
            self.merge_require(request.get('confirmation_token') == record.get('commit_token')
                               and self.state_digest({k: v for k, v in request.items() if k not in {'action', 'confirm', 'confirmation_token'}})
                               == record.get('commit_request'), 'Запрос завершения merge изменился')
            repo, _ = self.entry(request['id'], request['repo'])
            self.merge_require(not self.hidden_index_paths(repo), 'Индекс скрывает локальные изменения')
            _, config = self.validate_registry()
            self.merge_require(record['branch'] not in {'main', 'master', self.workspace_settings(config)['target_branch']},
                               'Целевая ветка изменилась; восстановление merge заблокировано')
            self.merge_require(set(self.merge_status_paths(repo)).issubset(record['owned']),
                               'Есть посторонние изменения; восстановление merge заблокировано')
            head = self.text(repo, 'rev-parse', 'HEAD')
            self.merge_require(head in {record['original_head'], record['commit']}, 'HEAD восстановления merge изменился')
            self.merge_require(self.merge_index_hash(repo) == record['committed_index_sha256']
                               and [self.merge_file(repo, item['path'], markers=True) for item in record['working_files']]
                               == record['working_files'], 'Индекс или файлы восстановления merge изменились')
            if head == record['original_head']:
                self.pending_merge(request)
                self.git(repo, 'update-ref', '-m', 'engineering-agent-kit merge commit',
                         f"refs/heads/{record['branch']}", record['commit'], record['original_head'])
            return self.finish_merge_cleanup(request, record, repo)
        preview, entries, stage = self.merge_commit_snapshot(request)
        if request['action'] == 'commit_preview':
            return {**preview, 'confirmation_token': self.state_digest(preview)}
        token = self.state_digest(preview)
        self.merge_require(request.get('confirmation_token') == token, 'Merge, индекс, файлы или сообщение изменились; повторите commit_preview')
        repo, _, record, _ = self.pending_merge(request)
        if stage:
            self.git(repo, 'add', '-A', '--', *stage)
        index = self.merge_index(repo)
        self.merge_require(all(set(value) == {'0'} for value in index.values())
                           and {name: value['0'] for name, value in index.items()} == entries,
                           'Индекс после разрешения конфликтов отличается от согласованного')
        self.merge_require([self.merge_file(repo, item['path'], markers=True) for item in preview['working_files']]
                           == preview['working_files'], 'Рабочие файлы изменились при подготовке merge-коммита')
        tree = self.text(repo, 'write-tree')
        self.merge_require(self.merge_tree_entries(repo, tree) == entries, 'Дерево merge изменилось')
        commit = self.text(repo, '-c', f"user.name={preview['author']['name']}", '-c', f"user.email={preview['author']['email']}",
                           'commit-tree', tree, '-p', record['original_head'], '-p', record['source_sha'], '-m', preview['message'])
        record.update(phase='committing', commit=commit, tree=tree, commit_token=token,
                      committed_index_sha256=self.merge_index_hash(repo), working_files=preview['working_files'],
                      commit_request=self.state_digest({k: v for k, v in request.items() if k not in {'action', 'confirm', 'confirmation_token'}}))
        self.save_merge(request, record)
        self.pending_merge(request)
        self.merge_require(self.merge_index_hash(repo) == record['committed_index_sha256']
                           and [self.merge_file(repo, item['path'], markers=True) for item in record['working_files']]
                           == record['working_files'], 'Индекс или файлы изменились перед commit')
        self.git(repo, 'update-ref', '-m', 'engineering-agent-kit merge commit', f"refs/heads/{record['branch']}", commit, record['original_head'])
        return self.finish_merge_cleanup(request, record, repo)

    def merge_abort(self, request):
        repo, entry, record, changes = self.pending_merge(request)
        snapshots = [self.merge_file(repo, name) for name in record['owned']]
        preview = {'id': request['id'], 'repo': request['repo'], 'path': str(repo), 'branch': entry['branch'],
                   'head': record['original_head'], 'parents': [record['original_head'], record['source_sha']],
                   'will_discard': sorted(changes), 'working_files': snapshots, 'index_sha256': self.merge_index_hash(repo)}
        if request['action'] == 'merge_abort_preview':
            return {**preview, 'confirmation_token': self.state_digest(preview)}
        self.merge_require(request.get('confirmation_token') == self.state_digest(preview),
                           'Файлы или merge-состояние изменились; повторите merge_abort_preview')
        result = self.git(repo, 'merge', '--abort', check=False)
        if result.returncode:
            return {**preview, 'aborted': False, 'outcome': 'blocked', 'git_exit_code': result.returncode}
        self.merge_require(self.text(repo, 'rev-parse', 'HEAD') == record['original_head'] and not self.merge_head(repo),
                           'Результат abort требует ручной проверки status')
        record['phase'] = 'aborted'
        self.save_merge(request, record)
        return {'id': request['id'], 'repo': request['repo'], 'aborted': True, 'head': record['original_head'],
                'remaining_changes': self.merge_status_paths(repo)}

    def merge_status(self, request):
        repo, _ = self.entry(request['id'], request['repo'])
        record = self.merge_record(request)
        parents = self.merge_head(repo)
        index = self.merge_index(repo)
        conflicts = sorted(name for name, stages in index.items() if set(stages) != {'0'})
        head = self.text(repo, 'rev-parse', 'HEAD')
        blockers = []
        if parents:
            try:
                self.pending_merge(request)
            except (ValueError, kit.KitError) as error:
                blockers.append(str(error))
        blocked = set(record.get('blocked_paths', []) if record else [])
        for name in conflicts:
            try:
                self.safe_file(name)
            except ValueError:
                blocked.add(name)
        if blocked:
            blockers.append('Конфликт затрагивает запрещённый файл; содержимое не читается')
        if conflicts:
            blockers.append('Нужно исправить конфликты и передать resolved_files в commit_preview(mode=merge)')
        return {'in_progress': bool(parents), 'managed': bool(record and parents == [record['source_sha']]),
                'phase': record['phase'] if record else None,
                'parents': [record['original_head'] if record and parents else head, *parents],
                'conflicts': conflicts, 'blocked_paths': sorted(blocked),
                'ready_to_complete': bool(parents) and not blockers, 'blockers': blockers,
                'cleanup_required': bool(record and record.get('commit') == head and record['phase'] == 'committing')}
