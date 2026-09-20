from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_compose_contains_complete_private_runtime():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert {"postgres", "redis", "chroma", "migrate", "init-admin", "mcp", "worker", "api"} <= set(services)
    published = {name for name, service in services.items() if service.get("ports")}
    assert published == {"api"}
    assert services["api"]["ports"] == ["127.0.0.1:${DOCKER_APP_PORT:-18000}:8000"]
    for name in ["postgres", "redis", "chroma", "mcp", "api"]:
        assert "healthcheck" in services[name]


def test_compose_uses_persistent_volumes_and_admin_secret():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert {"postgres_data", "redis_data", "chroma_data", "app_data"} <= set(compose["volumes"])
    assert compose["secrets"]["admin_password"]["file"] == "./var/secrets/docker-admin-initial-password"
    assert compose["services"]["init-admin"]["secrets"] == ["admin_password"]


def test_seetacloud_compose_uses_host_network_and_private_ports():
    compose = yaml.safe_load((ROOT / "docker-compose.seetacloud.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert all(service.get("network_mode") == "host" for service in services.values())
    assert all(not service.get("ports") for service in services.values())
    assert "listen_addresses=127.0.0.1" in services["postgres"]["command"]
    assert "--bind 127.0.0.1" in services["redis"]["command"][-1]
    assert services["api"]["healthcheck"]["test"][-1].find("127.0.0.1:18000") >= 0


def test_docker_build_does_not_copy_runtime_secrets():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored and ".env.*" in ignored and "var" in ignored
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER bankapp" in dockerfile
    assert "DASHSCOPE_API_KEY" not in dockerfile
