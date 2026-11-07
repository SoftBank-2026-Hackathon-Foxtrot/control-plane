"""Create/rotate the app login and grant access. User apps own tables and data.

Runs once inside the image for its psycopg dependency; never imports application code.
"""

import os
import time

import psycopg
from psycopg import sql

env = os.environ
admin_args = dict(
    host=env["DB_HOST"],
    port=env["DB_PORT"],
    dbname=env["DB_NAME"],
    user=env["DB_ADMIN_USER"],
    password=env["DB_ADMIN_PASSWORD"],
    sslmode="require",
    connect_timeout=10,
    autocommit=True,
)

deadline = time.time() + 900
while True:
    try:
        admin = psycopg.connect(**admin_args)
        break
    except psycopg.OperationalError as exc:
        if time.time() > deadline:
            raise
        print(f"database not reachable yet: {exc}".splitlines()[0], flush=True)
        time.sleep(10)

role, password = env["APP_DB_USER"], env["APP_DB_PASSWORD"]
# API and queue-worker containers can start concurrently on managed runtimes.
admin.execute("SELECT pg_advisory_lock(hashtext(%s))", ("foxtrot-bootstrap:" + role,))
exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
verb = "ALTER" if exists else "CREATE"
# Container restarts must preserve NOLOGIN fences established by the migration operator.
login = "" if exists else " LOGIN"
admin.execute(
    sql.SQL(verb + " ROLE {}" + login + " PASSWORD {}").format(
        sql.Identifier(role), sql.Literal(password)
    )
)
admin.execute(
    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(env["DB_NAME"]), sql.Identifier(role)
    )
)
admin.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(sql.Identifier(role)))
# The app role owns its tables, and only a table's owner may add it to a publication or apply
# replicated rows to it (managed admins are not superusers). The platform's DB migration runs as
# this admin, so the admin takes the app role's privileges.
try:
    admin.execute(
        sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET TRUE").format(
            sql.Identifier(role), sql.Identifier(env["DB_ADMIN_USER"])
        )
    )
except psycopg.Error as exc:
    print(
        f"could not grant {role} to the admin (DB migration needs it): {exc}".splitlines()[0],
        flush=True,
    )
admin.close()

print("DB_ROLE_READY", flush=True)
