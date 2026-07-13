# Smart Stopover Flight Finder

Smart Stopover Flight Finder is a full stack flight search prototype that finds cheaper A -> B -> C itineraries through intentional stopovers.

Instead of brute forcing every route and date, the app uses an offline route graph to prune possible stopovers, then calls live flight search APIs only for promising candidates.

## Demo architecture

```text
GitHub Pages frontend
        ↓ POST JSON
FastAPI backend on Render
        ↓
SQLite route graph + cache
        ↓
SerpApi Google Flights
        ↓
Ranked stopover itineraries
```

## Main features

- Multi origin and multi destination search
- Route graph pruning before live API calls
- B -> C first validation to avoid wasting API calls
- 48 hour query cache
- SQLite storage for cache, raw flight observations and final itineraries
- Optional per request SerpApi key from the frontend
- Server side fallback SerpApi key through Render environment variables
- Static frontend deployable through GitHub Pages
- FastAPI backend deployable through Render

## Repository structure

```text
smart-stopover/
  docs/
    index.html
    styles.css
    app.js
    config.js
    demo_response.json

  backend/
    main.py
    requirements.txt
    Procfile
    .python-version
    .env.example
    smart_stopover.db

  render.yaml
  README.md
  LICENSE
  .gitignore
```

## Security model

The SerpApi key must never be hardcoded in frontend JavaScript or committed to GitHub.

This project supports two modes:

1. Private deployment mode
   - Add `SERPAPI_API_KEY` as an environment variable on Render.
   - Users do not need to enter a key in the frontend.

2. Public demo mode
   - Leave the Render key empty.
   - Users paste their own SerpApi key into the frontend.
   - The key is sent to the backend for that request only.
   - The backend does not save it in SQLite and does not return it in responses.

Important: when a user enters a key in the frontend, the backend receives it. For a fully trustless setup, users should deploy their own backend.

## Environment variables on Render

```text
SERPAPI_API_KEY=optional_server_side_key
DB_PATH=smart_stopover.db
FRONTEND_ORIGINS=*
PYTHON_VERSION=3.11.11
```

For production, replace `FRONTEND_ORIGINS=*` with your GitHub Pages origin.

## Local backend test

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Then open:

```text
http://127.0.0.1:8000/api/health
```

## Local frontend test

Open `docs/index.html` in the browser, or serve it with:

```bash
cd docs
python -m http.server 8080
```

Then open:

```text
http://127.0.0.1:8080
```

If the backend is local, edit `docs/config.js` and set:

```js
API_URL: "http://127.0.0.1:8000/api/search"
```

## Deploy frontend with GitHub Pages

1. Go to repository Settings.
2. Open Pages.
3. Set source to Deploy from a branch.
4. Choose branch `main`.
5. Choose folder `/docs`.
6. Save.

GitHub will publish the frontend from the `docs/` directory.

## Deploy backend with Render

Render must use the `backend` directory as the service root.

Manual setup:

```text
Runtime: Python
Root Directory: backend
Build Command: pip install -r requirements.txt
Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
```

Then add environment variables:

```text
PYTHON_VERSION=3.11.11
DB_PATH=smart_stopover.db
FRONTEND_ORIGINS=*
SERPAPI_API_KEY=optional
```

Health check:

```text
https://your-render-service.onrender.com/api/health
```

Database counts check:

```text
https://your-render-service.onrender.com/api/debug/db-counts
```

## API request example

```json
{
  "departure_airports": ["BGY", "MXP", "LIN"],
  "destination_airports": ["IST", "SAW"],
  "earliest_departure_date": "2026-08-01",
  "max_trip_days": 7,
  "min_destination_hours": 96,
  "min_stopover_hours": 12,
  "average_direct_price": 120,
  "currency": "EUR",
  "adults": 1,
  "cabin_class": "economy",
  "total_api_budget": 20,
  "bc_api_budget": 12,
  "ab_api_budget": 8,
  "top_b_for_bc_api": 5,
  "top_b_after_bc_for_ab_api": 5,
  "top_bc_flights_per_bc_pair": 2,
  "serpapi_api_key": "optional_per_request_key"
}
```

## Database notes

The included SQLite database is enough for a first working deployment.

For a public portfolio repository, check the licensing of any third party route data before publishing the full database. If uncertain, keep the full database private and publish only a small sample database.

For production, SQLite on a free web service is not ideal because local file changes may not be persistent after redeploys. A later version should move dynamic tables to PostgreSQL or Supabase.

## Algorithm summary

1. Receive user input.
2. Compute the effective destination time tolerance.
3. Find plausible A -> B -> C candidates in the offline route graph.
4. Rank candidate stopovers offline.
5. Search B -> C first with cache and API budget limits.
6. Score B -> C using useful flight count, average useful price and best price.
7. Search A -> B only for promising stopovers and compatible dates.
8. Combine compatible A -> B and B -> C flights.
9. Score final itineraries by total price, stopover duration and route quality.
10. Return ranked itineraries to the frontend and save the run in SQLite.

## Portfolio highlights

This project demonstrates:

- API cost aware search design
- Graph based route pruning
- Backend caching
- Full stack deployment
- SQLite data modeling
- FastAPI API design
- Static frontend integration
- Practical product thinking around travel search
