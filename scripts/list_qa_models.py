"""List model IDs from the configured provider, without exposing credentials."""
from pathlib import Path
import os
import sys
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).resolve().parents[1] / '.env')
client = OpenAI(api_key=os.environ['OPENAI_API_KEY'], base_url=os.environ['OPENAI_BASE_URL'], timeout=15, max_retries=0)
try:
    for model in client.models.list().data:
        print(model.id)
except Exception as exc:
    print(type(exc).__name__)
    sys.exit(1)
