import os


def print_tree(startpath, ignore_dirs=None):
    if ignore_dirs is None:
        ignore_dirs = {'.git', '.idea', '__pycache__', '.venv', 'venv', 'env', 'node_modules', '.pytest_cache'}

    for root, dirs, files in os.walk(startpath):
        # Удаляем игнорируемые папки из обхода
        dirs[:] = [d for d in dirs if d not in ignore_dirs]

        level = root.replace(startpath, '').count(os.sep)
        indent = ' ' * 4 * (level)
        print('{}{}/'.format(indent, os.path.basename(root)))
        subindent = ' ' * 4 * (level + 1)
        for f in files:
            # Игнорируем временные файлы
            if not f.endswith('.pyc') and not f.startswith('.'):
                print('{}{}'.format(subindent, f))


if __name__ == '__main__':
    # Запускаем от текущей папки
    print_tree('.')