import sys
"""Offline checks for the language setting and owner messages."""
import ast
import json
import unicodedata
from string import Formatter

if __package__:
    from .test_skill import SCRIPTS, Sandbox
else:
    from test_skill import SCRIPTS, Sandbox


class LanguageTests(Sandbox):
    def configure(self, config):
        path = self.base / 'config' / 'config.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(config), encoding='utf-8')

    def language(self):
        return self.py('from common import language\nprint(language())')

    def test_defaults_to_english(self):
        self.assertEqual(self.language(), 'en')
        self.configure({})
        self.assertEqual(self.language(), 'en')

    def test_russian_config_switches_owner_messages(self):
        receipt = "from i18n import t\nprint(t('inbox_received', project='Demo'))"
        self.assertEqual(self.py(receipt), 'Received ✓ Project: Demo. Passed to Claude.')
        self.configure({'language': 'ru'})
        self.assertEqual(self.language(), 'ru')
        self.assertEqual(self.py(receipt), 'Принято ✓ Проект: Demo. Передал Claude.')

    def test_unknown_language_falls_back_to_english(self):
        for value in ('fr', None, ['ru']):
            with self.subTest(value=value):
                self.configure({'language': value})
                self.assertEqual(self.language(), 'en')

    def test_catalogs_have_the_same_keys_and_placeholders(self):
        catalogs = json.loads(self.py('import json\nfrom i18n import MESSAGES\nprint(json.dumps(MESSAGES))'))
        self.assertEqual(set(catalogs['en']), set(catalogs['ru']))
        formatter = Formatter()
        for key, english in catalogs['en'].items():
            with self.subTest(key=key):
                russian = catalogs['ru'][key]
                self.assertCountEqual([field for _, field, _, _ in formatter.parse(english) if field],
                                      [field for _, field, _, _ in formatter.parse(russian) if field])

    def test_cyrillic_literals_are_limited_to_catalog_and_inbox_reply_patterns(self):
        violations = []
        for path in sorted(SCRIPTS.rglob('*.py')):
            if path == SCRIPTS / 'i18n.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            allowed = set()
            if path == SCRIPTS / 'inbox.py':
                for node in tree.body:
                    match node:
                        case ast.Assign(
                            targets=[ast.Name(id='RELEASE_APPROVAL_REPLY' | 'RELEASE_REJECTION_REPLY')],
                            value=ast.Call(func=ast.Attribute(value=ast.Name(id='re'), attr='compile'),
                                           args=[ast.Constant() as pattern]),
                        ):
                            allowed.add(pattern)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in allowed:
                    if any('CYRILLIC' in unicodedata.name(char, '') for char in node.value):
                        violations.append(f'{path.name}:{node.lineno}')
        self.assertEqual(violations, [], 'Fixed Cyrillic strings belong in i18n.py')

    def test_release_reply_words_in_both_languages(self):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import inbox
        finally:
            sys.path.remove(str(SCRIPTS))
        for answer in ('a', 'а', 'yes', 'да', 'ok', 'approve'):
            with self.subTest(answer=answer):
                self.assertIsNotNone(inbox.RELEASE_APPROVAL_REPLY.match(answer))
                self.assertIsNone(inbox.RELEASE_REJECTION_REPLY.match(answer))
        for answer in ('b', 'б', 'no', 'нет', 'reject'):
            with self.subTest(answer=answer):
                self.assertIsNotNone(inbox.RELEASE_REJECTION_REPLY.match(answer))
                self.assertIsNone(inbox.RELEASE_APPROVAL_REPLY.match(answer))
