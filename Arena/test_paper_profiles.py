"""Offline tests for profile storage; does not start an engine or alter trades."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_profiles as profiles


class ProfileStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot = {'version': 1, 'config': {'market': 'IN'},
                         'state': {'BANKBEES.NS': {'pos': {'qty': 2}}},
                         'trades': [], 'ui': {'symbol_rows': []}}

    def test_save_load_and_isolation(self):
        profiles.save(self.root, 'nse_20_11_2026', self.snapshot, create_only=True)
        other = dict(self.snapshot, config={'market': 'US'}, state={})
        profiles.save(self.root, 'us_20112026', other, create_only=True)
        self.assertEqual(profiles.load(self.root, 'nse_20_11_2026'), self.snapshot)
        self.assertEqual(profiles.load(self.root, 'us_20112026'), other)
        self.assertEqual(len(profiles.list_names(self.root)), 2)
        with self.assertRaises(profiles.ProfileError):
            profiles.save(self.root, 'us_20112026', other, create_only=True)

    def test_failed_replace_preserves_previous_snapshot(self):
        profiles.save(self.root, 'nse', self.snapshot)
        with patch.object(profiles.os, 'replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                profiles.save(self.root, 'nse', dict(self.snapshot, state={}))
        self.assertEqual(profiles.load(self.root, 'nse'), self.snapshot)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_failure_after_backup_before_main_replace_keeps_main(self):
        profiles.save(self.root, 'nse', self.snapshot)
        newer = dict(self.snapshot, state={'BANKBEES.NS': {'pos': {'qty': 3}}})
        replace = profiles.os.replace
        calls = []
        def fail_main(source, target):
            calls.append(target)
            if str(target).endswith('.json'):
                raise OSError('interrupted before main replace')
            return replace(source, target)
        with patch.object(profiles.os, 'replace', side_effect=fail_main):
            with self.assertRaises(OSError):
                profiles.save(self.root, 'nse', newer)
        self.assertEqual(len(calls), 2)
        self.assertEqual(profiles.load(self.root, 'nse'), self.snapshot)
        self.assertEqual(profiles.load_backup(self.root, 'nse'), self.snapshot)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_unserializable_snapshot_does_not_modify_existing_data(self):
        profiles.save(self.root, 'nse', self.snapshot)
        with self.assertRaises(profiles.ProfileError):
            profiles.save(self.root, 'nse', dict(self.snapshot, state={'price': float('nan')}))
        self.assertEqual(profiles.load(self.root, 'nse'), self.snapshot)

    def test_backup_is_previous_generation_and_recovery_is_explicit(self):
        profiles.save(self.root, 'nse', self.snapshot)
        newer = dict(self.snapshot, state={'BANKBEES.NS': {'pos': {'qty': 3}}})
        profiles.save(self.root, 'nse', newer)
        self.assertEqual(profiles.load(self.root, 'nse'), newer)
        self.assertEqual(profiles.load_backup(self.root, 'nse'), self.snapshot)
        (self.root / 'nse.json').write_text('{')
        with self.assertRaises(profiles.ProfileError):
            profiles.load(self.root, 'nse')
        with self.assertRaises(profiles.ProfileError):
            profiles.save(self.root, 'nse', newer)
        self.assertEqual(profiles.load_backup(self.root, 'nse'), self.snapshot)

    def test_names_and_invalid_data_rejected(self):
        for name in ('../escape', '', 'bad/name', '.hidden'):
            with self.assertRaises(profiles.ProfileError):
                profiles.save(self.root, name, self.snapshot)
        with self.assertRaises(profiles.ProfileError):
            profiles.save(self.root, 'bad', {'version': 1})
        (self.root / 'broken.json').write_text('{')
        with self.assertRaises(profiles.ProfileError):
            profiles.load(self.root, 'broken')


if __name__ == '__main__':
    unittest.main()