# 🤖 AI Investor

An AI-powered paper trading system. Claude analyzes market data daily and executes trades automatically via Alpaca.

## How it works

1. **market_data.py** — fetches live prices + news from Polygon.io
2. **analysis.py** — sends data to Claude, gets back trade decisions as JSON
3. **execute.py** — places orders on Alpaca (paper trading)
4. **main.py** — orchestrates all of the above, runs daily at 9:45am

## Setup

### 1. Clone and enter the project
```bash
git clone https://github.com/YOUR_USERNAME/ai-investor.git
cd ai-investor
```

### 2. Create your virtual environment
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

### 4. Add your API keys
```bash
cp .env.example .env
```
Then open `.env` and fill in your real keys.

### 5. Run it
```bash
python main.py
```

## API Keys needed

| Key | Where to get it |
|-----|----------------|
| `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` | alpaca.markets → Paper Trading → API Keys |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `POLYGON_API_KEY` | polygon.io → Dashboard → API Keys |

## Going live (when ready)

In `execute.py`, change:
```python
BASE_URL = "https://paper-api.alpaca.markets"
```
to:
```python
BASE_URL = "https://api.alpaca.markets"
```
And swap in your live Alpaca keys in `.env`.
