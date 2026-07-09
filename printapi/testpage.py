"""Canned test receipt for POST /print/test — exercises every block type."""

import base64
import io

from PIL import Image, ImageDraw


def _logo_b64():
    img = Image.new("1", (384, 96), 1)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 383, 95], outline=0, width=4)
    for x in range(16, 368, 32):
        draw.rectangle([x, 16, x + 16, 80], fill=0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_blocks():
    return [
        {"type": "text", "content": "EPSON RP API", "align": "center", "bold": True, "width": 2, "height": 2},
        {"type": "text", "content": "test page", "align": "center"},
        {"type": "feed", "lines": 1},
        {"type": "text", "content": "left aligned"},
        {"type": "text", "content": "center aligned", "align": "center"},
        {"type": "text", "content": "right aligned", "align": "right"},
        {"type": "text", "content": "bold", "bold": True},
        {"type": "text", "content": "underline", "underline": 1},
        {"type": "text", "content": "inverted", "invert": True},
        {"type": "text", "content": "font b", "font": "b"},
        {"type": "text", "content": "double wide", "width": 2},
        {"type": "feed", "lines": 1},
        {"type": "barcode", "data": "TEST-1234", "symbology": "CODE128"},
        {"type": "feed", "lines": 1},
        {"type": "qr", "data": "https://github.com/python-escpos/python-escpos"},
        {"type": "feed", "lines": 1},
        {"type": "image", "data": _logo_b64()},
        {"type": "feed", "lines": 2},
        {"type": "beep", "times": 1, "duration": 2},
        {"type": "cut", "mode": "full"},
    ]
