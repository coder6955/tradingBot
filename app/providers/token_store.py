from __future__ import annotations

from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
ENV_FILE = ROOT.joinpath('.env')


def save_access_token(token: str) -> None:
    """Save or replace KITE_ACCESS_TOKEN in the local .env file.

    This is a convenience for local development only.
    """
    lines = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text().splitlines()

    updated = False
    out_lines: list[str] = []
    for line in lines:
        if line.strip().startswith('KITE_ACCESS_TOKEN='):
            out_lines.append(f'KITE_ACCESS_TOKEN={token}')
            updated = True
        else:
            out_lines.append(line)

    if not updated:
        out_lines.append(f'KITE_ACCESS_TOKEN={token}')

    ENV_FILE.write_text('\n'.join(out_lines) + '\n')


def load_access_token() -> Optional[str]:
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        if line.strip().startswith('KITE_ACCESS_TOKEN='):
            return line.split('=', 1)[1]
    return None
