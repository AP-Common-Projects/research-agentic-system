"""Reading JSON that a model wrote slightly wrong.

Two errors ended the Cinema run's log, and each one cost real work: a
channel the run had already paid to classify was dropped, and a batch of
video descriptions was thrown away. Neither payload was ambiguous -- one
had a trailing comma, the other left the quotes in around a film title.

The two failing shapes are pinned here verbatim, together with the thing
that matters more than either: that a payload which cannot be read without
guessing still raises. A parser that invents an object to avoid an error
puts fabricated research in a client's workbook.
"""

from __future__ import annotations

import json

import pytest

from src.llm.json_parse import JSONResponseError, loads_forgiving


class TestTheTwoFailuresFromTheCinemaRun:
    """Both of these raised JSONDecodeError in production on 2026-09-05."""

    def test_trailing_comma_before_the_closing_brace(self):
        """"Classifying channels: Expecting property name enclosed in double
        quotes: line 4 column 2" -- classify_channel lost one channel of 16."""
        payload = (
            '{\n'
            '  "face_status": "face",\n'
            '  "dominant_format": "vlog",\n'
            '}'
        )
        with pytest.raises(json.JSONDecodeError):
            json.loads(payload)

        assert loads_forgiving(payload, expect="object") == {
            "face_status": "face",
            "dominant_format": "vlog",
        }

    def test_unescaped_quotes_around_a_title_inside_a_string(self):
        """"Describing videos: Expecting ',' delimiter: line 31 column 123"
        -- a description quoting a film, with the quotes left unescaped."""
        payload = '["A review of "Star Wars" and its sequels", "Another one"]'
        with pytest.raises(json.JSONDecodeError):
            json.loads(payload)

        assert loads_forgiving(payload, expect="array") == [
            'A review of "Star Wars" and its sequels',
            "Another one",
        ]

    def test_the_repaired_description_keeps_the_title_it_quoted(self):
        """The repair is punctuation-only. If it dropped the inner quotes the
        parse would succeed and the description would be subtly wrong, which
        is worse than the error it replaced."""
        out = loads_forgiving(
            '["A review of "Star Wars" and its sequels"]', expect="array"
        )
        assert "Star Wars" in out[0]
        assert out[0].count('"') == 2


class TestTheFailureFromTheTechnologyRun:
    """Six errors, five of them this shape. The payload was captured by
    re-issuing the node's own prompt against the same titles: thirty
    complete descriptions, and no closing bracket."""

    #: The real reply's shape, abbreviated. The captured original ended
    #: exactly like this -- after the last description, no "]".
    UNCLOSED = (
        '[\n'
        '  "Creators share embarrassing stories involving Linus.",\n'
        '  "Reviews the Apple iPhone Air.",\n'
        '  "Compares the Kindle Oasis, Paperwhite, and Basic eReaders."'
    )

    def test_a_missing_closing_bracket_is_added(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads(self.UNCLOSED)

        out = loads_forgiving(self.UNCLOSED, expect="array")
        assert len(out) == 3
        assert out[-1] == "Compares the Kindle Oasis, Paperwhite, and Basic eReaders."

    def test_nothing_is_lost_when_only_the_bracket_was_missing(self):
        """The whole argument for repairing rather than discarding: every
        description the run paid for is still there."""
        assert loads_forgiving(self.UNCLOSED, expect="array") == json.loads(
            self.UNCLOSED + "]"
        )

    def test_a_missing_closing_brace_is_added(self):
        assert loads_forgiving('{"face_status": "face"', expect="object") == {
            "face_status": "face"
        }

    def test_nested_unclosed_structures(self):
        assert loads_forgiving('{"a": {"b": [1, 2') == {"a": {"b": [1, 2]}}

    def test_a_dangling_comma_before_the_missing_bracket(self):
        assert loads_forgiving('["a", "b",', expect="array") == ["a", "b"]


class TestTruncationMidValue:
    """A reply cut off inside a string is the one case where closing the
    structure would invent content. The incomplete item is dropped."""

    def test_the_severed_element_is_dropped_not_completed(self):
        out = loads_forgiving(
            '["complete one", "complete two", "this one was cut off mid-sen',
            expect="array",
        )
        assert out == ["complete one", "complete two"]

    def test_a_severed_object_value_is_dropped(self):
        assert loads_forgiving('{"a": 1, "b": "half a sen') == {"a": 1}

    def test_nothing_complete_means_nothing_to_salvage(self):
        """Rather than return [] and let a caller record thirty blanks."""
        with pytest.raises(JSONResponseError):
            loads_forgiving('["only a severed first ite', expect="array")

    def test_describe_video_titles_maps_positionally_so_a_short_array_is_safe(self):
        """The node zips descriptions onto video_ids. Dropping from the END
        leaves every surviving pair correctly aligned; dropping from the
        middle would silently mislabel every video after it, which is why
        the repair only ever truncates the tail."""
        video_ids = ["v1", "v2", "v3"]
        descriptions = loads_forgiving(
            '["about v1", "about v2", "about v3 but cut', expect="array"
        )
        assert list(zip(video_ids, descriptions)) == [
            ("v1", "about v1"), ("v2", "about v2"),
        ]


class TestValidJsonIsLeftAlone:
    def test_a_clean_object_is_returned_untouched(self):
        payload = '{"a": 1, "b": [1, 2], "c": {"d": "e"}}'
        assert loads_forgiving(payload) == json.loads(payload)

    def test_a_legitimately_escaped_quote_survives_the_round_trip(self):
        """The escaping repair must never fire on a payload that was already
        correct -- doubling the backslashes would corrupt good data."""
        payload = json.dumps({"note": 'he said "hello" twice'})
        assert loads_forgiving(payload) == {"note": 'he said "hello" twice'}

    def test_a_comma_inside_a_string_is_not_a_trailing_comma(self):
        payload = '{"note": "one, two, three"}'
        assert loads_forgiving(payload) == {"note": "one, two, three"}


class TestTheWrappingModelsAddAnyway:
    def test_markdown_fences(self):
        assert loads_forgiving('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fences_without_a_language(self):
        assert loads_forgiving('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_either_side(self):
        content = 'Here is the classification you asked for:\n{"a": 1}\nHope that helps!'
        assert loads_forgiving(content, expect="object") == {"a": 1}

    def test_expect_array_ignores_a_brace_in_the_prose(self):
        content = 'The set {a, b} maps to:\n[1, 2, 3]'
        assert loads_forgiving(content, expect="array") == [1, 2, 3]

    def test_smart_quotes(self):
        """A model asked to write descriptions sometimes styles its own JSON."""
        assert loads_forgiving('{“a”: “b”}') == {"a": "b"}

    def test_a_typographic_apostrophe_inside_a_description_is_preserved(self):
        """JSON has no single-quoted strings, so a curly apostrophe can only
        ever be content. Rewriting it would quietly edit what the model wrote
        about Apple's products on the way into the workbook."""
        out = loads_forgiving('["Reviews Apple’s newest laptop",', expect="array")
        assert out == ["Reviews Apple’s newest laptop"]


class TestRepairsCompose:
    def test_a_nested_trailing_comma(self):
        payload = '{"a": {"b": 1,}, "c": [1, 2,],}'
        assert loads_forgiving(payload) == {"a": {"b": 1}, "c": [1, 2]}

    def test_an_inner_quote_and_a_trailing_comma_together(self):
        payload = '{\n  "note": "a review of "Heat"",\n}'
        assert loads_forgiving(payload, expect="object") == {
            "note": 'a review of "Heat"'
        }


class TestItRaisesRatherThanGuesses:
    """The point of the whole module. Callers already handle "skip this
    item"; what they cannot handle is a plausible object that is fiction."""

    @pytest.mark.parametrize(
        "payload",
        [
            "",
            "   \n  ",
            "not json at all",
            "I could not complete that request.",
            '{"a": ',
            "{",
        ],
    )
    def test_unreadable_input_raises(self, payload):
        with pytest.raises(JSONResponseError):
            loads_forgiving(payload)

    def test_the_error_carries_the_payload_for_the_log(self):
        with pytest.raises(JSONResponseError) as exc:
            loads_forgiving('{"a": ')
        assert exc.value.payload

    def test_it_is_a_valueerror_so_existing_handlers_still_catch_it(self):
        """Several nodes catch (json.JSONDecodeError, ValueError, KeyError)
        around their parse. Subclassing ValueError means migrating a call
        site does not silently widen what escapes it."""
        assert issubclass(JSONResponseError, ValueError)


class TestEveryLlmReplyGoesThroughIt:
    def test_no_function_that_calls_a_model_also_parses_its_reply_by_hand(self):
        """The pattern this module replaces, left anywhere, is one more node
        that drops a channel the next time a model adds a comma.

        Scoped to the function, not the file: reading our own JSONL off disk
        with a strict json.loads is right, and judge.py does both.

        This will not catch a function that hands the raw reply to a helper
        to parse; the check below covers that shape instead.
        """
        import ast
        import pathlib

        def _calls(node: ast.AST) -> set[str]:
            names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    names.add(ast.unparse(sub.func))
            return names

        offenders = []
        for path in pathlib.Path("src").rglob("*.py"):
            if path.name == "json_parse.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                called = _calls(fn)
                if "complete_tier" in called and "json.loads" in called:
                    offenders.append(f"{path}::{fn.name}")

        assert offenders == [], f"still parsing model replies by hand: {offenders}"

    def test_no_source_file_scans_for_a_bracket_with_a_regex(self):
        """``re.search(r"\\{[\\s\\S]*\\}", reply)`` is the tell for the old
        approach wherever it lives, helper or not. Extraction belongs in one
        place now, and that place handles the repair too."""
        import pathlib

        offenders = []
        for path in pathlib.Path("src").rglob("*.py"):
            if path.name == "json_parse.py":
                continue
            code = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#")
            )
            if r"[\s\S]*" in code:
                offenders.append(str(path))

        assert offenders == [], f"hand-rolled JSON extraction remains in: {offenders}"


class TestProseWhereJsonWasAskedFor:
    """A history run got this back from a batch of ordinary video titles:

        关于这个问题，我没有相关信息，您可以尝试问我其它问题，我会尽力为您解答~
        ("I don't have information on that; try asking me something else.")

    Not malformed JSON -- no JSON at all. Nothing can be repaired out of
    it, and the parser is right to raise rather than invent. But it is a
    failed call in the same sense as an empty completion, and the same
    prompt succeeds on the next attempt.
    """

    #: The exact reply, kept verbatim.
    REFUSAL = "关于这个问题，我没有相关信息，您可以尝试问我其它问题，我会尽力为您解答~"

    def test_the_parser_still_refuses_to_invent_something(self):
        with pytest.raises(JSONResponseError):
            loads_forgiving(self.REFUSAL, expect="array")

    def test_complete_json_asks_again(self):
        from unittest.mock import patch

        from src.llm.json_parse import complete_json

        replies = [
            {"content": self.REFUSAL},
            {"content": '["a description"]', "cost_usd": 0.01},
        ]
        with patch("src.llm.cascade.complete_tier", side_effect=replies) as call:
            parsed, result = complete_json("cheap", "p", "s", expect="array")

        assert parsed == ["a description"]
        assert result["cost_usd"] == 0.01, "the caller still needs the usage"
        assert call.call_count == 2

    def test_it_gives_up_rather_than_asking_forever(self):
        from unittest.mock import patch

        from src.llm.json_parse import complete_json

        with patch("src.llm.cascade.complete_tier",
                   return_value={"content": self.REFUSAL}) as call:
            with pytest.raises(JSONResponseError):
                complete_json("cheap", "p", "s", expect="array", attempts=3)
        assert call.call_count == 3

    def test_a_reply_it_can_repair_costs_no_extra_call(self):
        """The repairs come first; only a reply with no JSON in it at all
        is worth paying for twice."""
        from unittest.mock import patch

        from src.llm.json_parse import complete_json

        with patch("src.llm.cascade.complete_tier",
                   return_value={"content": '["a", "b"'}) as call:
            parsed, _ = complete_json("cheap", "p", "s", expect="array")

        assert parsed == ["a", "b"]
        assert call.call_count == 1

    def test_the_error_it_raises_is_still_the_one_callers_catch(self):
        """Nodes treat JSONResponseError as "skip this item"; exhausting the
        retries must not change what escapes."""
        from unittest.mock import patch

        from src.llm.json_parse import complete_json

        with patch("src.llm.cascade.complete_tier",
                   return_value={"content": self.REFUSAL}):
            with pytest.raises(ValueError):
                complete_json("cheap", "p", "s", attempts=1)


class TestTheSitesThatCannotRetryForThemselves:
    def test_classify_channel_uses_it(self):
        """It moves to the next channel on a parse failure, so a refused
        channel was simply lost."""
        import inspect

        from src.nodes.classify_channel import classify_channel

        assert "complete_json(" in inspect.getsource(classify_channel)

    def test_describe_video_titles_uses_it(self):
        import inspect

        from src.nodes.describe_video_titles import describe_video_titles

        assert "complete_json(" in inspect.getsource(describe_video_titles)
