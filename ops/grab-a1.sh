#!/usr/bin/env bash
# Keeps asking Oracle for an Always Free ARM A1 server (4 OCPU / 24 GB) in ap-sydney-1
# until capacity frees up, then stops. Safe to leave running: it only ever creates one.
# Usage: ./grab-a1.sh            (Ctrl+C to stop)
set -uo pipefail
export SUPPRESS_LABEL_WARNING=True

CFG=~/.oci/config
TENANCY=$(awk -F= '/^tenancy/{print $2}' "$CFG")
AD="kNkj:AP-SYDNEY-1-AD-1"
SUBNET=$(awk '{print $2}' ~/.oci/postline_ids)   # same public subnet as Postline
SSH_KEY=~/.ssh/postline_oracle.pub
NAME="sonar-a1"
WAIT=${WAIT:-90}   # seconds between tries; lower risks Oracle rate-limiting the account

# Stop early if it already exists (e.g. a previous run won while you were away).
existing=$(oci compute instance list -c "$TENANCY" --display-name "$NAME" \
  --query "data[?\"lifecycle-state\"!='TERMINATED'] | length(@)" --raw-output 2>/dev/null)
[ "${existing:-0}" != "0" ] && { echo "$NAME already exists. Done."; exit 0; }

IMAGE=$(oci compute image list -c "$TENANCY" --operating-system "Canonical Ubuntu" \
  --operating-system-version "24.04" --shape VM.Standard.A1.Flex \
  --sort-by TIMECREATED --sort-order DESC --query 'data[0].id' --raw-output)
[ -z "$IMAGE" ] && { echo "Could not find an Ubuntu 24.04 ARM image"; exit 1; }

try=0
while true; do
  try=$((try+1))
  out=$(oci compute instance launch -c "$TENANCY" --availability-domain "$AD" \
    --shape VM.Standard.A1.Flex --shape-config '{"ocpus":4,"memoryInGBs":24}' \
    --image-id "$IMAGE" --subnet-id "$SUBNET" --assign-public-ip true \
    --boot-volume-size-in-gbs 100 --display-name "$NAME" \
    --ssh-authorized-keys-file "$SSH_KEY" 2>&1)
  if echo "$out" | grep -q '"lifecycle-state"'; then
    echo "$(date '+%F %T') try $try: GOT IT"; echo "$out" | grep -E '"id"|lifecycle-state' | head -2
    command -v osascript >/dev/null && osascript -e 'display notification "ARM A1 server created" with title "grab-a1"'
    exit 0
  fi
  reason=$(echo "$out" | grep -oE '"message": "[^"]*"' | head -1)
  echo "$(date '+%F %T') try $try: ${reason:-$out}"
  # Anything other than "no capacity" / rate limit is a real error worth a human look.
  echo "$out" | grep -qiE 'capacity|TooManyRequests|LimitExceeded.*A1' || { echo "Unexpected error, stopping."; exit 1; }
  sleep "$WAIT"
done
