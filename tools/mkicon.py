#!/usr/bin/env python3
"""
Generate amigactld.info -- AmigaOS Workbench icon file.

Creates a standard old-style (OS 2.x/3.x compatible) WBTOOL icon with
4-color planar imagery, complement highlighting, and Tool Types.

Usage: python3 tools/mkicon.py [output_path] [--preview]
       Default output: dist/amigactld.info
"""

import struct
import sys

# --- AmigaOS constants ---

WB_DISKMAGIC = 0xE310
WB_DISKVERSION = 1
WB_DISKREVISION = 1
WBTOOL = 3
NO_ICON_POSITION = -2147483648  # 0x80000000 signed

# Gadget flags
GFLG_GADGIMAGE = 0x0004   # render as Image (not Border)
# Bits 0-1 = 00 = complement highlighting (GADGHCOMP)
GACT_RELVERIFY = 0x0001
GACT_IMMEDIATE = 0x0002
BOOLGADGET = 0x0001

# --- Icon parameters ---

WIDTH = 30
HEIGHT = 20
DEPTH = 2       # 4 colors
STACK_SIZE = 65536

TOOLTYPES = [
    "PORT=6800",
]

# --- 4-color Workbench palette ---
# 0 = background (grey/blue)
# 1 = black (outlines, shadow)
# 2 = white (highlights)
# 3 = orange (accent)


def build_icon_pixels():
    """Build the icon as a 2D list of color indices (0-3).

    Design: A beveled rectangle with a gear/daemon symbol in orange.
    The gear has a central hub with radiating teeth, representing
    a background service/daemon.
    """
    W = WIDTH
    img = [[0] * W for _ in range(HEIGHT)]

    # --- Beveled rectangle outline ---
    # Top edge: white highlight (row 1, cols 2-27)
    for x in range(2, 28):
        img[1][x] = 2

    # Bottom edge: black shadow (row 18, cols 2-27)
    for x in range(2, 28):
        img[18][x] = 1

    # Left edge: white highlight (rows 2-17, col 2)
    for y in range(2, 18):
        img[y][2] = 2

    # Right edge: black shadow (rows 2-17, col 27)
    for y in range(2, 18):
        img[y][27] = 1

    # Corner connectors
    img[1][1] = 2     # top-left outer
    img[18][28] = 1   # bottom-right outer
    img[1][28] = 1    # top-right shadow
    img[18][1] = 1    # bottom-left shadow

    # --- Gear symbol (orange, centered) ---
    # Center hub: solid 4x4 block at rows 8-11, cols 13-16
    for y in range(8, 12):
        for x in range(13, 17):
            img[y][x] = 3

    # Gear teeth: protrusions from the hub
    # Top tooth (row 6-7, cols 14-15)
    img[6][14] = 3
    img[6][15] = 3
    img[7][14] = 3
    img[7][15] = 3

    # Bottom tooth (row 12-13, cols 14-15)
    img[12][14] = 3
    img[12][15] = 3
    img[13][14] = 3
    img[13][15] = 3

    # Left tooth (rows 9-10, cols 11-12)
    img[9][11] = 3
    img[9][12] = 3
    img[10][11] = 3
    img[10][12] = 3

    # Right tooth (rows 9-10, cols 17-18)
    img[9][17] = 3
    img[9][18] = 3
    img[10][17] = 3
    img[10][18] = 3

    # Diagonal teeth (single pixel protrusions)
    # Top-left diagonal (row 7, col 12)
    img[7][12] = 3
    # Top-right diagonal (row 7, col 17)
    img[7][17] = 3
    # Bottom-left diagonal (row 12, col 12)
    img[12][12] = 3
    # Bottom-right diagonal (row 12, col 17)
    img[12][17] = 3

    # Hub center hole (black, 2x2 at rows 9-10, cols 14-15)
    img[9][14] = 1
    img[9][15] = 1
    img[10][14] = 1
    img[10][15] = 1

    # --- Network indicator: small arrow/signal at right side ---
    # Three horizontal bars suggesting network connectivity (rows 4, 6, 8)
    # Short bar (row 4, cols 21-23)
    img[4][21] = 3
    img[4][22] = 3
    img[4][23] = 3

    # Medium bar (row 6, cols 20-24)
    img[6][20] = 3
    img[6][21] = 3
    img[6][22] = 3
    img[6][23] = 3
    img[6][24] = 3

    # --- "d" for daemon indicator at bottom-right ---
    # Vertical stroke (rows 13-16, col 23)
    for y in range(13, 17):
        img[y][23] = 1

    # Arc of 'd' (rows 14-15, cols 21-22)
    img[14][21] = 1
    img[15][21] = 1
    img[14][22] = 1
    img[15][22] = 1
    img[13][22] = 1
    img[16][22] = 1

    return img


def pixels_to_planes(img, width, height, depth):
    """Convert 2D pixel array to planar bitplane data (Amiga format).

    Returns bytes: plane 0 in full, then plane 1 in full, etc.
    Each row is word-aligned (padded to 16-bit boundary).
    """
    row_stride = ((width + 15) // 16) * 2  # bytes per row, word-aligned
    result = bytearray()

    for plane_idx in range(depth):
        for y in range(height):
            row_bytes = bytearray(row_stride)
            for x in range(width):
                color = img[y][x]
                if color & (1 << plane_idx):
                    row_bytes[x // 8] |= (1 << (7 - (x % 8)))
            result.extend(row_bytes)

    return bytes(result)


def serialize_string(s):
    """Serialize a string with 4-byte length prefix (includes NUL)."""
    encoded = s.encode('ascii') + b'\x00'
    return struct.pack('>I', len(encoded)) + encoded


def serialize_tooltypes(types):
    """Serialize a Tool Types array for the .info file."""
    # Header: (num_entries + 1) * 4  (pointer array size including NULL)
    data = struct.pack('>I', (len(types) + 1) * 4)
    for tt in types:
        data += serialize_string(tt)
    return data


def generate_info():
    """Generate the complete .info file as bytes."""
    img = build_icon_pixels()
    image_data = pixels_to_planes(img, WIDTH, HEIGHT, DEPTH)

    # --- DiskObject header (78 bytes) ---
    # Magic + Version (4 bytes)
    header = struct.pack('>HH', WB_DISKMAGIC, WB_DISKVERSION)

    # Gadget structure (44 bytes)
    gadget = struct.pack('>IhhhhHHHIIIiIHI',
        0,                              # NextGadget (NULL)
        0, 0,                           # LeftEdge, TopEdge
        WIDTH, HEIGHT,                  # Width, Height (hit-box)
        GFLG_GADGIMAGE,                 # Flags (image + complement)
        GACT_RELVERIFY | GACT_IMMEDIATE,  # Activation
        BOOLGADGET,                     # GadgetType
        1,                              # GadgetRender (non-zero = image present)
        0,                              # SelectRender (0 = no second image)
        0,                              # GadgetText (NULL)
        0,                              # MutualExclude
        0,                              # SpecialInfo (NULL)
        0,                              # GadgetID
        WB_DISKREVISION,                # UserData (revision in low byte)
    )

    # DiskObject fields after Gadget (30 bytes)
    do_fields = struct.pack('>BBIIiiII',
        WBTOOL,                         # do_Type
        0,                              # padding
        0,                              # do_DefaultTool (NULL, we ARE the tool)
        1,                              # do_ToolTypes (non-zero = array follows)
        NO_ICON_POSITION,               # do_CurrentX
        NO_ICON_POSITION,               # do_CurrentY
        0,                              # do_DrawerData (NULL)
        0,                              # do_ToolWindow (NULL)
    )

    # Stack size (4 bytes)
    do_stack = struct.pack('>I', STACK_SIZE)

    disk_object = header + gadget + do_fields + do_stack
    assert len(disk_object) == 78, \
        "DiskObject is {} bytes, expected 78".format(len(disk_object))

    # --- Image header (20 bytes) ---
    image_header = struct.pack('>hhhhhIBBI',
        0, 0,                           # LeftEdge, TopEdge
        WIDTH, HEIGHT, DEPTH,           # Width, Height, Depth
        1,                              # ImageData (non-zero = data follows)
        (1 << DEPTH) - 1,              # PlanePick (0x03 for 2 planes)
        0,                              # PlaneOnOff
        0,                              # NextImage (NULL)
    )
    assert len(image_header) == 20, \
        "Image header is {} bytes, expected 20".format(len(image_header))

    # --- Assemble ---
    output = bytearray()
    output.extend(disk_object)
    # No DrawerData (do_DrawerData == 0)
    output.extend(image_header)
    output.extend(image_data)
    # No second image (SelectRender == 0)
    # No DefaultTool string (do_DefaultTool == 0)
    output.extend(serialize_tooltypes(TOOLTYPES))

    return bytes(output)


def preview_icon():
    """Print ASCII art preview of the icon."""
    img = build_icon_pixels()
    chars = ['.', '#', 'W', 'O']  # bg, black, white, orange
    print("Icon preview ({0}x{1}, {2}-bit depth):".format(WIDTH, HEIGHT, DEPTH))
    for y, row in enumerate(img):
        line = ''.join(chars[c] for c in row)
        print("  {0:2d}: {1}".format(y, line))


if __name__ == '__main__':
    if '--preview' in sys.argv:
        preview_icon()
        sys.exit(0)

    output_path = 'dist/amigactld.info'
    for arg in sys.argv[1:]:
        if not arg.startswith('-'):
            output_path = arg
            break

    info_data = generate_info()
    with open(output_path, 'wb') as f:
        f.write(info_data)
    print("Generated {0} ({1} bytes)".format(output_path, len(info_data)))
