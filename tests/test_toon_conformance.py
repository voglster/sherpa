"""Conformance check for the python-toon encoder against the TOON spec.

Run: uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_toon_conformance.py -v

`tests/fixtures/toon-spec/` is a vendored snapshot of the official reference
suite (github.com/toon-format/spec, tests/fixtures/encode, commit 6b8e74c).
KNOWN_NONCONFORMANT documents every fixture case python-toon 0.1.3 fails,
each tagged with the spec behavior it violates. This is a decision record,
not a workaround: see docs/axi-toon-decision.md for the adoption verdict.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import toon

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "toon-spec" / "encode"

_DELIMITER_MARKER = re.compile(r"\[(\d+),\]")


def _strip_accepted_delimiter_marker(encoded: str) -> str:
    """python-toon always emits the explicit comma delimiter marker (e.g. `[2,]`)
    even though comma is the default. SPEC §6 allows `key[N<delim>]{...}` with an
    explicit delimiter character, so this is a legal, non-corrupting deviation
    that only costs one extra byte per header — not a fixture failure."""
    return _DELIMITER_MARKER.sub(r"[\1]", encoded)


def _load_fixture_cases() -> list[tuple[str, dict]]:
    cases = []
    for fixture_file in sorted(FIXTURES_DIR.glob("*.json")):
        data = json.loads(fixture_file.read_text())
        for case in data["tests"]:
            cases.append((fixture_file.name, case))
    return cases


KNOWN_NONCONFORMANT = {
    ("arrays-nested.json", "encodes empty inner arrays"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-nested.json", "encodes root-level array mixing primitive, object, and array of objects in list format"): "missing '- ' hyphen prefix on a nested list-of-arrays line",
    ("arrays-nested.json", "encodes root-level arrays of arrays"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-nested.json", "encodes empty root-level array"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-nested.json", "encodes complex nested structure"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-nested.json", "uses list format for arrays mixing objects and arrays"): "missing '- ' hyphen prefix on a nested list-of-arrays line",
    ("arrays-nested.json", "quotes hash-leading string as list item"): "encoder under-quotes '#'-leading values (would be read back as a comment by a spec-compliant decoder)",
    ("arrays-objects.json", "preserves field order in list items - array first"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "uses list format for objects containing arrays of arrays"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "uses tabular format for nested uniform object arrays"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "uses list format for nested object arrays with mismatched keys"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "uses list format for objects with multiple array fields"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "uses list format for objects with only array fields"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "encodes objects with empty arrays in list format"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-objects.json", "uses canonical encoding for multi-field list-item objects with tabular arrays"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "uses canonical encoding for single-field list-item tabular arrays"): "list-item's first field is placed on its own indented line instead of sharing the hyphen line (SPEC 9.4/9.5)",
    ("arrays-objects.json", "places empty arrays on hyphen line when first"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-objects.json", "uses field order from first object for tabular headers"): "tabular detection requires identical key order across rows instead of reordering to the first row's order",
    ("arrays-primitive.json", "encodes empty arrays"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-primitive.json", "encodes empty string keys for inline arrays"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-primitive.json", "encodes empty string keys for empty arrays"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-primitive.json", "quotes hash-leading string in inline array"): "encoder under-quotes '#'-leading values (would be read back as a comment by a spec-compliant decoder)",
    ("arrays-tabular.json", "encodes tabular arrays with keys needing quotes"): "tabular header field names containing delimiter/space characters are not quoted",
    ("arrays-tabular.json", "encodes tabular arrays with empty string keys"): 'empty array/empty-string-key literal forms ([] / "") not emitted; also stray trailing space after empty inline header',
    ("arrays-tabular.json", "quotes hash-leading string in tabular cell"): "encoder under-quotes '#'-leading values (would be read back as a comment by a spec-compliant decoder)",
    ("arrays-tabular.json", "collapses a uniform nested object column into a nested field group"): "nested field-group column collapsing (SPEC 10) is not implemented",
    ("arrays-tabular.json", "collapses sibling nested field groups with depth-first row layout"): "nested field-group column collapsing (SPEC 10) is not implemented",
    ("arrays-tabular.json", "collapses nested field groups recursively without a depth cap"): "nested field-group column collapsing (SPEC 10) is not implemented",
    ("arrays-tabular.json", "uses the active delimiter inside nested field groups"): "nested field-group column collapsing (SPEC 10) is not implemented",
    ("arrays-tabular.json", "quotes subfield names inside nested field groups per key encoding"): "nested field-group column collapsing (SPEC 10) is not implemented",
    ("objects-keyed.json", "encodes objects of uniform objects in keyed tabular form"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "encodes an eligible root object in keyless keyed form"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "collapses uniform nested object columns inside keyed headers"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "orders fields by the first entry value's encounter order"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "uses the active delimiter in keyed headers and entry-row cells"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "quotes entry keys per key encoding"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "quotes entry-row cells containing the active delimiter"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "emits a keyed header on the hyphen line when it is the first field of a list item"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects-keyed.json", "never encodes an anonymous array element in keyed form"): "keyed tabular form (SPEC 9.5/10) is not implemented; falls back to verbose nested form",
    ("objects.json", "escapes U+0004 control character in key via \\uXXXX"): "raw control character emitted instead of \\uXXXX escape",
    ("objects.json", "escapes U+001F control character in key via \\uXXXX"): "raw control character emitted instead of \\uXXXX escape",
    ("objects.json", "quotes hash-leading string in object field value"): "encoder under-quotes '#'-leading values (would be read back as a comment by a spec-compliant decoder)",
    ("primitives.json", "encodes string with U+0004 control character via \\uXXXX"): "raw control character emitted instead of \\uXXXX escape",
    ("primitives.json", "encodes small decimal without exponent notation"): "canonical-range small decimal encoded in exponent form instead of required decimal form (SPEC 2)",
    ("primitives.json", "quotes string equal to hash"): "encoder under-quotes '#'-leading values (would be read back as a comment by a spec-compliant decoder)",
    ("primitives.json", "quotes string starting with hash"): "encoder under-quotes '#'-leading values (would be read back as a comment by a spec-compliant decoder)",
}


def _fixture_options(case: dict) -> dict | None:
    options = case.get("options") or {}
    mapped = {}
    if "delimiter" in options:
        mapped["delimiter"] = options["delimiter"]
    if "indentSize" in options:
        mapped["indent"] = options["indentSize"]
    return mapped or None


@pytest.mark.parametrize(
    "fixture_file,case",
    _load_fixture_cases(),
    ids=[f"{f}::{c['name']}" for f, c in _load_fixture_cases()],
)
def test_encoder_matches_official_reference_fixture(fixture_file, case):
    reason = KNOWN_NONCONFORMANT.get((fixture_file, case["name"]))
    if reason:
        pytest.xfail(reason)
    actual = toon.encode(case["input"], _fixture_options(case))
    assert _strip_accepted_delimiter_marker(actual) == _strip_accepted_delimiter_marker(case["expected"])


def test_uniform_object_array_uses_tabular_form():
    encoded = toon.encode({"tasks": [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]})
    header, *rows = encoded.splitlines()
    assert header.startswith("tasks[2")
    assert header.endswith("]{id,title}:")
    assert [row.strip() for row in rows] == ["1,a", "2,b"]


def test_delimiter_bearing_values_are_quoted():
    encoded = toon.encode({"rows": [{"v": "a,b"}]})
    assert '"a,b"' in encoded


def test_plain_values_are_not_quoted():
    assert '"' not in toon.encode({"rows": [{"v": "plain text"}]})


def test_nested_objects_indent_rather_than_flatten():
    encoded = toon.encode({"task": {"number": 42, "state": "open"}})
    assert encoded.splitlines() == ["task:", "  number: 42", "  state: open"]


def test_none_is_not_rendered_as_python_repr():
    assert "None" not in toon.encode({"rows": [{"v": None}]})


@pytest.mark.parametrize("hostile", ['has "quotes"', "has\nnewline", "-leading hyphen", "#leading hash"])
def test_values_needing_quotes_round_trip(hostile):
    assert toon.decode(toon.encode({"rows": [{"v": hostile}]})) == {"rows": [{"v": hostile}]}
