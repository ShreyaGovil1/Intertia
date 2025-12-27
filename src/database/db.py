from dotenv import load_dotenv
from pathlib import Path
from motor.motor_asyncio import AsyncIOMotorClient
from setting.config import MONGO_URL, DB_NAME

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

__all__ = ["client", "db"]
