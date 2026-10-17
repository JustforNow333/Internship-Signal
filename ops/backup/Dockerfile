# Scheduled FindSooner PostgreSQL backup and backup monitor (Railway cron).
# The PostgreSQL 18 image supplies pg_dump/pg_restore matching production and
# the server binaries for the throwaway restore check.
FROM postgres:18.6-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-psycopg age ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ops/__init__.py ops/__init__.py
COPY ops/backup/__init__.py ops/backup/pgbackup.py ops/backup/

ENV PG_BIN=/usr/lib/postgresql/18/bin \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# initdb refuses to run as root.
USER postgres
ENTRYPOINT []
CMD ["python3", "-m", "ops.backup.pgbackup", "backup"]
