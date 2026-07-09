"""Translate normalized block documents into python-escpos calls.

render() takes any Escpos-shaped object, so tests can pass a mock or an
escpos Dummy printer. Blocks must already be normalized by validation.py
(every field present).
"""

import base64
import binascii
import io

from escpos.constants import QR_ECLEVEL_H, QR_ECLEVEL_L, QR_ECLEVEL_M, QR_ECLEVEL_Q
from PIL import Image

QR_EC_MAP = {"L": QR_ECLEVEL_L, "M": QR_ECLEVEL_M, "Q": QR_ECLEVEL_Q, "H": QR_ECLEVEL_H}
BARCODE_POS_MAP = {"none": "OFF", "above": "ABOVE", "below": "BELOW", "both": "BOTH"}


class RenderError(Exception):
    """A block that passed validation still can't be rendered (e.g. bad image data)."""


def render(printer, blocks, paper_width_dots=512):
    for block in blocks:
        _RENDERERS[block["type"]](printer, block, paper_width_dots)


def _render_text(p, block, _width):
    p.set(
        align=block["align"],
        font=block["font"],
        bold=block["bold"],
        underline=block["underline"],
        invert=block["invert"],
        custom_size=True,
        width=block["width"],
        height=block["height"],
    )
    if block["newline"]:
        p.textln(block["content"])
    else:
        p.text(block["content"])
    p.set_with_default()


def _render_feed(p, block, _width):
    p.ln(block["lines"])


def _render_cut(p, block, _width):
    p.cut(mode="FULL" if block["mode"] == "full" else "PART")


def _render_barcode(p, block, _width):
    data = block["data"]
    # Epson native CODE128 needs an explicit code-set selector prefix.
    if block["symbology"] == "CODE128" and not data.startswith("{"):
        data = "{B" + data
    p.set(align=block["align"])
    p.barcode(
        data,
        block["symbology"],
        height=block["height"],
        width=block["width"],
        pos=BARCODE_POS_MAP[block["text_position"]],
        align_ct=False,
    )
    p.set_with_default()


def _render_qr(p, block, _width):
    p.set(align=block["align"])
    p.qr(block["data"], ec=QR_EC_MAP[block["ec"]], size=block["size"], native=True)
    p.set_with_default()


def _render_image(p, block, paper_width_dots):
    img = _prepare_image(block, paper_width_dots)
    p.image(img, impl="bitImageRaster")


def _prepare_image(block, paper_width_dots):
    try:
        raw = base64.b64decode(block["data"], validate=True)
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (binascii.Error, OSError) as e:
        raise RenderError(f"invalid image data: {e}") from e

    # Flatten transparency onto white before thresholding.
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        background = Image.new("RGBA", img.size, (255, 255, 255, 255))
        background.alpha_composite(img.convert("RGBA"))
        img = background.convert("RGB")

    target = block["width"] or min(img.width, paper_width_dots)
    target = min(target, paper_width_dots)
    if target != img.width:
        img = img.resize(
            (target, max(1, round(img.height * target / img.width))), Image.LANCZOS
        )

    if block["dither"]:
        img = img.convert("1")  # Floyd-Steinberg
    else:
        img = img.convert("L").point(lambda px: 255 if px > 127 else 0, mode="1")

    if block["align"] != "left" and img.width < paper_width_dots:
        canvas = Image.new("1", (paper_width_dots, img.height), 1)
        x = (paper_width_dots - img.width) if block["align"] == "right" else (paper_width_dots - img.width) // 2
        canvas.paste(img, (x, 0))
        img = canvas
    return img


def _render_drawer(p, block, _width):
    p.cashdraw(block["pin"])


def _render_beep(p, block, _width):
    p.buzzer(block["times"], block["duration"])


_RENDERERS = {
    "text": _render_text,
    "feed": _render_feed,
    "cut": _render_cut,
    "barcode": _render_barcode,
    "qr": _render_qr,
    "image": _render_image,
    "drawer": _render_drawer,
    "beep": _render_beep,
}
