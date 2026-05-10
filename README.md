# Hexa

A real-time hex-grid war simulation: Super Earth (the attacker) pushes against an Enemy defender on a single planet, with diver pressure, FOBs, artillery, salients, and factories evolving the front tick by tick.

A Python sim server runs the world on a fixed-rate tick loop and streams snapshots to a TypeScript canvas client over WebSocket. The client renders the hex map and drives the sim through a small REST control surface (pressure pins, POI placement, artillery fire, params, scenarios).

## Architecture

```
client (Vite + TS canvas) ──HTTP /control──►  server (FastAPI)
                          ◄──WS /stream────   └─ sim/world.py tick loop
```

- [server/](server/) — FastAPI app + sim core ([server/main.py](server/main.py), [server/sim/world.py](server/sim/world.py))
- [client/](client/) — Vite dev server, canvas renderer, control sidebar ([client/src/main.ts](client/src/main.ts))
- [docs/](docs/) — design docs for the pressure / supply / salient model

Read [server/sim/world.py](server/sim/world.py) for the tick phases and [server/sim/params.py](server/sim/params.py) for every tunable.

## Running it

### Option 1 — Docker Compose (simplest)

```bash
docker compose up --build
```

- Server: http://localhost:8800 (`/healthz`, `/state`, `/stream`)
- Client: http://localhost:5273

The client proxies `/control`, `/state`, `/healthz`, and `/stream` to the server, so you only need to open the client URL.

### Option 2 — local dev (two terminals)

**Server** (Python 3.11+, [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
uv run uvicorn server.main:app --host 0.0.0.0 --port 8800 --reload
```

**Client** (Node 18+):

```bash
cd client
npm install
npm run dev
```

Then open http://localhost:5273. The Vite dev server proxies to `http://127.0.0.1:8800` by default; override with `HEXA_PROXY_TARGET` if the server runs elsewhere.

## Tests

```bash
uv run pytest
```

Covers controllers, factory mechanics, salients, SE diver AI, and high command — see [server/tests/](server/tests/).

## Configuration

Server env vars:

- `HEXA_HOST` / `HEXA_PORT` — bind address (default `0.0.0.0:8800`)
- `HEXA_SCENARIO` — scenario to load on boot (default `demo_planet`, see [server/scenarios/](server/scenarios/))

Client env var:

- `HEXA_PROXY_TARGET` — server URL the Vite dev server proxies to (default `http://127.0.0.1:8800`)

Sim parameters (tick rate, flip threshold, supply curves, salient cadence, etc.) live in [server/sim/params.py](server/sim/params.py) and can be edited at runtime via the control panel or `POST /control/params`.
