from main import app
import sys

print("Registered Routes:")
for route in app.routes:
    print(f"{route.path} [{route.name}]")
