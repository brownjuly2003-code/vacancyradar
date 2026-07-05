#!/bin/bash
# VacancyRadar collection runner - iMac (no-VPN host). Installed 2026-06-04.
# WHY iMac: hh.ru redirects VPN/datacenter IPs to /vpncheck, which curl_cffi
# cannot pass; this host has a clean IP and collects directly.
# Publish surface = the Hugging Face dataset mirror only (Neon/Vercel legacy
# path removed 2026-07-06); the static Space rebuilds itself from the mirror
# via GitHub Actions (refresh-storefront.yml).
# Do NOT "set -a; . ./.env": .env values may contain & which bash source
# truncates; the CLI itself calls load_dotenv (correct parser).
cd "$HOME/vradar" || exit 1
export PATH="$HOME/vradar/.venv/bin:$PATH"
export HF_HUB_DISABLE_XET=1
PY="$HOME/vradar/.venv/bin/python"
mkdir -p logs
LOG="logs/collect_$(date +%F).log"
exec >>"$LOG" 2>&1
ts(){ date "+%F %T"; }
echo "[$(ts)] ===== collect start (pid $$) ====="

step(){ local name="$1"; shift; echo "[$(ts)] BEGIN $name"
  if "$PY" -m src.cli "$@"; then echo "[$(ts)] OK $name"; return 0
  else local rc=$?; echo "[$(ts)] FAIL $name (exit $rc)"; return $rc; fi; }

# AdGuard VPN CLI in SOCKS mode (127.0.0.1:1080) tunnels ONLY clients that use
# the proxy — hh.ru traffic never goes through it (a system-wide AdGuard tunnel
# broke hh with 451 on 2026-06-05, hence SOCKS, not TUN). Owner's rule:
# включить AdGuard перед телегой, выключить в самом конце — connect right
# before the tg step, disconnect right after it, and the EXIT trap guarantees
# the disconnect even if the run dies mid-way.
AGVPN="$HOME/adguardvpn_cli/adguardvpn-cli"
agvpn_off(){ [ -x "$AGVPN" ] && "$AGVPN" disconnect >/dev/null 2>&1 || true; }
trap agvpn_off EXIT

CRIT=1
step "ingest cbr" ingest cbr || true
# --full-sweep: drain every role completely (auto-segmenting window-capped
# ones by experience, then Moscow/SPb/rest areas) so last_seen is re-confirmed
# for every open vacancy and --detect-closed sees the full active set.
# C3 (plan 2026-06-05): closed events on, slim active window 14d below.
step "ingest hh" ingest hh --scope it --full-sweep --detect-closed --per-page 100 --area 113 || CRIT=0

# TG ingest through the LOCAL AdGuard SOCKS proxy (TG_PROXY=socks5://127.0.0.1:1080
# in .env): Telegram DCs are blocked on this line. Non-critical: hh is the
# primary source; if AdGuard is not logged in / connect fails, tg FAILs soft.
# FloodWait resume: the CLI persists master/run_state/tg_resume.json (exit 75
# on FloodWait, clears the file on a clean pass).
TG_START=$("$PY" - <<'PYEOF'
import json, pathlib, time
p = pathlib.Path("master/run_state/tg_resume.json")
out = "0"
if p.exists():
    try:
        d = json.loads(p.read_text())
        if d.get("retry_after_epoch") and float(d["retry_after_epoch"]) > time.time():
            out = "SKIP"
        else:
            out = str(int(d.get("resume_index") or 0))
    except Exception:
        pass
print(out)
PYEOF
)
if [ "$TG_START" = "SKIP" ]; then
  echo "[$(ts)] SKIP ingest telegram: FloodWait window active (master/run_state/tg_resume.json)"
elif [ ! -x "$AGVPN" ]; then
  echo "[$(ts)] SKIP ingest telegram: adguardvpn-cli not installed at $AGVPN"
else
  echo "[$(ts)] BEGIN adguardvpn connect (SOCKS 127.0.0.1:1080)"
  if "$AGVPN" connect -y >/dev/null 2>&1; then
    echo "[$(ts)] OK adguardvpn connect"
    step "ingest telegram" ingest telegram --scope it --channels 504 --limit 5 --channel-start "${TG_START:-0}" || true
    agvpn_off
    echo "[$(ts)] OK adguardvpn disconnect"
  else
    echo "[$(ts)] FAIL adguardvpn connect (not logged in?) - skipping tg ingest"
  fi
fi

if [ "$CRIT" = "1" ]; then
  # --active-days 14: "active" means confirmed by a sweep (hh) or posted (tg)
  # within 14 days — not the accumulated corpus (audit C3).
  step "publish slim" publish slim --scope it --strict --active-days 14 || CRIT=0
fi
if [ "$CRIT" = "1" ]; then
  step "publish events" publish events || true
  step "publish weekly" publish weekly --strict || true
  step "publish hf-mirror" publish hf-mirror || true
  echo "[$(ts)] ===== collect done ====="
else
  echo "[$(ts)] ===== SKIP publish: critical ingest failed (no stale republish) ====="
fi

# enrich runs LAST (moved 2026-06-06): it is non-critical (teaser/fts are
# incremental, next slim picks them up), and on the first unattended run it
# died silently ~4 min in (403s + connection resets, no FAIL line, launchd
# exit 0 — looks like the whole process group was killed), leaving the
# publish chain unrun and the live layer stale. With enrich at the tail the
# same death costs nothing: today's data is already published above.
step "enrich hh-details" enrich hh-details --rate 1.0 --limit 1500 || true
echo "[$(ts)] ===== collect end ====="
