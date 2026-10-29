"""Install optional user-unit files only; never enable or start a unit."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def systemd_quoted(value: str) -> str:
    if any(character in value for character in ('\n', '\r', '\0')):
        raise ValueError('Unsupported control character in project path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def install_units(root: Path = ROOT, destination: Path | None = None) -> list[Path]:
    root = root.resolve()
    destination = destination or Path(os.getenv('XDG_CONFIG_HOME') or Path.home() / '.config') / 'systemd/user'
    destination.mkdir(parents=True, exist_ok=True)
    rendered = {}
    for name in ('quark-bilibili-daily-report.service', 'quark-bilibili-daily-report.timer'):
        target = destination / name
        template = (root / 'deploy/systemd' / name).read_text(encoding='utf-8')
        template = template.replace('@@PROJECT@@', systemd_quoted(str(root)))
        template = template.replace('@@RUN@@', systemd_quoted(str(root / 'scripts/daily-report.sh')))
        rendered[name] = template
        if target.is_symlink() or (target.exists() and target.read_text(encoding='utf-8') != template):
            raise FileExistsError('Existing user units were not overwritten; review the installed files')
    written = []
    for name in ('quark-bilibili-daily-report.service', 'quark-bilibili-daily-report.timer'):
        path = destination / name
        if path.exists():
            continue
        with path.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(rendered[name])
        path.chmod(0o600)
        written.append(path)
    return written


if __name__ == '__main__':
    paths = install_units()
    print(f'Installed {len(paths)} new optional report units; no unit was enabled or started.')
