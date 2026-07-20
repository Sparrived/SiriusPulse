from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_dockerfile_reuses_the_complete_environment_before_application_source():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim AS environment" in dockerfile
    assert "FROM ${SIRIUS_ENV_CACHE_IMAGE} AS runtime" in dockerfile
    assert dockerfile.index("playwright install --with-deps chromium") < dockerfile.index(
        "sirius_pulse ./sirius_pulse"
    )
    assert dockerfile.index("rm -rf /app/sirius_pulse /app/sirius_pulse.egg-info") < dockerfile.index(
        "sirius_pulse ./sirius_pulse"
    )


def test_update_script_refuses_to_replace_an_unmigrated_container_data_directory():
    script = (ROOT / "scripts" / "update-container.sh").read_text(encoding="utf-8")

    assert "docker container inspect sirius-pulse-v2-test" in script
    assert "docker image inspect sirius-pulse:latest" in script
    assert "export SIRIUS_ENV_CACHE_KEY=" in script
    assert "export SIRIUS_ENV_CACHE_IMAGE=sirius-pulse:latest" in script
    assert '\\"org.sirius-pulse.environment-cache-key\\"' not in script
    assert "exit 2" in script
    assert script.index("docker compose config -q") < script.index("docker compose up -d")


def test_deployment_guide_uses_the_single_update_script_path():
    guide = (ROOT / "docs" / "guide" / "docker-deployment.md").read_text(encoding="utf-8")

    assert "bash /root/SiriusPulse/scripts/update-container.sh" in guide
