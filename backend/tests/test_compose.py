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


def test_third_party_images_are_pinned_by_digest():
    for name in ("db", "caddy"):
        image = COMPOSE["services"][name]["image"]
        assert "@sha256:" in image and len(image.split("@sha256:")[1]) == 64, name


def test_caddy_limits_api_request_body():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text()
    api = caddyfile[caddyfile.index("handle @api") :]
    assert "max_size 11MB" in api[: api.index("reverse_proxy")]


def test_containers_have_memory_limits():
    limits = {name: service.get("mem_limit") for name, service in COMPOSE["services"].items()}
    assert limits == {"db": "512m", "migrate": "512m", "api": "1536m", "worker": "512m", "caddy": "128m"}


def test_containers_do_not_take_swap_beyond_memory_limit():
    for name in ("db", "api", "worker"):
        service = COMPOSE["services"][name]
        assert service["memswap_limit"] == service["mem_limit"], name
