import base64
import io
from unittest.mock import MagicMock, call

import pytest
from escpos.constants import QR_ECLEVEL_M
from PIL import Image

from printapi.render import RenderError, render
from printapi.validation import validate_document


def normalized(*blocks):
    return validate_document({"blocks": list(blocks)})


@pytest.fixture
def p():
    return MagicMock()


def test_text_sets_style_then_resets(p):
    render(p, normalized({"type": "text", "content": "hi", "bold": True, "align": "center", "width": 2, "height": 2}))
    assert p.method_calls == [
        call.set(
            align="center", font="a", bold=True, underline=0, invert=False,
            custom_size=True, width=2, height=2,
        ),
        call.textln("hi"),
        call.set_with_default(),
    ]


def test_text_without_newline(p):
    render(p, normalized({"type": "text", "content": "hi", "newline": False}))
    p.text.assert_called_once_with("hi")
    p.textln.assert_not_called()


def test_feed_and_cut(p):
    render(p, normalized({"type": "feed", "lines": 3}, {"type": "cut", "mode": "partial"}))
    p.ln.assert_called_once_with(3)
    p.cut.assert_called_once_with(mode="PART")


def test_qr_native_with_ec(p):
    render(p, normalized({"type": "qr", "data": "https://example.com"}))
    p.set.assert_called_once_with(align="center")
    p.qr.assert_called_once_with("https://example.com", ec=QR_ECLEVEL_M, size=6, native=True)
    p.set_with_default.assert_called_once()


def test_code128_gets_codeset_prefix(p):
    render(p, normalized({"type": "barcode", "data": "HELLO"}))
    args, kwargs = p.barcode.call_args
    assert args == ("{BHELLO", "CODE128")
    assert kwargs == {"height": 64, "width": 3, "pos": "BELOW", "align_ct": False}


def test_ean13_data_untouched(p):
    render(p, normalized({"type": "barcode", "data": "4006381333931", "symbology": "EAN13", "text_position": "none"}))
    args, kwargs = p.barcode.call_args
    assert args == ("4006381333931", "EAN13")
    assert kwargs["pos"] == "OFF"


def test_drawer_and_beep(p):
    render(p, normalized({"type": "drawer", "pin": 5}, {"type": "beep", "times": 2, "duration": 4}))
    p.cashdraw.assert_called_once_with(5)
    p.buzzer.assert_called_once_with(2, 4)


def _png_b64(width, height, mode="RGB", color=(128, 128, 128)):
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_image_shrunk_to_paper_width(p):
    render(p, normalized({"type": "image", "data": _png_b64(1024, 100), "align": "left"}), paper_width_dots=512)
    (img,), kwargs = p.image.call_args
    assert img.mode == "1"
    assert img.width == 512
    assert img.height == 50
    assert kwargs == {"impl": "bitImageRaster"}


def test_small_image_not_upscaled_but_centered_on_canvas(p):
    render(p, normalized({"type": "image", "data": _png_b64(100, 40)}), paper_width_dots=512)
    (img,), _ = p.image.call_args
    assert img.width == 512  # centered by pasting onto a full-width canvas
    assert img.height == 40


def test_small_image_left_aligned_keeps_size(p):
    render(p, normalized({"type": "image", "data": _png_b64(100, 40), "align": "left"}), paper_width_dots=512)
    (img,), _ = p.image.call_args
    assert img.size == (100, 40)


def test_image_explicit_width(p):
    render(p, normalized({"type": "image", "data": _png_b64(400, 200), "width": 200, "align": "left"}))
    (img,), _ = p.image.call_args
    assert img.size == (200, 100)


def test_transparent_image_flattened_to_white(p):
    data = _png_b64(10, 10, mode="RGBA", color=(0, 0, 0, 0))
    render(p, normalized({"type": "image", "data": data, "align": "left", "dither": False}))
    (img,), _ = p.image.call_args
    assert img.convert("L").getextrema() == (255, 255)  # fully transparent -> white, nothing printed


def test_invalid_image_data_raises_render_error(p):
    with pytest.raises(RenderError):
        render(p, normalized({"type": "image", "data": "bm90IGFuIGltYWdl"}))
    with pytest.raises(RenderError):
        render(p, normalized({"type": "image", "data": "!!! not base64 !!!"}))


def test_dummy_printer_produces_escpos_bytes():
    from escpos.printer import Dummy

    p = Dummy(profile="TM-T88V")
    render(p, normalized(
        {"type": "text", "content": "hello"},
        {"type": "cut", "mode": "full"},
    ))
    assert b"hello" in p.output
    assert b"\x1dV" in p.output  # GS V = paper cut
