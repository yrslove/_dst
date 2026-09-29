"""Built-in idle world profile, applied before DST starts by managed bootstrap."""

from __future__ import annotations

import inspect
import re

SAFE_PROFILE = {
    "day": "onlyday",
    "hunger": "nonlethal",
    "temperaturedamage": "nonlethal",
    "winter": "noseason",
    "spring": "noseason",
    "summer": "noseason",
    "hounds": "never",
    "shadowcreatures": "never",
    "weather": "never",
    "lightning": "never",
}


def render_profile(source: str, profile: dict[str, str]) -> str:
    """Preserve prepared metadata/world generation; replace only existing settings."""
    for key, value in profile.items():
        pattern = rf'(\b{re.escape(key)}\s*=\s*)"[^"]*"'
        source, count = re.subn(
            pattern, lambda m, value=value: m[1] + '"' + value + '"', source
        )
        if count != 1:
            raise ValueError(f"expected exactly one supported setting: {key}")
    return source


def application_script() -> str:
    return (
        "import re, pathlib, os\n"
        + inspect.getsource(render_profile)
        + f"""
profile = {SAFE_PROFILE!r}
for p in pathlib.Path('/proc').glob('[0-9]*/comm'):
    try: name = p.read_text().strip()
    except OSError: continue
    if name.startswith('dontstarve'):
        raise RuntimeError('safe world profile requires DST quiescent')
paths = list(pathlib.Path('/home/dst/.klei/DoNotStarveTogether').glob('*/Cluster_1/Master/leveldataoverride.lua'))
if len(paths) != 1:
    raise RuntimeError('expected one prepared Cluster_1 world configuration')
p = paths[0]
original = p.read_text()
updated = render_profile(original, profile)
if updated != original:
    temp = p.with_suffix('.lua.tmp')
    temp.write_text(updated)
    os.chown(temp, p.stat().st_uid, p.stat().st_gid)
    os.chmod(temp, p.stat().st_mode & 0o777)
    os.replace(temp, p)
print('SAFE_IDLE_PROFILE_APPLIED')
"""
    )
