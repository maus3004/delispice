
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app/data/app.db")
RAW_DATA_PATH = os.getenv("RAW_DATA_PATH", "./data/raw")
