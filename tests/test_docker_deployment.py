from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_dockerfile_reuses_the_complete_environment_before_application_source():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim AS environment" in dockerfile
    assert "ARG SIRIUS_BROWSER_CACHE_IMAGE=browser-empty" in dockerfile
    assert "FROM ${SIRIUS_BROWSER_CACHE_IMAGE} AS browser-cache" in dockerfile
    assert "COPY --from=browser-cache /ms-playwright/ /ms-playwright/" in dockerfile
    assert "FROM ${SIRIUS_ENV_CACHE_IMAGE} AS runtime" in dockerfile
    runtime = dockerfile[dockerfile.index("FROM ${SIRIUS_ENV_CACHE_IMAGE} AS runtime") :]
    assert "COPY pyproject.toml uv.lock README.md ./" in runtime
    assert "uv sync --frozen --no-dev --no-install-project" in runtime
    assert dockerfile.index("playwright install --with-deps chromium") < dockerfile.index(
        "sirius_pulse ./sirius_pulse"
    )
    assert "chown sirius:sirius /app" in dockerfile
    assert dockerfile.index(
        "rm -rf /app/sirius_pulse /app/sirius_pulse.egg-info"
    ) < dockerfile.index("sirius_pulse ./sirius_pulse")


def test_update_script_refuses_to_replace_an_unmigrated_container_data_directory():
    script = (ROOT / "scripts" / "update-container.sh").read_text(encoding="utf-8")

    assert "docker container inspect sirius-pulse-v2-test" in script
    assert "docker image inspect sirius-pulse:latest" in script
    assert "export SIRIUS_ENV_CACHE_KEY=" in script
    assert "export SIRIUS_BROWSER_CACHE_IMAGE=browser-empty" in script
    assert "export SIRIUS_BROWSER_CACHE_IMAGE=sirius-pulse:latest" in script
    assert "test -d /ms-playwright" in script
    assert "export SIRIUS_ENV_CACHE_IMAGE=sirius-pulse:latest" in script
    assert script.index("test -d /ms-playwright") < script.index("docker compose up -d")
    assert '\\"org.sirius-pulse.environment-cache-key\\"' not in script
    assert "exit 2" in script
    assert "systemctl restart sirius-container-admin" in script
    assert "git -c fetch.recurseSubmodules=false pull --ff-only origin master" in script
    assert "submodule sync --recursive" in script
    assert script.index("submodule sync --recursive") < script.index(
        "submodule update --init --recursive"
    )
    assert script.index("docker compose config -q") < script.index("docker compose up -d")


def test_update_script_restores_persistent_system_package_manifests():
    script = (ROOT / "scripts" / "update-container.sh").read_text(encoding="utf-8")

    assert "data/runtime-packages/apt.txt" in script
    assert "data/runtime-packages/yum.txt" in script
    assert "docker compose exec -T --user root sirius-pulse" in script
    assert "apt-get update" in script
    assert "apt-get install -y --no-install-recommends" in script
    assert "yum install -y" in script
    assert "无效的系统包名" in script
    assert script.index("docker compose up -d") < script.index("if ! restore_system_packages")


def test_deployment_guide_uses_the_single_update_script_path():
    guide = (ROOT / "docs" / "guide" / "docker-deployment.md").read_text(encoding="utf-8")

    assert "bash /root/SiriusPulse/scripts/update-container.sh" in guide


def test_environment_cache_label_is_stamped_in_the_environment_stage():
    """缓存标签必须盖在装系统包的那一层，而不是 runtime 层。

    盖在 runtime 层时，标签值取自本次构建传入的键（即当前 Dockerfile 哈希），
    于是每次构建后标签都恰好等于当前哈希、判等永远成立：旧镜像一旦被复用就再也
    换不掉，environment 阶段不再进入构建图，apt 装的东西（如 gh/git）永远装不上。
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    environment = dockerfile[
        dockerfile.index("FROM python:3.12-slim AS environment") : dockerfile.index(
            "FROM ${SIRIUS_ENV_CACHE_IMAGE} AS runtime"
        )
    ]
    runtime = dockerfile[dockerfile.index("FROM ${SIRIUS_ENV_CACHE_IMAGE} AS runtime") :]

    assert "LABEL org.sirius-pulse.environment-cache-key=$SIRIUS_ENV_CACHE_KEY" in environment
    assert "LABEL org.sirius-pulse.environment-cache-key" not in runtime
    # 标签所在的环境阶段必须真的包含装包动作，否则键变化也换不来新包。
    assert "apt-get install -y --no-install-recommends procps git gh" in environment


def test_update_script_does_not_treat_a_missing_cache_label_as_a_hit():
    """没有缓存标签的镜像不能算命中。

    旧镜像不带标签，而"标签为空"一度被当作"键相同"接受，导致环境阶段的改动被
    永久跳过——镜像里缺的系统包在任何一次部署中都不会被补上。
    """
    script = (ROOT / "scripts" / "update-container.sh").read_text(encoding="utf-8")

    assert '-z "$current_environment_key"' not in script
    assert '-n "$current_environment_key"' in script
