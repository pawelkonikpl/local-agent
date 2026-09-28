#!/usr/bin/env bash
# Xvfb (the screen), x11vnc (that screen for a person, via site-vnc), Google Chrome and web-agent.
# If any of them exits, the container exits and compose restarts it.
set -euo pipefail

export DISPLAY=:99
Xvfb "$DISPLAY" -screen 0 1366x900x24 -nolisten tcp &
for _ in $(seq 50); do [[ -e /tmp/.X11-unix/X99 ]] && break; sleep 0.1; done

# No password: reachable only on the `sites` network (api, egress-proxy, site-vnc).
x11vnc -display "$DISPLAY" -rfbport 5900 -forever -shared -nopw -quiet &

# Profile on the tmpfs: no state survives a restart. CDP listens on the container's 127.0.0.1 only.
# --no-sandbox: Chrome's sandbox needs privileges the container doesn't have (same as Playwright's
# Chromium in web-agent); the container is the isolation.
google-chrome \
  --user-data-dir=/tmp/chrome-profile \
  --remote-debugging-port=9222 \
  --proxy-server="${SITE_BROWSER_PROXY:?}" \
  --no-sandbox --no-first-run --no-default-browser-check \
  --window-size=1366,900 --start-maximized \
  about:blank &
for _ in $(seq 100); do
  python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:9222/json/version', timeout=1)" 2>/dev/null && break
  sleep 0.2
done

uvicorn web_agent.main:app --host 0.0.0.0 --port 8080 --app-dir /app/services/web-agent/src &

wait -n
echo "site-agent: a process exited; stopping the container" >&2
exit 1
