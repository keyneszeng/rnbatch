# rnbatch → Cloudflare domain deployment (restricted preview)

Target public hostname: **https://rnbatch.aibuild.work**

This deployment reuses the existing Python/SQLite app as-is and is designed for
one Alibaba Cloud/Linux VM plus a remotely managed Cloudflare Tunnel. It is **not**
Cloudflare Workers or Pages hosting. Only the `rnbatch web` application is
started — never the old `rnbatch serve` local-file interface.

## Prerequisites

- DNS zone `aibuild.work` managed in Cloudflare, and permission to create a tunnel and a hostname route.
- Alibaba Cloud ECS VM running Linux with recent Docker Engine / Compose plugin.
- Outbound network access to Cloudflare from the VM (Cloudflare Tunnel can use port 7844).
- A review/approval that this deployment is a **restricted test deployment**.
  File analysis currently runs inside the same application process, workspace
  indexing is in-memory, and upload processing has not passed a production
  security or load assessment. **Do not offer open public signup.**

## Step 1 — Prepare a tunnel in Cloudflare

Cloudflare dashboard → **Networking > Tunnels** → **Create Tunnel**.
Choose a remotely managed `cloudflared` tunnel. Under **Routes**, add a
**Published application** with:

| Field | Value |
|---|---|
| Hostname | `rnbatch.aibuild.work` |
| Service URL | `http://127.0.0.1:8766` |
| Public endpoint | HTTPS, proxied through Cloudflare |

The `cloudflared` container shares the application's network namespace, so
`127.0.0.1:8766` correctly points to the app **inside the container group**.
A route added through the Cloudflare dashboard also manages the associated DNS.
Do not change the DNS for `erp.aibuild.work` or the apex site.

For a restricted preview, also create a **Cloudflare Access application**
limiting the hostname to authorized test identities. Its policy is separate
from the rnbatch username/password login.

Copy the **connector/tunnel token** privately; **never send tokens in chat,
commit them, or add them to a GitHub issue**.

## Step 2 — Deploy to the VM

```bash
git clone https://github.com/keyneszeng/rnbatch.git
cd rnbatch
cp deploy/cloudflare/.env.example deploy/cloudflare/.env
chmod 600 deploy/cloudflare/.env
```

Set `CLOUDFLARE_TUNNEL_TOKEN` in `deploy/cloudflare/.env` *on the VM* and
ensure `APP_ORIGIN=https://rnbatch.aibuild.work`.

Build once, then initialize an account in the named SQLite volume.
The account command prompts for a password on the terminal and does not
take the password as a command-line argument:

```bash
docker compose --env-file deploy/cloudflare/.env -f deploy/cloudflare/compose.yaml build rnbatch
docker compose --env-file deploy/cloudflare/.env -f deploy/cloudflare/compose.yaml run --rm --no-deps -it rnbatch rnbatch user-add admin --db /data/users.sqlite3
docker compose --env-file deploy/cloudflare/.env -f deploy/cloudflare/compose.yaml up -d
docker compose --env-file deploy/cloudflare/.env -f deploy/cloudflare/compose.yaml ps
```

The application does **not** publish a host port. It binds to `127.0.0.1:8766`
inside its private Docker network namespace, and only the tunnel connector
can reach that listener. Cloudflare carries external HTTPS traffic to it.

## Step 3 — Verify

```bash
docker compose --env-file deploy/cloudflare/.env -f deploy/cloudflare/compose.yaml ps
docker compose --env-file deploy/cloudflare/.env -f deploy/cloudflare/compose.yaml logs --tail=50 rnbatch
docker compose --env-file deploy/cloudflare/.env -f deploy/cloudflare/compose.yaml logs --tail=50 cloudflared
curl -I https://rnbatch.aibuild.work/
```

`curl -I` may return 501 because this minimal Python application supports
GET and POST, not HEAD. Prefer `curl -i https://rnbatch.aibuild.work/`
and verify that Cloudflare Access (if configured) and the rnbatch login page
both work. Also verify a real uploaded-file roundtrip via the browser.

## Operational guardrails

- **Do not expose the legacy `rnbatch serve` command** — it offers arbitrary
  local server directory operations and is not a multi-user cloud interface.
- **Never publish port 8766** in Alibaba Cloud security group or Compose
  `ports:`. The tunnel needs only outbound connectivity.
- **SQLite account database** lives in the `rnbatch-accounts` Docker volume;
  back it up securely. Workspaces and renamed file copies currently live in
  container `/tmp` and are temporary. They will not survive a container
  restart and can be lost without a recoverable record.
- **Never use this for sensitive or regulated files** until security controls,
  sandboxed file parsing, privacy policy, storage retention, and incident
  recovery have been reviewed and implemented.
- **One container replica only.** In-memory workspace indexing means multiple
  replicas cannot share sessions/workspaces safely.
- The app has basic login rate throttling only; add Cloudflare WAF/Access
  and proxy-side rate limiting, especially on `/auth/login` and uploads.
- Avoid making a tunnel token visible in shell history, Compose output or logs.
  Use a VM secret manager or protected env file with strict permissions.
- For rollback: deploy the prior Git commit, rebuild/restart `rnbatch`.
  Preserve the account volume; warn users that active temporary workspaces
  may be lost on restart.
- Uploaded content processing occurs server-side and consumes CPU/RAM. The
  container is memory-limited but **not yet** an adversarial-input sandbox.

## Verified references

- https://developers.cloudflare.com/tunnel/get-started/
- https://developers.cloudflare.com/tunnel/concepts/routing/
- https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/
- https://docs.docker.com/compose/how-tos/networking/
