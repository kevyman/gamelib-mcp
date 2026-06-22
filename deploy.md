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

#### 3. Configure the server

```bash
cd ~/mcps
mkdir -p data/library
nano .env
```

```
DATABASE_URL=file:/data/gamelib.db
STEAM_API_KEY=your-key-from-steamcommunity.com/dev/apikey
STEAM_ID=your-64bit-steamid
MCP_AUTH_TOKEN=<generate with: openssl rand -hex 32>
MCP_ALLOWED_ORIGINS=https://claude.ai,https://chatgpt.com
PORT=8000
EPIC_LEGENDARY_HOST_PATH=/root/.config/legendary          # host path to legendary config dir (mounted read-only)
STEAM_PROFILE_ID=your-steam-community-profile-id   # your steamcommunity.com/id/<this part>
BACKLOGGD_USER=your-backloggd-username             # your backloggd.com/u/<this part>
```

`MCP_ALLOWED_ORIGINS` is required for browser-based MCP clients. Leave it limited to trusted web app origins; native/CLI clients that send no `Origin` header are not affected.

#### 4. Add DNS record

Point your subdomain to the server IP. Caddy handles TLS automatically.

#### 5. Update the Caddyfile

```
gamelibmcp.johnwilkos.com {
    reverse_proxy app:8000
}
```

#### 6. Deploy

```bash
cd ~/mcps
docker compose --profile prod up -d --build
docker compose --profile prod logs -f
```

#### 7. Check integration status before anything else

```bash
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
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
   deploy: if tests fail, nothing ships.
2. **Deploy** — SSHes into the Hetzner box and runs the equivalent of the
   manual redeploy: `git fetch` → `git reset --hard origin/main` →
   `docker compose --profile prod up -d --build` → `docker image prune -f`
   (the prune keeps the small VM's disk from filling with stale layers).

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

If the NPSSO token expires, repeat the browser extraction and update `.env`, then restart the container.

If the control plane reports PSN auth as stale, re-extract `PSN_NPSSO`, update `.env`, and restart the container.

---

### Nintendo in Docker

Switch sync needs **no extra binaries** in the container — both data sources are HTTP/OAuth and run on Python dependencies already in the image. Two complementary sources combine into the `switch2` platform:

- **Ownership** (your digital library): the Nintendo Account VGCS cookie API. Populate via the `set_nintendo_session` MCP tool (paste a Cookie-Editor JSON export from `accounts.nintendo.com/portal/vgcs/`). Stored at `NINTENDO_COOKIES_FILE` (default `data/nintendo_cookies.json`).
- **Playtime** (per-game minutes — including games played on the console but owned on another account): the Nintendo Switch **Parental Controls** API via `pynintendoparental` — plain OAuth, no Coral `f`-token. Requires the console to be registered to Parental Controls. Set up via the `set_nintendo_pctl_session` MCP tool:
  1. Call it with no argument → it returns a `login_url`.
  2. Open the URL, sign in, right-click "Select this person" → Copy Link.
  3. Call it again with that `npf://auth` link. The session token is saved to `NINTENDO_PCTL_SESSION_FILE` (default `data/nintendo_pctl_session.json`).

Then run `refresh_library(["switch2"])` (or a full refresh) — ownership and playtime sync together.

**Notes:**
- Playtime is forward-only: Parental Controls tracks from console registration onward (no retroactive history). Today's in-progress play is captured and refines through the day.
- The legacy `nxapi`/Coral `play-activity` path is **no longer used** — the public `f`-token providers it depended on are defunct. `NINTENDO_SESSION_TOKEN`/`NXAPI_BIN` remain wired but dormant.
- If the control plane reports Nintendo as stale, re-run `set_nintendo_session` (ownership) and/or `set_nintendo_pctl_session` (playtime), then retry sync.

---

### Verify

```bash
curl https://gamelibmcp.johnwilkos.com/health
# {"status": "ok", "library_synced_at": "..."}
```

Then check:

```bash
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  https://gamelibmcp.johnwilkos.com/admin/integrations/ui
```

---

### Configure Claude to use gamelib-mcp

In your Claude MCP config:
```json
{
  "mcpServers": {
    "steam": {
      "url": "https://gamelibmcp.johnwilkos.com/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_MCP_AUTH_TOKEN>"
      }
    }
  }
}
```

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
