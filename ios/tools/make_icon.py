"""Draw the Airistotle app icon: the web client's pixel-art Aristotle (static/img/favicon.svg),
a 16x16 grid on #28598a, as a 1024x1024 RGB PNG with crisp 64-pixel cells (no alpha, as iOS
app icons require).

    uv run python ios/tools/make_icon.py ios/Airistotle/Resources/Assets.xcassets/AppIcon.appiconset/AppIcon.png

The grid and colours are those of the favicon; the PNG writer is pure Python.
"""

import struct
import sys
import zlib

GRID = """
AAAAAAAAAAAAAAAA
AAAAALLLLLLAAAAA
AAAALHHHHHHLAAAA
AAALHHHHHHHHLAAA
AAAHHSSSSSSHHAAA
AAAHSSSSSSSSHAAA
AAAHSESSSSESHAAA
AAAHSSSSsSSSHAAA
AAAHBSSSssSBHAAA
AAABBBBssBBBBAAA
AAABBBBEEBBBBAAA
AAAAbBBBBBBbAAAA
AAAAAbBBBBbAAAAA
AATTTTbBBbTTTTAA
ATTTTtTbbTtTTTTA
TTTTtTTTTTTtTTTT
""".split()
COLOURS = {
    "A": "#28598a", "L": "#7fb04a", "H": "#d8d2c4", "S": "#e6b088", "s": "#c48a60",
    "E": "#2a2019", "B": "#f3efe6", "b": "#c2bbab", "T": "#f7f3ea", "t": "#b9c3d1",
}
SIZE = 1024


def rgb(hex_colour: str) -> bytes:
    return bytes(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def png(path: str, size: int) -> None:
    assert len(GRID) == 16 and all(len(row) == 16 for row in GRID)
    assert size % 16 == 0, "the icon size must be a multiple of the grid"
    scale = size // 16
    rows = []
    for grid_row in GRID:
        line = b"".join(rgb(COLOURS[cell]) * scale for cell in grid_row)
        rows += [b"\x00" + line] * scale  # filter type 0 per scanline

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as out:
        out.write(data)


if __name__ == "__main__":
    for target in sys.argv[1:]:
        png(target, SIZE)
