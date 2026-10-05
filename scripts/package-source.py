"""Build a deployment archive from a strict source allowlist."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parent.parent
target = root / 'output/deploy/frameforge-source.zip'
target.parent.mkdir(parents=True, exist_ok=True)
files = [root / name for name in ['README.md', 'Dockerfile', '.dockerignore', '.gitignore',
                                  '.env.example', 'requirements.txt', 'requirements-dev.txt',
                                  'pyproject.toml', 'docker-compose.yml', 'render.yaml']]
for folder in ['app', 'web', 'docs', 'scripts', 'tests', '.github']:
    files.extend(path for path in (root / folder).rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts
                 and path.suffix not in {'.pyc', '.pyo'})
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, path.relative_to(root).as_posix())
print(f'Packaged {len(files)} source files: {target}')
