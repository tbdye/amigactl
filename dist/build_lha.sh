#!/bin/sh
# Build an LHA distribution archive for Aminet.
# Run from the project root: sh dist/build_lha.sh
set -e

SCRIPT_DIR="$(dirname "$0")"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Extract version from daemon.h
VERSION=$(grep '#define AMIGACTLD_VERSION' daemon/daemon.h | sed 's/.*"\(.*\)".*/\1/')
if [ -z "$VERSION" ]; then
    echo "ERROR: could not extract version from daemon/daemon.h" >&2
    exit 1
fi

echo "Building amigactld v${VERSION} distribution archive..."

# Generate icon
python3 tools/mkicon.py dist/amigactld.info

# Clean build
make clean
make

# Verify binary exists
if [ ! -f amigactld ]; then
    echo "ERROR: binary 'amigactld' not found after build" >&2
    exit 1
fi

# Create staging directory
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$STAGING/amigactl"

cp amigactld               "$STAGING/amigactl/"
cp dist/amigactld.info      "$STAGING/amigactl/"
cp dist/amigactld.conf.example "$STAGING/amigactl/"
cp LICENSE                  "$STAGING/amigactl/"

# Copy client directory
mkdir -p "$STAGING/amigactl/client/amigactl"

cp client/amigactl.sh          "$STAGING/amigactl/client/"
cp client/amigactl.ps1         "$STAGING/amigactl/client/"
cp client/amigactl.conf.example "$STAGING/amigactl/client/"
cp client/pyproject.toml       "$STAGING/amigactl/client/"

cp client/amigactl/__init__.py  "$STAGING/amigactl/client/amigactl/"
cp client/amigactl/__main__.py  "$STAGING/amigactl/client/amigactl/"
cp client/amigactl/protocol.py  "$STAGING/amigactl/client/amigactl/"
cp client/amigactl/shell.py     "$STAGING/amigactl/client/amigactl/"
cp client/amigactl/colors.py    "$STAGING/amigactl/client/amigactl/"

# Copy documentation
mkdir -p "$STAGING/amigactl/docs"

cp docs/COMMANDS.md             "$STAGING/amigactl/docs/"
cp docs/PROTOCOL.md             "$STAGING/amigactl/docs/"
cp docs/AGENT_GUIDE.md          "$STAGING/amigactl/docs/"
cp dist/amigactld.readme        "$STAGING/amigactl/"
cp README.md                    "$STAGING/amigactl/"

# Create archive
ARCHIVE="amigactld-${VERSION}.lha"
(cd "$STAGING" && jlha c "$PROJECT_ROOT/$ARCHIVE" amigactl)

# Copy Aminet readme alongside the archive (not inside it)
cp dist/amigactld.readme "$PROJECT_ROOT/amigactld.readme"
echo "Copied amigactld.readme alongside archive"

SIZE=$(ls -l "$ARCHIVE" | awk '{print $5}')
echo "Created ${ARCHIVE} (${SIZE} bytes)"
