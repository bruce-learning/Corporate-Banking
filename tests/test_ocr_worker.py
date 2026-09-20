from io import BytesIO

from pypdf import PdfWriter

from app.etl import parser
from app.etl.contracts import ParsedBlock
from app.etl.parser import DocumentParseError, parse_document


def blank_pdf() -> bytes:
    writer = PdfWriter(); writer.add_blank_page(width=200, height=200)
    stream = BytesIO(); writer.write(stream); return stream.getvalue()


def test_scanned_pdf_requires_worker_ocr(monkeypatch):
    data = blank_pdf()
    try:
        parse_document("scan.pdf", data)
        raise AssertionError("扫描PDF不应在同步预览中静默通过")
    except DocumentParseError as error:
        assert error.code == "ocr_required"
    monkeypatch.setattr(parser, "_ocr_pdf", lambda raw, pages: (
        [ParsedBlock(kind="ocr", text="合成OCR文本", location={"page": 1, "ocr": True})],
        ["扫描PDF已执行OCR，发布前必须人工核对识别结果"],
    ))
    parsed = parse_document("scan.pdf", data, allow_ocr=True)
    assert parsed.blocks[0].kind == "ocr" and parsed.parser_version == "etl-1.2"
