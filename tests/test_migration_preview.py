"""Offline migration previews must never perform an import or hide ownership problems."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from summon.migration_preview import PreviewInputError, build_report, main


@pytest.fixture
def sample():
    catalog = [
        {
            "id": "01",
            "name": "Example A",
            "anime": "Example series",
            "rarity": "Legacy rare",
            "img_url": "https://example.test/a.webp",
        },
        {
            "id": "02",
            "name": "Example B",
            "anime": "Example series",
            "rarity": "Legacy rare",
            "img_url": "https://example.test/b.webp",
        },
    ]
    users = [
        {
            "id": 998877665544321,
            "username": "PRIVATE_USERNAME_CANARY",
            "favorites": ["01"],
            "balance": 900,
            "characters": [{"id": "01"}, {"id": "01"}, {"id": "02"}],
        },
        {"id": 223344556677889, "characters": [{"id": "02"}]},
    ]
    mapping = {
        "version": 1,
        "character_ids": {"01": "legacy-01", "02": "legacy-02"},
        "rarities": {"Legacy rare": "rare"},
    }
    return catalog, users, mapping


def test_preview_counts_copies_and_duplicate_quantities_without_claiming_import_readiness(sample):
    catalog, users, mapping = sample
    original = copy.deepcopy(sample)
    report = build_report(catalog, users, mapping)
    assert sample == original  # read-only, including nested arrays and metadata
    assert report["scope"] == "offline-preview-only"
    assert report["importer_available"] is False
    assert report["safe_to_import"] is False
    assert report["source_snapshot_verified"] is False
    assert report["has_issues"] is False
    assert report["counts"] == {
        "catalog_documents": 2,
        "catalog_ids": 2,
        "owner_documents": 2,
        "owner_ids": 2,
        "observed_owned_entries": 4,
        "valid_owner_character_entries": 4,
        "mapped_owned_entries": 4,
        "owner_character_pairs": 3,
        "observed_duplicate_copies": 1,
        "owner_documents_with_favorites": 1,
        "owner_documents_with_other_fields": 1,
    }
    assert not any(report["issues"].values())
    serialized = json.dumps(report)
    assert "details" not in report
    for private in ("998877665544321", "PRIVATE_USERNAME_CANARY", "legacy-01", "example.test"):
        assert private not in serialized


def test_private_details_are_explicit_and_preserve_exact_identities_and_quantities(sample):
    catalog, users, mapping = sample
    report = build_report(catalog, users, mapping, include_private_details=True)
    owners = report["details"]["owners"]
    assert [owner["user_id"] for owner in owners] == ["223344556677889", "998877665544321"]
    assert owners[1]["characters"] == [
        {"source_id": "01", "target_id": "legacy-01", "quantity": 2, "status": "mapped"},
        {"source_id": "02", "target_id": "legacy-02", "quantity": 1, "status": "mapped"},
    ]
    serialized = json.dumps(report)
    assert "PRIVATE_USERNAME_CANARY" not in serialized
    assert "example.test" not in serialized


def test_private_owner_ids_are_lossless_decimal_strings_even_above_javascript_precision(sample):
    catalog, users, mapping = sample
    users[0]["id"] = (1 << 63) - 1
    report = build_report(catalog, users, mapping, include_private_details=True)
    assert report["details"]["owners"][-1]["user_id"] == "9223372036854775807"
    assert (
        json.loads(json.dumps(report))["details"]["owners"][-1]["user_id"] == "9223372036854775807"
    )


def test_preview_is_deterministic_despite_input_record_order(sample):
    catalog, users, mapping = sample
    forward = build_report(catalog, users, mapping, include_private_details=True)
    backwards = build_report(catalog[::-1], users[::-1], mapping, include_private_details=True)
    assert forward == backwards


def test_duplicate_catalog_ids_and_duplicate_owner_documents_are_never_silently_merged(sample):
    catalog, users, mapping = sample
    catalog.append(copy.deepcopy(catalog[0]))
    users.append(copy.deepcopy(users[0]))
    report = build_report(catalog, users, mapping)
    assert report["has_issues"] is True
    assert report["issues"]["duplicate_catalog_ids"] == 1
    assert report["issues"]["duplicate_owner_ids"] == 1
    assert report["issues"]["ambiguous_catalog_character_copies"] == 4
    # No entries belonging to an ambiguous owner or catalogue character are marked mapped.
    assert report["counts"]["mapped_owned_entries"] == 1


def test_colliding_target_ids_block_all_affected_copies(sample):
    catalog, users, mapping = sample
    mapping["character_ids"]["02"] = "legacy-01"
    report = build_report(catalog, users, mapping, include_private_details=True)
    assert report["issues"]["colliding_target_ids"] == 1
    assert report["issues"]["colliding_character_copies"] == 4
    assert report["counts"]["mapped_owned_entries"] == 0
    assert all(
        character["status"] == "colliding_target"
        for owner in report["details"]["owners"]
        for character in owner["characters"]
    )


def test_unknown_unmapped_and_invalid_ownership_are_counted_without_assumptions(sample):
    catalog, users, mapping = sample
    del mapping["character_ids"]["02"]
    users[0]["characters"].extend([{"id": "missing"}, {"id": True}, {}, None])
    users.append({"id": "123", "characters": [{"id": "01"}]})
    users.append({"id": 11})
    report = build_report(catalog, users, mapping, include_private_details=True)
    issues = report["issues"]
    assert issues["unmapped_catalog_ids"] == 1
    assert issues["unmapped_character_copies"] == 2
    assert issues["unknown_character_copies"] == 1
    assert issues["invalid_owned_entries"] == 3
    assert issues["invalid_owner_ids"] == 1
    assert issues["unassigned_owner_entries"] == 1
    assert issues["missing_or_invalid_character_lists"] == 1
    assert report["counts"]["observed_owned_entries"] == 9
    assert report["counts"]["mapped_owned_entries"] == 2
    assert report["has_issues"] is True


def test_mapping_never_coerces_source_ids_or_silently_accepts_unsafe_targets(sample):
    catalog, users, mapping = sample
    catalog[0]["id"] = 1  # numeric and "01" are not interchangeable
    mapping["character_ids"]["02"] = "invalid/target"
    report = build_report(catalog, users, mapping)
    assert report["issues"]["invalid_catalog_ids"] == 1
    assert report["issues"]["unknown_character_copies"] == 2
    assert report["issues"]["invalid_target_mappings"] == 1
    assert report["issues"]["invalid_target_character_copies"] == 2
    assert report["counts"]["mapped_owned_entries"] == 0


def test_leading_zero_source_ids_stay_distinct(sample):
    catalog, users, mapping = sample
    renamed = {**catalog[0], "id": "1"}
    catalog.append(renamed)
    mapping["character_ids"]["1"] = "legacy-1"
    users[0]["characters"].append({"id": "1"})
    report = build_report(catalog, users, mapping, include_private_details=True)
    assert report["has_issues"] is False
    characters = report["details"]["owners"][1]["characters"]
    assert [(item["source_id"], item["target_id"], item["quantity"]) for item in characters] == [
        ("01", "legacy-01", 2),
        ("02", "legacy-02", 1),
        ("1", "legacy-1", 1),
    ]


def test_catalog_rarity_artwork_and_stale_embedded_metadata_are_flagged(sample):
    catalog, users, mapping = sample
    catalog[0]["rarity"] = "Unmapped legacy tier"
    catalog[0]["img_url"] = "https://user:password@example.test/private"
    catalog[1]["name"] = ""
    users[0]["characters"][0]["name"] = "Old name"
    report = build_report(catalog, users, mapping)
    assert report["issues"]["unmapped_rarity_documents"] == 1
    assert report["issues"]["invalid_artwork_documents"] == 1
    assert report["issues"]["invalid_name_or_category_documents"] == 1
    assert report["issues"]["embedded_metadata_mismatch_copies"] == 1
    assert "password" not in json.dumps(report)
    assert report["safe_to_import"] is False


def test_empty_sources_and_unused_mappings_require_review(sample):
    _, _, mapping = sample
    report = build_report([], [], mapping)
    assert report["issues"]["empty_catalog"] == 1
    assert report["issues"]["empty_owners"] == 1
    assert report["issues"]["unused_character_mappings"] == 2
    assert report["issues"]["unused_rarity_mappings"] == 1
    assert report["has_issues"] is True


@pytest.mark.parametrize(
    "bad_mapping",
    [
        {},
        {"version": True, "character_ids": {}, "rarities": {}},
        {"version": 2, "character_ids": {}, "rarities": {}},
        {"version": 1, "character_ids": [], "rarities": {}},
        {"version": 1, "character_ids": {}, "rarities": {}, "extra": "secret"},
    ],
)
def test_malformed_mapping_manifest_is_rejected_without_guessing(sample, bad_mapping):
    catalog, users, _ = sample
    with pytest.raises(PreviewInputError):
        build_report(catalog, users, bad_mapping)


def target_inventory_for_sample():
    return [
        {"user_id": 998877665544321, "character_id": "legacy-01", "quantity": 2},
        {"user_id": 998877665544321, "character_id": "legacy-02", "quantity": 1},
        {"user_id": 223344556677889, "character_id": "legacy-02", "quantity": 1},
        {"user_id": 555, "character_id": "unused", "quantity": 0},
    ]


def test_optional_target_snapshot_compares_owner_character_pairs_and_quantities(sample):
    catalog, users, mapping = sample
    target = target_inventory_for_sample()
    before = copy.deepcopy(target)
    report = build_report(catalog, users, mapping, target_inventory=target)
    assert target == before
    comparison = report["target_comparison"]
    assert comparison["performed"] is True
    assert comparison["reason"] is None
    assert comparison["counts"] == {
        "target_documents": 4,
        "valid_target_documents": 4,
        "target_pairs": 4,
        "zero_quantity_documents": 1,
        "observed_target_copies": "4",
        "matched_pairs": 3,
        "missing_target_pairs": 0,
        "quantity_mismatch_pairs": 0,
        "unexpected_target_pairs": 0,
        "missing_copies": "0",
        "excess_copies": "0",
    }
    assert not any(comparison["issues"].values())
    assert report["has_issues"] is False
    assert report["safe_to_import"] is False  # a match is not cutover approval
    assert "998877665544321" not in json.dumps(report)


def test_target_mismatches_report_missing_and_extra_copies_without_losing_identity(sample):
    catalog, users, mapping = sample
    target = [
        {"user_id": 998877665544321, "character_id": "legacy-01", "quantity": 1},
        {"user_id": 223344556677889, "character_id": "legacy-02", "quantity": 3},
        {"user_id": 555, "character_id": "unexpected", "quantity": 5},
    ]
    report = build_report(
        catalog, users, mapping, target_inventory=target, include_private_details=True
    )
    comparison = report["target_comparison"]
    assert comparison["performed"] is True
    assert comparison["counts"]["matched_pairs"] == 0
    assert comparison["counts"]["missing_target_pairs"] == 1
    assert comparison["counts"]["quantity_mismatch_pairs"] == 2
    assert comparison["counts"]["unexpected_target_pairs"] == 1
    assert comparison["counts"]["missing_copies"] == "2"
    assert comparison["counts"]["excess_copies"] == "7"
    assert report["has_issues"] is True
    details = report["details"]["comparison"]
    assert {entry["status"] for entry in details} == {"quantity_mismatch", "missing", "unexpected"}
    assert all(isinstance(entry["user_id"], str) for entry in details)
    assert all(isinstance(entry["target_quantity"], str) for entry in details)
    assert all(isinstance(entry["expected_quantity"], str) for entry in details)


@pytest.mark.parametrize(
    ("rows", "issue"),
    [
        ([{"user_id": 1, "character_id": "a", "quantity": 1}] * 2, "duplicate_target_pairs"),
        ([{"user_id": True, "character_id": "a", "quantity": 1}], "invalid_target_user_ids"),
        ([{"user_id": 1, "character_id": "a/b", "quantity": 1}], "invalid_target_character_ids"),
        ([{"user_id": 1, "character_id": "a", "quantity": -1}], "invalid_target_quantities"),
        ([{"user_id": 1, "character_id": "a", "quantity": True}], "invalid_target_quantities"),
        ([{"user_id": 1, "character_id": "a", "quantity": "1"}], "invalid_target_quantities"),
        (["not an object"], "invalid_target_documents"),
    ],
)
def test_ambiguous_or_invalid_target_snapshot_blocks_comparison(sample, rows, issue):
    catalog, users, mapping = sample
    report = build_report(catalog, users, mapping, target_inventory=rows)
    comparison = report["target_comparison"]
    assert comparison["performed"] is False
    assert comparison["reason"] == "target_issues"
    assert comparison["issues"][issue] == 1
    assert comparison["counts"]["matched_pairs"] is None
    assert comparison["counts"]["missing_copies"] is None
    assert report["has_issues"] is True


def test_source_ownership_ambiguity_blocks_target_comparison_instead_of_false_differences(sample):
    catalog, users, mapping = sample
    users.append(copy.deepcopy(users[0]))
    report = build_report(catalog, users, mapping, target_inventory=target_inventory_for_sample())
    assert report["target_comparison"]["performed"] is False
    assert report["target_comparison"]["reason"] == "source_issues"
    assert report["target_comparison"]["counts"]["unexpected_target_pairs"] is None
    assert report["has_issues"] is True


def test_metadata_warnings_do_not_prevent_separate_ownership_comparison(sample):
    catalog, users, mapping = sample
    catalog[0]["img_url"] = "not-an-https-image"
    report = build_report(catalog, users, mapping, target_inventory=target_inventory_for_sample())
    assert report["issues"]["invalid_artwork_documents"] == 1
    assert report["target_comparison"]["performed"] is True
    assert report["target_comparison"]["counts"]["matched_pairs"] == 3
    assert report["has_issues"] is True
    assert report["safe_to_import"] is False


def test_target_comparison_quantities_match_exhaustive_small_cases(sample):
    from itertools import product

    catalog, _, mapping = sample
    # Compare every combination of 0–2 copies for two owners, plus one target-only owner.
    for first, second, target_first, target_second, target_extra in product(range(3), repeat=5):
        users = [
            {"id": 1, "characters": [{"id": "01"}] * first},
            {"id": 2, "characters": [{"id": "01"}] * second},
        ]
        target = [
            {"user_id": 1, "character_id": "legacy-01", "quantity": target_first},
            {"user_id": 2, "character_id": "legacy-01", "quantity": target_second},
            {"user_id": 3, "character_id": "legacy-01", "quantity": target_extra},
        ]
        report = build_report(catalog, users, mapping, target_inventory=target)
        counts = report["target_comparison"]["counts"]
        assert counts["missing_copies"] == str(
            max(0, first - target_first) + max(0, second - target_second)
        )
        assert counts["excess_copies"] == str(
            max(0, target_first - first) + max(0, target_second - second) + target_extra
        )
        assert report["target_comparison"]["performed"] is True


def test_target_quantity_totals_are_lossless_strings_for_large_integer_values(sample):
    catalog, users, mapping = sample
    rows = [{"user_id": 1, "character_id": "other", "quantity": (1 << 63) - 1}]
    report = build_report(
        catalog, users, mapping, target_inventory=rows, include_private_details=True
    )
    assert report["target_comparison"]["counts"]["observed_target_copies"] == "9223372036854775807"
    assert report["target_comparison"]["counts"]["excess_copies"] == "9223372036854775807"
    assert report["details"]["comparison"][0]["target_quantity"] == "9223372036854775807"


def write_input_files(tmp_path: Path, sample):
    catalog, users, mapping = sample
    paths = [tmp_path / "catalog.jsonl", tmp_path / "users.jsonl", tmp_path / "mapping.json"]
    paths[0].write_text("\n".join(json.dumps(row) for row in catalog) + "\n", encoding="utf-8")
    paths[1].write_text("\n".join(json.dumps(row) for row in users) + "\n", encoding="utf-8")
    paths[2].write_text(json.dumps(mapping), encoding="utf-8")
    return paths


def cli_args(paths, *extra):
    return [
        "--catalog",
        str(paths[0]),
        "--owners",
        str(paths[1]),
        "--mapping",
        str(paths[2]),
        *extra,
    ]


def test_cli_reads_only_local_files_emits_redacted_summary_and_performs_no_network(
    sample, tmp_path, capsys, monkeypatch
):
    paths = write_input_files(tmp_path, sample)
    before = [path.read_bytes() for path in paths]

    def no_network(*args, **kwargs):
        raise AssertionError("preview must not create network sockets")

    monkeypatch.setattr("socket.socket", no_network)
    assert main(cli_args(paths)) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["counts"]["observed_owned_entries"] == 4
    assert "PRIVATE_USERNAME_CANARY" not in output.out + output.err
    assert "998877665544321" not in output.out + output.err
    assert "legacy-01" not in output.out + output.err
    assert [path.read_bytes() for path in paths] == before
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(path.name for path in paths)


def test_module_cli_runs_as_a_separate_process_on_synthetic_files(sample, tmp_path):
    import subprocess
    import sys

    paths = write_input_files(tmp_path, sample)
    result = subprocess.run(  # noqa: S603 - fixed executable and local synthetic fixture paths
        [sys.executable, "-m", "summon.migration_preview", *cli_args(paths)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["safe_to_import"] is False
    assert "PRIVATE_USERNAME_CANARY" not in result.stdout + result.stderr
    assert "998877665544321" not in result.stdout + result.stderr


def test_cli_can_compare_a_separate_target_export_without_writing_files(sample, tmp_path, capsys):
    paths = write_input_files(tmp_path, sample)
    target = tmp_path / "target.jsonl"
    target.write_text(
        "\n".join(json.dumps(row) for row in target_inventory_for_sample()) + "\n",
        encoding="utf-8",
    )
    before = [path.read_bytes() for path in [*paths, target]]
    assert main(cli_args(paths, "--target-inventory", str(target))) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["target_comparison"]["counts"]["matched_pairs"] == 3
    assert "998877665544321" not in output.out + output.err
    assert [path.read_bytes() for path in [*paths, target]] == before


def test_cli_supports_canonical_extended_json_64bit_owner_ids(sample, tmp_path, capsys):
    paths = write_input_files(tmp_path, sample)
    paths[1].write_text(
        '{"_id":{"$oid":"64d01afc5e2a36a5a33f0001"},'
        '"id":{"$numberLong":"998877665544321"},"characters":[{"id":"01"}]}\n',
        encoding="utf-8",
    )
    assert main(cli_args(paths, "--include-private-details")) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["details"]["owners"][0]["user_id"] == "998877665544321"
    assert report["counts"]["mapped_owned_entries"] == 1
    assert "private" in output.err.lower()


def test_cli_issues_return_nonzero_without_outputting_source_fields(sample, tmp_path, capsys):
    paths = write_input_files(tmp_path, sample)
    paths[1].write_text(
        '{"id":123,"username":"PRIVATE_USERNAME_CANARY","characters":[{"id":"UNKNOWN_CHARACTER_CANARY"}]}\n',
        encoding="utf-8",
    )
    assert main(cli_args(paths)) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["issues"]["unknown_character_copies"] == 1
    assert "PRIVATE_USERNAME_CANARY" not in output.out + output.err
    assert "UNKNOWN_CHARACTER_CANARY" not in output.out + output.err


@pytest.mark.parametrize(
    "bad_line",
    [
        '{"id":"01","id":"02"}\n',
        '{"id":"01","characters":[}\n',
        '{"id":{"$numberLong":"PRIVATE_CANARY"}}\n',
        '{"id":NaN}\n',
        b"\xff\n",
    ],
)
def test_invalid_jsonl_fails_closed_without_leaking_contents(sample, tmp_path, capsys, bad_line):
    paths = write_input_files(tmp_path, sample)
    if isinstance(bad_line, bytes):
        paths[1].write_bytes(bad_line)
    else:
        paths[1].write_text(bad_line, encoding="utf-8")
    assert main(cli_args(paths)) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "PRIVATE_CANARY" not in output.err
    assert str(tmp_path) not in output.err


def test_missing_file_and_bad_cli_options_do_not_echo_paths_or_arguments(sample, tmp_path, capsys):
    paths = write_input_files(tmp_path, sample)
    paths[1] = tmp_path / "PRIVATE_PATH_CANARY"
    assert main(cli_args(paths)) == 2
    output = capsys.readouterr()
    assert "PRIVATE_PATH_CANARY" not in output.err
    with pytest.raises(SystemExit) as exc:
        main(["--bogus", "PRIVATE_ARGUMENT_CANARY"])
    assert exc.value.code == 2
    assert "PRIVATE_ARGUMENT_CANARY" not in capsys.readouterr().err


def test_preview_has_explicit_bounded_work_limits(sample, monkeypatch):
    import summon.migration_preview as preview

    catalog, users, mapping = sample
    monkeypatch.setattr(preview, "MAX_OWNED_ENTRIES", 3)
    with pytest.raises(PreviewInputError):
        build_report(catalog, users, mapping)


@pytest.mark.parametrize(
    "limit_name", ["MAX_JSONL_LINE_BYTES", "MAX_JSONL_FILE_BYTES", "MAX_MAPPING_BYTES"]
)
def test_oversized_local_inputs_fail_closed(sample, tmp_path, capsys, monkeypatch, limit_name):
    import summon.migration_preview as preview

    paths = write_input_files(tmp_path, sample)
    monkeypatch.setattr(preview, limit_name, 16)
    assert main(cli_args(paths)) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert str(tmp_path) not in output.err


@pytest.mark.parametrize(
    "limit_name", ["MAX_CATALOG_DOCUMENTS", "MAX_OWNER_DOCUMENTS", "MAX_TARGET_DOCUMENTS"]
)
def test_record_limits_fail_closed_without_a_partial_report(sample, monkeypatch, limit_name):
    import summon.migration_preview as preview

    catalog, users, mapping = sample
    if limit_name == "MAX_CATALOG_DOCUMENTS":
        catalog.append(copy.deepcopy(catalog[0]))
    if limit_name == "MAX_OWNER_DOCUMENTS":
        users.append(copy.deepcopy(users[0]))
    monkeypatch.setattr(preview, limit_name, 2)
    with pytest.raises(PreviewInputError):
        build_report(catalog, users, mapping, target_inventory=target_inventory_for_sample())


def test_cli_rejects_symlinks_and_nonregular_files_without_opening_pipes(sample, tmp_path, capsys):
    import os

    paths = write_input_files(tmp_path, sample)
    alias = tmp_path / "catalog-link"
    alias.symlink_to(paths[0])
    assert main(cli_args([alias, paths[1], paths[2]])) == 2
    assert capsys.readouterr().out == ""
    fifo = tmp_path / "owners-pipe"
    os.mkfifo(fifo)
    assert main(cli_args([paths[0], fifo, paths[2]])) == 2
    assert capsys.readouterr().out == ""


def test_input_modified_during_read_fails_without_publishing_partial_report(
    sample, tmp_path, capsys, monkeypatch
):
    import summon.migration_preview as preview

    paths = write_input_files(tmp_path, sample)
    real_parse = preview._parse_json
    changed = False

    def modify_during_parse(data, *, extended):
        nonlocal changed
        result = real_parse(data, extended=extended)
        if extended and not changed:
            with paths[0].open("ab") as source:
                source.write(b"\n")
            changed = True
        return result

    monkeypatch.setattr(preview, "_parse_json", modify_during_parse)
    assert main(cli_args(paths)) == 2
    assert changed
    output = capsys.readouterr()
    assert output.out == ""
    assert str(tmp_path) not in output.err


def test_duplicate_mapping_keys_are_rejected_before_any_report(sample, tmp_path, capsys):
    paths = write_input_files(tmp_path, sample)
    paths[2].write_text(
        '{"version":1,"character_ids":{"01":"a","01":"b"},"rarities":{}}', encoding="utf-8"
    )
    assert main(cli_args(paths)) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert str(tmp_path) not in output.err
