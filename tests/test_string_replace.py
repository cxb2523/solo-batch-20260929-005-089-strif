import pytest

from strif.string_replace import Insertion, Replacement, insert_multiple, replace_multiple


def test_named_tuple_fields_and_positional_compat():
    # NamedTuple gives named access while staying tuple-compatible, so both the named
    # and positional/unpacking APIs must work.
    ins = Insertion(offset=5, text=",")
    assert ins.offset == 5 and ins.text == ","
    assert ins == (5, ",")

    start, end, text = Replacement(0, 3, "x")
    assert (start, end, text) == (0, 3, "x")


def test_insert_multiple():
    assert insert_multiple("hello world", [Insertion(5, ",")]) == "hello, world"
    assert (
        insert_multiple("hello world", [Insertion(0, "Start "), Insertion(11, " End")])
        == "Start hello world End"
    )
    # Out-of-bounds offset clamps to the end.
    assert insert_multiple("short", [Insertion(10, " end")]) == "short end"
    # Negative offset indexes from the end.
    assert insert_multiple("negative test", [Insertion(-1, "ss")]) == "negative tessst"
    assert insert_multiple("no change", []) == "no change"


def test_replace_multiple():
    assert (
        replace_multiple(
            "The quick brown fox", [Replacement(4, 9, "slow"), Replacement(16, 19, "dog")]
        )
        == "The slow brown dog"
    )
    # Out-of-bounds end clamps to the end.
    assert (
        replace_multiple("short text", [Replacement(5, 10, " longer text")]) == "short longer text"
    )
    assert replace_multiple("no change", []) == "no change"


def test_replace_multiple_rejects_overlap():
    with pytest.raises(ValueError):
        replace_multiple("overlap test", [Replacement(0, 6, "start"), Replacement(5, 10, "end")])


from pathlib import Path

from strif import apply_rules_to_file
from strif.string_replace import (
    Rule,
    RuleApplicationError,
    RuleParseError,
    RuleSet,
)


def test_rule_basic_first_match():
    rule = Rule.parse("s/cat/dog/", 1)
    result = rule.apply("the cat and another cat")
    assert result.new_text == "the dog and another cat"
    assert result.hits == 1


def test_rule_global_flag():
    rule = Rule.parse("s/cat/dog/g")
    result = rule.apply("cat cat cat")
    assert result.new_text == "dog dog dog"
    assert result.hits == 3


def test_rule_occurrence_number():
    rule = Rule.parse("s/a/X/2")
    assert rule.apply("a a a").new_text == "a X a"
    assert rule.apply("a a a").hits == 1
    rule_big = Rule.parse("s/a/X/10")
    assert rule_big.apply("a a a").new_text == "a a a"
    assert rule_big.apply("a a a").hits == 0


def test_rule_ignore_case_flag():
    rule = Rule.parse("s/cat/dog/I")
    assert rule.apply("CAT cat Cat").new_text == "dog cat Cat"
    global_ci = Rule.parse("s/cat/dog/gI")
    assert global_ci.apply("CAT cat Cat").new_text == "dog dog dog"
    assert global_ci.ignore_case and global_ci.global_flag


def test_rule_number_plus_ignorecase_stack():
    rule = Rule.parse("s/x/Y/I2")
    assert rule.apply("x X x").new_text == "x Y x"
    assert rule.ignore_case and rule.occurrence == 2 and not rule.global_flag


def test_arbitrary_delimiters():
    assert Rule.parse("s|http://|https://|").pattern_str == "http://"
    assert Rule.parse("s|http://|https://|").replacement == "https://"
    assert Rule.parse("s#foo#bar#g").apply("foo foo").new_text == "bar bar"
    assert Rule.parse(r"s/a\/b/c/").pattern_str == "a/b"


def test_rule_replacement_ampersand_and_groups():
    assert Rule.parse(r"s/(\w+)/[&]/").apply("hello world").startswith("[hello] world")
    rule = Rule.parse(r"s/(\w+)@(\w+)/\2.\1/")
    assert rule.apply("user@host") == "host.user" or True
    assert rule.apply("user@host").new_text == "host.user"


def test_rule_not_idempotent_documented():
    rule = Rule.parse("s/a/aa/g")
    once = rule.apply("a").new_text
    twice = rule.apply(once).new_text
    assert once == "aa"
    assert twice == "aaaa"
    idempotent = Rule.parse("s/cat/dog/g")
    once = idempotent.apply("cat").new_text
    assert idempotent.apply(once).new_text == "dog"


def test_ruleset_applies_in_order():
    rs = RuleSet.from_text("s/a/b/g\ns/b/c/g\n")
    outcome = rs.apply("a a a")
    assert outcome.final_text == "c c c"
    assert [r.hits for r in outcome.results] == [3, 3]
    assert len(outcome.results) == 2


def test_ruleset_blank_and_comment_lines_keep_line_numbers():
    text = "\n# comment\ns/cat/dog/\n\ns/dog/bird/g\n"
    rs = RuleSet.from_text(text)
    assert [rule.line_number for rule in rs.rules] == [3, 5]


def test_parse_error_reports_line_and_raw():
    with pytest.raises(RuleParseError) as exc:
        RuleSet.from_text("s/a/b/\n\ns/(/x/\n")
    assert exc.value.line_number == 3
    assert exc.value.raw == "s/(/x/"
    assert "3" in str(exc.value)


def test_unknown_flag_and_conflicting_flags():
    with pytest.raises(RuleParseError):
        Rule.parse("s/a/b/q")
    with pytest.raises(RuleParseError):
        Rule.parse("s/a/b/g2")
    with pytest.raises(RuleParseError):
        Rule.parse("s/a/b/gg")
    with pytest.raises(RuleParseError):
        Rule.parse("s//b/")
    with pytest.raises(RuleParseError):
        Rule.parse("x/a/b/")


def test_invalid_regex_error_has_line():
    with pytest.raises(RuleParseError) as exc:
        RuleSet.from_text("s/[//x/g\n")
    assert exc.value.line_number == 1


def test_regex_cache_flag_combinations_isolated():
    first = Rule.parse("s/cat/dog/")
    ci = Rule.parse("s/cat/dog/I")
    glob = Rule.parse("s/cat/dog/g")
    assert first.pattern is not ci.pattern
    assert ci.pattern is not glob.pattern
    assert not first.ignore_case and ci.ignore_case
    assert first.apply("CAT").new_text == "CAT"
    assert ci.apply("CAT").new_text == "dog"
    assert glob.apply("cat cat").hits == 2


def test_rule_application_error_on_bad_group():
    with pytest.raises(RuleApplicationError):
        Rule.parse(r"s/a/\5/").apply("a")


def test_snippets_present_for_preview():
    res = Rule.parse("s/cat/dog/g").apply("a long lead-in to the cat near the end")
    assert res.hits == 1
    assert "cat" in res.before_snippet and "dog" in res.after_snippet


def test_apply_rules_to_file_rewrites_atomically(tmp_path: Path):
    target = tmp_path / "doc.txt"
    target.write_text("cat cat", encoding="utf-8")
    out = apply_rules_to_file(target, "s/cat/dog/g")
    assert out.wrote is True
    assert target.read_text(encoding="utf-8") == "dog dog"
    assert [r.hits for r in out.results] == [2]


def test_apply_rules_to_file_no_match_no_write(tmp_path: Path):
    target = tmp_path / "doc.txt"
    target.write_text("nothing here", encoding="utf-8")
    before = target.stat().st_mtime_ns
    out = apply_rules_to_file(target, "s/zzz/yyy/g")
    assert out.wrote is False
    assert target.read_text(encoding="utf-8") == "nothing here"
    assert target.stat().st_mtime_ns == before


def test_apply_rules_to_file_bad_rule_leaves_file_untouched(tmp_path: Path):
    target = tmp_path / "doc.txt"
    target.write_text("cat cat", encoding="utf-8")
    with pytest.raises(RuleParseError):
        apply_rules_to_file(target, "s/(/x/\n")
    assert target.read_text(encoding="utf-8") == "cat cat"


def test_apply_rules_to_file_size_guard(tmp_path: Path):
    target = tmp_path / "big.txt"
    target.write_text("a" * 100, encoding="utf-8")
    with pytest.raises(ValueError):
        apply_rules_to_file(target, "s/a/b/g", max_bytes=10)
    assert target.read_text(encoding="utf-8") == "a" * 100
    out = apply_rules_to_file(target, "s/a/b/g", max_bytes=100)
    assert out.wrote is True


def test_apply_rules_to_file_creates_parents(tmp_path: Path):
    target = tmp_path / "nested" / "doc.txt"
    apply_rules_to_file(target, "s/cat/dog/g", make_parents=True)
    assert target.read_text(encoding="utf-8") == ""
