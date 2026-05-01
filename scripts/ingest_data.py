
import pandas as pd
import os
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
RAW_DATA_PATH = os.getenv("RAW_DATA_PATH")

engine = create_engine(DATABASE_URL)

def ingest():
    for file in os.listdir(RAW_DATA_PATH):
        if file.endswith(".csv"):
            path = os.path.join(RAW_DATA_PATH, file)
            df = pd.read_csv(path)
            df.to_sql("stats", engine, if_exists="append", index=False)

if __name__ == "__main__":
    ingest()
