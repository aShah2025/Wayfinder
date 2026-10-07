"""
Entry point for hosting on Vercel.

Vercel runs Python files in the api/ folder as "serverless functions". This one just
hands every /api/... request to our FastAPI app in backend/main.py.
(The website files in frontend/ are served directly by Vercel; see vercel.json.)
"""
from backend.main import app  # noqa: F401
