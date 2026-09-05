# Running the root Polymarket arb bot in Docker

No browser automation here (that's only `sniping/`, which has its own
separate `Dockerfile`/`docker-compose.yml`/`DOCKER.md`) — this is a plain
Python process making network calls to Polymarket's CLOB API, Polygon RPC,
and (if `ANTHROPIC_API_KEY` is set) the Anthropic API.

## Setup

Make sure `.env` (repo root) has `PRIVATE_KEY`, `API_KEY`, `API_SECRET`,
`API_PASSPHRASE`, and `ANTHROPIC_API_KEY` set.

## Build & run

```bash
docker compose up --build -d
docker compose logs -f
```

`trades.db` and `bot.log` live on the `polymarket-data` volume (`/data` in
the container, symlinked in — see `docker/entrypoint.sh`), so they survive
`docker compose restart` and image rebuilds.

## Stopping

```bash
docker compose down          # stops the container, keeps the data volume
docker compose down -v       # also deletes trades.db/bot.log — careful
```
