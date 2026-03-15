## Deployment: Hetzner Cloud — Multi-MCP Host

This VM hosts multiple MCP servers behind a shared Caddy reverse proxy. The steam-mcp repo root serves as the host-level config (`docker-compose.yml`, `Caddyfile`). Each additional MCP lives in its own subdirectory.

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
  steam_mcp/
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
git clone https://github.com/kevyman/steam-mcp ~/mcps
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
STEAM_PROFILE_ID=your-steam-community-profile-id   # your steamcommunity.com/id/<this part>
BACKLOGGD_USER=your-backloggd-username             # your backloggd.com/u/<this part>
```

#### 4. Add DNS record

Point your subdomain to the server IP. Caddy handles TLS automatically.

#### 5. Update the Caddyfile

```
steammcp.johnwilkos.com {
    reverse_proxy steam-mcp:8000
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
cd ~/mcps && git pull && docker compose up -d --build steam-mcp
```

---

### Verify

```bash
curl https://steammcp.johnwilkos.com/health
# {"status": "ok", "library_synced_at": "..."}
```

---

### Configure Claude to use steam-mcp

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
