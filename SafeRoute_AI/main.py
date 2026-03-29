import os
import osmnx as ox
import networkx as nx
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from database import get_safety_data, get_user_profile, log_sos_alert
from twilio.rest import Client as TwilioClient
from pydantic import BaseModel

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

CHENNAI_POINT = (13.0827, 80.2707)
print("Downloading Chennai street network...")
G = ox.graph_from_point(CHENNAI_POINT, dist=2500, network_type='walk')
for u, v, data in G.edges(data=True):
    data['weight'] = data.get('length', 100)

# --- EXISTING route endpoint (keep as-is) ---
@app.get("/calculate_route")
def calculate_safe_route(start_lat: float, start_lon: float,
                          end_lat: float, end_lon: float):
    # your existing code here unchanged
    pass

# --- NEW: Check if a coordinate is dangerous ---
@app.get("/check_danger")
def check_danger(lat: float, lon: float):
    safety_points = get_safety_data()
    danger_score = 0
    for point in safety_points:
        dist_lat = abs(point['latitude'] - lat)
        dist_lon = abs(point['longitude'] - lon)
        if dist_lat < 0.0015 and dist_lon < 0.0015:
            danger_score += point['safety_score']
    is_dangerous = danger_score < -50
    return {"is_dangerous": is_dangerous, "score": danger_score}

# --- NEW: Trigger SOS alert ---
class SOSRequest(BaseModel):
    user_id: str
    latitude: float
    longitude: float

@app.post("/trigger_sos")
def trigger_sos(req: SOSRequest):
    profile = get_user_profile(req.user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    # Log to Supabase
    log_sos_alert(req.user_id, req.latitude, req.longitude)

    # Send SMS via Twilio
    twilio_client = TwilioClient(
        os.environ.get("TWILIO_ACCOUNT_SID"),
        os.environ.get("TWILIO_AUTH_TOKEN")
    )
    location_link = f"https://maps.google.com/?q={req.latitude},{req.longitude}"
    message = (f"🚨 EMERGENCY ALERT from SafeRoute AI!\n"
               f"{profile.get('full_name', 'User')} may be in danger.\n"
               f"Live location: {location_link}")

    twilio_client.messages.create(
        body=message,
        from_=os.environ.get("TWILIO_PHONE"),
        to=profile.get("emergency_contact_phone")
    )
    return {"status": "SOS sent", "contact": profile.get("emergency_contact_phone")}
