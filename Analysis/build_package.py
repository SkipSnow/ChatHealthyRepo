import json, os

EXCLUDE_DIRS = {'__pycache__', '.venv', 'node_modules', '.git', '.pytest_cache', '.claude', '.vscode'}
EXCLUDE_EXTS = {'.pyc', '.pyo', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.ico',
                '.zip', '.exe', '.dll', '.so', '.pyd', '.db', '.sqlite', '.tmp', '.lock'}
EXCLUDE_FILES = {'.env', '.gitignore', 'findcare-code-package.json', 'build_package.py',
                 'api-tests.http', 'settings.local.json'}

base = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(base)

files = []
for root, dirs, filenames in os.walk(root_dir):
    dirs[:] = sorted([d for d in dirs if d not in EXCLUDE_DIRS])
    for fn in sorted(filenames):
        if fn in EXCLUDE_FILES:
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext in EXCLUDE_EXTS:
            continue
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, root_dir).replace(os.sep, '/')
        try:
            with open(full, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            files.append({'path': rel, 'content': content})
        except Exception as e:
            files.append({'path': rel, 'content': f'[ERROR: {e}]'})

package = {
    'project': 'ChatHealthy FindCare',
    'description': 'Complete codebase — all source, docs, workflows, website. No .env or binaries.',
    'date': '2026-03-24',
    'branch': 'dev',
    'files': files
}

out_path = os.path.join(base, 'findcare-code-package.json')
out = json.dumps(package, indent=2, ensure_ascii=False)
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(out)

size_kb = len(out.encode('utf-8')) / 1024
print(f'Written: {len(files)} files, {size_kb:.0f} KB -> {out_path}')
for fi in files:
    print(f'  {fi["path"]}')
