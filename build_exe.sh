#!/usr/bin/env bash
set -e

echo "========================================================"
echo "  DRMT V2 - Standalone Executable Build Script"
echo "========================================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="./venv/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

echo ""
echo "[1/3] Installing and verifying build dependencies..."
"$PYTHON_BIN" -m pip install -r requirements.txt
"$PYTHON_BIN" -m pip install pyinstaller

echo ""
echo "[2/3] Building executable via PyInstaller..."
"$PYTHON_BIN" -m PyInstaller drmt.spec --clean --noconfirm

echo ""
echo "[3/3] Preparing deployment folders..."
mkdir -p dist/drmt/static dist/drmt/data/logs

if [ -f "data/drmt.db" ]; then
    cp -f "data/drmt.db" "dist/drmt/data/drmt.db"
fi

if [ -f ".env" ]; then
    cp -f ".env" "dist/drmt/.env"
elif [ -f ".env.example" ]; then
    cp -f ".env.example" "dist/drmt/.env"
fi

# Create one-click launcher in dist/drmt
cat << 'EOF' > dist/drmt/run.sh
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "Starting DRMT V2 Server on port 8000..."
./drmt
EOF
chmod +x dist/drmt/run.sh

echo ""
echo "========================================================"
echo "  BUILD SUCCESSFUL!"
echo "  Executable package: dist/drmt/"
echo "  Run using: ./dist/drmt/run.sh or ./dist/drmt/drmt"
echo "========================================================"
