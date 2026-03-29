import os, math, requests
import osmnx as ox
import networkx as nx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import get_user_profile, get_emergency_contacts, log_sos_alert, supabase_admin
from twilio.rest import Client as TwilioClient
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
app.add_middleware(CORSMiddleware,
                   allow_origins=["*"],
                   allow_credentials=False,   # must be False when allow_origins="*"
                   allow_methods=["*"],
                   allow_headers=["*"])

# ─────────────────────────────────────────────
# OSM graph cache (keyed by rounded centre + mode)
# ─────────────────────────────────────────────
_graph_cache: dict = {}

def get_graph(lat: float, lon: float, mode: str):
    ntype = {"walk": "walk", "bike": "bike",
             "drive": "drive", "transit": "drive"}[mode]
    key = (round(lat, 2), round(lon, 2), ntype)
    if key not in _graph_cache:
        print(f"[osmnx] downloading {ntype} graph around {lat:.3f},{lon:.3f} ...")
        G = ox.graph_from_point((lat, lon), dist=5000,
                                network_type=ntype, simplify=True)
        _graph_cache[key] = G
        print(f"[osmnx] done — {len(G.nodes)} nodes")
    return _graph_cache[key]


# ─────────────────────────────────────────────
# Overpass — fetch REAL POIs from OpenStreetMap
# ─────────────────────────────────────────────
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# (osm_key, osm_value, safety_score)
OSM_TAGS = {
    "police":      ("amenity", "police",           +100),
    "hospital":    ("amenity", "hospital",          +80),
    "pharmacy":    ("amenity", "pharmacy",           +60),
    "clinic":      ("amenity", "clinic",             +55),
    "cafe":        ("amenity", "cafe",               +20),
    "restaurant":  ("amenity", "restaurant",         +15),
    "convenience": ("shop",    "convenience",         +25),
    "supermarket": ("shop",    "supermarket",         +30),
    "bus_stop":    ("highway", "bus_stop",            +15),
    "train":       ("railway", "station",             +35),
    "metro":       ("railway", "subway_station",      +35),
    "bar":         ("amenity", "bar",                -10),
    "nightclub":   ("amenity", "nightclub",          -20),
    "industrial":  ("landuse", "industrial",         -25),
    "cemetery":    ("landuse", "cemetery",           -20),
}

CATEGORY_META = {
    "police":     {"emoji": "🚔", "label": "Police Station",    "layer": "police"},
    "hospital":   {"emoji": "🏥", "label": "Hospital",           "layer": "safe"},
    "pharmacy":   {"emoji": "💊", "label": "Pharmacy",            "layer": "safe"},
    "clinic":     {"emoji": "🏥", "label": "Clinic",              "layer": "safe"},
    "cafe":       {"emoji": "☕", "label": "Café",               "layer": "safe"},
    "restaurant": {"emoji": "🍽️","label": "Restaurant",         "layer": "safe"},
    "convenience":{"emoji": "🏪", "label": "Convenience Store",  "layer": "safe"},
    "supermarket":{"emoji": "🛒", "label": "Supermarket",        "layer": "safe"},
    "bus_stop":   {"emoji": "🚌", "label": "Bus Stop",           "layer": "safe"},
    "train":      {"emoji": "🚆", "label": "Train Station",      "layer": "safe"},
    "metro":      {"emoji": "🚇", "label": "Metro Station",      "layer": "safe"},
    "bar":        {"emoji": "🍺", "label": "Bar",                "layer": "danger"},
    "nightclub":  {"emoji": "🎵", "label": "Nightclub",          "layer": "danger"},
    "industrial": {"emoji": "🏭", "label": "Industrial Area",    "layer": "danger"},
    "cemetery":   {"emoji": "⬛", "label": "Cemetery",           "layer": "danger"},
}


def fetch_pois(lat: float, lon: float, radius_m: int = 3000) -> list:
    deg = radius_m / 111_000
    bbox = f"{lat-deg},{lon-deg},{lat+deg},{lon+deg}"

    parts = []
    for cat, (key, val, _) in OSM_TAGS.items():
        parts.append(f'node["{key}"="{val}"]({bbox});')
        parts.append(f'way["{key}"="{val}"]({bbox});')

    query = "[out:json][timeout:30];\n(\n" + "\n".join(parts) + "\n);\nout center 200;"

    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=35)
        r.raise_for_status()
        elements = r.json().get("elements", [])
    except Exception as e:
        print(f"[overpass] error: {e}")
        return []

    pois, seen = [], set()
    for el in elements:
        tags = el.get("tags", {})
        plat = el.get("lat") or el.get("center", {}).get("lat")
        plon = el.get("lon") or el.get("center", {}).get("lon")
        if plat is None or plon is None:
            continue

        cat = next((c for c, (k, v, _) in OSM_TAGS.items()
                    if tags.get(k) == v), None)
        if cat is None:
            continue

        uid = f"{cat}:{round(plat,5)}:{round(plon,5)}"
        if uid in seen:
            continue
        seen.add(uid)

        score = OSM_TAGS[cat][2]
        meta  = CATEGORY_META[cat]
        pois.append({
            "category":     cat,
            "label":        meta["label"],
            "emoji":        meta["emoji"],
            "layer":        meta["layer"],
            "latitude":     plat,
            "longitude":    plon,
            "safety_score": score,
            "name":         tags.get("name", meta["label"]),
        })
    return pois


# ─────────────────────────────────────────────
# Safety-weighted routing helpers
# ─────────────────────────────────────────────
def _apply_safety_weights(G, pois: list):
    for u, v, data in G.edges(data=True):
        base = data.get("length", 100)
        penalty = 0.0
        try:
            mid_lat = (G.nodes[u]["y"] + G.nodes[v]["y"]) / 2
            mid_lon = (G.nodes[u]["x"] + G.nodes[v]["x"]) / 2
        except KeyError:
            data["safety_weight"] = base
            continue
        for pt in pois:
            d = math.sqrt((pt["latitude"] - mid_lat)**2 +
                          (pt["longitude"] - mid_lon)**2)
            if d > 0.004:
                continue
            proximity = 1 - d / 0.004
            s = pt["safety_score"]
            penalty += (-s * 8 * proximity) if s < 0 else (-s * 1.5 * proximity)
        data["safety_weight"] = max(base + penalty, 1)


def _path_distance(G, path) -> float:
    total = 0.0
    for u, v in zip(path[:-1], path[1:]):
        ed = G.get_edge_data(u, v)
        if ed is None:
            continue
        vals = list(ed.values()) if isinstance(ed, dict) else [ed]
        total += min(d.get("length", 0) for d in vals)
    return total


def _coords(G, path) -> list:
    return [[G.nodes[n]["y"], G.nodes[n]["x"]] for n in path]


MODE_KMH = {"walk": 5, "bike": 15, "drive": 40, "transit": 25}

def travel_time(dist_m: float, mode: str) -> int:
    return max(1, round(dist_m / 1000 / MODE_KMH[mode] * 60))


def route_score(G, path, pois):
    coords = _coords(G, path)
    danger, safe_ct = 0, 0
    step = max(1, len(coords) // 40)
    for c in coords[::step]:
        for pt in pois:
            if (abs(pt["latitude"] - c[0]) < 0.003 and
                    abs(pt["longitude"] - c[1]) < 0.003):
                if pt["safety_score"] < 0:
                    danger += 1
                else:
                    safe_ct += 1
    return {"danger_zones": danger, "safe_landmarks": safe_ct}


# ─────────────────────────────────────────────
# /calculate_route
# ─────────────────────────────────────────────
@app.get("/calculate_route")
def calculate_safe_route(
    start_lat: float, start_lon: float,
    end_lat:   float, end_lon:   float,
    mode: str = "walk"
):
    if mode not in MODE_KMH:
        raise HTTPException(400, "mode must be: walk | bike | drive | transit")

    mid_lat = (start_lat + end_lat) / 2
    mid_lon = (start_lon + end_lon) / 2
    straight_m = math.sqrt((end_lat - start_lat)**2 +
                           (end_lon - start_lon)**2) * 111_000
    radius = min(int(straight_m / 2) + 2000, 8000)

    # 1. Real POIs from Overpass
    pois = fetch_pois(mid_lat, mid_lon, radius_m=radius)

    # 2. OSM road graph for this transport mode
    try:
        G = get_graph(mid_lat, mid_lon, mode)
    except Exception as e:
        raise HTTPException(500, f"Road graph error: {e}")

    # 3. Safety-weight edges
    _apply_safety_weights(G, pois)

    # 4. Snap to nearest nodes
    try:
        orig = ox.nearest_nodes(G, start_lon, start_lat)
        dest = ox.nearest_nodes(G, end_lon,   end_lat)
    except Exception as e:
        raise HTTPException(500, f"Node snap error: {e}")

    if orig == dest:
        raise HTTPException(400, "Start and destination are too close.")

    # 5. Compute both routes
    try:
        short_path = nx.shortest_path(G, orig, dest, weight="length")
    except nx.NetworkXNoPath:
        raise HTTPException(404, "No route found. Try a different mode or locations.")

    try:
        safe_path = nx.shortest_path(G, orig, dest, weight="safety_weight")
    except nx.NetworkXNoPath:
        safe_path = short_path

    short_dist = _path_distance(G, short_path)
    safe_dist  = _path_distance(G, safe_path)

    return {
        "status":         "success",
        "mode":           mode,
        "safe_route":     _coords(G, safe_path),
        "short_route":    _coords(G, short_path),
        "safe_dist_m":    round(safe_dist),
        "short_dist_m":   round(short_dist),
        "safe_time_min":  travel_time(safe_dist,  mode),
        "short_time_min": travel_time(short_dist, mode),
        "safe_summary":   route_score(G, safe_path,  pois),
        "short_summary":  route_score(G, short_path, pois),
        "pois":           pois,   # real live markers for the map
    }


# ─────────────────────────────────────────────
# /pois  — live markers for any location
# ─────────────────────────────────────────────
@app.get("/pois")
def get_pois(lat: float, lon: float, radius: int = 1500):
    return {"pois": fetch_pois(lat, lon, radius_m=radius)}


# ─────────────────────────────────────────────
# /check_danger  — is a coordinate dangerous?
# ─────────────────────────────────────────────
@app.get("/check_danger")
def check_danger(lat: float, lon: float):
    pois  = fetch_pois(lat, lon, radius_m=300)
    score = sum(p["safety_score"] for p in pois)
    return {"is_dangerous": score < -20, "score": score, "nearby": pois[:6]}


# ─────────────────────────────────────────────
# Live location sharing
# ─────────────────────────────────────────────
class LocationUpdate(BaseModel):
    user_id:     str
    latitude:    float
    longitude:   float
    share_token: str

@app.post("/share_location")
def share_location(req: LocationUpdate):
    supabase_admin.table("live_locations").upsert({
        "user_id":     req.user_id,
        "latitude":    req.latitude,
        "longitude":   req.longitude,
        "share_token": req.share_token,
    }, on_conflict="user_id").execute()
    return {"status": "ok"}

@app.get("/live_location/{token}")
def get_live_location(token: str):
    res = supabase_admin.table("live_locations") \
        .select("latitude,longitude,updated_at") \
        .eq("share_token", token).single().execute()
    if not res.data:
        raise HTTPException(404, "Location not found or sharing stopped.")
    return res.data


# ─────────────────────────────────────────────
# SOS
# ─────────────────────────────────────────────
class SOSRequest(BaseModel):
    user_id:   str
    latitude:  float
    longitude: float
    
@app.post("/trigger_sos")
def trigger_sos(req: SOSRequest):
    try:
        profile = get_user_profile(req.user_id)
        if not profile:
            raise HTTPException(404, "Profile not found")
        
        contacts = get_emergency_contacts(req.user_id)
        if not contacts:
            raise HTTPException(404, "No emergency contacts found for this user")

        log_sos_alert(req.user_id, req.latitude, req.longitude)

        # Ensure credentials exist
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        twilio_phone = os.getenv("TWILIO_PHONE")

        if not all([account_sid, auth_token, twilio_phone]):
            raise HTTPException(500, "Twilio configuration missing in .env")

        tc = TwilioClient(account_sid, auth_token)
        
        loc_link = f"https://www.google.com/maps?q={req.latitude},{req.longitude}"
        sender_name = profile.get("full_name", "A SafeRoute user")
        msg = (f"🚨 SOS! {sender_name} needs help.\n"
               f"Location: {loc_link}")

        sent, failed = [], []
        for c in contacts:
            raw_phone = c.get("phone_number")
            if not raw_phone:
                continue
            
            # Cleaner phone formatting
            clean_phone = "".join(filter(str.isdigit, raw_phone))
            if not raw_phone.startswith("+"):
                # Defaulting to +91 for Coimbatore/India context
                target_phone = f"+91{clean_phone[-10:]}"
            else:
                target_phone = f"+{clean_phone}"

            try:
                tc.messages.create(body=msg, from_=twilio_phone, to=target_phone)
                sent.append(c.get("name", target_phone))
            except Exception as twilio_err:
                print(f"Twilio failed for {target_phone}: {twilio_err}")
                failed.append({"contact": c.get("name"), "error": str(twilio_err)})

        return {"status": "complete", "sent_to": sent, "failed": failed}

    except Exception as global_err:
        print(f"CRITICAL SOS ERROR: {global_err}")
        raise HTTPException(500, f"Internal Server Error: {str(global_err)}")