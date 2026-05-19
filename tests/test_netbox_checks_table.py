"""netbox_checks table output helpers."""

import netbox_checks as nc


def test_row_has_diff_and_filters():
    row_match = (
        "dev",
        "intF",
        "intN",
        "",
        "descF",
        "descN",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "",
    )
    row_diff = list(row_match)
    row_diff[6] = 5  # nD diff code
    assert nc._row_has_diff(tuple(row_diff)) is True
    assert nc._row_has_diff(tuple(row_match)) is False

    col_spec = [("name", 0, "Name"), ("note", 2, "Note")]
    filtered = nc._filter_empty_note_cols(col_spec, [row_diff])
    assert filtered


def test_build_col_spec_all_flags():
    args = type(
        "Args",
        (),
        {
            "intname": True,
            "description": True,
            "mediatype": True,
            "bandwidth": True,
            "duplex": True,
            "mac": True,
            "mtu": True,
            "tx_power": True,
            "forwarding_model": True,
            "ip_address": True,
            "lag": True,
            "parent": True,
            "show_change": False,
        },
    )()
    spec = nc._build_col_spec(args)
    assert any(c[0] == "name" for c in spec)
