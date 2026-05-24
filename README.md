# Expense Processing Agent
### WhatsApp to Priority ERP | Prototype for Arad Group

## Architecture

```text
Employee WhatsApp
      |
      v
Twilio Business API webhook
      |
      v
FastAPI Orchestrator
      |-- Gemini 1.5 Flash Vision
      |-- Redis FSM
      |-- Policy Engine
      |-- Celery Task Queue
      |-- Priority ERP Client
      v
Employee WhatsApp + Dashboard
```

## Quick Start

```bash
git clone <repo>
cd expense-agent
pip install -r requirements.txt
cp .env.example .env
docker-compose up -d redis
uvicorn app.main:app --reload --port 8000
```

In another terminal:

```bash
celery -A celery_worker worker --loglevel=info
ngrok http 8000
```

Set the Twilio sandbox webhook to `{ngrok_url}/webhook/twilio`.

Open the dashboard at `http://localhost:8000`.

## Demo Data

```bash
python scripts/seed_demo_data.py
```

## Tests

```bash
pytest tests/ -v --asyncio-mode=auto -k "not live"
```

## Production Swap

- `PRIORITY_USE_MOCK=false` enables the real Priority ERP client.
- `DATABASE_URL=postgresql+asyncpg://...` can replace SQLite for production.
- Deploy behind HTTPS and set Twilio to the production webhook URL.
