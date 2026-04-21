#!/bin/bash
# Railway start command (configured in nixpacks.toml / Procfile).
#
# Why this exists: Railway's build-phase install of Chromium has been seen
# to silently drop the binary between build and runtime (the default cache
# lives at /root/.cache/ms-playwright which isn't always preserved). This
# wrapper does a second install at boot so the bot always starts with a
# working Chromium if the harvester is wanted. It's idempotent — Playwright
# skips the download when the binary is already present (~1 s warm path).
#
# Logs are tagged [BOOT] so they grep cleanly in Railway's log viewer.

set -e

export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/app/.playwright-browsers}"

echo "[BOOT] PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH"

# Only attempt the Chromium install if Playwright itself is available.
# pip will have installed it via requirements.txt during [phases.install].
if python -c "import playwright" >/dev/null 2>&1; then
    echo "[BOOT] Installing Chromium via start.sh (idempotent)…"
    # --with-deps needs sudo/root which we don't have at runtime. The
    # apt deps came from nixpacks [phases.setup].aptPkgs already.
    if python -m playwright install chromium 2>&1 | tail -20; then
        echo "[BOOT] Chromium install step completed."
    else
        echo "[BOOT] WARNING: 'playwright install chromium' exited non-zero — harvester may be unavailable."
    fi
    # Surface whether the binary actually exists so Railway logs make the
    # state obvious before bot.py even starts.
    if [ -d "$PLAYWRIGHT_BROWSERS_PATH" ]; then
        echo "[BOOT] Chromium cache dir contents:"
        ls -la "$PLAYWRIGHT_BROWSERS_PATH" 2>&1 | head -10
    else
        echo "[BOOT] WARNING: $PLAYWRIGHT_BROWSERS_PATH does not exist — Chromium not installed."
    fi
else
    echo "[BOOT] playwright python package not importable — skipping Chromium install."
fi

echo "[BOOT] Launching bot.py"
exec python bot.py
