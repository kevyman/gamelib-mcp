## Deployment: Hetzner Cloud — Multi-MCP Host

This VM hosts multiple MCP servers behind a shared Caddy reverse proxy. The gamelib-mcp repo root serves as the host-level config (`docker-compose.yml`, `Caddyfile`). Each additional MCP lives in its own subdirectory.

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
    steam/               ← steam.db lives here (persists across redeploys)
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
mkdir -p data/steam
nano .env
```

```
DATABASE_URL=file:/data/steam.db
STEAM_API_KEY=your-key-from-steamcommunity.com/dev/apikey
STEAM_ID=your-64bit-steamid
MCP_AUTH_TOKEN=<generate with: openssl rand -hex 32>
PORT=8000
EPIC_LEGENDARY_HOST_PATH=/root/.config/legendary          # host path to legendary config dir (mounted read-only)
STEAM_PROFILE_ID=your-steam-community-profile-id   # your steamcommunity.com/id/<this part>
BACKLOGGD_USER=your-backloggd-username             # your backloggd.com/u/<this part>
```

#### 4. Add DNS record

Point your subdomain to the server IP. Caddy handles TLS automatically.

#### 5. Update the Caddyfile

```
steammcp.johnwilkos.com {
    reverse_proxy gamelib-mcp:8000
}
```

#### 6. Deploy

```bash
cd ~/mcps
docker compose up -d --build
docker compose logs -f
```

---

### Redeploying after code changes

```bash
# From local machine — push changes
git push

# On server
ssh root@178.104.53.83
cd ~/mcps && git pull && docker compose up -d --build gamelib-mcp
```

### Epic in Docker

Epic sync now reads Legendary's cached files directly from the mounted config directory instead of invoking the `legendary` CLI inside the container. The container expects a read-only mount at `/legendary`, which `docker-compose.yml` wires from `EPIC_LEGENDARY_HOST_PATH`.

On the host:

```bash
legendary auth
legendary list --force-refresh >/dev/null
```

That populates `/root/.config/legendary` with `user.json`, `assets.json`, and `metadata/*.json`, which the container then uses for both owned-game import and the reverse-engineered Epic playtime endpoint.

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

---

### PSN Setup

PSN sync uses the [PSNAWP](https://github.com/isFakeAccount/psnawp) library with an NPSSO cookie for authentication. No CLI tools needed — just a single cookie value in `.env`.

**One-time setup:**

1. Log in to your PSN account in a browser
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

---

### Nintendo in Docker

Nintendo sync uses the `nxapi` CLI to fetch Switch play history. Auth is done once on the host machine and the session token is passed via `.env`.

**One-time setup:**

```bash
# Install nxapi on the host machine (requires Node.js)
npm install -g nxapi

# Authenticate with your Nintendo account
nxapi nso auth
# Follow the prompts; copy the session token printed at the end
```

**Server `.env`** (add):
```
NINTENDO_SESSION_TOKEN=<token from nxapi nso auth>
```

`nxapi` must be installed **inside the container** if you want to use it in a Dockerized deployment — subprocesses spawned by the app run inside the container, not on the host. Add `npm install -g nxapi` to the Dockerfile for this.

Alternatively, skip nxapi entirely and use the VGCS cookie fallback (see below) — this is the recommended path for Docker since it requires no extra tooling.

If the session token expires, re-run `nxapi nso auth` and update `.env`, then restart the container.

**Note:** Only titles that have been launched appear in Nintendo's play history. Unplayed digital purchases and physical cartridges that were never inserted will not sync. This is a Nintendo platform limitation.

---

### Verify

```bash
curl https://steammcp.johnwilkos.com/health
# {"status": "ok", "library_synced_at": "..."}
```

---

### Configure Claude to use gamelib-mcp

In your Claude MCP config:
```json
{
  "mcpServers": {
    "steam": {
      "url": "https://steammcp.johnwilkos.com/sse",
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
   cd ~/mcps && git pull && docker compose up -d --build
   ```
