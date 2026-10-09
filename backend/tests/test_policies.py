from app.domain.policies import VerdictFacts, enforce_pr_verdict, required_checks_status


def green_facts(**overrides) -> VerdictFacts:
    base = VerdictFacts(
        blocking_findings=0,
        coverage="complete",
        freshness="fresh",
        draft=False,
        mergeable=True,
        required_checks_configured=True,
        required_checks_passed=True,
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_hold_is_never_upgraded():
    verdict, _, _ = enforce_pr_verdict("hold", green_facts())
    assert verdict == "hold"


def test_insufficient_is_never_upgraded():
    verdict, _, _ = enforce_pr_verdict("insufficient_evidence", green_facts())
    assert verdict == "insufficient_evidence"


def test_recommend_merge_kept_only_all_green():
    verdict, hard, gaps = enforce_pr_verdict("recommend_merge", green_facts())
    assert verdict == "recommend_merge" and not hard and not gaps


def test_hard_blocks_downgrade_to_hold():
    for facts in (
        green_facts(blocking_findings=1),
        green_facts(mergeable=False),
        green_facts(required_checks_passed=False),
        green_facts(draft=True),
    ):
        verdict, hard, _ = enforce_pr_verdict("recommend_merge", facts)
        assert verdict == "hold" and hard


def test_information_gaps_downgrade_to_insufficient():
    for facts in (
        green_facts(coverage="partial"),
        green_facts(freshness="stale"),
        green_facts(mergeable=None),
        green_facts(required_checks_configured=False, required_checks_passed=None),
        green_facts(required_checks_passed=None),
    ):
        verdict, _, gaps = enforce_pr_verdict("recommend_merge", facts)
        assert verdict == "insufficient_evidence" and gaps


def test_hard_block_wins_over_gaps():
    verdict, hard, gaps = enforce_pr_verdict(
        "recommend_merge", green_facts(coverage="partial", blocking_findings=2)
    )
    assert verdict == "hold" and hard and gaps


def test_required_checks_status():
    assert required_checks_status([], []) == (False, None, [])
    configured, passed, detail = required_checks_status(
        ["ci", "lint"],
        [
            {"workflow": "ci", "status": "completed", "conclusion": "success", "started_at": "1"},
            {"workflow": "lint", "status": "completed", "conclusion": "failure", "started_at": "1"},
        ],
    )
    assert configured and passed is False
    assert any("lint" in item for item in detail)
    configured, passed, _ = required_checks_status(
        ["ci"],
        [
            {"workflow": "ci", "status": "completed", "conclusion": "failure", "started_at": "1"},
            {"workflow": "ci", "status": "completed", "conclusion": "success", "started_at": "2"},
        ],
    )
    assert configured and passed is True  # latest attempt wins
    configured, passed, detail = required_checks_status(["deploy"], [])
    assert configured and passed is False and any("无匹配运行" in item for item in detail)
