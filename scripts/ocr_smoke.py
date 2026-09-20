"""Generate an image-only PDF and verify the real Poppler/Tesseract OCR path."""
from __future__ import annotations

import sys
from pathlib import Path
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.etl.parser import parse_document


FONT = {
    "A":["01110","10001","10001","11111","10001","10001","10001"],
    "B":["11110","10001","10001","11110","10001","10001","11110"],
    "C":["01111","10000","10000","10000","10000","10000","01111"],
    "E":["11111","10000","10000","11110","10000","10000","11111"],
    "K":["10001","10010","10100","11000","10100","10010","10001"],
    "N":["10001","11001","11001","10101","10011","10011","10001"],
    "O":["01110","10001","10001","10001","10001","10001","01110"],
    "R":["11110","10001","10001","11110","10100","10010","10001"],
    "S":["01111","10000","10000","01110","00001","00001","11110"],
    "T":["11111","00100","00100","00100","00100","00100","00100"],
    "2":["01110","10001","00001","00010","00100","01000","11111"],
    "0":["01110","10001","10011","10101","11001","10001","01110"],
    "6":["00110","01000","10000","11110","10001","10001","01110"],
    " ":["00000"]*7,
}


def image() -> tuple[int, int, bytes]:
    text, scale, margin = "BANK OCR TEST 2026", 10, 30
    width = margin * 2 + len(text) * 6 * scale
    height = margin * 2 + 7 * scale
    pixels = bytearray([255]) * (width * height)
    for index, character in enumerate(text):
        glyph = FONT[character]
        for row, line in enumerate(glyph):
            for column, value in enumerate(line):
                if value == "1":
                    left, top = margin + index * 6 * scale + column * scale, margin + row * scale
                    for y in range(top, top + scale):
                        pixels[y * width + left:y * width + left + scale] = b"\x00" * scale
    return width, height, bytes(pixels)


def image_pdf() -> bytes:
    width, height, pixels = image()
    compressed = zlib.compress(pixels)
    content = f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>".encode(),
        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode()+compressed+b"\nendstream",
        f"<< /Length {len(content)} >>\nstream\n".encode()+content+b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(output)); output.extend(f"{number} 0 obj\n".encode()+body+b"\nendobj\n")
    start = len(output); output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(output)


def main():
    parsed = parse_document("synthetic-scan.pdf", image_pdf(), allow_ocr=True)
    assert parsed.blocks and parsed.blocks[0].kind == "ocr"
    assert any("OCR" in warning for warning in parsed.warnings)
    print({"ocr": "passed", "pages": len(parsed.blocks), "characters": parsed.character_count,
           "parser_version": parsed.parser_version})


if __name__ == "__main__":
    main()
