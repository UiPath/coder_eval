"""Tests for prompt mutation models and apply_prompt_mutations()."""

import re

import pytest
from pydantic import TypeAdapter, ValidationError

from coder_eval.models import (
    PromptMutation,
    PromptPrefix,
    PromptReplace,
    PromptSuffix,
    PromptTemplate,
    apply_prompt_mutations,
)


class TestPromptPrefix:
    def test_prepends_with_default_separator(self):
        result = apply_prompt_mutations("base prompt", [PromptPrefix(content="Think step by step.")])
        assert result == "Think step by step.\n\nbase prompt"

    def test_custom_separator(self):
        result = apply_prompt_mutations("base", [PromptPrefix(content="prefix", separator=" ")])
        assert result == "prefix base"


class TestPromptSuffix:
    def test_appends_with_default_separator(self):
        result = apply_prompt_mutations("base prompt", [PromptSuffix(content="Include type hints.")])
        assert result == "base prompt\n\nInclude type hints."

    def test_custom_separator(self):
        result = apply_prompt_mutations("base", [PromptSuffix(content="suffix", separator=" -- ")])
        assert result == "base -- suffix"


class TestPromptReplace:
    def test_literal_replacement(self):
        result = apply_prompt_mutations("Create a Python file", [PromptReplace(pattern="Create", replacement="Write")])
        assert result == "Write a Python file"

    def test_literal_multiple_occurrences(self):
        result = apply_prompt_mutations("foo bar foo", [PromptReplace(pattern="foo", replacement="baz")])
        assert result == "baz bar baz"

    def test_literal_no_match(self):
        result = apply_prompt_mutations("hello world", [PromptReplace(pattern="xyz", replacement="abc")])
        assert result == "hello world"

    def test_regex_replacement(self):
        result = apply_prompt_mutations(
            "version 123 build 456", [PromptReplace(pattern=r"\d+", replacement="N", regex=True)]
        )
        assert result == "version N build N"

    def test_regex_with_groups(self):
        result = apply_prompt_mutations(
            "hello world",
            [PromptReplace(pattern=r"(\w+) (\w+)", replacement=r"\2 \1", regex=True)],
        )
        assert result == "world hello"

    def test_invalid_regex_raises(self):
        with pytest.raises(re.error):
            apply_prompt_mutations("text", [PromptReplace(pattern="[invalid", replacement="x", regex=True)])


class TestPromptTemplate:
    def test_single_variable(self):
        result = apply_prompt_mutations(
            "Create a {language} file",
            [PromptTemplate(variables={"language": "Python"})],
        )
        assert result == "Create a Python file"

    def test_multiple_variables(self):
        result = apply_prompt_mutations(
            "Write {language} code using {style} style",
            [PromptTemplate(variables={"language": "Python", "style": "functional"})],
        )
        assert result == "Write Python code using functional style"

    def test_missing_variable_left_intact(self):
        result = apply_prompt_mutations(
            "Use {known} and {unknown} variables",
            [PromptTemplate(variables={"known": "resolved"})],
        )
        assert result == "Use resolved and {unknown} variables"


class TestComposition:
    def test_mutations_compose_sequentially(self):
        mutations: list[PromptMutation] = [
            PromptPrefix(content="Step 1:"),
            PromptSuffix(content="Step 3: done."),
        ]
        result = apply_prompt_mutations("Step 2: work", mutations)
        assert result == "Step 1:\n\nStep 2: work\n\nStep 3: done."

    def test_empty_mutations_list(self):
        result = apply_prompt_mutations("unchanged", [])
        assert result == "unchanged"

    def test_mutation_on_empty_prompt(self):
        result = apply_prompt_mutations("", [PromptPrefix(content="hello")])
        assert result == "hello\n\n"


class TestPydanticDiscriminator:
    def test_round_trip_serialize_deserialize(self):
        adapter = TypeAdapter(list[PromptMutation])
        mutations: list[PromptMutation] = [
            PromptPrefix(content="pre"),
            PromptSuffix(content="suf"),
            PromptReplace(pattern="a", replacement="b"),
            PromptTemplate(variables={"x": "y"}),
        ]
        data = adapter.dump_python(mutations, mode="json")
        restored = adapter.validate_python(data)
        assert len(restored) == 4
        assert isinstance(restored[0], PromptPrefix)
        assert isinstance(restored[1], PromptSuffix)
        assert isinstance(restored[2], PromptReplace)
        assert isinstance(restored[3], PromptTemplate)


class TestMutationForbidsExtras:
    """Typo'd mutation keys must fail loudly, not be silently dropped."""

    @pytest.mark.parametrize(
        ("payload", "bad_key"),
        [
            ({"type": "prefix", "content": "x", "seperator": "-"}, "seperator"),
            ({"type": "suffix", "content": "x", "seperater": "-"}, "seperater"),
            ({"type": "replace", "pattern": "a", "replacement": "b", "regexp": True}, "regexp"),
            ({"type": "template", "variables": {"a": "b"}, "varaibles": {"c": "d"}}, "varaibles"),
        ],
        ids=["prefix", "suffix", "replace", "template"],
    )
    def test_prompt_mutation_rejects_unknown_key(self, payload: dict, bad_key: str):
        adapter = TypeAdapter(PromptMutation)
        with pytest.raises(ValidationError) as exc:
            adapter.validate_python(payload)
        assert bad_key in str(exc.value)

    @pytest.mark.parametrize(
        "payload",
        [
            {"type": "prefix", "content": "x"},
            {"type": "suffix", "content": "x"},
            {"type": "replace", "pattern": "a", "replacement": "b"},
            {"type": "template", "variables": {"a": "b"}},
        ],
        ids=["prefix", "suffix", "replace", "template"],
    )
    def test_prompt_mutation_valid_payloads_unaffected(self, payload: dict):
        adapter = TypeAdapter(PromptMutation)
        mutation = adapter.validate_python(payload)
        assert mutation.type == payload["type"]
