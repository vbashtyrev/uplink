"""netbox_checks _print_combined_table and _row_to_dict."""

import netbox_checks as nc


def _sample_row():
    return (
        "dev1",
        "intF",
        "intN",
        "5",
        "descF",
        "descN",
        "6",
        "mtF",
        "mtN",
        "7",
        "mtToSet",
        100,
        200,
        "10",
        "dupF",
        "dupN",
        "11",
        "macF",
        "macN",
        "12",
        1500,
        1600,
        "13",
        1.0,
        2.0,
        "14",
        "descToSet",
        "speedToSet",
        "dupToSet",
        "mtuToSet",
        "txpToSet",
        "fwdF",
        "fwdN",
        "15",
        "fwdToSet",
        "ipF",
        "ipN",
        "ipVrfF",
        "ipVrfN",
        "17",
        "lagF",
        "lagN",
        "18",
        "parentF",
        "parentN",
        "19",
    )


def test_print_combined_table(capsys):
    args = type(
        "A",
        (),
        {
            "intname": True,
            "description": True,
            "mediatype": False,
            "bandwidth": True,
            "duplex": False,
            "mac": False,
            "mtu": False,
            "tx_power": False,
            "forwarding_model": False,
            "ip_address": False,
            "lag": False,
            "parent": False,
            "show_change": False,
        },
    )()
    col_spec = nc._build_col_spec(args)
    nc._print_combined_table([_sample_row()], {5, 6}, col_spec)
    out = capsys.readouterr().out
    assert "dev1" in out
    assert "intF" in out


def test_row_to_dict():
    args = type(
        "A",
        (),
        {
            "intname": True,
            "description": True,
            "mediatype": False,
            "bandwidth": False,
            "duplex": False,
            "mac": False,
            "mtu": False,
            "tx_power": False,
            "forwarding_model": False,
            "ip_address": False,
            "lag": False,
            "parent": False,
            "show_change": False,
        },
    )()
    col_spec = nc._build_col_spec(args)
    d = nc._row_to_dict(_sample_row(), col_spec)
    assert d["name"] == "dev1"
