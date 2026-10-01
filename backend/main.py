from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI()

@app.get("/api/health")
def health():
    return {"status": "ok", "message": "Wayfinder is alive"}

app.mount("/", StaticFiles(directory="frontend", html=True))