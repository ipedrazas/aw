import pytest

from wf.expr import ExprError, Path, evaluate, expressions_in, parse, render, walk
from wf.expr.evaluator import EvalError

STATE = {
    "inputs": {"topic": "durable execution", "depth": 1},
    "steps": {
        "review": {
            "output": {"verdict": "go_deeper", "followup_topics": [{"topic": "a"}, {"topic": "b"}]}
        },
        "go_deeper": {"outputs": [{"report": {"title": "A"}}, {"report": {"title": "B"}}]},
        "skipped": {"output": None},
    },
    "item": {"topic": "x"},
}


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ('steps.review.output.verdict == "go_deeper"', True),
        ('steps.review.output.verdict != "accept"', True),
        ("inputs.depth + 1", 2),
        ("inputs.depth < 2 and not (inputs.depth > 5)", True),
        ('steps.review.output.verdict == "accept" or inputs.depth == 1', True),
        ("steps.go_deeper.outputs[*].report.title", ["A", "B"]),
        ("steps.review.output.followup_topics[1].topic", "b"),
        ("steps.skipped.output.anything", None),
        ("steps.missing.output", None),
        ("item.topic", "x"),
        ("true", True),
        ("null == null", True),
        ("'single quoted'", "single quoted"),
    ],
)
def test_evaluate(src, expected):
    assert evaluate(parse(src), STATE) == expected


@pytest.mark.parametrize(
    "src",
    [
        "len(steps)",  # function call
        "steps.review.output.verdict = 1",  # assignment
        "inputs.depth * 2",  # unsupported operator
        "inputs.depth >= 2",  # not in the grammar
        "steps.review.output[verdict]",  # non-integer index
        "__import__('os')",
        "a.b.",
        "and",
        "steps.review.output.verdict == ",
    ],
)
def test_rejects_anything_outside_the_grammar(src):
    with pytest.raises(ExprError):
        parse(src)


def test_plus_only_adds_integers():
    with pytest.raises(EvalError):
        evaluate(parse("inputs.topic + 1"), STATE)
    with pytest.raises(EvalError):
        evaluate(parse("true + 1"), STATE)


def test_walk_reports_paths_and_comparisons():
    info = walk(parse('steps.review.output.verdict == "go_deeper" and inputs.depth < 2'))
    assert [str(p) for p in info.paths] == ["steps.review.output.verdict", "inputs.depth"]
    assert info.comparisons == [
        (Path(("steps", "review", "output", "verdict")), "==", "go_deeper"),
        (Path(("inputs", "depth")), "<", 2),
    ]


def test_templates_render_values_and_strings():
    assert render("${inputs.topic}", STATE) == "durable execution"
    assert (
        render("Report on ${inputs.topic}, depth ${inputs.depth + 1}", STATE)
        == "Report on durable execution, depth 2"
    )
    assert render({"a": ["${item.topic}"], "b": 3}, STATE) == {"a": ["x"], "b": 3}
    assert expressions_in({"a": "${x} and ${y}", "b": ["${z}"]}) == ["x", "y", "z"]
