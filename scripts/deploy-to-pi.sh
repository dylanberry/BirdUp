#!/usr/bin/env bash
# Deploy the latest Bird Up! front-end + illustrations + Caddy changes from
# this repo to the live Pi. Run from the repo root.
#
# Preflight (before any copying): aborts if the Pi has < 2G free on /, and
# aborts with an apt-get hint if rsync is not installed on the Pi.
# Illustrations are synced with `rsync -avz --size-only` (no --delete, so
# stale files on the Pi are never removed; --size-only avoids re-transferring
# the whole tree when checkout mtimes differ from the Pi's copies).
#
# This script needs a one-time, temporary passwordless sudoers entry on the
# Pi because the agent workstation cannot prompt for the Pi sudo password.
# If the entry is missing, the script prints the command to create it and exits.
set -euo pipefail

PI_HOST="192.168.86.50"
PI_USER="dylanberry"
PI_AVIAN_DIR="/home/dylanberry/BirdNET-Pi/avian"
SUDOERS_FILE="/etc/sudoers.d/avian-deploy"

if [ ! -f avian/frontend/index.html ]; then
    echo "Error: must be run from the repository root." >&2
    exit 1
fi

echo "Checking temporary sudoers on Pi (${PI_HOST})..."
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" "test -f ${SUDOERS_FILE}"; then
    cat <<EOF >&2

Temporary sudoers file not found on the Pi.
Run the following on the Pi to grant passwordless sudo for this deploy only:

ssh ${PI_USER}@${PI_HOST}

echo '${PI_USER} ALL=(root) NOPASSWD: /usr/bin/cp /tmp/Caddyfile /etc/caddy/Caddyfile, /usr/bin/systemctl reload caddy, /usr/bin/sed -i -E * /etc/php/8.4/fpm/pool.d/www.conf, /usr/sbin/php-fpm8.4 -t, /usr/bin/systemctl reload php8.4-fpm, /usr/bin/chown -R caddy\:caddy ${PI_AVIAN_DIR}/ota, /usr/bin/rm -f ${SUDOERS_FILE}' | sudo tee ${SUDOERS_FILE} && sudo chmod 440 ${SUDOERS_FILE} && sudo visudo -c

Then run this script again. The sudoers file is removed automatically at the end.

EOF
    exit 1
fi

echo "Checking free space on Pi..."
free_space=$(ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" "df --output=avail -BG / | tail -1" | tr -d '[:space:]')
echo "  / has ${free_space} available"
if [ "${free_space%G}" -lt 2 ]; then
    echo "Error: less than 2G free on the Pi (/ has ${free_space}); aborting before any copying." >&2
    exit 1
fi

echo "Checking rsync on Pi..."
if ! ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" "command -v rsync" >/dev/null; then
    cat <<EOF >&2

Error: rsync is not installed on the Pi (${PI_HOST}).
Install it by running this on the Pi:

    sudo apt-get install -y rsync

Then run this script again.

EOF
    exit 1
fi

echo "Syncing illustrations..."
rsync -avz --size-only -e "ssh -o BatchMode=yes" avian/assets/illustrations/ \
    "${PI_USER}@${PI_HOST}:${PI_AVIAN_DIR}/assets/illustrations/"

echo "Copying front-end files..."
scp -o BatchMode=yes avian/frontend/index.html avian/frontend/apt.js avian/frontend/styles.css avian/frontend/ebird-codes.js avian/frontend/personality.json \
    "${PI_USER}@${PI_HOST}:${PI_AVIAN_DIR}/frontend/"

echo "Ensuring web-root symlinks..."
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" \
    'for f in index.html apt.js styles.css ebird-codes.js personality.json; do target="/home/dylanberry/BirdSongs/Extracted/$f"; src="/home/dylanberry/BirdNET-Pi/avian/frontend/$f"; [ -L "$target" ] || ln -s "$src" "$target"; done'

echo "Copying API files..."
scp -o BatchMode=yes avian/api/*.php avian/api/.user.ini \
    "${PI_USER}@${PI_HOST}:${PI_AVIAN_DIR}/api/"

echo "Creating OTA server directory (caddy-writable for php-fpm)..."
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" "mkdir -p ${PI_AVIAN_DIR}/ota"
if ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" "sudo -n /usr/bin/chown -R caddy:caddy ${PI_AVIAN_DIR}/ota"; then
    echo "  ${PI_AVIAN_DIR}/ota -> caddy:caddy"
else
    cat <<EOF >&2
Warning: could not chown ${PI_AVIAN_DIR}/ota to caddy:caddy.
The OTA server API cannot write firmware until this is fixed. Add the
chown command to the temporary sudoers entry and re-run, or run on the Pi:

    sudo chown -R caddy:caddy ${PI_AVIAN_DIR}/ota

EOF
fi

echo "Copying Caddy auth snippet..."
scp -o BatchMode=yes avian/forwarding/caddy-auth.caddy \
    "${PI_USER}@${PI_HOST}:${PI_AVIAN_DIR}/forwarding/"

echo "Copying live Caddyfile..."
scp -o BatchMode=yes docs/deployment/Caddyfile \
    "${PI_USER}@${PI_HOST}:/tmp/Caddyfile"

echo "Installing Caddyfile and reloading Caddy..."
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" \
    "sudo cp /tmp/Caddyfile /etc/caddy/Caddyfile && sudo systemctl reload caddy"

echo "Tuning php-fpm www pool (collage bursts one image request per species)..."
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" \
    "sudo sed -i -E 's/^pm.max_children = .*/pm.max_children = 12/; s/^pm.start_servers = .*/pm.start_servers = 4/; s/^pm.min_spare_servers = .*/pm.min_spare_servers = 2/; s/^pm.max_spare_servers = .*/pm.max_spare_servers = 8/' /etc/php/8.4/fpm/pool.d/www.conf \
     && sudo php-fpm8.4 -t && sudo systemctl reload php8.4-fpm"

ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" "sudo rm -f ${SUDOERS_FILE}"

echo "Verifying..."
public_urls=(
    "http://${PI_HOST}/"
    "http://${PI_HOST}/avian/api/birdnet-api.php"
)
for url in "${public_urls[@]}"; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url")
    echo "  $url -> $code"
    if [ "$code" != "200" ]; then
        echo "Error: public endpoint returned $code" >&2
        exit 1
    fi
done

admin_urls=(
    "http://${PI_HOST}/avian/api/menu.php"
    "http://${PI_HOST}/avian/api/config.php"
    "http://${PI_HOST}/avian/api/birdnet-status.php"
    "http://${PI_HOST}/avian/api/correction.php"
    "http://${PI_HOST}/avian/api/ota-server.php"
)
for url in "${admin_urls[@]}"; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url")
    echo "  $url -> $code"
    if [ "$code" != "401" ]; then
        echo "Error: admin endpoint returned $code (expected 401)" >&2
        exit 1
    fi
done

echo "Deploy complete."
