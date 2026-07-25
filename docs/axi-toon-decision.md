# TOON encoder decision record

Decision gate for Task 1 of the AXI foundation plan. **Verdict: BLOCKED.**
`python-toon` 0.1.3 does not pass the official TOON reference test suite
closely enough to adopt as-is. Do not proceed to Task 2 (`render.py`) against
this dependency without a human decision on how to resolve the findings
below (patch, wrap only the safe subset, or pick a different implementation).

## `toon-format` (PyPI): rejected

Confirmed prior finding, not re-derived here: `toon_format.encode()` raises
`NotImplementedError`. It is a placeholder package, not a usable encoder.

## `python-toon`: version pinning

**Pinned version: `python-toon==0.1.3`**

The earlier observation that an unpinned `uv run --with python-toon` resolved
to `0.1.1` while PyPI reports `0.1.3` as latest was a red herring, not a
resolver or `requires-python` issue. Both `0.1.0`–`0.1.3` declare
`requires-python: >=3.8`, so there is no version bound forcing an older
release. The real cause: `python-toon` 0.1.3 ships a metadata bug — its
`toon/__init__.py` still hardcodes `__version__ = "0.1.1"` even though the
installed distribution is 0.1.3 per `importlib.metadata.version("python-toon")`.
Verified directly:

```
$ uv run --with 'python-toon==0.1.3' --no-cache python -c "
import toon, importlib.metadata as m
print('module __version__:', toon.__version__)
print('installed dist version:', m.version('python-toon'))"
Installed 1 package in 3ms
module __version__: 0.1.1
installed dist version: 0.1.3
```

An unpinned `uv run --with python-toon` was already resolving to 0.1.3 all
along; `toon.__version__` just reports it wrong. Pin by the distribution
version string (`python-toon==0.1.3`) in dependency blocks, not by trusting
`toon.__version__` at runtime.

## Conformance test suite used

`tests/fixtures/encode/*` from `github.com/toon-format/spec`
(commit `6b8e74c`, `main` branch as of 2026-07-25) was fetched, inspected,
and found to be a clean, data-driven, language-agnostic JSON fixture format
(`{name, input, expected, options, specSection}`) — well suited to a loader,
so the Step 2 fallback (hand-written SPEC.md examples only) was not needed.
Fixtures are vendored at `tests/fixtures/toon-spec/encode/*.json` (encode
side only; decode fixtures were not vetted — see Concerns) so the suite runs
offline and reproducibly. `tests/test_toon_conformance.py` loads all 173
encode fixture cases plus the plan's hand-written decision-gate tests.

### `[N,]` header marker — expected, not a bug

`python-toon` always emits the explicit comma delimiter marker in array
headers, e.g. `tasks[2,]{id,title}:` instead of `tasks[2]{id,title}:`. SPEC
§6 permits `key[N<delimiter>]{fields}:` with an explicit delimiter character
even when it equals the default, so this is legal and non-corrupting — it
just costs one extra byte per header. The conformance test normalizes this
one marker before comparing so it doesn't mask real mismatches.

### Result: 136 passed, 46 xfailed, 0 unexpected failures

```
$ uv run --with pytest --with 'python-toon==0.1.3' pytest tests/test_toon_conformance.py -v
...
136 passed, 46 xfailed in 0.17s
```

127 of 173 official encode fixtures (73%) pass byte-exact after normalizing
the `[N,]` marker. The plan's five named decision-gate tests
(`test_uniform_object_array_uses_tabular_form`,
`test_delimiter_bearing_values_are_quoted`, `test_plain_values_are_not_quoted`,
`test_nested_objects_indent_rather_than_flatten`,
`test_none_is_not_rendered_as_python_repr`) and all four
`test_values_needing_quotes_round_trip` cases pass.

**Why this is BLOCKED despite the named gate tests passing:** the plan's
hand-written tests are a narrow sample. Running the full official suite
surfaced a real under-quoting bug in the same family the gate tests were
designed to catch — the gate tests just didn't happen to hit it.

## Confirmed defects (grouped, with fixture counts)

- **Hash-leading value under-quoting (7 fixtures)** — `toon.encode("#hello")`
  produces `#hello` instead of `"#hello"`. TOON reserves `#` as a full-line
  comment marker (SPEC §5.1/§7.2/§14.1); an unquoted `#`-leading value is
  legal-looking TOON to *this* library's own decoder only because its
  decoder has a matching bug (see below) — a spec-compliant decoder (e.g.
  the JS reference implementation) would silently drop the value as a
  comment. This is real data corruption risk across implementations, not
  cosmetic.
- **Control characters emitted raw instead of `\uXXXX`-escaped (3 fixtures)**
  — U+0004 and U+001F land unescaped in both keys and values, which is
  illegal per SPEC §7.1 and can corrupt output or break terminals/toolchains
  that don't expect raw control bytes.
- **Keyed tabular form (SPEC §9.5/§10) not implemented (9 fixtures)** —
  uniform-object maps never collapse to `key[N:]{fields}:` form; they always
  fall back to fully-expanded nested objects. This is the single largest gap
  and directly guts the token savings TOON exists to capture for map-shaped
  data (e.g. `{"servers": {"alpha": {...}, "beta": {...}}}`).
- **Nested field-group column collapsing (SPEC §10) not implemented
  (5 fixtures)** — tabular arrays whose rows share a uniform nested object
  column (e.g. `orders[].customer.{name,country}`) never collapse that
  column into the header; they fall back to per-row nested indentation. Same
  class of loss as above, for the common "flat row + nested sub-object"
  shape.
- **List-item first field not inlined onto the hyphen line (10 fixtures)** —
  SPEC §9.4/§9.5 wants `- nums[3]: 1,2,3` when an array/tabular field is
  first in a list-item object; this library always emits a bare `-` then
  indents the field on the next line, adding a line per list item.
- **Tabular detection requires identical key order across rows (1 fixture)**
  — objects with the same keys in a different order should still collapse
  to tabular form (reordered to the first row's order); this library falls
  back to verbose list form instead, losing the optimization.
- **Empty array / empty string key literal forms (7 fixtures)** — expects
  `key: []` / `""[3]: ...` / `"": []`; library emits `key[0]:` (plus a stray
  trailing space after some empty inline headers) and drops the required
  quoting on empty string keys.
- **Tabular header field names needing quotes are not quoted (1 fixture)**
  — a field name containing `:` or a space should be quoted in the tabular
  `{...}` header; this library leaves it bare, which is ambiguous with the
  header's own delimiter/type syntax.
- **Canonical-range small decimal uses exponent form (1 fixture)** — `1e-6`
  is inside SPEC §2's canonical decimal range (`1e-6 ≤ |n| < 1e21`), which
  requires byte-exact decimal output (`0.000001`); this library emits
  `1e-06` instead.

Full list with per-case reasons: `tests/test_toon_conformance.py`,
`KNOWN_NONCONFORMANT`.

## Concerns / not covered here

- Only `encode` fixtures were vetted. `decode` fixtures (including
  `validation-errors.json`, `indentation-errors.json`) were not run; given
  the encode-side defects found, decode conformance should not be assumed.
- The under-quoting and control-character bugs are corruption-class and,
  per the task's decision gate, are reported rather than patched around.
- Recommendation for whoever picks this up next: either (a) find/patch a
  fork, (b) write a thin pre/post-processing shim that only covers the
  quoting and control-character bugs (the two genuinely corrupting ones) and
  accept the missing tabular optimizations as a token-efficiency loss, or
  (c) evaluate other TOON implementations before committing further tasks
  to this package. This decision should be made explicitly, not implied by
  proceeding to Task 2.
