"""Set the extension version in one place (extension/src/manifest.base.json), for every browser build.

    python scripts/bump_extension_version.py 1.0.0       # set it
    python scripts/bump_extension_version.py --patch     # 0.1.0 -> 0.1.1 (also --minor, --major)
    python scripts/bump_extension_version.py             # show the current version

Stores want 1 to 4 dot-separated numbers and every upload must be higher than the last one.
"""
import argparse
import json
import re
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "extension" / "src" / "manifest.base.json"
VERSION_RE = re.compile(r"^\d{1,5}(\.\d{1,5}){0,3}$")


def read_version(path: Path = MANIFEST) -> str:
    return json.loads(path.read_text(encoding="utf-8"))["version"]


def bumped(version: str, part: str) -> str:
    nums = [int(x) for x in version.split(".")]
    nums += [0] * (3 - len(nums))
    i = {"major": 0, "minor": 1, "patch": 2}[part]
    nums[i] += 1
    nums[i + 1:3] = [0] * (2 - i)
    return ".".join(str(n) for n in nums[:3])


def write_version(version: str, path: Path = MANIFEST) -> None:
    if not VERSION_RE.match(version):
        raise ValueError(f"{version!r} is not a store-valid version (1 to 4 numbers separated by dots)")
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r'("version"\s*:\s*")[^"]*(")', lambda m: f"{m.group(1)}{version}{m.group(2)}", text, count=1)
    if n != 1:
        raise ValueError("no version field found in the manifest")
    path.write_text(new, encoding="utf-8")  # a text edit, so the rest of the file stays exactly as it was


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("version", nargs="?", help="the new version, e.g. 1.0.0")
    for part in ("major", "minor", "patch"):
        p.add_argument(f"--{part}", action="store_true", help=f"raise the {part} number")
    args = p.parse_args(argv)
    current = read_version()
    new = args.version or next((bumped(current, part) for part in ("major", "minor", "patch") if getattr(args, part)), None)
    if not new:
        print(current)
        return 0
    write_version(new)
    print(f"extension version {current} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
