"""netbox_checks table column filters."""

import netbox_checks as nc


class _Args:
    intname = True
    description = True
    mediatype = True
    bandwidth = True
    duplex = True
    mac = False
    mtu = True
    tx_power = True
    forwarding_model = True
    ip_address = False
    lag = True
    parent = True
    show_change = True


def test_filter_empty_and_no_diff_cols():
    args = _Args()
    col_spec = nc._build_col_spec(args)
    rows = [
        ("h", "i", "i", "", "d1", "d1", "", "mt", "mt", "", "", 0, 0, "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""),
    ]
    filtered = nc._filter_empty_note_cols(col_spec, rows)
    assert len(filtered) <= len(col_spec)
    no_diff = nc._filter_no_diff_cols(col_spec, rows)
    assert len(no_diff) <= len(col_spec)


def test_row_has_diff():
    row = [""] * 46
    row[6] = 5
    assert nc._row_has_diff(tuple(row)) is True
    assert nc._row_has_diff(tuple([""] * 46)) is False
