"""Validation and normalization of print documents.

validate_document() takes the raw JSON payload for POST /print and returns a
list of fully normalized blocks (every optional field filled with its
default), raising ValidationError with a JSON-path-style message otherwise.
Unknown block types and unknown fields are rejected so typos fail loudly
instead of silently no-oping.
"""

MAX_BLOCKS = 500

ALIGNMENTS = ("left", "center", "right")
BARCODE_SYMBOLOGIES = (
    "CODE39",
    "CODE93",
    "CODE128",
    "EAN13",
    "EAN8",
    "UPC-A",
    "UPC-E",
    "ITF",
    "NW7",
)
BARCODE_TEXT_POSITIONS = ("none", "above", "below", "both")
QR_EC_LEVELS = ("L", "M", "Q", "H")


class ValidationError(Exception):
    def __init__(self, path, message):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}" if path else message)


def _field_path(path, key):
    return f"{path}.{key}" if path else key


def _req_str(block, path, key, max_len=None):
    if key not in block:
        raise ValidationError(path, f"missing required field '{key}'")
    value = block[key]
    if not isinstance(value, str) or not value:
        raise ValidationError(_field_path(path, key), "must be a non-empty string")
    if max_len is not None and len(value) > max_len:
        raise ValidationError(_field_path(path, key), f"must be at most {max_len} characters")
    return value


def _opt_bool(block, path, key, default):
    value = block.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(_field_path(path, key), "must be a boolean")
    return value


def _opt_int(block, path, key, default, lo, hi):
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(_field_path(path, key), f"must be an integer between {lo} and {hi}")
    if not lo <= value <= hi:
        raise ValidationError(_field_path(path, key), f"must be an integer between {lo} and {hi}")
    return value


def _opt_enum(block, path, key, default, choices):
    value = block.get(key, default)
    if value not in choices:
        raise ValidationError(_field_path(path, key), f"must be one of {', '.join(map(str, choices))}")
    return value


def _reject_unknown_fields(block, path, allowed):
    unknown = set(block) - allowed
    if unknown:
        raise ValidationError(path, f"unknown field(s): {', '.join(sorted(unknown))}")


def _validate_text(block, path):
    _reject_unknown_fields(
        block,
        path,
        {"type", "content", "bold", "underline", "align", "width", "height", "invert", "font", "newline"},
    )
    return {
        "type": "text",
        "content": _req_str(block, path, "content"),
        "bold": _opt_bool(block, path, "bold", False),
        "underline": _opt_int(block, path, "underline", 0, 0, 2),
        "align": _opt_enum(block, path, "align", "left", ALIGNMENTS),
        "width": _opt_int(block, path, "width", 1, 1, 8),
        "height": _opt_int(block, path, "height", 1, 1, 8),
        "invert": _opt_bool(block, path, "invert", False),
        "font": _opt_enum(block, path, "font", "a", ("a", "b")),
        "newline": _opt_bool(block, path, "newline", True),
    }


def _validate_feed(block, path):
    _reject_unknown_fields(block, path, {"type", "lines"})
    return {"type": "feed", "lines": _opt_int(block, path, "lines", 1, 1, 20)}


def _validate_cut(block, path):
    _reject_unknown_fields(block, path, {"type", "mode"})
    return {"type": "cut", "mode": _opt_enum(block, path, "mode", "full", ("full", "partial"))}


def _validate_barcode(block, path):
    _reject_unknown_fields(
        block, path, {"type", "data", "symbology", "height", "width", "text_position", "align"}
    )
    data = _req_str(block, path, "data")
    symbology = _opt_enum(block, path, "symbology", "CODE128", BARCODE_SYMBOLOGIES)
    _check_barcode_data(data, symbology, f"{path}.data")
    return {
        "type": "barcode",
        "data": data,
        "symbology": symbology,
        "height": _opt_int(block, path, "height", 64, 1, 255),
        "width": _opt_int(block, path, "width", 3, 2, 6),
        "text_position": _opt_enum(block, path, "text_position", "below", BARCODE_TEXT_POSITIONS),
        "align": _opt_enum(block, path, "align", "center", ALIGNMENTS),
    }


def _check_barcode_data(data, symbology, path):
    digit_lengths = {"EAN13": (12, 13), "EAN8": (7, 8), "UPC-A": (11, 12)}
    if symbology in digit_lengths:
        lengths = digit_lengths[symbology]
        if not data.isdigit() or len(data) not in lengths:
            raise ValidationError(
                path, f"{symbology} requires {' or '.join(map(str, lengths))} digits"
            )
    elif symbology == "ITF" and (not data.isdigit() or len(data) % 2 != 0):
        raise ValidationError(path, "ITF requires an even number of digits")


def _validate_qr(block, path):
    _reject_unknown_fields(block, path, {"type", "data", "size", "ec", "align"})
    return {
        "type": "qr",
        "data": _req_str(block, path, "data"),
        "size": _opt_int(block, path, "size", 6, 1, 16),
        "ec": _opt_enum(block, path, "ec", "M", QR_EC_LEVELS),
        "align": _opt_enum(block, path, "align", "center", ALIGNMENTS),
    }


def _validate_image(block, path):
    _reject_unknown_fields(block, path, {"type", "data", "align", "width", "dither"})
    width = block.get("width")
    if width is not None:
        width = _opt_int(block, path, "width", None, 1, 4096)
    return {
        "type": "image",
        "data": _req_str(block, path, "data"),
        "align": _opt_enum(block, path, "align", "center", ALIGNMENTS),
        "width": width,
        "dither": _opt_bool(block, path, "dither", True),
    }


def _validate_drawer(block, path):
    _reject_unknown_fields(block, path, {"type", "pin"})
    return {"type": "drawer", "pin": _opt_enum(block, path, "pin", 2, (2, 5))}


def _validate_beep(block, path):
    _reject_unknown_fields(block, path, {"type", "times", "duration"})
    return {
        "type": "beep",
        "times": _opt_int(block, path, "times", 1, 1, 9),
        "duration": _opt_int(block, path, "duration", 3, 1, 9),
    }


BLOCK_VALIDATORS = {
    "text": _validate_text,
    "feed": _validate_feed,
    "cut": _validate_cut,
    "barcode": _validate_barcode,
    "qr": _validate_qr,
    "image": _validate_image,
    "drawer": _validate_drawer,
    "beep": _validate_beep,
}


def validate_document(payload):
    if not isinstance(payload, dict):
        raise ValidationError("", "request body must be a JSON object")
    unknown = set(payload) - {"blocks"}
    if unknown:
        raise ValidationError("", f"unknown field(s): {', '.join(sorted(unknown))}")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValidationError("blocks", "must be a non-empty list")
    if len(blocks) > MAX_BLOCKS:
        raise ValidationError("blocks", f"must contain at most {MAX_BLOCKS} blocks")

    normalized = []
    for i, block in enumerate(blocks):
        path = f"blocks[{i}]"
        if not isinstance(block, dict):
            raise ValidationError(path, "must be an object")
        block_type = block.get("type")
        if block_type not in BLOCK_VALIDATORS:
            raise ValidationError(
                f"{path}.type",
                f"must be one of {', '.join(sorted(BLOCK_VALIDATORS))}",
            )
        normalized.append(BLOCK_VALIDATORS[block_type](block, path))
    return normalized


def validate_text_request(payload):
    """Validate POST /print/text and translate it to a block document."""
    if not isinstance(payload, dict):
        raise ValidationError("", "request body must be a JSON object")
    _reject_unknown_fields(payload, "", {"text", "align", "bold", "cut", "feed"})
    text = payload.get("text")
    if not isinstance(text, str) or not text:
        raise ValidationError("text", "must be a non-empty string")
    blocks = [
        {
            "type": "text",
            "content": text,
            "align": _opt_enum(payload, "", "align", "left", ALIGNMENTS),
            "bold": _opt_bool(payload, "", "bold", False),
        }
    ]
    feed = _opt_int(payload, "", "feed", 0, 0, 20)
    if feed:
        blocks.append({"type": "feed", "lines": feed})
    if _opt_bool(payload, "", "cut", True):
        blocks.append({"type": "cut"})
    return validate_document({"blocks": blocks})
