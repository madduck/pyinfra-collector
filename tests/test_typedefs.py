from pyinfra.facts import server

from pyinfra_collector.typedefs import fact_id


def test_fact_id_returns_class_name() -> None:
    assert fact_id(server.Uptime) == "server.Uptime"


def test_fact_id_passes_through_a_string() -> None:
    assert fact_id("server.Uptime") == "server.Uptime"
