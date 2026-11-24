# Handoff: get Brandon an Oracle ARM A1 server

**Goal:** create one Always Free `VM.Standard.A1.Flex` server (4 OCPU, 24 GB RAM, Ubuntu 24.04, 100 GB disk) in Oracle region `ap-sydney-1`. Sydney almost always says "Out of host capacity", so the job is to keep retrying until it works.

**Current state (checked 2026-11-24):**
- The only server is `postline`, a `VM.Standard.E2.1.Micro` (x86, 1 GB RAM, 2 GB swap, ~470 MB free). It runs Postline, codedrop, patchbay, tradinglab and **Sonar** (each with a Cloudflare tunnel). **Do not touch it.**
- The OCI CLI on the Mac is set up and works (`~/.oci/config`). Sydney has one availability domain: `kNkj:AP-SYDNEY-1-AD-1`.
- The new server reuses Postline's public subnet (`~/.oci/postline_ids`) and SSH key (`~/.ssh/postline_oracle`).

**What to do:**
1. Run `ops/grab-a1.sh` in the background on the Mac (it must stay awake; `caffeinate -i ./grab-a1.sh`). It retries every 90 s, stops on success, and stops on any error that isn't a capacity or rate-limit error.
2. On success: note the new public IP (`oci compute instance list-vnics --instance-id <id>`), add a `Host sonar-a1` entry to `~/.ssh/config`, check `ssh sonar-a1 nproc` shows 4, and tell Brandon.
3. If the script stops on an unexpected error, read the message and tell Brandon; don't guess at fixes that touch billing or the Postline server.
4. Don't move any apps onto the new server yourself. Moving Sonar is a separate step Brandon will ask for (the "Moving to the ARM A1 server later" section of `DEPLOY.md` in the Sonar repo).

**Rules:**
- Never create more than one A1 server; the Always Free limit is 4 OCPU / 24 GB in total.
- Never terminate or change `postline`.
- Don't commit `~/.oci` or any keys anywhere.
- If it's still failing after a few days: the well-known fix is upgrading the account to Pay As You Go (A1 stays free within the limits, but it needs a card on file). That's **Brandon's decision**; suggest it, never do it.
