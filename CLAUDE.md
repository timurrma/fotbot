# Claude Rules for football_bot

## Database safety

NEVER run DELETE or UPDATE SQL queries on the production database without explicit user permission.
This includes:
- Direct SQL via asyncpg/psql
- SQLAlchemy `session.delete()`, `session.execute(DELETE...)`, `session.execute(UPDATE...)`
- Any script that modifies or removes existing rows

Before doing any DELETE/UPDATE, ask the user: "Хочешь удалить/обновить X — подтверди?"
