import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from macro_score import b3_instruments
from macro_score.b3_instruments import import_instrument_files, parse_instrument_file

XML = b'''<Document xmlns="urn:bvmf.052.01.xsd" xmlns:h="urn:iso:std:iso:20022:tech:xsd:head.001.001.01">
<BizFileHdr><Xchg><BizGrpDesc/></Xchg></BizFileHdr><BizGrp><h:AppHdr><h:BizMsgIdr>BV-ID-1</h:BizMsgIdr><h:MsgDefIdr>bvmf.100.02</h:MsgDefIdr></h:AppHdr>
<Document xmlns="urn:bvmf.100.02.xsd"><Instrm><RptParams><RptDtAndTm><Dt>2026-10-19</Dt></RptDtAndTm><FinInstrmId><OthrId><Id>200001113994</Id></OthrId></FinInstrmId></RptParams><InstrmInf><FutrCtrctsInf><TckrSymb>DI1J28</TckrSymb><ISIN>BRBMEFD1I7S8</ISIN><XprtnDt>2028-04-03</XprtnDt><TradgStartDt>2023-04-03</TradgStartDt><TradgEndDt>2028-03-31</TradgEndDt></FutrCtrctsInf></InstrmInf></Instrm></Document></BizGrp></Document>'''


def test_stream_parser_extracts_contract_fields_and_header(tmp_path: Path) -> None:
    source = tmp_path / "catalog.xml"
    source.write_bytes(XML)
    rows, report = parse_instrument_file(source, captured_at=datetime(2026, 10, 20, tzinfo=timezone.utc))
    assert report["message_definition"] == "bvmf.100.02"
    assert report["DI1_count"] == 1
    assert rows[0]["report_date"] == "2026-10-19"
    assert rows[0]["fin_instrm_id"] == "200001113994"
    assert rows[0]["security_id"] == "BRBMEFD1I7S8"
    assert rows[0]["maturity"] == "2028-04-03"
    assert rows[0]["trading_end"] == "2028-03-31"
    assert rows[0]["maturity"] != rows[0]["trading_end"]
    assert rows[0]["message_id"] == "BV-ID-1"


def test_stream_parser_accepts_observed_2025_instrument_identifier_path(tmp_path: Path) -> None:
    source = tmp_path / "catalog-2025.xml"
    source.write_bytes(XML.replace(
        b"<RptParams><RptDtAndTm><Dt>2026-10-19</Dt></RptDtAndTm><FinInstrmId>",
        b"<RptParams><RptDtAndTm><Dt>2025-08-29</Dt></RptDtAndTm></RptParams><FinInstrmId>",
    ).replace(
        b"</OthrId></FinInstrmId></RptParams><InstrmInf>",
        b"</OthrId></FinInstrmId><InstrmInf>",
    ).replace(b"2028-04-03", b"2025-09-01").replace(
        b"2023-04-03", b"2024-09-02",
    ).replace(b"2028-03-31", b"2025-08-29"))
    rows, _report = parse_instrument_file(source)
    assert rows[0]["report_date"] == "2025-08-29"
    assert rows[0]["fin_instrm_id"] == "200001113994"
    assert rows[0]["maturity"] == "2025-09-01"


def test_dry_run_and_append_only_snapshot_import(tmp_path: Path) -> None:
    source = tmp_path / "catalog.xml"
    source.write_bytes(XML)
    target = tmp_path / "isolated.db"
    assert import_instrument_files(target, [source])["records"] == 1
    assert not target.exists()
    first = import_instrument_files(target, [source], execute=True)
    second = import_instrument_files(target, [source], execute=True)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    with sqlite3.connect(target) as connection:
        rows = connection.execute(
            "SELECT source_basis,message_definition,maturity,security_id,fin_instrm_id "
            "FROM b3_di_instrument_snapshots"
        ).fetchall()
    assert rows == [("b3_instrument_snapshot_v1", "bvmf.100.02", "2028-04-03", "BRBMEFD1I7S8", "200001113994")]


def test_rejects_dtd_even_when_declaration_splits_across_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "unsafe.xml"
    source.write_bytes(b'<!DOCTYPE x><x/>')
    monkeypatch.setattr(b3_instruments, "HASH_CHUNK_BYTES", 7)
    with pytest.raises(ValueError, match="DTD/entity"):
        parse_instrument_file(source)
    source.write_bytes('<!DOCTYPE x><x/>'.encode('utf-16'))
    with pytest.raises(ValueError, match="DTD/entity"):
        parse_instrument_file(source)


def test_rejects_price_message_instrument_input(tmp_path: Path) -> None:
    source = tmp_path / "price.xml"
    source.write_bytes(b'<Document xmlns="urn:bvmf.217.01.xsd"><PricRpt/></Document>')
    with pytest.raises(ValueError, match="unexpected instrument message"):
        parse_instrument_file(source)
