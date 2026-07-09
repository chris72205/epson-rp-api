import pytest

from printapi.validation import ValidationError, validate_document, validate_text_request


def doc(*blocks):
    return {"blocks": list(blocks)}


def test_text_defaults_filled():
    [block] = validate_document(doc({"type": "text", "content": "hi"}))
    assert block == {
        "type": "text",
        "content": "hi",
        "bold": False,
        "underline": 0,
        "align": "left",
        "width": 1,
        "height": 1,
        "invert": False,
        "font": "a",
        "newline": True,
    }


def test_all_block_types_accepted():
    blocks = validate_document(
        doc(
            {"type": "text", "content": "x"},
            {"type": "feed", "lines": 3},
            {"type": "cut", "mode": "partial"},
            {"type": "barcode", "data": "ABC123", "symbology": "CODE39"},
            {"type": "qr", "data": "https://example.com", "size": 8, "ec": "H"},
            {"type": "image", "data": "aGVsbG8="},
            {"type": "drawer", "pin": 5},
            {"type": "beep", "times": 2, "duration": 4},
        )
    )
    assert [b["type"] for b in blocks] == [
        "text", "feed", "cut", "barcode", "qr", "image", "drawer", "beep",
    ]
    assert blocks[5]["width"] is None  # image default: shrink-to-fit


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ("nope", "must be a JSON object"),
        ({}, "must be a non-empty list"),
        ({"blocks": []}, "must be a non-empty list"),
        ({"blocks": {}}, "must be a non-empty list"),
        ({"blocks": [1], "extra": 1}, "unknown field(s): extra"),
        ({"blocks": ["x"]}, "blocks[0]: must be an object"),
        ({"blocks": [{"type": "nope"}]}, "blocks[0].type"),
        ({"blocks": [{"type": "text"}]}, "missing required field 'content'"),
        ({"blocks": [{"type": "text", "content": ""}]}, "blocks[0].content"),
        ({"blocks": [{"type": "text", "content": "x", "wat": 1}]}, "unknown field(s): wat"),
        ({"blocks": [{"type": "text", "content": "x", "height": 9}]}, "between 1 and 8"),
        ({"blocks": [{"type": "text", "content": "x", "height": True}]}, "between 1 and 8"),
        ({"blocks": [{"type": "text", "content": "x", "align": "middle"}]}, "one of left, center, right"),
        ({"blocks": [{"type": "feed", "lines": 0}]}, "between 1 and 20"),
        ({"blocks": [{"type": "cut", "mode": "half"}]}, "one of full, partial"),
        ({"blocks": [{"type": "barcode", "data": "abc", "symbology": "EAN13"}]}, "EAN13 requires"),
        ({"blocks": [{"type": "barcode", "data": "123", "symbology": "ITF"}]}, "even number"),
        ({"blocks": [{"type": "qr", "data": "x", "ec": "Z"}]}, "one of L, M, Q, H"),
        ({"blocks": [{"type": "drawer", "pin": 3}]}, "one of 2, 5"),
        ({"blocks": [{"type": "beep", "times": 10}]}, "between 1 and 9"),
    ],
)
def test_rejections_include_path(payload, fragment):
    with pytest.raises(ValidationError) as exc:
        validate_document(payload)
    assert fragment in str(exc.value)


def test_too_many_blocks():
    with pytest.raises(ValidationError, match="at most 500"):
        validate_document(doc(*[{"type": "feed"}] * 501))


def test_ean13_valid():
    [block] = validate_document(doc({"type": "barcode", "data": "4006381333931", "symbology": "EAN13"}))
    assert block["height"] == 64 and block["width"] == 3 and block["text_position"] == "below"


def test_text_request_translates_to_blocks():
    blocks = validate_text_request({"text": "hello", "align": "center", "feed": 2})
    assert [b["type"] for b in blocks] == ["text", "feed", "cut"]
    assert blocks[0]["align"] == "center"
    assert blocks[1]["lines"] == 2
    assert blocks[2]["mode"] == "full"


def test_text_request_no_cut():
    blocks = validate_text_request({"text": "hello", "cut": False})
    assert [b["type"] for b in blocks] == ["text"]


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({}, "text"),
        ({"text": ""}, "text"),
        ({"text": "x", "wat": 1}, "unknown field(s): wat"),
        ({"text": "x", "feed": 21}, "between 0 and 20"),
    ],
)
def test_text_request_rejections(payload, fragment):
    with pytest.raises(ValidationError) as exc:
        validate_text_request(payload)
    assert fragment in str(exc.value)
