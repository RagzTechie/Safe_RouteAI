import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
service_key = os.environ.get("SUPABASE_SERVICE_KEY")

supabase: Client = create_client(url, key)
supabase_admin: Client = create_client(url, service_key)  # for SOS triggers

def get_safety_data():
    response = supabase.table("safety_data").select("*").execute()
    return response.data

def get_user_profile(user_id: str):
    response = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
    return response.data

def update_user_profile(user_id: str, data: dict):
    response = supabase.table("profiles").update(data).eq("id", user_id).execute()
    return response.data

def get_emergency_contacts(user_id: str):
    """Fetch ALL emergency contacts for a user from the emergency_contacts table."""
    response = supabase.table("emergency_contacts") \
        .select("*") \
        .eq("user_id", user_id) \
        .execute()
    return response.data  # list of {id, user_id, name, phone_number, created_at}

def log_sos_alert(user_id: str, lat: float, lon: float):
    response = supabase_admin.table("sos_alerts").insert({
        "user_id": user_id,
        "latitude": lat,
        "longitude": lon
    }).execute()
    return response.data