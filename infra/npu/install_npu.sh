#!/usr/bin/env bash
# install_npu.sh — install AMD RyzenAI 1.7.1 NPU driver + runtime on Ubuntu 24.04.
#
# The driver/runtime archives are ACCOUNT-GATED at account.amd.com and cannot be
# downloaded non-interactively. Download these two files manually first and drop
# them in $PKG_DIR:
#   - RAI_1.7.1_Linux_NPU_XRT.zip   (XRT base/base-dev/npu .debs + amdxdna plugin .deb)
#   - ryzen_ai-1.7.1.tgz            (RyzenAI runtime + installer + quicktest)
#
# Usage:  PKG_DIR=~/Downloads/ryzenai TARGET=/opt/ryzenai ./install_npu.sh
set -euo pipefail

PKG_DIR="${PKG_DIR:-$HOME/Downloads/ryzenai}"
TARGET="${TARGET:-/opt/ryzenai}"
ZIP="$PKG_DIR/RAI_1.7.1_Linux_NPU_XRT.zip"
TGZ="$PKG_DIR/ryzen_ai-1.7.1.tgz"

echo "==> Preconditions"
. /etc/os-release; [ "${VERSION_ID:-}" = "24.04" ] || { echo "Need Ubuntu 24.04"; exit 1; }
KMAJ=$(uname -r | cut -d. -f1); KMIN=$(uname -r | cut -d. -f2)
{ [ "$KMAJ" -gt 6 ] || { [ "$KMAJ" -eq 6 ] && [ "$KMIN" -ge 10 ]; }; } || { echo "Need kernel >= 6.10"; exit 1; }
lspci -nn | grep -q '1022:17f0' || echo "WARN: NPU PCI id 1022:17f0 not seen — is the NPU enabled in BIOS?"
[ -f "$ZIP" ] || { echo "Missing $ZIP — download from account.amd.com"; exit 1; }
[ -f "$TGZ" ] || { echo "Missing $TGZ — download from account.amd.com"; exit 1; }

echo "==> Base deps"
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv libboost-filesystem1.74.0 git-lfs unzip
git lfs install

echo "==> NPU driver (XRT + amdxdna)"
WORK="$(mktemp -d)"; unzip -o "$ZIP" -d "$WORK" >/dev/null
shopt -s nullglob
for deb in \
  "$WORK"/xrt_*-base.deb \
  "$WORK"/xrt_*-base-dev.deb \
  "$WORK"/xrt_*-npu.deb \
  "$WORK"/xrt_plugin*amdxdna.deb; do
  echo "   installing $(basename "$deb")"
  sudo apt-get install --fix-broken -y "$deb"
done

export LD_LIBRARY_PATH=/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}
# shellcheck disable=SC1091
source /opt/xilinx/xrt/setup.sh
echo "==> Verify NPU"
xrt-smi examine || true   # expect a device row showing 'NPU Strix'

echo "==> RyzenAI runtime"
sudo mkdir -p "$TARGET"; sudo chown "$USER:$USER" "$TARGET"
tar -xzf "$TGZ" -C "$TARGET" --strip-components=0
INSTALLER="$(find "$TARGET" -maxdepth 2 -name install_ryzen_ai.sh | head -n1)"
[ -n "$INSTALLER" ] || { echo "install_ryzen_ai.sh not found under $TARGET"; exit 1; }
( cd "$(dirname "$INSTALLER")" && ./install_ryzen_ai.sh -a yes -p "$TARGET/venv" )

# shellcheck disable=SC1091
source "$TARGET/venv/bin/activate"
echo "RYZEN_AI_INSTALLATION_PATH=$RYZEN_AI_INSTALLATION_PATH"

echo "==> Quicktest (compiles a small CNN and runs it on the NPU)"
( cd "$TARGET/venv/quicktest" && python quicktest.py ) || echo "quicktest failed — inspect output"

echo "==> (optional) model-generate for OGA model prep"
pip install model-generate==1.7.1 --force-reinstall --no-deps \
  --extra-index-url https://pypi.amd.com/ryzenai_llm/1.7.1/linux/simple/ || true

echo "DONE. Add to your shell profile:"
echo "  source /opt/xilinx/xrt/setup.sh"
echo "  source $TARGET/venv/bin/activate"
