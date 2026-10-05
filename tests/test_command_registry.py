import unittest

from ixel_mat.commands import build_help_rows, resolve_command_name


class CommandRegistryTests(unittest.TestCase):
    def test_resolves_exact_and_alias_commands(self):
        self.assertEqual(resolve_command_name('configure', mode='cli'), 'setup')
        self.assertEqual(resolve_command_name('q', mode='mat'), 'quit')

    def test_resolves_unique_prefix(self):
        self.assertEqual(resolve_command_name('stat', mode='cli'), 'status')
        self.assertEqual(resolve_command_name('ans', mode='mat'), 'answers')

    def test_returns_ambiguous_for_shared_prefix(self):
        result = resolve_command_name('co', mode='cli')
        self.assertEqual(result[0], 'ambiguous')
        self.assertIn('config', result[1])
        self.assertIn('setup', result[1])

    def test_build_help_rows_filters_by_mode(self):
        rows = build_help_rows(mode='mat')
        commands = [row[0] for row in rows]
        self.assertIn('/compare <prompt>', commands)
        self.assertNotIn('ixel setup', commands)

    def test_old_names_still_work(self):
        self.assertEqual(resolve_command_name('full', mode='mat'), 'full')
        self.assertEqual(resolve_command_name('compare', mode='mat'), 'full')
        self.assertEqual(resolve_command_name('up', mode='cli'), 'update')


if __name__ == '__main__':
    unittest.main()
