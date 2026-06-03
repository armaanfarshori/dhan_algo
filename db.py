from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

_engine = None
_SessionLocal = None


def init_db(db_url: str):
    global _engine, _SessionLocal
    _engine = create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def get_engine():
    if _engine is None:
        raise RuntimeError("DB not initialised — call init_db() first")
    return _engine


@contextmanager
def get_session() -> Session:
    if _SessionLocal is None:
        raise RuntimeError("DB not initialised — call init_db() first")
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_connection() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
