"""The one and only test. conftest.py runs it once for every row of your sheet, in the order the brain planned."""
import pytest

from restora import allure_support


def test_api(row, session, input_name, request):
    record = session.run_row(row)
    request.node._restora_result = record.result
    allure_support.annotate(record, input_name)

    if record.result == "SKIPPED":
        pytest.skip(record.failures[0])
    if record.result == "FAILED":
        header = f"Row {record.sheet_row}: {record.method} {record.url}"
        why = f"\n  In plain English: {record.explanation}" if record.explanation else ""
        pytest.fail(header + "\n  - " + "\n  - ".join(record.failures) + why, pytrace=False)
    if record.result == "HEALED" and session.settings.heal.count_healed_as == "fail":
        pytest.fail(f"Row {record.sheet_row} only passed after healing: "
                    + " | ".join(a["reason"] for a in record.heal_attempts), pytrace=False)
