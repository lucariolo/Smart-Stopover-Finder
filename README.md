# Smart Stopover Flight Finder

A full-stack flight search prototype that finds cheaper or more interesting itineraries by combining two direct flights through an intentional stopover.

Instead of brute-forcing every possible route, the system first uses an offline airport-route graph to identify plausible `A -> B -> C` combinations, then calls live flight data only for the most promising candidates. This keeps API usage under control while still surfacing unusual low-cost opportunities.

---

## Live demo

Frontend demo: https://lucariolo.github.io/smart-stopover-finder/

The frontend includes a **Load demo results** button, so the interface can be previewed without using a SerpApi key or consuming live API calls.

---

## What it does

Most flight search tools optimize for direct flights or standard one-ticket connections. This project explores a different idea:

> Can we intentionally stop in another city for 12, 24, or 48 hours and still save money compared with a direct trip?

Example:

```text
Milan -> Athens -> Istanbul
```

The system searches for two direct segments:

```text
A -> B
B -> C
```

where:

```text
A = departure airport
B = stopover airport
C = final destination
```

It supports multiple departure airports and multiple destination airports in the same search.

---

## Why this is interesting

A naive search would be too expensive.

If we tried every possible stopover, every route, and every date through a live flight API, the number of requests would explode quickly.

This project solves that problem with progressive pruning:

1. Use a local SQLite route graph to find plausible stopovers.
2. Search live prices first for `B -> C`, the most important segment.
3. Continue to `A -> B` only if the stopover looks economically promising.
4. Cache API searches for 48 hours.
5. Rank final itineraries by savings, stopover duration, and route quality.

This makes the system API-budget-aware while still allowing unconventional routes to emerge.

---

## Features

- Multi-origin and multi-destination search
- Intentional stopover itinerary generation
- Offline airport-route graph pruning
- Live Google Flights data through SerpApi
- 48-hour API query cache
- API budget limits for cost control
- SQLite-based route and cache storage
- FastAPI backend
- Static frontend deployable on GitHub Pages
- Demo mode without live API calls
- Final itinerary scoring based on price, stopover duration, and route quality
- Optional user-provided SerpApi key from the frontend

---

## How the algorithm works

### 1. Input

The user provides:

```text
departure airports
destination airports
earliest departure date
maximum trip duration
minimum destination time
minimum stopover time
average direct flight price
SerpApi key, optional depending on deployment
```

### 2. Offline pruning

The backend queries the SQLite route graph and finds all plausible stopover airports `B` such that:

```text
A -> B exists
B -> C exists
B can be used as a stopover
C can be used as a final destination
```

Each candidate is scored using offline route quality and airport connectivity.

### 3. Live search phase 1: B -> C

The system searches real flights from the candidate stopovers to the final destination.

Queries are deduplicated by:

```text
origin airport
destination airport
date
```

Cached results are reused when available.

### 4. Conditional ranking

After collecting `B -> C` prices, the backend calculates whether each stopover still makes economic sense.

A `B -> C` flight is considered useful if:

```text
B -> C price / average direct price <= 0.85
```

### 5. Live search phase 2: A -> B

Only the best stopovers continue to the second API phase.

The system searches compatible `A -> B` flights that arrive early enough to satisfy the minimum stopover duration before the selected `B -> C` flight.

### 6. Final itinerary scoring

Valid itineraries are ranked using:

```text
total price compared with the direct benchmark
stopover duration
B -> C conditional quality
database route quality
```

The frontend then displays the best combinations.

---

## Architecture

```text
GitHub Pages frontend
        |
        | JSON request
        v
FastAPI backend on Render
        |
        | SQLite route graph and cache
        v
SerpApi Google Flights API
        |
        v
Ranked stopover itineraries
```

---

## Tech stack

### Frontend

- HTML
- CSS
- JavaScript
- GitHub Pages

### Backend

- Python
- FastAPI
- Uvicorn
- Pandas
- SQLite
- SerpApi Google Flights API

### Deployment

- GitHub Pages for the static frontend
- Render for the FastAPI backend

---

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

---

## Local setup

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/smart-stopover.git
cd smart-stopover
```

### 2. Install backend dependencies

```bash
cd backend
pip install -r requirements.txt
```

### 3. Create environment variables

Create a local `.env` file if needed, or export variables manually:

```bash
export DB_PATH=smart_stopover.db
export SERPAPI_API_KEY=your_serpapi_key_here
export FRONTEND_ORIGINS='*'
```

The project can also accept a SerpApi key from the frontend for each search, so a server-side key is optional depending on the deployment mode.

### 4. Run the backend

```bash
uvicorn main:app --reload
```

Backend health check:

```text
http://127.0.0.1:8000/api/health
```

### 5. Open the frontend

Open `docs/index.html` in the browser or serve it with a simple static server.

---

## Deployment

### Frontend on GitHub Pages

The frontend is stored in the `docs/` folder.

In GitHub:

```text
Settings -> Pages -> Deploy from branch -> main -> /docs
```

Then update `docs/config.js` with your backend URL:

```javascript
window.SMART_STOPOVER_CONFIG = {
  API_URL: "https://YOUR_RENDER_BACKEND.onrender.com/api/search",
  HEALTH_URL: "https://YOUR_RENDER_BACKEND.onrender.com/api/health",
  DEBUG_COUNTS_URL: "https://YOUR_RENDER_BACKEND.onrender.com/api/debug/db-counts"
};
```

### Backend on Render

Create a Render Web Service from this repository and set:

```text
Root Directory: backend
Build Command: pip install -r requirements.txt
Start Command: uvicorn main:app --host 0.0.0.0 --port $PORT
```

Environment variables:

```text
PYTHON_VERSION=3.11.11
DB_PATH=smart_stopover.db
FRONTEND_ORIGINS=*
SERPAPI_API_KEY=optional_server_side_key
```

If `SERPAPI_API_KEY` is not set on the server, the user can provide a key from the frontend.

---

## API key and security model

The SerpApi key is never hardcoded in the frontend or committed to the repository.

There are two supported modes:

### Personal mode

The backend stores your SerpApi key as an environment variable on Render:

```text
SERPAPI_API_KEY=your_key
```

This is convenient for private use, but it means visitors can consume your API quota.

### Public demo mode

The backend does not store any SerpApi key.

Users enter their own key in the frontend for live searches. The key is sent to the backend only for that request and is not saved in the database.

For public portfolio use, this is the safer option.

---

## Database notes

The project uses SQLite for the first version.

The database contains:

```text
airports
routes
airlines
api_search_cache
flight_observations
route_price_stats
search_runs
itinerary_results
```

For a production version, the dynamic tables should eventually move to PostgreSQL or another persistent database.

On free hosting environments, local SQLite writes may not persist reliably across redeploys or restarts.

---

## Demo mode

The frontend includes a demo response file:

```text
docs/demo_response.json
```

Clicking **Load demo results** displays example itineraries without calling the backend or consuming API requests.

This makes the project easy to review even when live API access is unavailable.

---

## Limitations

- The project is a prototype, not a commercial flight booking engine.
- Live prices depend on SerpApi and Google Flights availability.
- SQLite is suitable for the prototype but not ideal for multi-user production deployments.
- The route graph is used for pruning and may not perfectly reflect current airline schedules.
- The frontend does not book flights. It only discovers and ranks candidate itineraries.

---

## Future improvements

- Move dynamic storage from SQLite to PostgreSQL or Supabase
- Add asynchronous job handling for long searches
- Add progress updates during search execution
- Add route exclusion filters
- Add airport and city autocomplete
- Add export to CSV or Excel
- Add historical price analytics
- Add user accounts and saved searches
- Improve validation against real booking availability

---

## Project status

Working prototype.

The system has already been tested end-to-end with real API calls and successfully returned money-saving stopover itineraries.

---

## License

This project is released under the MIT License.

