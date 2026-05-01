
from sqlalchemy import Column, Integer, String
from app.db.base import Base

class Stats(Base):
    __tablename__ = "stats"

    id = Column(Integer, primary_key=True, index=True)
    player = Column(String)
    team = Column(String)
