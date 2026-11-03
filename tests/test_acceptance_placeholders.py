"""Every implemented B/R case maps to real tests; no pending cases remain."""

from tests.acceptance_cases import CASES, IMPLEMENTED, PENDING


def test_acceptance_checklist_covers_every_case() -> None:
    ids = [case.case_id for case in CASES]
    assert ids[:32] == [f"B{index:02d}" for index in range(1, 33)]
    assert ids[32:] == [f"R{index:02d}" for index in range(1, 25)]
    assert {case.case_id for case in IMPLEMENTED} == {
        *[f"R{index:02d}" for index in range(1, 10)],
        "R10",
        "R11",
        "R12",
        "R13",
        "R14",
        "R15",
        "R24",
        "R16",
        "R17",
        "R18",
        "R19",
        "R20",
        "R21",
        "R22",
        "R23",
        *[f"B{index:02d}" for index in range(1, 33)],
    }
    assert len(PENDING) == 0
    assert "R18" not in {case.case_id for case in PENDING}
    assert "R01" not in {case.case_id for case in PENDING}
    assert "B19" not in {case.case_id for case in PENDING}


def test_stage2_cases_map_to_real_tests():
    from tests import test_stage2

    for case in IMPLEMENTED:
        if case.stage == "2":
            matches = [
                name for name in vars(test_stage2) if name.startswith(f"test_{case.case_id}_")
            ]
            assert len(matches) == 1, case.case_id
            assert callable(getattr(test_stage2, matches[0]))


def test_stage3_cases_map_to_real_tests():
    from tests import test_stage3

    for case in IMPLEMENTED:
        if case.stage == "3":
            matches = [
                name for name in vars(test_stage3) if name.startswith(f"test_{case.case_id}_")
            ]
            assert matches, case.case_id
            assert all(callable(getattr(test_stage3, name)) for name in matches)
