#!/usr/bin/env python3
"""Run a small Alibaba Cloud Model Studio model smoke-test matrix."""

from __future__ import annotations

import argparse
import binascii
import http.client
import json
import math
import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from verify_bailian_api import create_ssl_context, load_api_key, redact, validate_base_url

DEFAULT_BASE_URL = "https://ws-iuzncg7jj8e6onlg.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(slots=True)
class Result:
    category: str
    model: str
    ok: bool
    status: int | None
    ttfb_ms: float | None
    total_ms: float
    detail: str
    request_id: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测试百炼文本、Embedding 与视觉多模态模型矩阵。")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--no-prompt", action="store_true")
    return parser.parse_args()


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def make_test_png() -> bytes:
    """Create a 64x32 PNG whose left half is red and right half is blue."""
    width, height = 64, 32
    rows = []
    for _row in range(height):
        pixels = b"".join(b"\xff\x00\x00" if column < width // 2 else b"\x00\x00\xff" for column in range(width))
        rows.append(b"\x00" + pixels)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + png_chunk(b"IEND", b"")
    )


def data_url() -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(make_test_png()).decode("ascii")


def post_json(
    *, host: str, port: int, path: str, api_key: str, payload: dict[str, Any], timeout: float
) -> tuple[int, dict[str, Any] | None, str, float, float, str | None]:
    connection = http.client.HTTPSConnection(host, port=port, timeout=timeout, context=create_ssl_context())
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    started = time.perf_counter()
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "bailian-model-suite/1.0",
            },
        )
        response = connection.getresponse()
        ttfb_ms = (time.perf_counter() - started) * 1000
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        total_ms = (time.perf_counter() - started) * 1000
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("响应超过 1 MiB 安全上限")
        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        request_id = response.getheader("x-request-id") or response.getheader("x-dashscope-request-id")
        return response.status, data, text, ttfb_ms, total_ms, request_id
    finally:
        connection.close()


def error_detail(data: dict[str, Any] | None, text: str, api_key: str) -> str:
    if data:
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or error.get("type")
            if message:
                return redact(str(message), api_key)[:300]
        message = data.get("message") or data.get("code")
        if message:
            return redact(str(message), api_key)[:300]
    return redact(text.strip(), api_key)[:300] or "空响应"


def chat_parser(data: dict[str, Any]) -> str:
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return f"响应={str(content)[:100]!r}; tokens={usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')}"


def embedding_parser(data: dict[str, Any]) -> str:
    vectors = [item["embedding"] for item in data["data"]]
    if not vectors or not all(isinstance(value, int | float) and math.isfinite(value) for vector in vectors for value in vector):
        raise ValueError("Embedding 向量为空或包含非有限值")
    dimensions = {len(vector) for vector in vectors}
    norms = [math.sqrt(sum(float(value) ** 2 for value in vector)) for vector in vectors]
    return f"向量数={len(vectors)}; 维度={sorted(dimensions)}; L2范数={[round(value, 4) for value in norms]}"


def run_case(
    *,
    category: str,
    model: str,
    path: str,
    payload: dict[str, Any],
    parser: Callable[[dict[str, Any]], str],
    host: str,
    port: int,
    api_key: str,
    timeout: float,
) -> Result:
    started = time.perf_counter()
    try:
        status, data, text, ttfb_ms, total_ms, request_id = post_json(
            host=host, port=port, path=path, api_key=api_key, payload=payload, timeout=timeout
        )
        if not 200 <= status < 300 or data is None:
            return Result(category, model, False, status, ttfb_ms, total_ms, error_detail(data, text, api_key), request_id)
        return Result(category, model, True, status, ttfb_ms, total_ms, parser(data), request_id)
    except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException) as exc:
        return Result(
            category,
            model,
            False,
            None,
            None,
            (time.perf_counter() - started) * 1000,
            redact(f"{type(exc).__name__}: {exc}", api_key),
        )


def main() -> int:
    args = parse_args()
    try:
        host, port, chat_path = validate_base_url(args.base_url)
        api_key = load_api_key(args.api_key_env, no_prompt=args.no_prompt)
    except ValueError as exc:
        print(f"配置错误: {exc}")
        return 2

    base_path = chat_path.removesuffix("/chat/completions")
    image = data_url()
    text_message = [{"role": "user", "content": "只回答数字：7乘以8等于多少？"}]
    vision_message = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image}},
                {"type": "text", "text": "图像左半和右半分别是什么颜色？只回答：左=颜色，右=颜色"},
            ],
        }
    ]
    cases: list[tuple[str, str, str, dict[str, Any], Callable[[dict[str, Any]], str]]] = [
        (
            "旗舰文本",
            "qwen3.7-max",
            chat_path,
            {"model": "qwen3.7-max", "messages": text_message, "max_tokens": 32},
            chat_parser,
        ),
        (
            "均衡文本",
            "qwen3.7-plus",
            chat_path,
            {"model": "qwen3.7-plus", "messages": text_message, "max_tokens": 32, "enable_thinking": False},
            chat_parser,
        ),
        (
            "低延时文本",
            "qwen3.6-flash",
            chat_path,
            {"model": "qwen3.6-flash", "messages": text_message, "max_tokens": 32, "enable_thinking": False},
            chat_parser,
        ),
        (
            "最佳文本Embedding",
            "text-embedding-v4",
            f"{base_path}/embeddings",
            {
                "model": "text-embedding-v4",
                "input": ["季度营收增长强劲", "Quarterly revenue grew strongly"],
                "dimensions": 2048,
            },
            embedding_parser,
        ),
        (
            "推荐视觉多模态",
            "qwen3.7-plus",
            chat_path,
            {"model": "qwen3.7-plus", "messages": vision_message, "max_tokens": 64, "enable_thinking": False},
            chat_parser,
        ),
        (
            "Max视觉快照",
            "qwen3.7-max-2026-06-08",
            chat_path,
            {
                "model": "qwen3.7-max-2026-06-08",
                "messages": vision_message,
                "max_tokens": 64,
                "enable_thinking": False,
            },
            chat_parser,
        ),
        (
            "专用视觉模型",
            "qwen3-vl-plus",
            chat_path,
            {"model": "qwen3-vl-plus", "messages": vision_message, "max_tokens": 64, "enable_thinking": False},
            chat_parser,
        ),
    ]

    results = [
        run_case(
            category=category,
            model=model,
            path=path,
            payload=payload,
            parser=parser,
            host=host,
            port=port,
            api_key=api_key,
            timeout=args.timeout,
        )
        for category, model, path, payload, parser in cases
    ]

    print(f"目标: {args.base_url.rstrip('/')}")
    print("密钥: 已安全读取（不显示、不落盘）")
    print()
    for result in results:
        outcome = "成功" if result.ok else "失败"
        status = result.status if result.status is not None else "-"
        ttfb = f"{result.ttfb_ms:.1f} ms" if result.ttfb_ms is not None else "-"
        print(f"[{outcome}] {result.category} | {result.model} | HTTP {status} | TTFB {ttfb} | 总计 {result.total_ms:.1f} ms")
        print(f"       {result.detail}")
        if result.request_id:
            print(f"       Request ID: {result.request_id}")
    passed = sum(result.ok for result in results)
    print(f"\n结论: {passed}/{len(results)} 个模型测试通过。")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
