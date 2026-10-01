"""Central settings, loaded from environment variables (.env supported)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# LLMs (Groq free tier). The agent model drives conversations; the fast model is used where latency
# matters most (live signal extraction) and for the simulated test customer.
AGENT_MODEL = os.getenv("AGENT_MODEL", "openai/gpt-oss-120b")
FAST_MODEL = os.getenv("FAST_MODEL", "openai/gpt-oss-20b")
ASR_MODEL = os.getenv("ASR_MODEL", "whisper-large-v3-turbo")

EMBED_MODEL = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
KB_DIR = DATA_DIR / "kb"
CRM_DIR = DATA_DIR / "crm"
PROFILES_DIR = ROOT / "profiles"
RECORDINGS_DIR = ROOT / "recordings"
REPORTS_DIR = ROOT / "reports"
SCENARIOS_DIR = ROOT / "live_scenarios"
WEB_DIR = ROOT / "web"
CACHE_DIR = ROOT / ".cache"

for _d in (KB_DIR, CRM_DIR, RECORDINGS_DIR, REPORTS_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SAMPLE_RATE = 16000
