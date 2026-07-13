import os
import re
import json
import math
import time
import hashlib
import sqlite3
from datetime import datetime, date, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator


# ============================================================
# CONFIG
# ============================================================

DB_PATH = os.getenv("DB_PATH", "smart_stopover.db")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "").strip()
FRONTEND_ORIGINS = os.getenv("FRONTEND_ORIGINS", "*")

CACHE_TTL_HOURS_DEFAULT = 48
USEFUL_BC_RATIO_THRESHOLD = 0.85
CONFIRMATION_BC_RATIO_THRESHOLD = 0.85
MAX_BC_RATIO_FOR_COMBINATION = 1.30

TOP_B_FOR_BC_API_DEFAULT = 35
TOP_B_AFTER_BC_FOR_AB_API_DEFAULT = 30
TOP_BC_FLIGHTS_PER_BC_PAIR_DEFAULT = 3

TOTAL_API_BUDGET_DEFAULT = 250
BC_API_BUDGET_DEFAULT = 150
AB_API_BUDGET_DEFAULT = 100

MIN_ROUTE_SCORE_AB = 1
MIN_ROUTE_SCORE_BC = 1

HL = "en"
GL = "it"
SORT_BY = 2
NONSTOP_ONLY = True
SHOW_HIDDEN = True
DEEP_SEARCH = False
REQUEST_TIMEOUT_SECONDS = 90
SERPAPI_SLEEP_SECONDS = 0.25


# ============================================================
# APP
# ============================================================

app = FastAPI(title="Smart Stopover Backend", version="1.0.0")

if FRONTEND_ORIGINS.strip() == "*":
    allow_origins = ["*"]
else:
    allow_origins = [x.strip() for x in FRONTEND_ORIGINS.split(",") if x.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# MODELS
# ============================================================

class SearchRequest(BaseModel):
    departure_airports: List[str]
    destination_airports: List[str]
    earliest_departure_date: str
    max_trip_days: int = Field(..., gt=0, le=60)
    min_destination_hours: float = Field(..., ge=0)
    min_stopover_hours: float = Field(12, ge=0)
    average_direct_price: float = Field(..., gt=0)
    currency: str = "EUR"
    adults: int = Field(1, ge=1, le=9)
    cabin_class: str = "economy"
    preferences_note: Optional[str] = ""
    # Optional per-search SerpApi key supplied by the user from the frontend.
    # It is never stored in SQLite and never returned to the frontend.
    serpapi_api_key: Optional[str] = Field(default=None, exclude=True)
    force_continue: bool = False

    top_b_for_bc_api: int = TOP_B_FOR_BC_API_DEFAULT
    top_b_after_bc_for_ab_api: int = TOP_B_AFTER_BC_FOR_AB_API_DEFAULT
    top_bc_flights_per_bc_pair: int = TOP_BC_FLIGHTS_PER_BC_PAIR_DEFAULT
    total_api_budget: int = TOTAL_API_BUDGET_DEFAULT
    bc_api_budget: int = BC_API_BUDGET_DEFAULT
    ab_api_budget: int = AB_API_BUDGET_DEFAULT
    cache_ttl_hours: int = CACHE_TTL_HOURS_DEFAULT

    @field_validator("departure_airports", "destination_airports")
    @classmethod
    def validate_airport_list(cls, value: List[str]) -> List[str]:
        cleaned = sorted(set([normalize_iata(x) for x in value if normalize_iata(x)]))
        if not cleaned:
            raise ValueError("Airport list cannot be empty")
        return cleaned


class HealthResponse(BaseModel):
    status: str
    db_exists: bool
    db_path: str
    server_serpapi_key_present: bool


# ============================================================
# BASIC HELPERS
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def normalize_iata(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip().upper()
    if value in {"", "NONE", "NAN", "NULL"}:
        return None
    return value


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    try:
        if isinstance(value, str):
            value = re.sub(r"[^\d\.\-]", "", value)
            if value == "":
                return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def parse_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        return None


def parse_date(value: Any) -> date:
    return pd.to_datetime(value).date()


def daterange(start_date: date, end_date: date) -> List[date]:
    out = []
    current = start_date
    while current <= end_date:
        out.append(current)
        current += timedelta(days=1)
    return out


def make_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).replace(",", "")
    match = re.search(r"[-+]?\d*\.?\d+", text)
    if not match:
        return None
    return float(match.group(0))


def airline_iata_from_flight_number(flight_number: Any) -> Optional[str]:
    if not flight_number:
        return None
    text = str(flight_number).strip().upper().replace(" ", "")
    match = re.match(r"^([A-Z0-9]{2})\d+", text)
    if match:
        return match.group(1)
    return None


def cabin_class_to_serpapi(cabin_class: str) -> int:
    text = str(cabin_class or "economy").strip().lower()
    mapping = {
        "economy": 1,
        "premium_economy": 2,
        "premium economy": 2,
        "business": 3,
        "first": 4,
    }
    return mapping.get(text, 1)

def get_effective_serpapi_key(req: "SearchRequest") -> str:
    """Return the per-request key if provided, otherwise the server-side Render env key."""
    user_key = (req.serpapi_api_key or "").strip()
    if user_key:
        return user_key
    return SERPAPI_API_KEY


def sanitized_request_json(req: "SearchRequest") -> str:
    """Serialize a request without secrets. This prevents saving user API keys in SQLite."""
    data = req.model_dump(exclude={"serpapi_api_key"})
    return json.dumps(data, ensure_ascii=False, default=str)


def df_to_records(df: pd.DataFrame, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    if df is None or df.empty:
        return []
    tmp = df.copy()
    if limit is not None:
        tmp = tmp.head(limit)
    tmp = tmp.replace({np.nan: None})
    for col in tmp.columns:
        if pd.api.types.is_datetime64_any_dtype(tmp[col]):
            tmp[col] = tmp[col].astype(str)
    return tmp.to_dict(orient="records")


# ============================================================
# DB
# ============================================================

def connect_db() -> sqlite3.Connection:
    db_path = Path(DB_PATH)
    if not db_path.exists():
        raise HTTPException(status_code=500, detail=f"Database not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS api_search_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_hash TEXT UNIQUE NOT NULL,
        origin_iata TEXT NOT NULL,
        destination_iata TEXT NOT NULL,
        flight_date TEXT NOT NULL,
        passengers INTEGER DEFAULT 1,
        cabin_class TEXT DEFAULT 'economy',
        source TEXT DEFAULT 'serpapi_google_flights',
        search_timestamp TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT,
        result_count INTEGER,
        min_price REAL,
        currency TEXT,
        raw_response_json TEXT
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS flight_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_hash TEXT NOT NULL,
        search_timestamp TEXT NOT NULL,
        origin_iata TEXT NOT NULL,
        destination_iata TEXT NOT NULL,
        flight_date TEXT NOT NULL,
        departure_datetime TEXT,
        arrival_datetime TEXT,
        airline_iata TEXT,
        airline_name TEXT,
        flight_number TEXT,
        price REAL,
        currency TEXT,
        duration_minutes INTEGER,
        stops INTEGER,
        source TEXT DEFAULT 'serpapi_google_flights',
        raw_result_json TEXT
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS route_price_stats (
        origin_iata TEXT NOT NULL,
        destination_iata TEXT NOT NULL,
        observations_count INTEGER,
        min_price_seen REAL,
        median_price_seen REAL,
        avg_price_seen REAL,
        last_price_seen REAL,
        last_seen_at TEXT,
        cheapness_score_observed REAL,
        PRIMARY KEY (origin_iata, destination_iata)
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS search_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        request_json TEXT NOT NULL,
        status TEXT,
        departure_airports TEXT,
        destination_airports TEXT,
        earliest_departure_date TEXT,
        max_trip_days INTEGER,
        min_destination_hours REAL,
        effective_min_destination_hours REAL,
        min_stopover_hours REAL,
        average_direct_price REAL,
        currency TEXT,
        offline_candidates INTEGER,
        bc_queries INTEGER,
        bc_flights INTEGER,
        ab_queries INTEGER,
        ab_flights INTEGER,
        final_itineraries INTEGER,
        api_calls_total INTEGER,
        api_calls_bc INTEGER,
        api_calls_ab INTEGER,
        cache_hits_bc INTEGER,
        cache_hits_ab INTEGER,
        needs_confirmation INTEGER DEFAULT 0,
        partial INTEGER DEFAULT 0
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS itinerary_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_run_id INTEGER NOT NULL,
        rank INTEGER,
        origin_A TEXT,
        stopover_B TEXT,
        destination_C TEXT,
        departure_datetime_AB TEXT,
        arrival_datetime_AB TEXT,
        airline_name_AB TEXT,
        flight_number_AB TEXT,
        price_AB REAL,
        departure_datetime_BC TEXT,
        arrival_datetime_BC TEXT,
        airline_name_BC TEXT,
        flight_number_BC TEXT,
        price_BC REAL,
        stopover_hours REAL,
        destination_hours REAL,
        total_price REAL,
        average_direct_price REAL,
        total_ratio REAL,
        saving_abs REAL,
        saving_pct REAL,
        final_itinerary_score REAL,
        result_json TEXT
    );
    """)

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_cache_hash ON api_search_cache(query_hash);",
        "CREATE INDEX IF NOT EXISTS idx_cache_route_date ON api_search_cache(origin_iata, destination_iata, flight_date);",
        "CREATE INDEX IF NOT EXISTS idx_obs_hash ON flight_observations(query_hash);",
        "CREATE INDEX IF NOT EXISTS idx_obs_route_date ON flight_observations(origin_iata, destination_iata, flight_date);",
        "CREATE INDEX IF NOT EXISTS idx_search_runs_created ON search_runs(created_at);",
        "CREATE INDEX IF NOT EXISTS idx_itinerary_run ON itinerary_results(search_run_id);",
    ]
    for idx in indexes:
        conn.execute(idx)
    conn.commit()


def validate_static_tables(conn: sqlite3.Connection) -> None:
    tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table';", conn)["name"].tolist()
    missing = sorted({"airports", "routes"} - set(tables))
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing static tables: {missing}")


# ============================================================
# TIME WINDOW
# ============================================================

def compute_time_window(req: SearchRequest) -> Dict[str, Any]:
    earliest_date = parse_date(req.earliest_departure_date)
    earliest_departure_datetime = datetime.combine(earliest_date, dtime(0, 0))
    effective_min_destination_hours = max(float(req.min_destination_hours) - 6, 0)
    latest_trip_end_datetime = earliest_departure_datetime + timedelta(days=req.max_trip_days) - timedelta(minutes=1)
    latest_arrival_to_C = latest_trip_end_datetime - timedelta(hours=effective_min_destination_hours)
    if latest_arrival_to_C < earliest_departure_datetime:
        raise HTTPException(status_code=400, detail="Time window impossible: min_destination_hours too high.")
    bc_dates = daterange(earliest_departure_datetime.date(), latest_arrival_to_C.date())
    return {
        "earliest_departure_datetime": earliest_departure_datetime,
        "latest_trip_end_datetime": latest_trip_end_datetime,
        "effective_min_destination_hours": effective_min_destination_hours,
        "latest_arrival_to_C": latest_arrival_to_C,
        "bc_dates": bc_dates,
    }


# ============================================================
# SCORING
# ============================================================

def connectivity_score(total_routes: Any) -> int:
    x = safe_int(total_routes, 0)
    if x >= 300: return 10
    if x >= 200: return 9
    if x >= 120: return 8
    if x >= 80: return 7
    if x >= 40: return 6
    if x >= 20: return 5
    if x >= 10: return 4
    if x > 0: return 3
    return 1


def useful_flights_score(useful_count: int, useful_dates: int, total_count: int) -> int:
    if useful_count >= 10 and useful_dates >= 3: return 10
    if useful_count >= 8 and useful_dates >= 3: return 9
    if useful_count >= 6 and useful_dates >= 2: return 8
    if useful_count >= 4 and useful_dates >= 2: return 7
    if useful_count >= 3: return 6
    if useful_count == 2: return 5
    if useful_count == 1: return 3
    if total_count > 0: return 2
    return 1


def average_useful_price_score(ratio: Optional[float]) -> int:
    if ratio is None or pd.isna(ratio): return 1
    if ratio <= 0.25: return 10
    if ratio <= 0.35: return 9
    if ratio <= 0.45: return 8
    if ratio <= 0.55: return 7
    if ratio <= 0.65: return 6
    if ratio <= 0.75: return 5
    if ratio <= 0.85: return 4
    if ratio <= 1.00: return 2
    return 1


def best_price_score(ratio: Optional[float]) -> int:
    if ratio is None or pd.isna(ratio): return 1
    if ratio <= 0.20: return 10
    if ratio <= 0.30: return 9
    if ratio <= 0.40: return 8
    if ratio <= 0.50: return 7
    if ratio <= 0.60: return 6
    if ratio <= 0.70: return 5
    if ratio <= 0.85: return 4
    if ratio <= 1.00: return 2
    return 1


def total_price_score_vs_direct(total_ratio: float) -> int:
    if total_ratio <= 0.60: return 10
    if total_ratio <= 0.70: return 9
    if total_ratio <= 0.80: return 8
    if total_ratio <= 0.90: return 7
    if total_ratio <= 1.00: return 6
    if total_ratio <= 1.15: return 4
    if total_ratio <= 1.30: return 2
    return 1


def stopover_duration_score(stopover_hours: float, min_stopover_hours: float) -> int:
    if stopover_hours < min_stopover_hours: return 0
    if stopover_hours < 16: return 6
    if stopover_hours < 24: return 8
    if stopover_hours <= 48: return 10
    if stopover_hours <= 72: return 8
    if stopover_hours <= 120: return 6
    return 4


# ============================================================
# OFFLINE CANDIDATES
# ============================================================

def sql_in(values: List[str]) -> str:
    return ",".join(["?"] * len(values))


def get_offline_candidates(conn: sqlite3.Connection, req: SearchRequest) -> pd.DataFrame:
    A = req.departure_airports
    C = req.destination_airports
    a_ph = sql_in(A)
    c_ph = sql_in(C)

    query = f"""
    SELECT
        r1.origin_iata AS origin_A,
        r1.destination_iata AS stopover_B,
        r2.destination_iata AS destination_C,
        CAST(r1.route_score AS REAL) AS route_score_A_B,
        CAST(r2.route_score AS REAL) AS route_score_B_C,
        r1.airline_iata_codes AS airlines_A_B,
        r2.airline_iata_codes AS airlines_B_C,
        r1.route_type AS route_type_A_B,
        r2.route_type AS route_type_B_C,
        aB.name AS stopover_name,
        aB.municipality AS stopover_city,
        aB.country_name AS stopover_country,
        aB.airport_scope AS stopover_scope,
        CAST(aB.can_be_stopover AS INTEGER) AS can_be_stopover,
        CAST(aB.graph_total_routes AS INTEGER) AS b_graph_total_routes,
        aC.name AS destination_name,
        aC.municipality AS destination_city,
        aC.country_name AS destination_country,
        aC.airport_scope AS destination_scope,
        CAST(aC.can_be_destination AS INTEGER) AS can_be_destination
    FROM routes r1
    JOIN routes r2 ON r1.destination_iata = r2.origin_iata
    JOIN airports aB ON aB.iata_code = r1.destination_iata
    JOIN airports aC ON aC.iata_code = r2.destination_iata
    WHERE r1.origin_iata IN ({a_ph})
      AND r2.destination_iata IN ({c_ph})
      AND CAST(aB.can_be_stopover AS INTEGER) = 1
      AND CAST(aC.can_be_destination AS INTEGER) = 1
      AND r1.destination_iata NOT IN ({a_ph})
      AND r1.destination_iata NOT IN ({c_ph})
      AND r1.origin_iata != r1.destination_iata
      AND r1.destination_iata != r2.destination_iata
      AND CAST(r1.route_score AS REAL) >= ?
      AND CAST(r2.route_score AS REAL) >= ?;
    """
    params = A + C + A + C + [MIN_ROUTE_SCORE_AB, MIN_ROUTE_SCORE_BC]
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df
    df["route_score_A_B"] = pd.to_numeric(df["route_score_A_B"], errors="coerce").fillna(1)
    df["route_score_B_C"] = pd.to_numeric(df["route_score_B_C"], errors="coerce").fillna(1)
    df["b_graph_total_routes"] = pd.to_numeric(df["b_graph_total_routes"], errors="coerce").fillna(0)
    df["B_connectivity_score"] = df["b_graph_total_routes"].apply(connectivity_score)
    df["B_stopover_basic_score"] = 5
    df["offline_B_score"] = (
        0.45 * df["route_score_B_C"] +
        0.35 * df["route_score_A_B"] +
        0.10 * df["B_connectivity_score"] +
        0.10 * df["B_stopover_basic_score"]
    ).round(3)
    return df.sort_values(["offline_B_score", "route_score_B_C", "route_score_A_B"], ascending=False).reset_index(drop=True)


# ============================================================
# API QUERY BUILDING
# ============================================================

class ApiBudget:
    def __init__(self, total: int, bc: int, ab: int):
        self.total_api_budget = total
        self.bc_api_budget = bc
        self.ab_api_budget = ab
        self.total_calls = 0
        self.bc_calls = 0
        self.ab_calls = 0

    def can_call(self, phase: str) -> bool:
        if self.total_calls >= self.total_api_budget:
            return False
        if phase == "BC" and self.bc_calls >= self.bc_api_budget:
            return False
        if phase == "AB" and self.ab_calls >= self.ab_api_budget:
            return False
        return True

    def record_call(self, phase: str) -> None:
        self.total_calls += 1
        if phase == "BC": self.bc_calls += 1
        if phase == "AB": self.ab_calls += 1


def build_query_hash(origin: str, destination: str, flight_date: str, req: SearchRequest) -> str:
    payload = {
        "source": "serpapi_google_flights",
        "engine": "google_flights",
        "type": 2,
        "origin_iata": normalize_iata(origin),
        "destination_iata": normalize_iata(destination),
        "flight_date": str(flight_date),
        "currency": req.currency,
        "adults": req.adults,
        "cabin_class": req.cabin_class,
        "travel_class": cabin_class_to_serpapi(req.cabin_class),
        "nonstop_only": NONSTOP_ONLY,
        "sort_by": SORT_BY,
        "hl": HL,
        "gl": GL,
    }
    return make_hash(payload)


def build_bc_queries(candidates: pd.DataFrame, bc_dates: List[date], req: SearchRequest) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()
    b_rank = candidates.groupby("stopover_B").agg(
        best_offline_B_score=("offline_B_score", "max"),
        compatible_rows=("stopover_B", "count"),
    ).reset_index().sort_values("best_offline_B_score", ascending=False).head(req.top_b_for_bc_api)
    top_b = set(b_rank["stopover_B"].tolist())
    pairs = candidates[candidates["stopover_B"].isin(top_b)].groupby(["stopover_B", "destination_C"]).agg(
        query_priority=("offline_B_score", "max"),
        best_route_score_B_C=("route_score_B_C", "max"),
        compatible_A_count=("origin_A", "nunique"),
    ).reset_index()
    rows = []
    for _, row in pairs.iterrows():
        for dt in bc_dates:
            rows.append({
                "phase": "BC",
                "origin_iata": row["stopover_B"],
                "destination_iata": row["destination_C"],
                "flight_date": dt.isoformat(),
                "query_priority": float(row["query_priority"]),
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("query_priority", ascending=False).drop_duplicates(["origin_iata", "destination_iata", "flight_date"]).reset_index(drop=True)
    df["query_hash"] = df.apply(lambda r: build_query_hash(r["origin_iata"], r["destination_iata"], r["flight_date"], req), axis=1)
    return df


def build_ab_queries(candidates_conditional: pd.DataFrame, bc_anchor_flights: pd.DataFrame, start_dt: datetime, min_stopover_hours: float, req: SearchRequest) -> pd.DataFrame:
    if candidates_conditional.empty or bc_anchor_flights.empty:
        return pd.DataFrame()
    compat = candidates_conditional[["origin_A", "stopover_B", "destination_C", "conditional_B_score"]].copy()
    rows = []
    for _, bc in bc_anchor_flights.iterrows():
        B = bc["origin_iata"]
        C = bc["destination_iata"]
        dep_bc = parse_datetime(bc["departure_datetime"])
        if dep_bc is None:
            continue
        latest_arrival_B = dep_bc - timedelta(hours=min_stopover_hours)
        latest_ab_date = latest_arrival_B.date()
        if latest_ab_date < start_dt.date():
            continue
        subset = compat[(compat["stopover_B"] == B) & (compat["destination_C"] == C)]
        for _, comp in subset.iterrows():
            for dt in daterange(start_dt.date(), latest_ab_date):
                rows.append({
                    "phase": "AB",
                    "origin_iata": comp["origin_A"],
                    "destination_iata": B,
                    "flight_date": dt.isoformat(),
                    "query_priority": float(comp["conditional_B_score"]),
                })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("query_priority", ascending=False).drop_duplicates(["origin_iata", "destination_iata", "flight_date"]).reset_index(drop=True)
    df["query_hash"] = df.apply(lambda r: build_query_hash(r["origin_iata"], r["destination_iata"], r["flight_date"], req), axis=1)
    return df


# ============================================================
# SERPAPI AND CACHE
# ============================================================

def get_cached(conn: sqlite3.Connection, query_hash: str) -> Tuple[bool, pd.DataFrame, Dict[str, Any]]:
    now = iso_utc(utc_now())
    cache = pd.read_sql_query(
        "SELECT * FROM api_search_cache WHERE query_hash = ? AND expires_at >= ? LIMIT 1;",
        conn,
        params=(query_hash, now),
    )
    if cache.empty:
        return False, pd.DataFrame(), {}
    flights = pd.read_sql_query("SELECT * FROM flight_observations WHERE query_hash = ?;", conn, params=(query_hash,))
    return True, flights, cache.iloc[0].to_dict()


def serpapi_params(origin: str, destination: str, flight_date: str, req: SearchRequest, api_key: str) -> Dict[str, Any]:
    params = {
        "engine": "google_flights",
        "type": "2",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": str(flight_date),
        "currency": req.currency,
        "hl": HL,
        "gl": GL,
        "adults": req.adults,
        "travel_class": cabin_class_to_serpapi(req.cabin_class),
        "sort_by": SORT_BY,
        "show_hidden": "true" if SHOW_HIDDEN else "false",
        "deep_search": "true" if DEEP_SEARCH else "false",
        "no_cache": "false",
        "api_key": api_key,
    }
    if NONSTOP_ONLY:
        params["stops"] = "1"
    return params


def fetch_serpapi(origin: str, destination: str, flight_date: str, req: SearchRequest) -> Dict[str, Any]:
    api_key = get_effective_serpapi_key(req)
    if not api_key:
        return {"status": "missing_api_key", "error": "SerpApi key missing. Provide it in the frontend or configure SERPAPI_API_KEY on Render.", "raw": None}
    try:
        response = requests.get("https://serpapi.com/search.json", params=serpapi_params(origin, destination, flight_date, req, api_key), timeout=REQUEST_TIMEOUT_SECONDS)
        time.sleep(SERPAPI_SLEEP_SECONDS)
    except Exception as e:
        return {"status": "request_error", "error": str(e), "raw": None}
    if response.status_code != 200:
        return {"status": "http_error", "error": f"HTTP {response.status_code}: {response.text[:500]}", "raw": None}
    try:
        data = response.json()
    except Exception as e:
        return {"status": "json_error", "error": str(e), "raw": None}
    if "error" in data:
        return {"status": "api_error", "error": str(data.get("error")), "raw": data}
    return {"status": data.get("search_metadata", {}).get("status", "unknown"), "error": None, "raw": data}


def parse_serpapi_flights(data: Dict[str, Any], origin: str, destination: str, flight_date: str, query_hash: str, req: SearchRequest) -> List[Dict[str, Any]]:
    if not data:
        return []
    sections = []
    for key in ["best_flights", "other_flights"]:
        if isinstance(data.get(key), list):
            sections.extend(data[key])
    rows = []
    for item in sections:
        price = parse_price(item.get("price"))
        flights = item.get("flights", [])
        if not isinstance(flights, list) or not flights:
            continue
        if NONSTOP_ONLY and len(flights) != 1:
            continue
        first = flights[0]
        last = flights[-1]
        dep_airport = first.get("departure_airport", {}) or {}
        arr_airport = last.get("arrival_airport", {}) or {}
        dep_id = normalize_iata(dep_airport.get("id"))
        arr_id = normalize_iata(arr_airport.get("id"))
        if dep_id != origin or arr_id != destination:
            continue
        dep_dt = parse_datetime(dep_airport.get("time"))
        arr_dt = parse_datetime(arr_airport.get("time"))
        if dep_dt is None or arr_dt is None or price is None:
            continue
        flight_number = first.get("flight_number")
        rows.append({
            "query_hash": query_hash,
            "origin_iata": origin,
            "destination_iata": destination,
            "flight_date": str(flight_date),
            "departure_datetime": dep_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "arrival_datetime": arr_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "airline_iata": airline_iata_from_flight_number(flight_number),
            "airline_name": first.get("airline"),
            "flight_number": flight_number,
            "price": price,
            "currency": req.currency,
            "duration_minutes": safe_int(item.get("total_duration", first.get("duration", None)), None),
            "stops": max(len(flights) - 1, 0),
            "source": "serpapi_google_flights",
            "raw_result_json": json.dumps(item, ensure_ascii=False),
        })
    return rows


def save_api_result(conn: sqlite3.Connection, query: pd.Series, query_hash: str, api_result: Dict[str, Any], flights: List[Dict[str, Any]], req: SearchRequest) -> None:
    now_dt = utc_now()
    now_iso = iso_utc(now_dt)
    expires_at = iso_utc(now_dt + timedelta(hours=req.cache_ttl_hours if flights else 2))
    prices = [safe_float(x.get("price")) for x in flights if safe_float(x.get("price")) is not None]
    min_price = min(prices) if prices else None
    conn.execute("DELETE FROM flight_observations WHERE query_hash = ?;", (query_hash,))
    conn.execute("""
    INSERT OR REPLACE INTO api_search_cache (
        query_hash, origin_iata, destination_iata, flight_date, passengers, cabin_class,
        source, search_timestamp, expires_at, status, result_count, min_price, currency, raw_response_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        query_hash,
        query["origin_iata"],
        query["destination_iata"],
        str(query["flight_date"]),
        req.adults,
        req.cabin_class,
        "serpapi_google_flights",
        now_iso,
        expires_at,
        api_result.get("status"),
        len(flights),
        min_price,
        req.currency,
        json.dumps(api_result.get("raw"), ensure_ascii=False) if api_result.get("raw") else None,
    ))
    for f in flights:
        conn.execute("""
        INSERT INTO flight_observations (
            query_hash, search_timestamp, origin_iata, destination_iata, flight_date,
            departure_datetime, arrival_datetime, airline_iata, airline_name, flight_number,
            price, currency, duration_minutes, stops, source, raw_result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            query_hash, now_iso, f["origin_iata"], f["destination_iata"], f["flight_date"],
            f["departure_datetime"], f["arrival_datetime"], f["airline_iata"], f["airline_name"], f["flight_number"],
            f["price"], f["currency"], f["duration_minutes"], f["stops"], f["source"], f["raw_result_json"],
        ))
    conn.commit()


def execute_query_batch(conn: sqlite3.Connection, qdf: pd.DataFrame, phase: str, budget: ApiBudget, req: SearchRequest) -> Tuple[pd.DataFrame, pd.DataFrame, bool]:
    all_flights = []
    logs = []
    partial = False
    if qdf.empty:
        return pd.DataFrame(), pd.DataFrame(), False
    for _, q in qdf.iterrows():
        origin = q["origin_iata"]
        dest = q["destination_iata"]
        flight_date = str(q["flight_date"])
        qhash = q.get("query_hash") or build_query_hash(origin, dest, flight_date, req)
        hit, cached, cache_row = get_cached(conn, qhash)
        if hit:
            if not cached.empty:
                cached = cached.copy()
                cached["cache_hit"] = 1
                cached["phase"] = phase
                all_flights.append(cached)
            logs.append({"phase": phase, "origin_iata": origin, "destination_iata": dest, "flight_date": flight_date, "cache_hit": 1, "api_called": 0, "result_count": int(cache_row.get("result_count", 0) or 0), "status": cache_row.get("status"), "skipped_budget": 0})
            continue
        if not budget.can_call(phase):
            partial = True
            logs.append({"phase": phase, "origin_iata": origin, "destination_iata": dest, "flight_date": flight_date, "cache_hit": 0, "api_called": 0, "result_count": 0, "status": "skipped_budget", "skipped_budget": 1})
            continue
        api_result = fetch_serpapi(origin, dest, flight_date, req)
        budget.record_call(phase)
        flights = parse_serpapi_flights(api_result.get("raw"), origin, dest, flight_date, qhash, req)
        save_api_result(conn, q, qhash, api_result, flights, req)
        if flights:
            fdf = pd.DataFrame(flights)
            fdf["cache_hit"] = 0
            fdf["phase"] = phase
            all_flights.append(fdf)
        logs.append({"phase": phase, "origin_iata": origin, "destination_iata": dest, "flight_date": flight_date, "cache_hit": 0, "api_called": 1, "result_count": len(flights), "status": api_result.get("status"), "skipped_budget": 0})
    return (pd.concat(all_flights, ignore_index=True) if all_flights else pd.DataFrame(), pd.DataFrame(logs), partial)


# ============================================================
# BC SUMMARY AND CONDITIONAL RANKING
# ============================================================

def prepare_flights(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.copy()
    df["origin_iata"] = df["origin_iata"].apply(normalize_iata)
    df["destination_iata"] = df["destination_iata"].apply(normalize_iata)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["departure_datetime"] = pd.to_datetime(df["departure_datetime"], errors="coerce")
    df["arrival_datetime"] = pd.to_datetime(df["arrival_datetime"], errors="coerce")
    return df.dropna(subset=["price", "departure_datetime", "arrival_datetime"])


def empty_bc_summary(pairs: pd.DataFrame) -> pd.DataFrame:
    out = pairs.copy()
    for col, val in {
        "total_flights_count": 0,
        "useful_flights_count": 0,
        "useful_dates_count": 0,
        "average_useful_BC_price": np.nan,
        "average_useful_BC_ratio": np.nan,
        "best_BC_price": np.nan,
        "best_BC_ratio": np.nan,
        "useful_flights_score": 1,
        "average_useful_price_score": 1,
        "best_price_score": 1,
        "conditional_BC_score": 1.0,
    }.items():
        out[col] = val
    return out


def summarize_bc(pairs: pd.DataFrame, bc_flights: pd.DataFrame, req: SearchRequest, latest_arrival_to_C: datetime) -> pd.DataFrame:
    pairs = pairs[["stopover_B", "destination_C"]].drop_duplicates().copy()
    if pairs.empty:
        return pd.DataFrame()
    if bc_flights.empty:
        return empty_bc_summary(pairs)
    df = prepare_flights(bc_flights)
    if df.empty:
        return empty_bc_summary(pairs)
    df = df[df["arrival_datetime"] <= pd.Timestamp(latest_arrival_to_C)].copy()
    if df.empty:
        return empty_bc_summary(pairs)
    df["price_ratio"] = df["price"] / req.average_direct_price
    df["is_useful_BC"] = df["price_ratio"] <= USEFUL_BC_RATIO_THRESHOLD
    rows = []
    for _, pair in pairs.iterrows():
        B = pair["stopover_B"]
        C = pair["destination_C"]
        g = df[(df["origin_iata"] == B) & (df["destination_iata"] == C)].copy()
        total_count = len(g)
        useful = g[g["is_useful_BC"]].copy()
        useful_count = len(useful)
        useful_dates = useful["flight_date"].nunique() if useful_count else 0
        avg_useful_price = useful["price"].mean() if useful_count else np.nan
        avg_ratio = avg_useful_price / req.average_direct_price if useful_count else np.nan
        best_price = g["price"].min() if total_count else np.nan
        best_ratio = best_price / req.average_direct_price if total_count else np.nan
        u_score = useful_flights_score(useful_count, useful_dates, total_count)
        avg_score = average_useful_price_score(avg_ratio)
        b_score = best_price_score(best_ratio)
        cond = 0.40 * avg_score + 0.35 * u_score + 0.25 * b_score
        rows.append({
            "stopover_B": B,
            "destination_C": C,
            "total_flights_count": total_count,
            "useful_flights_count": useful_count,
            "useful_dates_count": useful_dates,
            "average_useful_BC_price": avg_useful_price,
            "average_useful_BC_ratio": avg_ratio,
            "best_BC_price": best_price,
            "best_BC_ratio": best_ratio,
            "useful_flights_score": u_score,
            "average_useful_price_score": avg_score,
            "best_price_score": b_score,
            "conditional_BC_score": round(cond, 3),
        })
    return pd.DataFrame(rows)


def conditional_ranking(candidates: pd.DataFrame, bc_summary: pd.DataFrame) -> pd.DataFrame:
    if candidates.empty or bc_summary.empty:
        return pd.DataFrame()
    df = candidates.merge(bc_summary, on=["stopover_B", "destination_C"], how="left")
    for col in ["conditional_BC_score", "total_flights_count", "useful_flights_count", "useful_dates_count"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(1)
    df["conditional_B_score"] = (0.40 * df["route_score_A_B"] + 0.60 * df["conditional_BC_score"]).round(3)
    return df.sort_values(["conditional_B_score", "conditional_BC_score", "route_score_A_B"], ascending=False).reset_index(drop=True)


def select_bc_anchor_flights(bc_flights: pd.DataFrame, selected_pairs: pd.DataFrame, latest_arrival_to_C: datetime, req: SearchRequest) -> pd.DataFrame:
    if bc_flights.empty or selected_pairs.empty:
        return pd.DataFrame()
    df = prepare_flights(bc_flights)
    if df.empty:
        return df
    keys = selected_pairs[["stopover_B", "destination_C"]].drop_duplicates().rename(columns={"stopover_B": "origin_iata", "destination_C": "destination_iata"})
    df = df.merge(keys, on=["origin_iata", "destination_iata"], how="inner")
    df = df[df["arrival_datetime"] <= pd.Timestamp(latest_arrival_to_C)].copy()
    if df.empty:
        return df
    df["price_ratio"] = df["price"] / req.average_direct_price
    df = df[df["price_ratio"] <= MAX_BC_RATIO_FOR_COMBINATION].copy()
    if df.empty:
        return df
    return df.sort_values(["origin_iata", "destination_iata", "price", "departure_datetime"]).groupby(["origin_iata", "destination_iata"], group_keys=False).head(req.top_bc_flights_per_bc_pair).reset_index(drop=True)


# ============================================================
# FINAL ITINERARIES
# ============================================================

def build_final_itineraries(ab_flights: pd.DataFrame, bc_anchor: pd.DataFrame, cond: pd.DataFrame, req: SearchRequest, tw: Dict[str, Any]) -> pd.DataFrame:
    if ab_flights.empty or bc_anchor.empty or cond.empty:
        return pd.DataFrame()
    ab = prepare_flights(ab_flights)
    bc = prepare_flights(bc_anchor)
    if ab.empty or bc.empty:
        return pd.DataFrame()
    ab = ab.rename(columns={
        "origin_iata": "origin_A", "destination_iata": "stopover_B", "departure_datetime": "departure_datetime_AB", "arrival_datetime": "arrival_datetime_AB",
        "airline_iata": "airline_iata_AB", "airline_name": "airline_name_AB", "flight_number": "flight_number_AB", "price": "price_AB", "duration_minutes": "duration_minutes_AB", "query_hash": "query_hash_AB",
    })
    bc = bc.rename(columns={
        "origin_iata": "stopover_B", "destination_iata": "destination_C", "departure_datetime": "departure_datetime_BC", "arrival_datetime": "arrival_datetime_BC",
        "airline_iata": "airline_iata_BC", "airline_name": "airline_name_BC", "flight_number": "flight_number_BC", "price": "price_BC", "duration_minutes": "duration_minutes_BC", "query_hash": "query_hash_BC",
    })
    merged = ab.merge(bc, on="stopover_B", how="inner")
    compat_cols = ["origin_A", "stopover_B", "destination_C", "route_score_A_B", "route_score_B_C", "offline_B_score", "conditional_BC_score", "conditional_B_score", "stopover_name", "stopover_city", "stopover_country"]
    compat = cond[compat_cols].sort_values("conditional_B_score", ascending=False).drop_duplicates(["origin_A", "stopover_B", "destination_C"])
    merged = merged.merge(compat, on=["origin_A", "stopover_B", "destination_C"], how="inner")
    if merged.empty:
        return merged
    for c in ["departure_datetime_AB", "arrival_datetime_AB", "departure_datetime_BC", "arrival_datetime_BC"]:
        merged[c] = pd.to_datetime(merged[c], errors="coerce")
    merged["price_AB"] = pd.to_numeric(merged["price_AB"], errors="coerce")
    merged["price_BC"] = pd.to_numeric(merged["price_BC"], errors="coerce")
    merged = merged.dropna(subset=["departure_datetime_AB", "arrival_datetime_AB", "departure_datetime_BC", "arrival_datetime_BC", "price_AB", "price_BC"])
    if merged.empty:
        return merged
    earliest = pd.Timestamp(tw["earliest_departure_datetime"])
    latest_end = pd.Timestamp(tw["latest_trip_end_datetime"])
    latest_arrival_c = pd.Timestamp(tw["latest_arrival_to_C"])
    eff_dest_hours = float(tw["effective_min_destination_hours"])
    merged["stopover_hours"] = (merged["departure_datetime_BC"] - merged["arrival_datetime_AB"]).dt.total_seconds() / 3600
    merged["destination_hours"] = (latest_end - merged["arrival_datetime_BC"]).dt.total_seconds() / 3600
    mask = (
        (merged["departure_datetime_AB"] >= earliest) &
        (merged["arrival_datetime_AB"] + pd.to_timedelta(req.min_stopover_hours, unit="h") <= merged["departure_datetime_BC"]) &
        (merged["arrival_datetime_BC"] <= latest_arrival_c) &
        (merged["destination_hours"] >= eff_dest_hours) &
        (merged["stopover_hours"] >= req.min_stopover_hours)
    )
    merged = merged[mask].copy()
    if merged.empty:
        return merged
    merged["total_price"] = merged["price_AB"] + merged["price_BC"]
    merged["average_direct_price"] = req.average_direct_price
    merged["total_ratio"] = merged["total_price"] / req.average_direct_price
    merged["saving_abs"] = req.average_direct_price - merged["total_price"]
    merged["saving_pct"] = 100 * merged["saving_abs"] / req.average_direct_price
    merged["total_price_score_vs_direct"] = merged["total_ratio"].apply(total_price_score_vs_direct)
    merged["stopover_duration_score"] = merged["stopover_hours"].apply(lambda x: stopover_duration_score(x, req.min_stopover_hours))
    merged["database_route_quality"] = (pd.to_numeric(merged["route_score_A_B"], errors="coerce").fillna(1) + pd.to_numeric(merged["route_score_B_C"], errors="coerce").fillna(1)) / 2
    merged["final_itinerary_score"] = (0.70 * merged["total_price_score_vs_direct"] + 0.15 * merged["stopover_duration_score"] + 0.10 * pd.to_numeric(merged["conditional_BC_score"], errors="coerce").fillna(1) + 0.05 * merged["database_route_quality"]).round(3)
    cols = [
        "origin_A", "stopover_B", "destination_C", "stopover_name", "stopover_city", "stopover_country",
        "departure_datetime_AB", "arrival_datetime_AB", "airline_name_AB", "flight_number_AB", "price_AB", "duration_minutes_AB",
        "departure_datetime_BC", "arrival_datetime_BC", "airline_name_BC", "flight_number_BC", "price_BC", "duration_minutes_BC",
        "stopover_hours", "destination_hours", "total_price", "average_direct_price", "total_ratio", "saving_abs", "saving_pct",
        "route_score_A_B", "route_score_B_C", "conditional_BC_score", "conditional_B_score", "database_route_quality",
        "total_price_score_vs_direct", "stopover_duration_score", "final_itinerary_score", "query_hash_AB", "query_hash_BC"
    ]
    for col in cols:
        if col not in merged.columns:
            merged[col] = None
    return merged[cols].sort_values(["final_itinerary_score", "total_price", "stopover_hours"], ascending=[False, True, True]).reset_index(drop=True)


# ============================================================
# SAVE RUNS
# ============================================================

def save_search_run(conn: sqlite3.Connection, req: SearchRequest, tw: Dict[str, Any], summary: Dict[str, Any]) -> int:
    created_at = iso_utc(utc_now())
    conn.execute("""
    INSERT INTO search_runs (
        created_at, request_json, status, departure_airports, destination_airports,
        earliest_departure_date, max_trip_days, min_destination_hours, effective_min_destination_hours,
        min_stopover_hours, average_direct_price, currency, offline_candidates, bc_queries,
        bc_flights, ab_queries, ab_flights, final_itineraries, api_calls_total, api_calls_bc,
        api_calls_ab, cache_hits_bc, cache_hits_ab, needs_confirmation, partial
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        created_at,
        sanitized_request_json(req),
        summary.get("status"),
        ",".join(req.departure_airports),
        ",".join(req.destination_airports),
        req.earliest_departure_date,
        req.max_trip_days,
        req.min_destination_hours,
        tw["effective_min_destination_hours"],
        req.min_stopover_hours,
        req.average_direct_price,
        req.currency,
        summary.get("offline_candidates", 0),
        summary.get("bc_queries", 0),
        summary.get("bc_flights", 0),
        summary.get("ab_queries", 0),
        summary.get("ab_flights", 0),
        summary.get("final_itineraries", 0),
        summary.get("api_calls_total", 0),
        summary.get("api_calls_bc", 0),
        summary.get("api_calls_ab", 0),
        summary.get("cache_hits_bc", 0),
        summary.get("cache_hits_ab", 0),
        int(summary.get("needs_confirmation", False)),
        int(summary.get("partial", False)),
    ))
    conn.commit()
    return int(conn.execute("SELECT last_insert_rowid();").fetchone()[0])


def save_itineraries(conn: sqlite3.Connection, run_id: int, itineraries: pd.DataFrame) -> None:
    if itineraries.empty:
        return
    for idx, row in itineraries.reset_index(drop=True).iterrows():
        rec = row.replace({np.nan: None}).to_dict()
        for k, v in list(rec.items()):
            if isinstance(v, (pd.Timestamp, datetime)):
                rec[k] = str(v)
        conn.execute("""
        INSERT INTO itinerary_results (
            search_run_id, rank, origin_A, stopover_B, destination_C,
            departure_datetime_AB, arrival_datetime_AB, airline_name_AB, flight_number_AB, price_AB,
            departure_datetime_BC, arrival_datetime_BC, airline_name_BC, flight_number_BC, price_BC,
            stopover_hours, destination_hours, total_price, average_direct_price, total_ratio,
            saving_abs, saving_pct, final_itinerary_score, result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            run_id, idx + 1, rec.get("origin_A"), rec.get("stopover_B"), rec.get("destination_C"),
            str(rec.get("departure_datetime_AB")), str(rec.get("arrival_datetime_AB")), rec.get("airline_name_AB"), rec.get("flight_number_AB"), rec.get("price_AB"),
            str(rec.get("departure_datetime_BC")), str(rec.get("arrival_datetime_BC")), rec.get("airline_name_BC"), rec.get("flight_number_BC"), rec.get("price_BC"),
            rec.get("stopover_hours"), rec.get("destination_hours"), rec.get("total_price"), rec.get("average_direct_price"), rec.get("total_ratio"),
            rec.get("saving_abs"), rec.get("saving_pct"), rec.get("final_itinerary_score"), json.dumps(rec, ensure_ascii=False, default=str),
        ))
    conn.commit()


def refresh_route_price_stats(conn: sqlite3.Connection) -> None:
    obs = pd.read_sql_query("SELECT origin_iata, destination_iata, price, search_timestamp FROM flight_observations WHERE price IS NOT NULL;", conn)
    if obs.empty:
        return
    obs["price"] = pd.to_numeric(obs["price"], errors="coerce")
    obs["search_timestamp"] = pd.to_datetime(obs["search_timestamp"], errors="coerce")
    obs = obs.dropna(subset=["price"])
    if obs.empty:
        return
    grouped = obs.groupby(["origin_iata", "destination_iata"]).agg(
        observations_count=("price", "count"),
        min_price_seen=("price", "min"),
        median_price_seen=("price", "median"),
        avg_price_seen=("price", "mean"),
        last_seen_at=("search_timestamp", "max"),
    ).reset_index()
    last_prices = obs.sort_values("search_timestamp").groupby(["origin_iata", "destination_iata"]).tail(1)[["origin_iata", "destination_iata", "price"]].rename(columns={"price": "last_price_seen"})
    grouped = grouped.merge(last_prices, on=["origin_iata", "destination_iata"], how="left")
    conn.execute("DELETE FROM route_price_stats;")
    for _, r in grouped.iterrows():
        conn.execute("""
        INSERT OR REPLACE INTO route_price_stats (
            origin_iata, destination_iata, observations_count, min_price_seen, median_price_seen,
            avg_price_seen, last_price_seen, last_seen_at, cheapness_score_observed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (r["origin_iata"], r["destination_iata"], int(r["observations_count"]), float(r["min_price_seen"]), float(r["median_price_seen"]), float(r["avg_price_seen"]), float(r["last_price_seen"]), str(r["last_seen_at"]), None))
    conn.commit()


# ============================================================
# MAIN SEARCH
# ============================================================

@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", db_exists=Path(DB_PATH).exists(), db_path=DB_PATH, server_serpapi_key_present=bool(SERPAPI_API_KEY))

@app.get("/api/debug/db-counts")
def db_counts() -> Dict[str, Any]:
    """Small operational endpoint to verify which SQLite file the deployed backend is using."""
    conn = connect_db()
    ensure_tables(conn)
    tables = [
        "airports",
        "routes",
        "api_search_cache",
        "flight_observations",
        "route_price_stats",
        "search_runs",
        "itinerary_results",
    ]
    counts: Dict[str, Any] = {}
    for table in tables:
        try:
            counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0])
        except Exception as exc:
            counts[table] = f"error: {exc}"
    conn.close()
    return {"status": "ok", "db_path": DB_PATH, "counts": counts}


@app.post("/api/search")
def search(req: SearchRequest) -> Dict[str, Any]:
    conn = connect_db()
    ensure_tables(conn)
    validate_static_tables(conn)
    tw = compute_time_window(req)
    budget = ApiBudget(req.total_api_budget, req.bc_api_budget, req.ab_api_budget)

    offline = get_offline_candidates(conn, req)
    if offline.empty:
        summary = {"status": "no_offline_candidates", "offline_candidates": 0, "final_itineraries": 0}
        run_id = save_search_run(conn, req, tw, summary)
        conn.close()
        return {"status": "no_offline_candidates", "search_run_id": run_id, "summary": summary, "itineraries": []}

    bc_queries = build_bc_queries(offline, tw["bc_dates"], req)
    bc_flights, bc_logs, bc_partial = execute_query_batch(conn, bc_queries, "BC", budget, req)

    bc_pairs = bc_queries[["origin_iata", "destination_iata"]].drop_duplicates().rename(columns={"origin_iata": "stopover_B", "destination_iata": "destination_C"}) if not bc_queries.empty else pd.DataFrame(columns=["stopover_B", "destination_C"])
    bc_summary = summarize_bc(bc_pairs, bc_flights, req, tw["latest_arrival_to_C"])
    cond = conditional_ranking(offline, bc_summary)

    best_ratios = pd.to_numeric(bc_summary.get("best_BC_ratio", pd.Series(dtype=float)), errors="coerce").dropna()
    best_overall_bc_ratio = float(best_ratios.min()) if len(best_ratios) else None
    best_bc_price = float(pd.to_numeric(bc_summary.get("best_BC_price", pd.Series(dtype=float)), errors="coerce").dropna().min()) if len(pd.to_numeric(bc_summary.get("best_BC_price", pd.Series(dtype=float)), errors="coerce").dropna()) else None

    needs_confirmation = False
    if not req.force_continue:
        if best_overall_bc_ratio is None or best_overall_bc_ratio >= CONFIRMATION_BC_RATIO_THRESHOLD:
            needs_confirmation = True

    if needs_confirmation:
        summary = {
            "status": "needs_confirmation",
            "message": "Best B to C segment is expensive relative to the direct benchmark. Send the same request with force_continue=true to continue.",
            "best_BC_price": best_bc_price,
            "best_BC_ratio": best_overall_bc_ratio,
            "average_direct_price": req.average_direct_price,
            "offline_candidates": len(offline),
            "bc_queries": len(bc_queries),
            "bc_flights": len(bc_flights),
            "ab_queries": 0,
            "ab_flights": 0,
            "final_itineraries": 0,
            "api_calls_total": budget.total_calls,
            "api_calls_bc": budget.bc_calls,
            "api_calls_ab": budget.ab_calls,
            "cache_hits_bc": int(bc_logs["cache_hit"].sum()) if not bc_logs.empty else 0,
            "cache_hits_ab": 0,
            "needs_confirmation": True,
            "partial": bool(bc_partial),
        }
        run_id = save_search_run(conn, req, tw, summary)
        conn.close()
        return {"status": "needs_confirmation", "search_run_id": run_id, "summary": summary, "itineraries": [], "bc_summary": df_to_records(bc_summary, 50)}

    valid = cond[pd.to_numeric(cond.get("total_flights_count", 0), errors="coerce").fillna(0) > 0].copy() if not cond.empty else pd.DataFrame()
    if valid.empty:
        summary = {
            "status": "no_bc_flights",
            "offline_candidates": len(offline),
            "bc_queries": len(bc_queries),
            "bc_flights": len(bc_flights),
            "ab_queries": 0,
            "ab_flights": 0,
            "final_itineraries": 0,
            "api_calls_total": budget.total_calls,
            "api_calls_bc": budget.bc_calls,
            "api_calls_ab": budget.ab_calls,
            "cache_hits_bc": int(bc_logs["cache_hit"].sum()) if not bc_logs.empty else 0,
            "cache_hits_ab": 0,
            "needs_confirmation": False,
            "partial": bool(bc_partial),
        }
        run_id = save_search_run(conn, req, tw, summary)
        conn.close()
        return {"status": "no_bc_flights", "search_run_id": run_id, "summary": summary, "itineraries": [], "bc_summary": df_to_records(bc_summary, 50)}

    top_b = valid.groupby("stopover_B").agg(best=("conditional_B_score", "max")).reset_index().sort_values("best", ascending=False).head(req.top_b_after_bc_for_ab_api)
    selected_b = set(top_b["stopover_B"].tolist())
    selected_pairs = valid[valid["stopover_B"].isin(selected_b)].sort_values("conditional_B_score", ascending=False).drop_duplicates(["stopover_B", "destination_C"])
    bc_anchor = select_bc_anchor_flights(bc_flights, selected_pairs, tw["latest_arrival_to_C"], req)
    ab_queries = build_ab_queries(cond, bc_anchor, tw["earliest_departure_datetime"], req.min_stopover_hours, req)
    ab_flights, ab_logs, ab_partial = execute_query_batch(conn, ab_queries, "AB", budget, req)
    final = build_final_itineraries(ab_flights, bc_anchor, cond, req, tw)

    refresh_route_price_stats(conn)

    summary = {
        "status": "completed_partial" if (bc_partial or ab_partial) else "completed",
        "departure_airports": req.departure_airports,
        "destination_airports": req.destination_airports,
        "earliest_departure_datetime": str(tw["earliest_departure_datetime"]),
        "latest_trip_end_datetime": str(tw["latest_trip_end_datetime"]),
        "effective_min_destination_hours": tw["effective_min_destination_hours"],
        "latest_arrival_to_C": str(tw["latest_arrival_to_C"]),
        "average_direct_price": req.average_direct_price,
        "currency": req.currency,
        "offline_candidates": len(offline),
        "bc_queries": len(bc_queries),
        "bc_flights": len(bc_flights),
        "ab_queries": len(ab_queries),
        "ab_flights": len(ab_flights),
        "final_itineraries": len(final),
        "api_calls_total": budget.total_calls,
        "api_calls_bc": budget.bc_calls,
        "api_calls_ab": budget.ab_calls,
        "cache_hits_bc": int(bc_logs["cache_hit"].sum()) if not bc_logs.empty else 0,
        "cache_hits_ab": int(ab_logs["cache_hit"].sum()) if not ab_logs.empty else 0,
        "needs_confirmation": False,
        "partial": bool(bc_partial or ab_partial),
    }
    run_id = save_search_run(conn, req, tw, summary)
    save_itineraries(conn, run_id, final)
    conn.close()

    return {
        "status": summary["status"],
        "search_run_id": run_id,
        "summary": summary,
        "itineraries": df_to_records(final, 100),
        "bc_summary": df_to_records(bc_summary.sort_values("conditional_BC_score", ascending=False) if not bc_summary.empty else bc_summary, 50),
    }
