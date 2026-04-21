#!/bin/bash
# One-time setup for the Mac-local harvester.
#
# Installs the Python dependency (pychrome) and writes the harvester config
# to ~/.dct-harvester-config. Run once per Mac — the config persists.
#
# Re-run this any time:
#   * The bot's Railway URL changes
#   * The HARVESTER_SECRET rotates
#   * You want to verify the config is correct

set -e

echo "=== dct-truenorth-bot local harvester setup ==="
echo

# --- pip install ---------------------------------------------------------
if ! command -v pip3 >/dev/null 2>&1; then
    echo "ERROR: pip3 not found. Install Python 3 first (brew install python)."
    exit 1
fi

echo "[1/3] Installing pychrome…"
pip3 install --user --upgrade pychrome
echo

# --- prompt for config ---------------------------------------------------
CONFIG_PATH="$HOME/.dct-harvester-config"
echo "[2/3] Configuring $CONFIG_PATH"

read -r -p "  Bot webhook URL (e.g. https://dct-truenorth-bot.up.railway.app): " BOT_URL
if [ -z "$BOT_URL" ]; then
    echo "ERROR: bot URL is required."
    exit 1
fi

read -r -s -p "  HARVESTER_SECRET (matches Railway env var, hidden): " HARVESTER_SECRET
echo
if [ -z "$HARVESTER_SECRET" ]; then
    echo "ERROR: harvester secret is required."
    exit 1
fi

# Write config with user-only read permissions.
umask 077
cat > "$CONFIG_PATH" <<EOF
[harvester]
bot_url = $BOT_URL
harvester_secret = $HARVESTER_SECRET
EOF
chmod 600 "$CONFIG_PATH"
echo "  Wrote $CONFIG_PATH (permissions 600)."
echo

# --- test ---------------------------------------------------------------
echo "[3/3] Next steps:"
cat <<EOF

1. Launch Chrome with remote debugging enabled (KEEP this Chrome window open):

     /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
       --remote-debugging-port=9222 \\
       --user-data-dir=\$HOME/Library/Application\\ Support/Google/Chrome

   The --user-data-dir flag reuses your normal profile (logged into TN).

2. Open https://app.true-north.xyz in that Chrome window and make sure you're
   logged in. Optionally send a chat message to prime the /streams request.

3. Run a dry-run to verify:

     python3 $(dirname "$0")/harvester.py --verify

4. If dry-run looks good, run for real:

     python3 $(dirname "$0")/harvester.py

5. Set up a cron job (every 8 h):

     crontab -e

   Add:

     0 */8 * * * /usr/bin/python3 $(pwd)/harvester.py >> \$HOME/.dct-harvester.log 2>&1

   Check ~/.dct-harvester.log to confirm each run.

EOF
echo "Setup complete."
