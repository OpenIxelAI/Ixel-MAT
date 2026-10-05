import unittest

from ixel_mat.config.secrets import normalize_secret_input
from ixel_mat.mat import normalize_interactive_command


class NormalizeSecretInputTests(unittest.TestCase):
    def test_strips_newlines_and_non_latin1_garbage(self):
        raw = ' sk-abc\r\n123🙂\n '
        self.assertEqual(normalize_secret_input(raw), 'sk-abc123')

    def test_preserves_valid_plain_secret(self):
        self.assertEqual(normalize_secret_input('xai-token-123'), 'xai-token-123')


class NormalizeInteractiveCommandTests(unittest.TestCase):
    def test_maps_common_ixel_cli_commands_to_mat_commands(self):
        self.assertEqual(normalize_interactive_command('ixel agents'), '/agents')
        self.assertEqual(normalize_interactive_command('ixel status'), '/agents')
        self.assertEqual(normalize_interactive_command('ixel help'), '/help')
        self.assertEqual(normalize_interactive_command('ixel config'), '/config')

    def test_leaves_normal_prompt_unchanged(self):
        self.assertEqual(normalize_interactive_command('hello there'), 'hello there')


if __name__ == '__main__':
    unittest.main()
