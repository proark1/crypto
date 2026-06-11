"""Scheduled database backups to S3-compatible object storage (§7, §7.1).

Railway's Postgres is managed but the project itself is a single point of
failure; trade history and state must survive losing it (ARCHITECTURE.md
technology choices). The dump is a gzipped JSONL logical export of every
table — portable, diffable, and restorable through SQLAlchemy without any
``pg_dump`` binary in the image. Uploads use hand-rolled SigV4 so the only
dependency is httpx; R2, S3, and B2 all speak it.

The scheduler lives beside trading, never in it: a failed backup is a loud
log line and a retry next interval, not a stopped bot.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from tradebot.core.logging import log_event
from tradebot.persistence.database import Database, metadata

logger = logging.getLogger(__name__)

BACKUP_FORMAT_VERSION = 1


async def dump_tables(database: Database) -> bytes:
    """Export every table as gzipped JSONL (header line + one line per row).

    Decimals and datetimes are stringified; the restore side rebuilds them
    from the column types, so the round trip is exact — a backup that
    silently floats money would be worse than none.
    """
    lines = [
        json.dumps(
            {
                "backup_version": BACKUP_FORMAT_VERSION,
                "created_at": datetime.now(tz=UTC).isoformat(),
                "tables": [table.name for table in metadata.sorted_tables],
            }
        )
    ]
    async with database.engine.connect() as connection:
        for table in metadata.sorted_tables:
            rows = (await connection.execute(select(table))).mappings().all()
            for row in rows:
                lines.append(json.dumps({"table": table.name, "row": dict(row)}, default=str))
    return gzip.compress(("\n".join(lines) + "\n").encode())


async def restore_tables(database: Database, archive: bytes) -> dict[str, int]:
    """Insert an archive's rows into an empty schema; returns rows per table.

    Restore targets a *fresh* database (disaster recovery), so it inserts
    plainly — restoring over live data would create duplicates, and that
    must fail on primary keys rather than quietly merge.
    """
    lines = gzip.decompress(archive).decode().splitlines()
    header = json.loads(lines[0])
    if header.get("backup_version") != BACKUP_FORMAT_VERSION:
        raise ValueError(f"unsupported backup version: {header.get('backup_version')!r}")
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for line in lines[1:]:
        record = json.loads(line)
        rows_by_table.setdefault(record["table"], []).append(record["row"])

    counts: dict[str, int] = {}
    async with database.engine.begin() as connection:
        # sorted_tables is FK-dependency order, so parents land first.
        for table in metadata.sorted_tables:
            rows = rows_by_table.get(table.name, [])
            if not rows:
                continue
            typed_rows = [_coerce_row(table, row) for row in rows]
            await connection.execute(table.insert(), typed_rows)
            counts[table.name] = len(rows)
    return counts


def _coerce_row(table: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild Decimal/datetime values that JSON flattened to strings."""
    coerced: dict[str, Any] = {}
    for column in table.columns:
        value = row.get(column.name)
        if value is None:
            coerced[column.name] = None
            continue
        type_name = type(column.type).__name__
        if type_name == "Numeric" and isinstance(value, str | int | float):
            # Backups stringified exact Decimals; rebuild them exactly.
            coerced[column.name] = Decimal(str(value))
        elif type_name == "DateTime" and isinstance(value, str):
            coerced[column.name] = datetime.fromisoformat(value)
        else:
            coerced[column.name] = value
    return coerced


class S3Config(BaseModel):
    """Where backups go; any S3-compatible store (R2, S3, B2) works."""

    model_config = ConfigDict(frozen=True)

    endpoint: str
    """Base URL, e.g. ``https://<account>.r2.cloudflarestorage.com``."""

    bucket: str
    access_key: str
    secret_key: str
    region: str = "auto"
    """R2 uses the literal region ``auto``; AWS wants a real one."""


def sigv4_headers(
    *,
    method: str,
    host: str,
    path: str,
    payload_hash: str,
    moment: datetime,
    region: str,
    access_key: str,
    secret_key: str,
) -> dict[str, str]:
    """Build SigV4 auth headers for one S3 request.

    Pure and deterministic so it can be pinned against AWS's published
    signature test vector — a subtly wrong signer would otherwise fail
    only in production, against the real store.
    """
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    datestamp = moment.strftime("%Y%m%d")
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, quote(path), "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    def hmac_sha256(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    date_key = hmac_sha256(f"AWS4{secret_key}".encode(), datestamp)
    region_key = hmac_sha256(date_key, region)
    service_key = hmac_sha256(region_key, "s3")
    signing_key = hmac_sha256(service_key, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }


class S3Uploader:
    """PUTs objects into the configured bucket; raises on any failure."""

    def __init__(self, config: S3Config, client: httpx.AsyncClient) -> None:
        """``client`` is owned by the caller (worker shutdown closes it)."""
        self._config = config
        self._client = client

    async def upload(self, key: str, body: bytes, moment: datetime) -> None:
        """Upload ``body`` as ``key``; HTTP errors raise to the scheduler."""
        host = httpx.URL(self._config.endpoint).host
        path = f"/{self._config.bucket}/{key}"
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = sigv4_headers(
            method="PUT",
            host=host,
            path=path,
            payload_hash=payload_hash,
            moment=moment,
            region=self._config.region,
            access_key=self._config.access_key,
            secret_key=self._config.secret_key,
        )
        response = await self._client.put(
            f"{self._config.endpoint}{path}", content=body, headers=headers
        )
        response.raise_for_status()


async def run_backup(database: Database, uploader: S3Uploader, prefix: str) -> str:
    """Dump and upload one backup; returns the object key."""
    moment = datetime.now(tz=UTC)
    archive = await dump_tables(database)
    key = f"{prefix}/{moment.strftime('%Y%m%dT%H%M%SZ')}.jsonl.gz"
    await uploader.upload(key, archive, moment)
    log_event(
        logger,
        logging.INFO,
        "backup_uploaded",
        key=key,
        compressed_bytes=len(archive),
    )
    return key
