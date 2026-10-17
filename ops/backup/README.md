# PostgreSQL backups

Daily, encrypted, offsite backups of the production PostgreSQL database, each
restored and checked before it counts. One script, `pgbackup.py`, run by two
Railway cron services built from `Dockerfile`.

## Design

Railway's own backups and point-in-time recovery are not enabled for this
project, and a Railway bucket would not be independent of Railway. Backups
therefore go to an S3-compatible bucket at another provider.

Each `backup` run:

1. Takes a PostgreSQL advisory lock, so overlapping runs exit immediately
   (`stage=lock`). It reads only; nothing in the database is changed.
2. Opens a read-only `REPEATABLE READ` transaction, exports its snapshot,
   counts the rows of every table, and runs `pg_dump --format=custom
   --snapshot=...`, so the counts and the dump see exactly the same data.
3. Checks the archive with `pg_restore --list`.
4. Starts a throwaway PostgreSQL 18 server in the container, restores the
   archive into it, and requires the same tables, row counts, and Alembic
   revision. The server and its files are deleted afterwards.
5. Encrypts the archive with [age](https://age-encryption.org) to the public
   key(s) in `BACKUP_AGE_RECIPIENTS`. The private key is never in Railway.
6. Uploads it with `If-None-Match: *` (create-only), checks its stored size,
   downloads it again, and compares its SHA-256.
7. Writes a manifest next to it: timestamps, sizes, both SHA-256 hashes, the
   server version, the Alembic revision, and per-table row counts (counts
   only, never row data).

A backup counts only once its manifest exists. Any failure exits non-zero,
prints one sanitized `HOSTED-BACKUP status=failed stage=...` line, emails the
operator, and leaves no manifest. Temporary files are always removed.

Object keys are unique and timestamped:
`findsooner/postgres/YYYY/MM/findsooner-YYYYMMDDTHHMMSSZ-<random>.dump.age`
and the matching `.manifest.json`.

## Storage

Any S3-compatible provider works. Recommended, both free at this size (about
150 MB of data; compressed, encrypted daily archives of a few tens of MB; 35
retained is roughly 1–2 GB):

| Provider | Free tier | Beyond it | Deletion protection |
|---|---|---|---|
| Backblaze B2 | 10 GB stored | about $6/TB-month | application key without `deleteFiles`; versioning keeps any overwrite |
| Cloudflare R2 | 10 GB stored, no egress fees | about $15/TB-month | bucket lock rule (retention) |

Set up, for either:

1. A **private** bucket (no public access) with encryption at rest (default on
   both).
2. A lifecycle rule deleting objects under `findsooner/postgres/` after **35
   days**. Retention is the provider's job; the backup writer never deletes.
3. A credential scoped to that one bucket that can list, read, and write, but
   **not delete** (B2: `listFiles,readFiles,writeFiles`; R2: also add a bucket
   lock rule of at least 30 days so objects cannot be removed early).

## Encryption keys

Generate a key pair once, on a trusted machine:

```bash
age-keygen -o findsooner-backup-key.txt   # prints the public key: age1...
```

- Put only the **public** key in Railway (`BACKUP_AGE_RECIPIENTS`).
- Store the **private key file** in your password manager and in one offline
  copy (for example printed or on an encrypted USB drive). It must survive the
  loss of Railway, GitHub, and your laptop. Without it no backup can be read.
- More than one recipient is allowed (comma-separated), for example a second
  key held by someone you trust. Each one can decrypt alone.
- To rotate, add the new public key, wait for 35 days of backups, then remove
  the old one. Older archives stay readable only with the key they were made
  with, so keep old private keys until their backups expire.

## Railway configuration

Two cron services from this repository, each with **Dockerfile path**
`ops/backup/Dockerfile`, Wait for CI on, and resource limits of 1 vCPU and
1 GB of memory:

| Service | Schedule (UTC) | Start command |
|---|---|---|
| `postgres-backup` | `45 3 * * *` | `python3 -m ops.backup.pgbackup backup` |
| `backup-monitor` | `30 */6 * * *` | `python3 -m ops.backup.pgbackup monitor` |

03:45 UTC falls between the two-hourly collections (`17 */2 * * *`, which
finish by about :45 past even hours) and outside US daytime. The dump takes
seconds to a minute for this database, and the advisory lock prevents
overlap even if Railway starts a run twice.

Variables (never commit them):

| Variable | Service | Value |
|---|---|---|
| `BACKUP_DATABASE_URL` | backup | `${{Postgres.DATABASE_URL}}` (private network) |
| `BACKUP_S3_ENDPOINT` | both | `https://...` provider S3 endpoint |
| `BACKUP_S3_REGION` | both | e.g. `us-west-004` (B2) or `auto` (R2) |
| `BACKUP_S3_BUCKET` | both | bucket name |
| `BACKUP_S3_ACCESS_KEY_ID`, `BACKUP_S3_SECRET_ACCESS_KEY` | both | restricted key |
| `BACKUP_S3_PREFIX` | optional | default `findsooner/postgres` |
| `BACKUP_AGE_RECIPIENTS` | backup | `age1...` public key(s) |
| `BACKUP_ALERT_EMAIL` | both | where alerts go |
| `HOSTED_RESEND_API_KEY`, `HOSTED_RESEND_FROM_EMAIL` | both | the existing Resend sender |
| `BACKUP_HEALTHCHECK_URL` | backup, optional | e.g. a healthchecks.io ping URL |

## Monitoring and alerts

`backup-monitor` runs every 6 hours, independently of the backup job, and
prints one line:

```text
HOSTED-BACKUP-STATUS status=ok last_success=... age_hours=... size_bytes=...
upload_verified=true restore_verified=true retained=... unverified_newer=0
```

It exits non-zero and emails `BACKUP_ALERT_EMAIL` when there is no verified
backup newer than 36 hours, when the newest one is unverified, or when it
cannot read the bucket. It alerts in the first run of each day-long period of
an outage, so an unresolved problem is repeated daily rather than every run.
`unverified_newer` counts objects uploaded after the newest manifest, which
means a run failed after uploading. The backup job also emails on its own
failure.

Both run inside Railway, so an outage of Railway itself would silence them.
For alerts that survive that, create a free healthchecks.io check (daily
period, a few hours' grace) and set `BACKUP_HEALTHCHECK_URL`: the backup pings
it on success and `/fail` on failure, and healthchecks.io alerts when pings
stop.

## Restore

Restore only into a new, empty database. `restore` refuses a target that
matches `DATABASE_URL`, `HOSTED_DATABASE_URL`, or `BACKUP_DATABASE_URL`, that
has any tables, or whose name is not repeated in `--confirm-target`. It checks
both hashes, restores in one transaction, and compares every table's row count
and the Alembic revision with the manifest.

Requirements: PostgreSQL 18 client tools, `age`, Python 3.11+ with `psycopg`
(for example `pip install -r requirements.txt`), and the `BACKUP_S3_*`
variables in the environment.

```bash
PG_BIN=/usr/lib/postgresql/18/bin \
python -m ops.backup.pgbackup restore \
  --identity findsooner-backup-key.txt \
  --target "postgresql://user:pass@host:5432/findsooner_restore" \
  --confirm-target findsooner_restore
# optional: --manifest <key> for an older backup; default is the newest
```

Without this repository, the same can be done by hand: download the newest
`*.dump.age` and its manifest from the bucket, check the hash, then
`age -d -i findsooner-backup-key.txt -o backup.dump backup.dump.age` and
`pg_restore --no-owner --no-privileges --exit-on-error -d <empty db> backup.dump`.

### Periodic restore drill

Every backup is already restored in the job before it counts, and every upload
is re-downloaded and hash-checked. The one step the job cannot test is
decryption, because it never has the private key. Once a month, and after
rotating keys, run the restore command above into a scratch database, then
drop it.

## Disaster recovery

If Railway's database is lost or corrupted:

1. Create a new PostgreSQL 18 instance (Railway or elsewhere).
2. Retrieve the private key and run `restore` into its empty database. It
   verifies the data against the manifest.
3. Point `DATABASE_URL` of `Internship-Signal`, `hosted-collection`, and
   `hosted-notifications` at the new database and redeploy. Their pre-deploy
   `alembic upgrade head` is a no-op on a restored database at the current
   revision.
4. Check `/api/health`, the next collection's `HOSTED-AUDIT` line, and the
   notification worker logs.

If Railway itself is unavailable, the bucket and the private key are all that
is needed; nothing in the backup depends on Railway.

**Recovery point:** up to about 24 hours. Everything written after the last
backup (new accounts, preference changes, matches, and notification history)
is lost. Jobs come back on the next collection.

**Recovery time:** tens of minutes for this database size, dominated by
provisioning and reconfiguring services, not the restore itself.

**Not covered:** changes between backups; anything other than this database
(service variables, Railway configuration, the collection cache volume);
loss of every copy of the private key; and a credential with delete rights
being misused. The last is why the writer has no delete permission and
retention is enforced by the provider.
