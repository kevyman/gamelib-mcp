# Local Docker Testing

This repo's checked-in [docker-compose.yml](/home/john/code/gamelib-mcp/docker-compose.yml) is aimed at the deployed setup. Use the local override in [docker-compose.local.yml](/home/john/code/gamelib-mcp/docker-compose.local.yml) for local testing; it publishes the app port to localhost while leaving production-only services disabled.

## One-time setup

```bash
cp .env.local.example .env
mkdir -p data/steam data/legendary data/lgogdownloader
```

Then edit `.env` and set at least:

```env
STEAM_API_KEY=...
STEAM_ID=...
```

Optional integrations:

- `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` for IGDB enrichment
- `PSN_NPSSO` for PSN sync
- `BACKLOGGD_USER` for rating sync
- `NINTENDO_SESSION_TOKEN` or `NINTENDO_COOKIES_FILE` for Nintendo sync
- `EPIC_LEGENDARY_HOST_PATH` and `LGOGDOWNLOADER_HOST_PATH` if you want Epic/GOG sync in Docker

## Start the app locally

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build app
```

## Verify the service

```bash
curl http://localhost:8000/health
```

If `MCP_AUTH_TOKEN` is empty, the SSE endpoint is open:

```bash
curl -i http://localhost:8000/sse
```

If `MCP_AUTH_TOKEN` is set:

```bash
curl -i -H "Authorization: Bearer YOUR_TOKEN" http://localhost:8000/sse
```

## Logs and teardown

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f app
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

## Notes

- Start only `app` locally. The checked-in Caddy config expects a real domain and is not needed for localhost testing.
- `caddy` is behind the `prod` Compose profile, so it will not start during local runs unless you explicitly add `--profile prod`.
- The Docker image installs `lgogdownloader`, but not `nxapi`. Nintendo via `NINTENDO_SESSION_TOKEN` may require extending the image; the cookie fallback is safer in Docker.
