# Local Docker Testing

This repo's checked-in [docker-compose.yml](/home/john/code/gamelib-mcp/docker-compose.yml) is aimed at the deployed setup. Use the local override in [docker-compose.local.yml](/home/john/code/gamelib-mcp/docker-compose.local.yml) for local testing; it publishes the app port to localhost while leaving production-only services disabled.

## One-time setup

```bash
cp .env.local.example .env
mkdir -p data/library data/legendary data/lgogdownloader
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
- `NINTENDO_COOKIES_FILE` for Switch ownership and `NINTENDO_PCTL_SESSION_FILE` for Switch playtime (populated at runtime via `create_session_ingest_link(provider="nintendo")` and `create_session_ingest_link(provider="nintendo_pctl")`)
- `EPIC_LEGENDARY_HOST_PATH` and `LGOGDOWNLOADER_HOST_PATH` if you want Epic/GOG sync in Docker

## Start the app locally

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build app
```

## Verify the service

```bash
curl http://localhost:8000/health
```

The local example explicitly sets `MCP_AUTH_MODE=disabled`, so the Streamable HTTP endpoint is open on localhost:

```bash
curl -i http://localhost:8000/mcp
```

## Connect with Inspector CLI

The local MCP endpoint is `http://127.0.0.1:8000/mcp`.

Use the repo wrapper:

```bash
./scripts/mcp-local-inspector --method tools/list
./scripts/mcp-local-inspector --method tools/call --tool-name search_games --tool-arg query=halo
```

## Logs and teardown

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml logs -f app
docker compose -f docker-compose.yml -f docker-compose.local.yml down
```

## Notes

- Start only `app` locally. The checked-in Caddy config expects a real domain and is not needed for localhost testing.
- `caddy` is behind the `prod` Compose profile, so it will not start during local runs unless you explicitly add `--profile prod`.
- Nintendo needs no extra binaries in the image: Switch **ownership** uses the VGCS cookie HTTP API and Switch **playtime** uses the Parental Controls API via the pure-Python `pynintendoparental` dependency. Populate ownership with `create_session_ingest_link(provider="nintendo")` and playtime with `create_session_ingest_link(provider="nintendo_pctl")`.
