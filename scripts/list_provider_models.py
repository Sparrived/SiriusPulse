#!/usr/bin/env python
"""
查询已配置供应商的可用模型列表

从 data/providers/provider_keys.json 读取已配置的供应商，
调用各供应商的 /v1/models 端点，打印可用模型列表及详细信息。

用法:
    python scripts/list_provider_models.py              # 查询所有已启用的供应商
    python scripts/list_provider_models.py --all        # 查询所有供应商（包括已禁用的）
    python scripts/list_provider_models.py --provider deepseek  # 仅查询指定供应商
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"


def load_providers(
    *, include_disabled: bool = False, only: str | None = None,
) -> list[tuple[str, str, str, str, bool]]:
    """
    加载已配置的供应商列表。

    Returns:
        [(provider_name, api_key, base_url, models_url, enabled), ...]
    """
    registry_path = DATA_DIR / "providers" / "provider_keys.json"
    if not registry_path.exists():
        print(f"错误: 未找到供应商配置文件 {registry_path}")
        return []

    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    providers = raw.get("providers", {})
    results: list[tuple[str, str, str, str, bool]] = []

    for name, payload in providers.items():
        if not isinstance(payload, dict):
            continue

        provider_name = str(payload.get("type", name)).strip()
        api_key = str(payload.get("api_key", "")).strip()
        enabled = bool(payload.get("enabled", True))
        base_url = str(payload.get("base_url", "")).strip()
        models_url = str(payload.get("models_url", "")).strip()

        if not api_key:
            continue
        if only and provider_name != only:
            continue
        if not include_disabled and not enabled:
            continue

        results.append((provider_name, api_key, base_url, models_url, enabled))

    return results


def fetch_models(url: str, api_key: str) -> list[dict]:
    """
    通过 OpenAI 兼容的 /v1/models 端点获取模型列表。

    Args:
        url: 完整的模型查询 URL
        api_key: API 密钥

    Returns:
        模型列表，每项包含 id, object, created, owned_by 等字段
    """
    try:
        import httpx
    except ImportError:
        print("错误: 需要安装 httpx，请运行: pip install httpx")
        sys.exit(1)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            if not resp.content.strip():
                print(f"  响应体为空 (状态码 {resp.status_code})")
                return []
            data = resp.json()
            return data.get("data", [])
    except httpx.HTTPStatusError as e:
        print(f"  HTTP 错误: {e.response.status_code} - {e.response.text[:200]}")
        return []
    except httpx.RequestError as e:
        print(f"  请求失败: {e}")
        return []
    except ValueError as e:
        print(f"  JSON 解析失败: {e}")
        return []


def format_model_info(model: dict) -> str:
    """格式化单个模型的信息"""
    parts: list[str] = []
    parts.append(f"  ID: {model.get('id', '未知')}")

    if model.get("owned_by"):
        parts.append(f"  所有者: {model['owned_by']}")
    if model.get("created"):
        import datetime
        ts = model["created"]
        try:
            dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
            parts.append(f"  创建时间: {dt.strftime('%Y-%m-%d %H:%M')}")
        except (ValueError, OSError):
            pass

    # 打印额外的非标准字段
    standard_keys = {"id", "object", "created", "owned_by"}
    extra = {k: v for k, v in model.items() if k not in standard_keys and v is not None}
    if extra:
        parts.append(f"  额外属性: {json.dumps(extra, ensure_ascii=False)}")

    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="查询已配置供应商的可用模型列表",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="查询所有供应商（包括已禁用的）",
    )
    parser.add_argument(
        "--provider", "-p",
        type=str,
        default=None,
        help="仅查询指定供应商（如 deepseek、aliyun-bailian）",
    )
    args = parser.parse_args()

    providers = load_providers(
        include_disabled=args.all,
        only=args.provider,
    )

    if not providers:
        print("未找到已配置的供应商，请先在配置文件中添加供应商信息。")
        return 1

    print(f"\n共找到 {len(providers)} 个供应商\n")
    print("=" * 70)

    for provider_name, api_key, base_url, models_url, enabled in providers:
        status = "✓" if enabled else "✗"
        masked_key = api_key[:4] + "****" if len(api_key) > 4 else "****"
        print(f"\n[{status}] {provider_name}")
        print(f"  地址: {base_url}")
        if models_url:
            print(f"  模型查询: {models_url}")
        print(f"  密钥: {masked_key}")
        print("-" * 70)

        if not base_url:
            print("  跳过: 未配置 base_url")
            continue

        query_url = models_url if models_url else f"{base_url.rstrip('/')}/models"
        print(f"  正在查询: {query_url}")
        models = fetch_models(query_url, api_key)

        if not models:
            print("  无可用模型或查询失败")
        else:
            print(f"  共 {len(models)} 个模型:\n")
            for model in sorted(models, key=lambda m: m.get("id", "")):
                print(format_model_info(model))
                print()

    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
