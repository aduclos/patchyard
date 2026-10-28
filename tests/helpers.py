"""Shared test fixtures: a minimal valid Config without touching /etc or the network."""
from fleet.config import Auth, Config, Controller, Inventory, Site


def make_config(**overrides) -> Config:
    cfg = Config(site=Site(url="https://patch.example.com"), auth=Auth(realm="CORP.EXAMPLE.COM"),
                 inventory=Inventory(managed_group="managed", report_only_group="report_only"),
                 controller=Controller(host="ctl.example.com"))
    for dotted, value in overrides.items():
        node = cfg
        *parents, leaf = dotted.split("__")
        for p in parents:
            node = getattr(node, p)
        setattr(node, leaf, value)
    return cfg
