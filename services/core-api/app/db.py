from fastapi import Request
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Deterministic constraint and index names, so a migration can always refer to
# an existing constraint by a name it can predict (needed to drop or alter it).
#
# - pk, fk and uq reproduce PostgreSQL's own default names (users_pkey,
#   courses_owner_id_fkey, enrollments_user_id_course_id_key). The baseline
#   migration relies on this: the scaffold database already uses these names.
# - ix and ck are this project's convention for new objects. Every CHECK
#   constraint must be given a short explicit name, e.g.
#   CheckConstraint("mastery BETWEEN 0 AND 1", name="mastery_range") becomes
#   mastery_scores_mastery_range_check; an unnamed one raises an error as soon
#   as the table is defined, instead of getting an unpredictable name.
#   Expression indexes (no plain column) need an explicit name too.
# - PostgreSQL truncates identifiers at 63 characters; give long ones a
#   shorter explicit name rather than relying on truncation.
NAMING_CONVENTION = {
    "pk": "%(table_name)s_pkey",
    "fk": "%(table_name)s_%(column_0_name)s_fkey",
    "uq": "%(table_name)s_%(column_0_N_name)s_key",
    "ix": "%(table_name)s_%(column_0_N_name)s_idx",
    "ck": "%(table_name)s_%(constraint_name)s_check",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def get_db(request: Request):
    """FastAPI dependency: yields a DB session and always closes it.

    The session comes from the application's own factory (app.state, set by
    create_app), so an app built for tests never reaches the default engine.
    """
    db = request.app.state.session_factory()
    try:
        yield db
    finally:
        db.close()
