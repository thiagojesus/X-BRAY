"""Safe streaming reader and append-only ledger for B3 DI1 instrument snapshots."""

from __future__ import annotations

import hashlib
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterable
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

SOURCE_BASIS = "b3_instrument_snapshot_v1"
INSTRUMENT_NS = "urn:bvmf.100.02.xsd"
ENVELOPE_NS = "urn:bvmf.052.01.xsd"
MAX_XML_BYTES = 1_000_000_000
HASH_CHUNK_BYTES = 4 * 1024 * 1024


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _declaration_check(path: Path) -> str:
    """Hash in bounded chunks and reject DTD/entity declarations before parsing."""
    digest = hashlib.sha256()
    carry = b""
    markers = [token.encode(encoding) for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
               for token in ("<!DOCTYPE", "<!ENTITY")]
    with path.open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            upper = (carry + chunk).upper()
            if any(marker in upper for marker in markers):
                raise ValueError(f"DTD/entity declarations are not accepted: {path.name}")
            digest.update(chunk)
            carry = upper[-64:]
    return digest.hexdigest()


def _field_values(element: ET.Element) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = defaultdict(list)
    for node in element.iter():
        text = (node.text or "").strip()
        if text:
            fields[_local(node.tag)].append(text)
    return fields


def _text_at_path(element: ET.Element, path: tuple[str, ...]) -> str | None:
    if not path:
        text = (element.text or "").strip()
        return text or None
    for child in element:
        if _local(child.tag) == path[0]:
            value = _text_at_path(child, path[1:])
            if value is not None:
                return value
    return None


def _instrument_row(
    element: ET.Element, *, source: Path, digest: str, captured: datetime,
    message_definition: str, message_id: str,
) -> tuple[dict[str, str] | None, str | None]:
    fields = _field_values(element)
    symbols = fields.get("TckrSymb", [])
    if not symbols or not symbols[0].startswith("DI1"):
        return None, None
    futures = next((node for node in element.iter() if _local(node.tag) == "FutrCtrctsInf"), None)
    if futures is None:
        return None, "incomplete_DI1_records"
    contract = _field_values(futures)
    symbol = next(iter(contract.get("TckrSymb", [])), "")
    security_id = next(iter(contract.get("ISIN", [])), "")
    fin_instrm_id = (
        _text_at_path(element, ("FinInstrmId", "OthrId", "Id"))
        or _text_at_path(element, ("RptParams", "FinInstrmId", "OthrId", "Id"))
        or ""
    )
    expiry = next(iter(contract.get("XprtnDt", [])), "")
    report_date = _text_at_path(element, ("RptParams", "RptDtAndTm", "Dt")) or ""
    start = next(iter(contract.get("TradgStartDt", [])), "")
    end = next(iter(contract.get("TradgEndDt", [])), "")
    if not all((symbol.startswith("DI1"), security_id, fin_instrm_id,
                message_definition == "bvmf.100.02", message_id, report_date, expiry)):
        return None, "incomplete_DI1_records"
    try:
        date.fromisoformat(report_date)
        date.fromisoformat(expiry)
        if start:
            date.fromisoformat(start)
        if end:
            date.fromisoformat(end)
    except ValueError:
        return None, "invalid_date_rows"
    captured_text = captured.isoformat()
    return {
        "report_date": report_date, "symbol": symbol, "security_id": security_id,
        "fin_instrm_id": fin_instrm_id, "maturity": expiry,
        "trading_start": start, "trading_end": end, "source_hash": digest,
        "source_name": source.name, "message_definition": message_definition,
        "message_id": message_id, "captured_at": captured_text,
        "available_at": captured_text, "source_basis": SOURCE_BASIS,
    }, "DI1"


def parse_instrument_file(path: Path, *, captured_at: datetime | None = None) -> tuple[list[dict[str, str]], dict[str, object]]:
    """Read DI1 future instruments; ignores price reports and non-DI1 instruments."""
    source = Path(path)
    size = source.stat().st_size
    if size <= 0 or size > MAX_XML_BYTES:
        raise ValueError(f"instrument XML outside bounded size limits: {source.name}")
    digest = _declaration_check(source)
    captured = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    message_definition = ""
    message_id = ""
    stack: list[ET.Element] = []
    with source.open("rb") as stream:
        try:
            for event, element in ET.iterparse(stream, events=("start", "end")):
                local = _local(element.tag)
                if event == "start":
                    stack.append(element)
                    continue
                if local == "MsgDefIdr" and element.text:
                    message_definition = element.text.strip()
                elif local == "BizMsgIdr" and element.text:
                    message_id = element.text.strip()
                if local == "Instrm" and element.tag.startswith("{" + INSTRUMENT_NS + "}"):
                    counts["instrument_records"] += 1
                    row, outcome = _instrument_row(
                        element, source=source, digest=digest, captured=captured,
                        message_definition=message_definition, message_id=message_id,
                    )
                    if row is not None:
                        rows.append(row)
                    if outcome:
                        counts[outcome] += 1
                    element.clear()
                elif local == "BizGrp" and element.tag.startswith("{" + ENVELOPE_NS + "}"):
                    element.clear()
                stack.pop()
        except ET.ParseError as error:
            raise ValueError(f"malformed instrument XML: {source.name}") from error
    if message_definition != "bvmf.100.02":
        raise ValueError(f"unexpected instrument message definition: {source.name}")
    return rows, {
        "file": str(source), "size_bytes": size, "sha256": digest,
        "message_definition": message_definition, "instrument_counts": dict(counts),
        "DI1_count": len(rows), "DI1_symbols": sorted({row["symbol"] for row in rows}),
    }


def import_instrument_files(
    database: Path, paths: Iterable[Path], *, execute: bool = False,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Audit by default; explicit execution appends validated snapshots to isolated SQLite."""
    from macro_score.retrospective_di import validate_retrospective_target

    target = Path(database)
    validate_retrospective_target(target)
    captured = captured_at or datetime.now(timezone.utc)
    if captured.tzinfo is None or captured.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")
    snapshots: list[dict[str, str]] = []
    reports: list[dict[str, object]] = []
    for path in sorted((Path(item) for item in paths), key=lambda item: str(item)):
        parsed, report = parse_instrument_file(path, captured_at=captured)
        snapshots.extend(parsed)
        reports.append(report)
    by_identity: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in snapshots:
        by_identity[(row["report_date"], row["symbol"], row["security_id"])].add(row["maturity"])
    conflicts = [(*key, sorted(maturities)) for key, maturities in by_identity.items() if len(maturities) > 1]
    result: dict[str, object] = {
        "dry_run": not execute, "source_basis": SOURCE_BASIS, "captured_at": captured.isoformat(),
        "files": reports, "records": len(snapshots),
        "unique_identity_date": len(by_identity), "conflicts": conflicts, "inserted": 0,
    }
    if execute:
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(target)) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS b3_di_instrument_snapshots (
                snapshot_date TEXT NOT NULL, report_date TEXT NOT NULL, symbol TEXT NOT NULL,
                security_id TEXT NOT NULL, maturity TEXT NOT NULL, trading_start TEXT, trading_end TEXT,
                fin_instrm_id TEXT NOT NULL,
                source_hash TEXT NOT NULL, source_name TEXT NOT NULL, message_definition TEXT NOT NULL,
                message_id TEXT NOT NULL, captured_at TEXT NOT NULL, available_at TEXT NOT NULL,
                source_basis TEXT NOT NULL CHECK(source_basis='b3_instrument_snapshot_v1'),
                PRIMARY KEY (source_hash,symbol,security_id,maturity))""")
            snapshot_columns = {str(row[1]) for row in connection.execute(
                "PRAGMA table_info(b3_di_instrument_snapshots)"
            ).fetchall()}
            if "fin_instrm_id" not in snapshot_columns:
                connection.execute(
                    "ALTER TABLE b3_di_instrument_snapshots ADD COLUMN fin_instrm_id TEXT NOT NULL DEFAULT ''"
                )
            before = connection.total_changes
            connection.executemany("""INSERT OR IGNORE INTO b3_di_instrument_snapshots
                (snapshot_date,report_date,symbol,security_id,fin_instrm_id,maturity,trading_start,trading_end,
                 source_hash,source_name,message_definition,message_id,captured_at,available_at,source_basis)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [(
                    row["report_date"], row["report_date"], row["symbol"], row["security_id"], row["fin_instrm_id"],
                    row["maturity"], row["trading_start"], row["trading_end"], row["source_hash"],
                    row["source_name"], row["message_definition"], row["message_id"],
                    captured.astimezone(timezone.utc).isoformat(), row["available_at"], SOURCE_BASIS,
                ) for row in snapshots])
            result["inserted"] = connection.total_changes - before
            # Upgrade only rows created by the pre-release importer that lacked this newly parsed identity.
            connection.executemany(
                "UPDATE b3_di_instrument_snapshots SET fin_instrm_id=? "
                "WHERE source_hash=? AND symbol=? AND security_id=? AND maturity=? AND fin_instrm_id=''",
                [(row["fin_instrm_id"], row["source_hash"], row["symbol"], row["security_id"], row["maturity"])
                 for row in snapshots],
            )
            connection.commit()
    return result
