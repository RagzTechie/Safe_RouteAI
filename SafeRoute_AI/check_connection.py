from database import supabase

try:
    # This tries to count how many rows are in your table
    response = supabase.table("safety_data").select("*", count="exact").execute()
    print("✅ Connection Successful!")
    print(f"📊 Your database has {response.count} rows ready for SafeRoute AI.")
except Exception as e:
    print("❌ Connection Failed.")
    print(f"Error details: {e}")