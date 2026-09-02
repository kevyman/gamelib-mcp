## Deployment: Hetzner Cloud — Multi-MCP Host

This VM hosts multiple MCP servers behind a shared Caddy reverse proxy. The gamelib-mcp repo root serves as the host-level config (`docker-compose.yml`, `Caddyfile`). Each additional MCP lives in its own subdirectory.

### Control plane first

After each deploy or auth change, use the integration control plane before debugging any individual platform sync.

- MCP: `get_integration_status()`
- HTTP JSON: `GET /admin/integrations`
- HTTP UI: `GET /admin/integrations/ui`

These are the primary operator entrypoints for Hetzner/Docker. They show:

- whether each platform is `ready`, `degraded`, `stale`, `partially_configured`, or `unconfigured`
- which backend the container detected
- whether required env values, host mounts, or binaries are missing inside the container
- the last startup-sync error classification per platform
- remediation steps to run on the host before retrying sync

### Server details

- **Provider**: Hetzner Cloud
- **IP**: `178.104.53.83`
- **SSH**: `ssh root@178.104.53.83`
- **OS**: Ubuntu 24.04 LTS
- **Specs**: 2 vCPU, 4 GB RAM

### Server layout

```
~/mcps/                  ← git clone of this repo
  docker-compose.yml
  Caddyfile
  .env                   ← created manually on server (not in git)
  Dockerfile
  gamelib_mcp/
  data/
    library/             ← gamelib.db lives here (persists across redeploys)
    other-mcp/           ← future MCP data volumes
  other-mcp/             ← future MCP source (git submodule or separate clone)
```

---

### Initial setup (already done)

#### 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

#### 2. Clone the repo

```bash
git clone https://github.com/kevyman/gamelib-mcp ~/mcps
```

#### 3. Create the GitHub OAuth App

In GitHub → Settings → Developer settings → OAuth Apps, create an app with:

- **Application name:** `gamelib-mcp`
- **Homepage URL:** `https://gamelibmcp.johnwilkos.com`
- **Authorization callback URL:** `https://gamelibmcp.johnwilkos.com/auth/callback`

Record its client ID and generate a client secret. The server requests only
GitHub's `read:user` scope and separately restricts tool access to the
numeric GitHub user ID(s) listed in `MCP_OAUTH_GITHUB_USER_IDS` (e.g.
`12233501`).

#### 4. Configure the server

```bash
cd ~/mcps
mkdir -p data/library
mkdir -p data/fastmcp
nano .env
```

```
DATABASE_URL=file:/data/gamelib.db
STEAM_API_KEY=your-key-from-steamcommunity.com/dev/apikey
STEAM_ID=your-64bit-steamid
MCP_AUTH_MODE=oauth
MCP_PUBLIC_BASE_URL=https://gamelibmcp.johnwilkos.com
GITHUB_OAUTH_CLIENT_ID=<from the GitHub OAuth App>
GITHUB_OAUTH_CLIENT_SECRET=<from the GitHub OAuth App>
MCP_OAUTH_JWT_SIGNING_KEY=<generate with: openssl rand -hex 32>
MCP_OAUTH_GITHUB_USER_IDS=12233501   # comma-separated to authorize more than one GitHub user
MCP_ADMIN_AUTH_TOKEN=<generate separately with: openssl rand -hex 32>
FASTMCP_HOME=/data/fastmcp
MCP_ALLOWED_ORIGINS=https://claude.ai,https://chatgpt.com
PORT=8000
EPIC_LEGENDARY_HOST_PATH=/root/.config/legendary          # host path to legendary config dir (mounted read-only)
STEAM_PROFILE_ID=your-steam-community-profile-id   # your steamcommunity.com/id/<this part>
BACKLOGGD_USER=your-backloggd-username             # your backloggd.com/u/<this part>
```

Generate the JWT signing key and admin token independently. Never commit either
secret. `MCP_ALLOWED_ORIGINS` remains limited to trusted clients; the server's
own public origin is added automatically for the OAuth consent and callback
flow. Native/CLI clients that send no `Origin` header are unaffected.

FastMCP encrypts OAuth registrations and upstream tokens under
`/data/fastmcp`, which is inside the existing persistent `/data` mount. Keep
`MCP_OAUTH_JWT_SIGNING_KEY` stable when rotating the GitHub client secret so
that stored registrations remain decryptable. After any secret change, use
`docker compose up -d --force-recreate app`; `docker compose restart` does not
reload `.env`.

#### 5. Add DNS record

Point your subdomain to the server IP. Caddy handles TLS automatically.

#### 6. Update the Caddyfile

```
gamelibmcp.johnwilkos.com {
    reverse_proxy app:8000
}
```

#### 7. Deploy

```bash
cd ~/mcps
docker compose --profile prod up -d --build
docker compose --profile prod logs -f
```

#### 8. Check integration status before anything else

```bash
curl -H "Authorization: Bearer $MCP_ADMIN_AUTH_TOKEN" \
  https://gamelibmcp.johnwilkos.com/admin/integrations | jq
```

Use that output or `/admin/integrations/ui` as the first readiness check:

- `ready`: the container can see the inputs it needs
- `degraded` or `stale`: the backend exists, but auth/runtime needs intervention
- `partially_configured`: some required inputs are present, but not all
- `unconfigured`: the container cannot see the required env values, files, or mounts

---

### Redeploying after code changes

Pushes to `main` deploy automatically — see **Continuous deployment** below.

To deploy manually (or if the Action is unavailable):

```bash
# From local machine — push changes
git push

# On server
ssh root@178.104.53.83
cd ~/mcps && git pull && docker compose --profile prod up -d --build
```

---

### Continuous deployment (GitHub Actions)

`.github/workflows/deploy.yml` runs on every push to `main` (and can be
triggered manually from the Actions tab via *Run workflow*):

1. **Test** — `uv sync --frozen` then the full `pytest` suite. This gates the
   deploy: if tests fail, nothing ships. (`ci.yml` runs the same suite plus
   ruff, mypy, and a `pip-audit` of the locked dependencies on every pull
   request, and `audit.yml` re-runs the audit weekly on `main` so an advisory
   published against an unchanged lockfile is still caught.)
2. **Deploy** — SSHes into the Hetzner box and runs the equivalent of the
   manual redeploy: `git fetch` → `git reset --hard origin/main` →
   `docker compose --profile prod up -d --build`.
3. **Gate** — polls `/health` from inside the new `app` container for up to
   two minutes. A container that never answers HTTP 200 (a startup exception,
   a bad `.env`, a failed migration) fails the run, prints the last 100 log
   lines, and **rolls back**: `git reset --hard <previous commit>` and a
   rebuild of that commit, so `main` stays deployable without a laptop. Only a
   healthy deploy runs `docker image prune -f` (the prune keeps the small VM's
   disk from filling with stale layers).

   The rollback checks `PRAGMA user_version` before and after. If the failed
   build had already migrated the database, old code must not start against
   the newer schema (it would re-stamp the version down, and the next forward
   deploy would re-apply the migration over migrated data): the gate restores
   the app's own `gamelib.db.pre-v{N}.bak` snapshot first, and if that file is
   missing it refuses to roll back and leaves the new build crash-looping,
   which is visible, rather than a silently wrong schema, which is not.

Every third-party action in the workflows is pinned to a full commit SHA with
the release tag in a trailing comment; Dependabot's `github-actions` entry
moves the pins forward. Do not "simplify" a pin back to a bare tag: the deploy
job holds the production SSH key, and a retagged action would run someone
else's code with it.

#### Manual rollback

If a deploy passes the gate but misbehaves afterwards, or the automatic
rollback itself needs repeating:

```bash
cd ~/mcps
sqlite3 data/library/gamelib.db 'PRAGMA user_version'   # if this is HIGHER than the
                                                       # target commit's SCHEMA_VERSION,
                                                       # restore gamelib.db.pre-v{N}.bak first
git log --oneline -5                # pick the commit to return to
git reset --hard <commit>
docker compose --profile prod up -d --build
curl -fsS https://gamelibmcp.johnwilkos.com/health
```

The next push to `main` deploys `origin/main` again, so a rollback is a
stopgap; fix forward on `main` afterwards.

#### Required GitHub secrets

Add these under **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | `178.104.53.83` |
| `DEPLOY_USER` | `root` |
| `DEPLOY_SSH_KEY` | A **private** SSH key whose public half is in the server's `~/.ssh/authorized_keys` |
| `DEPLOY_PORT` | *(optional)* SSH port, defaults to `22` |

Generate a dedicated deploy key (don't reuse a personal key):

```bash
# On your local machine
ssh-keygen -t ed25519 -C "github-actions-deploy" -f deploy_key -N ""

# Authorize the public half on the server
ssh-copy-id -i deploy_key.pub root@178.104.53.83
# (or append deploy_key.pub to ~/.ssh/authorized_keys on the server)

# Paste the PRIVATE key file contents into the DEPLOY_SSH_KEY secret
cat deploy_key
```

Then delete the local `deploy_key`/`deploy_key.pub` files.

#### Source-of-truth caveat

The deploy does `git reset --hard origin/main`, so the server tracks `main`
exactly. **Anything committed to the repo (including `Caddyfile`) must be
correct for production** — local edits to *tracked* files on the server will be
overwritten on the next deploy. In particular, commit the real domain into
`Caddyfile` rather than editing it on the box. Untracked, gitignored files
(`.env`, `data/`, the Legendary/lgogdownloader mounts) are never touched.

#### First-run checklist

1. Add the secrets above.
2. Ensure the server clone at `~/mcps` is on `main` with a clean working tree
   (`cd ~/mcps && git status`). Commit/move any server-only edits first.
3. Push to `main` (or use *Run workflow*) and watch the run in the Actions tab.
4. Verify: `curl https://gamelibmcp.johnwilkos.com/health`.

### Epic in Docker

Epic sync now reads Legendary's cached files directly from the mounted config directory instead of invoking the `legendary` CLI inside the container. The container expects a read-only mount at `/legendary`, which `docker-compose.yml` wires from `EPIC_LEGENDARY_HOST_PATH`.

On the host:

```bash
legendary auth
legendary list --force-refresh >/dev/null
```

That populates `/root/.config/legendary` with `user.json`, `assets.json`, and `metadata/*.json`, which the container then uses for both owned-game import and the reverse-engineered Epic playtime endpoint.

If the control plane reports Epic auth as stale, rerun the two host commands above and restart the container.

---

### GOG in Docker

GOG sync uses lgogdownloader. Auth is done once on your local machine; the session is mounted read-only into the container.

**One-time local setup:**

```bash
# On your local machine (not the server)
sudo apt install lgogdownloader
lgogdownloader --login   # follow prompts, stores session to ~/.config/lgogdownloader/
```

**Copy the session to the server:**

```bash
rsync -av ~/.config/lgogdownloader/ root@178.104.53.83:~/mcps/data/lgogdownloader/
```

**Server `.env`** (add):
```
LGOGDOWNLOADER_HOST_PATH=/root/mcps/data/lgogdownloader
```

lgogdownloader refreshes its session automatically on each `--list j` call — no manual token rotation needed. If the session expires, re-run `lgogdownloader --login` locally and rsync again.

If the control plane reports a missing runtime dependency, the mount is present but `lgogdownloader` is not available inside the container image.

---

### PSN Setup

PSN sync uses the [PSNAWP](https://github.com/isFakeAccount/psnawp) library with an NPSSO cookie for authentication. No CLI tools needed — just a single cookie value in `.env`.

**One-time setup:**

1. Log in to your PSN account in a browser at `https://id.sonyentertainmentnetwork.com/id/management_ca/`
2. Navigate to `https://ca.account.sony.com/api/v1/ssocookie` — the page renders an error message, but the `npsso` cookie is set
3. Open browser DevTools (F12) → Application → Cookies → find `npsso` under the Sony domain
4. Copy the 64-character token value

**Server `.env`** (add):
```
PSN_NPSSO=<your 64-char npsso token>
```

PSNAWP is a pure Python library — no extra system packages required in Docker.

**Known limitation:** Only played titles appear in the library (`title_stats()` tracks play history, not purchases). Unplayed digital purchases will not sync. This is a PSN platform limitation.

If the NPSSO token expires, repeat the browser extraction, update `.env`, then `docker compose up -d --force-recreate app` (a plain `restart` does not reload `.env`).

If the control plane reports PSN auth as stale, re-extract `PSN_NPSSO`, update `.env`, and `docker compose up -d --force-recreate app`.

---

### Nintendo in Docker

Switch sync needs **no extra binaries** in the container — both data sources are HTTP/OAuth and run on Python dependencies already in the image. Two complementary sources combine into the `switch2` platform:

- **Ownership** (your digital library): the Nintendo Account VGCS cookie API. Populate via `create_session_ingest_link(provider="nintendo")` — open the returned link and paste a Cookie-Editor JSON export from `accounts.nintendo.com/portal/vgcs/`. Stored at `NINTENDO_COOKIES_FILE` (default `data/nintendo_cookies.json`).
- **Playtime** (per-game minutes — including games played on the console but owned on another account): the Nintendo Switch **Parental Controls** API via `pynintendoparental` — plain OAuth, no Coral `f`-token. Requires the console to be registered to Parental Controls. Set up via `create_session_ingest_link(provider="nintendo_pctl")`:
  1. Call it and open the returned single-use link in a browser.
  2. Click "Sign in to Nintendo", sign in, right-click "Select this person" → Copy Link.
  3. Paste that `npf://auth` link into the form. The session token is saved to `NINTENDO_PCTL_SESSION_FILE` (default `data/nintendo_pctl_session.json`). The link never passes through the chat — it carries a one-time code redeemable for a long-lived token.

Then run `refresh_library(["switch2"])` (or a full refresh) — ownership and playtime sync together.

**Notes:**
- Playtime is forward-only: Parental Controls tracks from console registration onward (no retroactive history). Today's in-progress play is captured and refines through the day.
- If the control plane reports Nintendo as stale, refresh ownership via `create_session_ingest_link(provider="nintendo")` and/or playtime via `create_session_ingest_link(provider="nintendo_pctl")`, then retry sync.

---

### Verify

```bash
curl https://gamelibmcp.johnwilkos.com/health
# {"status": "ok"}

curl https://gamelibmcp.johnwilkos.com/.well-known/oauth-protected-resource/mcp | jq
curl https://gamelibmcp.johnwilkos.com/.well-known/oauth-authorization-server | jq

# Must return 401 plus a WWW-Authenticate resource_metadata challenge.
curl -i https://gamelibmcp.johnwilkos.com/mcp
```

Then check:

```bash
curl -H "Authorization: Bearer $MCP_ADMIN_AUTH_TOKEN" \
  https://gamelibmcp.johnwilkos.com/admin/integrations/ui
```

---

### Database backups

The SQLite DB (`data/library/gamelib.db` on the server) holds data that cannot
be re-synced if lost: `nintendo_play_summary` (the Parental Controls API is
forward-only), manual ratings, manual overrides, and merge/split repairs.

Two layers of protection:

1. **Automatic pre-migration snapshot** — before any schema-changing
   migration, the app writes `gamelib.db.pre-v{N}.bak` next to the DB
   (via `VACUUM INTO`, atomic and WAL-safe). If a deploy's migration goes
   wrong, stop the container and restore that file. Old snapshots are safe to
   delete once a migration has been verified.

2. **Nightly backup cron (installed 2026-07-02)** — the snapshot only guards
   migrations; disk loss needs an off-machine copy. `/etc/cron.d/gamelib-backup`
   on the server takes a consistent `.backup` at 04:15 UTC and hands a copy to
   the dedicated `gamelib-backup` user (key-only SSH, no other access):

```bash
# /etc/cron.d/gamelib-backup
15 4 * * * root sqlite3 /root/mcps/data/library/gamelib.db ".backup /root/mcps/data/library/gamelib-nightly.bak" && install -o gamelib-backup -g gamelib-backup -m 600 /root/mcps/data/library/gamelib-nightly.bak /home/gamelib-backup/gamelib-nightly.bak
```

3. **Off-machine pull (Windows) — installed 2026-07-02** on the home Windows
   machine `CLOSET` (192.168.129.62): scheduled task `GamelibBackup` runs
   `C:\Scripts\gamelib-backup.ps1` daily at 08:00 local as user `porta`,
   pulling into `C:\Users\porta\Backups\gamelib\` with 14 rotated copies. The
   task runs only while `porta` is logged on (no stored password — the account
   is a passwordless Microsoft account), which matches how the machine is used.
   Note: port 22 on CLOSET is WSL2 Ubuntu's sshd, not Windows OpenSSH — remote
   admin goes through `ssh kevlarrelic@closet`, and Windows-side changes via
   WSL interop (`/mnt/c/...`, `powershell.exe`, `schtasks.exe`).

4. **Restore drill (scripted 2026-09-01)** — a backup that has never been
   restored is not a backup. `scripts/restore_drill.py` copies a backup file
   into a scratch directory, integrity-checks it, runs the app's own startup
   migration against the copy (so an older backup is proven to migrate
   forward), and reports row counts for the irreplaceable tables. It never
   opens the live database. On the server, from the repo clone (`scripts/` is
   not copied into the image, so mount it):

```bash
cd ~/mcps
docker compose run --rm --no-deps -v "$PWD/scripts:/app/scripts:ro" app \
  python /app/scripts/restore_drill.py /data/gamelib-nightly.bak
```

   On the off-machine copy (WSL on CLOSET, from a clone with `uv sync`):

```bash
uv run python scripts/restore_drill.py /mnt/c/Users/porta/Backups/gamelib/<newest>.bak
```

   Exit status 0 and `restore drill: PASS` is the whole point; a FAIL names
   the check. Run it after any migration lands and at least quarterly, and log
   it here so the next audit can see the backups actually restore:

   | Date | Backup | Result |
   | --- | --- | --- |
   | *(not yet run on the box)* | | |

   To redo the setup from scratch (PowerShell on the Windows machine):

```powershell
# Generate a dedicated key (no passphrase so the scheduled task can run unattended)
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\gamelib_backup -N '""'
Get-Content $env:USERPROFILE\.ssh\gamelib_backup.pub
```

Append that public key on the server (prefix with `restrict` to disable
port-forwarding/PTY for this key):

```bash
echo 'restrict ssh-ed25519 AAAA... windows-backup' >> /home/gamelib-backup/.ssh/authorized_keys
```

Save this as `C:\Scripts\gamelib-backup.ps1` on the Windows machine — it pulls
the latest snapshot into a dated file and keeps the newest 14:

```powershell
$dest = "$env:USERPROFILE\Backups\gamelib"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$stamp = Get-Date -Format yyyy-MM-dd
scp -i $env:USERPROFILE\.ssh\gamelib_backup -o StrictHostKeyChecking=accept-new `
  gamelib-backup@178.104.53.83:gamelib-nightly.bak "$dest\gamelib-$stamp.bak"
if ($LASTEXITCODE -ne 0) { throw "gamelib backup pull failed" }
Get-ChildItem $dest -Filter 'gamelib-*.bak' | Sort-Object Name -Descending |
  Select-Object -Skip 14 | Remove-Item
```

Then schedule it daily at 08:00 local (comfortably after the 04:15 UTC server
snapshot), from an elevated PowerShell:

```powershell
schtasks /Create /SC DAILY /ST 08:00 /TN GamelibBackup /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Scripts\gamelib-backup.ps1"
```

For continuous replication instead of nightly points, use
[Litestream](https://litestream.io/) pointed at any S3-compatible bucket.

---

### Running as non-root

The app container runs as UID/GID **10001** (`USER app` in the Dockerfile), so
everything it must write has to be writable by that UID on the host. Before
deploying an image with this change (deploys auto-run on merge to main, so do
this first), run on the server:

```bash
# The read-write data mount (SQLite DB + WAL, FASTMCP_HOME, session files)
chown -R 10001:10001 /root/mcps/data/library

# The read-only config mounts still need to be *readable* by the container UID
chown -R 10001:10001 /root/mcps/data/legendary /root/mcps/data/lgogdownloader
```

Also confirm `.env` points every writable path at the `/data` mount —
absolute container paths, e.g. `NINTENDO_PCTL_SESSION_FILE=/data/nintendo_pctl_session.json`.
A relative path would resolve under `/app`, which is root-owned and read-only
for the app user.

If the container ever needs to run as root again temporarily (e.g. restoring
a backup), `docker compose exec -u 0 app sh` still works.

---

### Configure ChatGPT to use gamelib-mcp

1. In ChatGPT web, enable **Settings → Apps → Advanced settings → Developer mode**.
2. Create an app with server URL `https://gamelibmcp.johnwilkos.com/mcp`.
3. Select **OAuth** and dynamic client registration (DCR). Do not provide a
   static bearer token and do not add credentials to the URL.
4. Complete the FastMCP consent screen and GitHub login as `kevyman`.
5. Scan tools, create the app, and test one read tool followed by a
   confirmation-gated write tool.
6. Delete the previous no-auth/query-token ChatGPT app. Remove
   `MCP_AUTH_TOKEN` from `.env` and never reuse its old value.

If authorization fails, confirm ChatGPT's callback starts with
`https://chatgpt.com/connector/oauth/`; other client redirect domains are
rejected deliberately.

---

### Adding a new MCP

1. **Add a DNS record** pointing a new subdomain to `178.104.53.83`

2. **Add the service** to `~/mcps/docker-compose.yml`:
   ```yaml
   notes-mcp:
     build: ./notes-mcp
     restart: always
     expose:
       - "8001"
     volumes:
       - ./data/notes:/data
     env_file: ./notes-mcp/.env
   ```

3. **Add a Caddy block** to `~/mcps/Caddyfile`:
   ```
   notes.yourdomain.com {
       reverse_proxy notes-mcp:8001
   }
   ```

4. Commit, push, then on the server:
   ```bash
   cd ~/mcps && git pull && docker compose --profile prod up -d --build
   ```

### The Stacks (static 3D library visualization)

Served by Caddy at `gamelibmcp.johnwilkos.com/stacks/`. It is a public,
unauthenticated static site.

- Code (`stacks/index.html`, `main.js`, `vendor/`, `assets_static/`) deploys
  with the repo; Caddy mounts `./stacks` read-only at `/srv/stacks`.
- Data (`stacks/assets/`: `library.json` + cover atlases) is generated from
  the prod DB, personal, and gitignored — deploy it with
  `scripts/deploy_stacks_assets.sh` (snapshots prod DB, re-exports locally,
  rsyncs to `/root/mcps/stacks/assets/`). Re-run it whenever the library
  should be re-reflected; the site has no server-side moving parts.
