from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# creating a SQL session

DATABASE_URL = "sqlite:///./backend/app/db/database.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
