import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from skill.scripts import doctor


class StrayGitTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.python_temp = self.root / 'custom' / 'tmp'
        self.python_temp.mkdir(parents=True)

    def check(self, *warned_paths):
        output = io.StringIO()
        with (patch.multiple(doctor, TEMP_SCAN_ROOT=self.root, HOME=self.root / 'korolev',
                             TELEGRAM_ENV=self.root / 'telegram.env', results=[]),
              patch.dict('os.environ', HOME=str(self.root)), patch('sys.argv', ['doctor.py', '--offline']),
              patch.object(doctor.tempfile, 'gettempdir', return_value=str(self.python_temp)),
              patch.object(doctor, 'cmd_ok', return_value=(True, ['fake command'])),
              patch.object(doctor.sandbox, 'enabled', return_value=False),
              patch.object(doctor, 'isolate_codex_home', return_value=False),
              patch.object(doctor, 'codex_home', return_value=self.root / 'codex'),
              patch.object(doctor, 'read_projects', return_value={}), redirect_stdout(output),
              self.assertRaises(SystemExit) as exit_status):
            doctor.main()
        self.assertEqual(exit_status.exception.code, 0, output.getvalue())
        self.assertEqual([line for line in output.getvalue().splitlines() if 'stray Git metadata' in line], [
            f'WARN stray Git metadata: Git discovery from temp folders may find {path}' for path in warned_paths
        ])

    def test_warns_once_for_stray_git_at_root(self):
        marker = self.root / '.git'
        marker.mkdir()
        self.check(marker)
        self.assertTrue(marker.is_dir())

    def test_warns_for_head_and_objects_above_python_temp(self):
        bare = self.python_temp.parent
        (bare / 'HEAD').touch()
        (bare / 'objects').mkdir()
        self.check(bare)

    def test_warns_for_bare_metadata_without_head(self):
        bare = self.python_temp.parent
        (bare / 'config').touch()
        (bare / 'objects').mkdir()
        (bare / 'refs').mkdir()
        self.check(bare)

    def test_warns_for_head_and_commondir_above_resolved_system_temp(self):
        metadata = self.root / 'storage'
        target = metadata / 'temp'
        target.mkdir(parents=True)
        (metadata / 'HEAD').touch()
        (metadata / 'commondir').touch()
        (self.root / 'var').mkdir()
        (self.root / 'var' / 'tmp').symlink_to(target, target_is_directory=True)
        self.check(metadata)

    def test_warns_for_dangling_git_entry(self):
        tmp = self.root / 'tmp'
        tmp.mkdir()
        marker = tmp / '.git'
        marker.symlink_to('missing')
        self.check(marker)

    def test_clean_roots_do_not_warn(self):
        (self.root / 'HEAD').touch()
        (self.python_temp / 'objects').mkdir()
        self.check()
