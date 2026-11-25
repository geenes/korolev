"""Fixed owner messages in the configured language."""
from common import language


MESSAGES = {
    'en': {
        'inbox_received': 'Received ✓ Project: {project}. Passed to Claude.',
        'inbox_unknown_project': (
            'Received ✓ Could not identify the project. Reply to a question or start your message with '
            '“project: …”. Passed to Claude.'
        ),
        'lesson_header': 'Proposed improvements to Korolev. Reply to this message, for example: 1a 2b',
        'lesson_safety': '⚠️ changes safety rules',
        'lesson_detail': 'What happened: {detail}',
        'lesson_proposal': 'Proposal: {proposal}',
        'lesson_options': 'a) accept  b) reject',
        'release_proposal': (
            'Korolev requests approval to publish v{version} to its public GitHub repository.\n\n'
            'What will change:\n{changes}\n\n'
            'Commits:\n{commits}\n\nFiles: {files}\n'
            'Privacy check passed ✅. Commits checked: {commit_count}. '
            'No personal email addresses, paths, secrets, or private terms found.\n\n'
            'Reply to this message with a to publish or b to decline.'
        ),
        'release_see_commits': '• See commits below',
        'release_commit_only': '- (release commit only)',
        'release_published': 'Published Korolev v{version}\n{url}\n\nWhat’s new:\n{changes}',
        'telegram_welcome': (
            'Korolev is connected. Project questions will appear here. '
            'Reply to a question or write “project: instruction”.'
        ),
    },
    'ru': {
        'inbox_received': 'Принято ✓ Проект: {project}. Передал Claude.',
        'inbox_unknown_project': (
            'Принято ✓ Проект не определён: ответьте реплаем на вопрос или начните с «проект: …». Передал Claude.'
        ),
        'lesson_header': 'Предложения по улучшению Korolev. Ответьте реплаем, например: 1a 2b',
        'lesson_safety': '⚠️ меняет правила безопасности',
        'lesson_detail': 'Что случилось: {detail}',
        'lesson_proposal': 'Предлагаю: {proposal}',
        'lesson_options': 'a) принять  b) отклонить',
        'release_proposal': (
            'Korolev хочет опубликовать v{version} на GitHub (публичный репозиторий).\n\n'
            'Что изменится:\n{changes}\n\n'
            'Коммиты:\n{commits}\n\nФайлы: {files}\n'
            'Проверка приватности: чисто ✅ ({commit_count} коммит(ов), '
            'нет личных email, путей, секретов и приватных слов)\n\n'
            'Ответьте реплаем: a — опубликовать, b — не публиковать.'
        ),
        'release_see_commits': '• см. коммиты',
        'release_commit_only': '- (только коммит релиза)',
        'release_published': 'Опубликовано: Korolev v{version}\n{url}\n\nЧто нового:\n{changes}',
        'telegram_welcome': (
            'Korolev подключён. Здесь будут вопросы по проектам; '
            'отвечайте реплаем или пишите «проект: указание».'
        ),
    },
}


def t(key, **kwargs):
    return MESSAGES[language()][key].format(**kwargs)
