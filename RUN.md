export NAGARE_AGENT_MODE=model
uvicorn web.expert:app --host 0.0.0.0 --port 8000

Open http://localhost:8000/schedule in a browser.
