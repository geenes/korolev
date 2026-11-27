#!/usr/bin/env python3
"""Create, inspect or remove task worktrees for a registered project."""
import argparse
import fcntl
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (git_common_dir, git_status, is_task_worktree, launcher_git, now, product_dir,  # noqa: E402
                    read_projects, read_tasks, task_lock_name, task_worktree, tasks_root, valid_product,
                    write_atomic)
from procs import LockBusyError, run_lock  # noqa: E402


def registered_project(product):
    project = read_projects().get(product) if valid_product(product) else None
    if project is None:
        raise ValueError(f'project {product!r} is not registered; run onboard.py attach first')
    return project


@contextmanager
def edit_tasks(product):
    directory = product_dir(product)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'tasks.json.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = read_tasks(product)
        yield records
        write_atomic(directory / 'tasks.json', json.dumps(records, indent=2) + '\n')


def check_slug(slug):
    if not re.fullmatch(r'[0-9]{2}[a-z0-9-]*', slug):
        raise ValueError('invalid task slug; use two digits followed by lowercase letters, digits or hyphens')


def owner_folder(project):
    folder = Path(project['folder']).resolve()
    if git_common_dir(folder) != Path(project['git_common_dir']).resolve():
        raise ValueError('the owner folder no longer uses the registered Git common directory')
    return folder


def check_remote_urls(folder):
    result = launcher_git(folder, 'config', '--null', '--get-regexp', r'^remote\..*\.(url|pushurl)$')
    if result.returncode not in (0, 1):
        raise ValueError('cannot check Git remote configuration')
    for entry in os.fsdecode(result.stdout).split('\0'):
        key, _, url = entry.partition('\n')
        userinfo = re.match(r'([a-z][a-z0-9+.-]*)://([^/?#@]+)@', url.lstrip(), re.IGNORECASE)
        if userinfo and (':' in userinfo[2] or userinfo[1].lower() in ('http', 'https')):
            remote = key.removeprefix('remote.').rsplit('.', 1)[0]
            raise ValueError(f'Git remote {remote!r} has embedded credentials')


def existing_branch_path(folder, branch, expected):
    result = launcher_git(folder, 'worktree', 'list', '--porcelain', '-z')
    for worktree in os.fsdecode(result.stdout).split('\0\0'):
        fields = dict(item.split(' ', 1) for item in worktree.split('\0') if ' ' in item)
        if fields.get('branch') == f'refs/heads/{branch}':
            return fields['worktree']
    return expected


def new_task(product, slug, base='HEAD'):
    project = registered_project(product)
    check_slug(slug)
    folder = owner_folder(project)
    root = tasks_root(product).resolve()
    path = root / task_worktree(product, slug).name
    if (root.is_relative_to(folder) or folder.is_relative_to(path)
            or root.is_relative_to(Path(project['git_common_dir']).resolve())
            or product_dir(product).resolve().is_relative_to(folder)):
        raise ValueError('task worktrees must be outside the owner folder')
    branch = f'korolev/{slug}'
    with edit_tasks(product) as records:
        if slug in records:
            raise ValueError(f"task {slug} already exists at {records[slug]['path']}")
        if path.exists() or path.is_symlink():
            raise ValueError(f'task path already exists at {path}')
        result = launcher_git(folder, 'show-ref', '--verify', '--quiet', f'refs/heads/{branch}')
        if result.returncode == 0:
            existing = existing_branch_path(folder, branch, path)
            raise ValueError(f'task branch {branch} already exists (task path {existing})')
        if result.returncode != 1:
            raise ValueError(f'cannot check task branch {branch}')
        check_remote_urls(folder)
        result = launcher_git(folder, 'rev-parse', '--verify', '--end-of-options', f'{base}^{{commit}}')
        if result.returncode:
            raise ValueError('cannot resolve the task base to a commit')
        base_sha = result.stdout.decode('ascii').strip()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        result = launcher_git(folder, 'worktree', 'add', '-b', branch, '--', str(path), base_sha)
        if result.returncode:
            raise ValueError(f'git worktree add failed for {path}')
        record = dict(slug=slug, path=str(path), branch=branch, base_sha=base_sha, created=now())
        records[slug] = record
    return record


def list_tasks(product):
    registered_project(product)
    tasks = []
    for record in read_tasks(product).values():
        path = Path(record['path'])
        head = dirty_count = None
        if is_task_worktree(product, path):
            result = launcher_git(path, 'rev-parse', '--verify', 'HEAD')
            if result.returncode == 0:
                head = result.stdout.decode('ascii').strip()
            dirty_count = sum(1 for _ in git_status(path))
        tasks.append(dict(record, exists=path.is_dir(), head=head, dirty_count=dirty_count))
    return tasks


def delete_task_branch(folder, branch, owner_branch):
    # Give Git's own -d check the recorded branch as the upstream for this command.
    # A private refspec also handles an existing branch.<name>.merge value, which Git appends to.
    merge_key = f'branch.{branch}.merge'
    current = launcher_git(folder, 'config', '--null', '--get-all', merge_key)
    source = os.fsdecode(current.stdout.partition(b'\0')[0]) or owner_branch
    remote = f'korolev-task-delete-{uuid.uuid4().hex}'
    return launcher_git(folder, '-c', f'branch.{branch}.remote={remote}', '-c', f'{merge_key}={owner_branch}',
                        '-c', f'remote.{remote}.url=.', '-c', f'remote.{remote}.fetch=+{source}:{owner_branch}',
                        'branch', '-d', '--', branch)


def remove_task(product, slug, *, delete_branch=False):
    project = registered_project(product)
    check_slug(slug)
    folder = owner_folder(project)
    with edit_tasks(product) as records:
        record = records.get(slug)
        if record is None:
            raise ValueError(f'task {slug} is not recorded for {product}')
        path = Path(record['path'])
        with run_lock(task_lock_name(product, path)):
            if not is_task_worktree(product, path):
                raise ValueError(f'{path} is no longer a registered task worktree')
            if next(git_status(path), None) is not None:
                raise ValueError(f'task worktree is dirty at {path}')
            branch = record['branch']
            owner_branch = f"refs/heads/{project['branch']}"
            if delete_branch and launcher_git(folder, 'merge-base', '--is-ancestor',
                                              f'refs/heads/{branch}', owner_branch).returncode:
                raise ValueError(f"task branch {branch} is not merged into recorded branch {project['branch']!r}")
            if launcher_git(folder, 'worktree', 'remove', '--', str(path)).returncode:
                raise ValueError(f'git worktree remove failed for {path}')
            del records[slug]
            if delete_branch:
                deleted = delete_task_branch(folder, branch, owner_branch)
    if delete_branch and deleted.returncode:
        raise ValueError(f'task worktree removed, but git branch -d failed for {branch}')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('new', 'list', 'remove'):
        subparser = commands.add_parser(command)
        subparser.add_argument('--product', required=True)
        if command != 'list':
            subparser.add_argument('--slug', required=True)
        if command == 'new':
            subparser.add_argument('--base', default='HEAD')
        if command == 'remove':
            subparser.add_argument('--delete-branch', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'new':
            result = new_task(args.product, args.slug, args.base)
        elif args.command == 'list':
            result = list_tasks(args.product)
        else:
            result = remove_task(args.product, args.slug, delete_branch=args.delete_branch)
    except (ValueError, OSError, LockBusyError) as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
