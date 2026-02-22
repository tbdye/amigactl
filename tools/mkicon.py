#!/usr/bin/env python3
"""
Generate amigactld.info -- AmigaOS Workbench icon file.

Creates a standard old-style (OS 2.x/3.x compatible) WBTOOL icon with
4-color planar imagery, complement highlighting, and Tool Types.

Icon: 54x22, 2-bit depth, CRT monitor/terminal with ">" prompt.
Represents a remote access / CLI tool. Matches standard WB 3.x tool
icon dimensions (HDToolBox, Format).

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

WIDTH = 54
HEIGHT = 22
DEPTH = 2       # 4 colors
STACK_SIZE = 65536

TOOLTYPES = [
    "PORT=6800",
]

# --- 4-color Workbench 3.x palette ---
# 0 = background (grey ~#AAAAAA)
# 1 = black (outlines, shadow -- highest contrast)
# 2 = white (fill, highlights)
# 3 = blue accent (~#6688BB -- low contrast, use sparingly)


def build_icon_pixels():
    """Build the icon as a 2D list of color indices (0-3).

    Design: A CRT monitor/terminal displaying a ">" command prompt.
    The monitor case is a beveled 3D rectangle (white top/left edges,
    black bottom/right edges for 3D depth). White case body is visible
    as strips around the dark screen. The screen interior is black
    with a white ">" prompt and a blue block cursor. A small beveled
    stand/pedestal sits below the monitor. LORES square pixels.
    """
    # Hand-drawn pixel map for precise control at this resolution.
    # Characters: '.' = 0 (bg grey), '#' = 1 (black), 'W' = 2 (white),
    #             'B' = 3 (blue accent)
    #
    # Layout:
    #   Row 0:       grey margin
    #   Rows 1-2:    case top edge (white bevel, with cut corner)
    #   Row 3:       case body + top screen bezel
    #   Rows 4-12:   screen area (black) with ">" prompt and cursor
    #   Row 13:      case body + bottom screen bezel
    #   Rows 14-15:  case bottom edge (black shadow, with cut corner)
    #   Rows 16-17:  stand neck (narrow, beveled)
    #   Rows 18-19:  stand base (wider, beveled)
    #   Rows 20-21:  grey margin
    pixel_map = [
        #0         1         2         3         4         5
        #0123456789012345678901234567890123456789012345678901234
        "......................................................",  # 0
        "....WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW#...",  # 1
        "...WWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWWW#..",  # 2
        "...WWWW########################################WW##...",  # 3
        "...WWWW########################################WW##...",  # 4
        "...WWWW###WW########BBB########################WW##...",  # 5
        "...WWWW#####WW######BBB########################WW##...",  # 6
        "...WWWW#######WW####BBB########################WW##...",  # 7
        "...WWWW#########WW##BBB########################WW##...",  # 8
        "...WWWW#######WW####BBB########################WW##...",  # 9
        "...WWWW#####WW######BBB########################WW##...",  # 10
        "...WWWW###WW########BBB########################WW##...",  # 11
        "...WWWW########################################WW##...",  # 12
        "...WWWW########################################WW##...",  # 13
        "...W###############################################...",  # 14
        "....###############################################...",  # 15
        ".....................WWWWWWWWWWW#.....................",  # 16
        ".....................W###########.....................",  # 17
        "..................WWWWWWWWWWWWWWWWW#..................",  # 18
        "..................##################..................",  # 19
        "......................................................",  # 20
        "......................................................",  # 21
    ]

    # Parse the pixel map into a 2D array of color indices
    char_to_idx = {'.': 0, '#': 1, 'W': 2, 'B': 3}
    img = []
    for y, row_str in enumerate(pixel_map):
        assert len(row_str) == WIDTH, \
            "Row {} is {} chars, expected {}".format(y, len(row_str), WIDTH)
        img.append([char_to_idx[ch] for ch in row_str])
    assert len(img) == HEIGHT, \
        "Pixel map has {} rows, expected {}".format(len(img), HEIGHT)

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
    chars = ['.', '#', 'W', 'B']  # bg, black, white, blue
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
