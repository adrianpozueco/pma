from scripts.build_faa_sdr_reports import build_row, normalise_date, normalise_timestamp, parse_counter


def test_dates_are_flagged_without_coercion():
    assert normalise_date("05/22/2023") == ("2023-05-22", "valid")
    assert normalise_date("31/99/2023") == ("", "invalid")
    assert normalise_timestamp("2022-12-06T15:35:35.340-05:00")[1] == "valid"
    assert normalise_timestamp("not-a-timestamp") == ("", "invalid")
    assert normalise_timestamp("2023-01-02T10:00:00") == ("", "missing_timezone")


def test_counter_parser_does_not_round_invalid_values():
    assert parse_counter("1391") == ("1391", "valid")
    assert parse_counter("1391.0") == ("1391", "valid_integer_decimal")
    assert parse_counter("1391.5") == ("", "invalid")
    assert parse_counter("") == ("", "missing")
    assert parse_counter(str(2**63)) == ("", "invalid")


def test_full_report_keeps_both_part_roles_and_raw_counter():
    row = build_row(
        {
            "OperatorControlNumber": "R-1",
            "DifficultyDate": "05/22/2023",
            "SubmissionDate": "2022-12-06T15:35:35.340-05:00",
            "PartNumber": "2085M31G03",
            "ComponentPartNumber": "2085M31G03",
            "AircraftTotalCycles": "1,391",
            "PartTotalCycles": "1391.5",
            "Discrepancy": "reported symptom",
        },
        "2023",
        "SDR-2023.csv",
        1,
        source_snapshot_hash="a" * 64,
    )

    assert row["PartNumber"] == "2085M31G03"
    assert row["ComponentPartNumber"] == "2085M31G03"
    assert row["AircraftTotalCycles"] == "1,391"
    assert row["aircraft_total_cycles_parse_status"] == "invalid"
    assert row["part_total_cycles_value"] == ""
    assert row["part_total_cycles_parse_status"] == "invalid"
    assert row["source_year"] == "2023"
    assert row["source_row_number"] == "1"
    assert row["source_snapshot_hash"] == "a" * 64
