from packages.qbr_core.application.runtime import answer_metrics


def test_answer_metrics_aggregate_planner_and_answer_usage() -> None:
    metrics = answer_metrics(
        created_at="2026-08-11T12:00:00.000Z",
        completed_at="2026-08-11T12:00:02.345Z",
        planner_diagnostics={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        answer_model={"input_tokens": 100, "output_tokens": 40, "total_tokens": 140},
    )

    assert metrics["duration_ms"] == 2345
    assert metrics["token_usage"] == {
        "input_tokens": 110,
        "output_tokens": 60,
        "total_tokens": 170,
    }
    assert metrics["components"]["planner"]["total_tokens"] == 30
    assert metrics["components"]["answer"]["total_tokens"] == 140
