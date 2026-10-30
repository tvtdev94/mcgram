"""Read a video's display size and duration with ffprobe.

Telegram's servers only inspect a bot-uploaded video up to about 10 MB. A larger
`sendVideo` upload without `width`/`height` is shown in a default-shaped bubble,
so a 9:16 clip appears squashed. Passing the real size fixes that for any file.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path

_TIMEOUT_S = 15.0


async def probe_video(path: Path) -> dict[str, int]:
    """Return `width`, `height` and `duration` (seconds) for `path`.

    Returns `{}` when ffprobe is not installed or cannot read the file, so the
    caller sends the video without metadata exactly as before."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return {}
    try:
        proc = await asyncio.create_subprocess_exec(
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:stream_side_data=rotation"
            ":format=duration",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), _TIMEOUT_S)
    except (OSError, TimeoutError):
        return {}
    try:
        info = json.loads(out)
        stream = info["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (ValueError, KeyError, IndexError, TypeError):
        return {}
    # Phone footage stores portrait as landscape pixels plus a 90° rotation;
    # Telegram wants the size as displayed.
    rotation = next((int(s.get("rotation", 0)) for s in stream.get("side_data_list", [])
                     if "rotation" in s), 0)
    if rotation % 180:
        width, height = height, width
    meta = {"width": width, "height": height}
    with contextlib.suppress(ValueError, KeyError, TypeError):
        meta["duration"] = round(float(info["format"]["duration"]))
    return meta
