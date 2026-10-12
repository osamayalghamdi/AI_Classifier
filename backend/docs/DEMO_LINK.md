# Public demo link (no VPN required)

Share the running stack with any device on the internet — no VPN, no
Cloudflare account, no port-forwarding on the router.

## Current link

```
https://yearly-logging-kinase-formatting.trycloudflare.com
```

Open it in any browser (desktop or mobile). It serves the same nginx front
that runs locally on `:8082`, so the dashboard, the API proxy and OCR all
behave exactly as they do on the LAN.

> The hostname is regenerated every time the tunnel restarts — re-run
> `./status-tunnel.sh` to get the current one.

## How it works

A **Cloudflare quick tunnel** (`cloudflared`) makes an *outbound* connection
from this machine to Cloudflare's edge, which then publishes a public HTTPS
URL that proxies back to `http://localhost:8082`. Because the connection is
outbound:

- no inbound port needs to be opened on the router or firewall;
- no VPN is needed on the viewing device;
- TLS is terminated at Cloudflare, so the demo link is HTTPS.

## Control

All scripts live in `.tunnel/` (gitignored — it also holds the 40 MB
`cloudflared` binary and runtime state).

```bash
cd .tunnel

./start-tunnel.sh          # start (default port 8082); prints the public URL
./start-tunnel.sh 8000     # optionally expose the API directly instead
./status-tunnel.sh         # is it up? shows URL + local/public HTTP codes
./stop-tunnel.sh           # take the demo offline
```

The process is detached (`setsid` + `nohup`) with `PPID 1`, so it keeps
running after the shell that started it exits — including after a DSH agent
session ends. It does **not** survive a machine reboot; re-run
`./start-tunnel.sh` after boot if you want the link back.

Runtime state written to `.tunnel/`:

| File | Purpose |
|------|---------|
| `tunnel.pid` | PID of the running `cloudflared` |
| `tunnel-url.txt` | Current public URL |
| `tunnel.log` | Full cloudflared log |

## Verify

```bash
URL=$(cat .tunnel/tunnel-url.txt)
curl -s -o /dev/null -w '%{http_code}\n' "$URL/health"    # -> 200
```

## Caveats

- **Account-less quick tunnels have no uptime guarantee.** Cloudflare may
  recycle the connection or the hostname. Fine for a demo, not for anything
  that must stay up — for that, create a *named* tunnel bound to a domain in
  a Cloudflare account.
- **The link is public.** Anyone who has it can load the dashboard and read
  `GET /incidents`; treat the URL itself as the secret. Take it down with
  `./stop-tunnel.sh` when the demo is over.
- **The exposed API is read-mostly.** Writes are bearer-gated: `POST /reset`
  (deletes every incident) and all `/admin/*` + `/api/v1/*` endpoints return
  `401` without a valid `INTEGRATION_API_TOKEN`. Note that the token currently
  in `.env` is a short placeholder — rotate it to a long random value before
  demoing, since a weak token is the only thing standing between the public
  internet and a full data wipe:

  ```bash
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
  # put it in .env as INTEGRATION_API_TOKEN=..., then: docker compose up -d api
  ```
