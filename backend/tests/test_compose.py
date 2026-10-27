from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "deploy" / "docker-compose.yml").read_text())


def test_api_trusts_forwarded_address_only_from_caddy():
    command = COMPOSE["services"]["api"]["command"]
    assert "--proxy-headers" in command
    trusted = command[command.index("--forwarded-allow-ips") + 1]
    caddy = COMPOSE["services"]["caddy"]["networks"]["default"]["ipv4_address"]
    assert trusted == caddy == "${CADDY_IP:-172.31.250.10}"


def test_ports_bind_to_configurable_address_and_demo_is_local():
    assert COMPOSE["services"]["caddy"]["ports"] == ["${BIND_ADDR:-0.0.0.0}:80:80", "${BIND_ADDR:-0.0.0.0}:443:443"]
    assert all("ports" not in service for name, service in COMPOSE["services"].items() if name != "caddy")
    assert "BIND_ADDR=127.0.0.1" in (ROOT / ".env.demo.example").read_text().splitlines()
