#!/usr/bin/env python3
"""Synthetic stand-in for the instaloader CLI, for demo instances only.

Put this on `PATH` ahead of the real binary before the demo server starts
(`INSTALOADER = shutil.which("instaloader")` is resolved at import), then point
every path at a throwaway tree. It writes generated abstract images and a JSON
sidecar per item at roughly 1.6 s each, prints instaloader-shaped progress, and
never touches the network -- which is the point: no real sessions, no real
handles, no requests to Instagram from a screenshot pass.

    cp demo-instaloader-stub.py <demo-root>/bin/instaloader && chmod +x …
    PATH=<demo-root>/bin:$PATH ./.venv/bin/python server.py --port 8766

Writes into `--dirname-pattern=`; takes the positional profile name and an
integer count (`… <profile> 9`). Prints `Using latest stamps from …` first and
`[  1/  9] <path>` per item, because that is what the UI's progress parser and
the job card read.
"""
import json
import os
import random
import sys
import time
from pathlib import Path

PALETTE = [
    (18, 22, 34), (36, 18, 40), (14, 34, 34), (44, 30, 16),
    (26, 26, 46), (12, 26, 20), (40, 16, 26), (22, 34, 44),
]


def make_image(path: Path, seed: int) -> None:
    """A small abstract gradient tile, drawn without any network access."""
    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    size = 320
    a, b = rnd.choice(PALETTE), rnd.choice(PALETTE)
    img = Image.new("RGB", (size, size), a)
    draw = ImageDraw.Draw(img)
    for i in range(0, size, 4):
        t = i / size
        draw.line(
            [(i, 0), (i, size)],
            fill=(
                int(a[0] + (b[0] - a[0]) * t),
                int(a[1] + (b[1] - a[1]) * t),
                int(a[2] + (b[2] - a[2]) * t),
            ),
        )
    for _ in range(rnd.randint(3, 7)):
        x0, y0 = rnd.randint(0, size), rnd.randint(0, size)
        x1, y1 = x0 + rnd.randint(20, 160), y0 + rnd.randint(20, 160)
        draw.ellipse([x0, y0, x1, y1], outline=(235, 235, 240), width=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=82)


def main(argv: list[str]) -> int:
    target = None
    count = 0
    profiles = []
    for arg in argv:
        if arg.startswith("--dirname-pattern="):
            target = arg.split("=", 1)[1]
        elif arg.startswith("--"):
            continue
        elif arg.isdigit():
            count = int(arg)
        else:
            profiles.append(arg)

    if not target:
        print("Fatal: --dirname-pattern is required", file=sys.stderr)
        return 2

    target_dir = Path(target)
    username = profiles[0] if profiles else target_dir.name
    total = count if count > 0 else 6
    config_home = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    print(f"Using latest stamps from {config_home}/instaloader/latest-stamps.ini.")
    print(f"Profile {username}: {total} posts")

    stamp_base = int(time.time()) - total * 86400
    for i in range(1, total + 1):
        stamp = time.strftime(
            "%Y-%m-%d_%H-%M-%S_UTC", time.localtime(stamp_base + i * 86400)
        )
        video = i % 5 == 0
        path = target_dir / (f"{stamp}.mp4" if video else f"{stamp}.jpg")
        print(f"[{i:>3}/{total:>3}] {path}")
        if video:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(96))
        else:
            make_image(path, seed=hash((username, i)) & 0xFFFF)
        path.with_suffix(path.suffix + ".json").write_text(
            json.dumps({"node": {"id": f"{username}-{i}", "date_utc": stamp[:19]}})
        )
        sys.stdout.flush()
        time.sleep(1.6)

    print(f"Finished {username}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
