#!/usr/bin/env python3
"""Verify an Alibaba Cloud Model Studio (Bailian) OpenAI-compatible API key.

The script uses only the Python standard library. It deliberately accepts the
API key only from an environment variable or a hidden terminal prompt so the
secret is not stored in this file or exposed in shell history.
"""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import socket
import ssl
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen3.8-max-preview"
MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(slots=True)
class ProbeResult:
    run: int
    ok: bool
    status: int | None
    category: str
    detail: str
    dns_ms: float | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None
    ttfb_ms: float | None = None
    total_ms: float | None = None
    request_id: str | None = None
    answer: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="验证阿里云百炼 OpenAI 兼容 API 密钥，并统计 DNS/TCP/TLS/TTFB/总延时。",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"API Base URL（默认：{DEFAULT_BASE_URL}）")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名（默认：{DEFAULT_MODEL}）")
    parser.add_argument("--runs", type=int, default=3, help="请求次数（默认：3）")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次连接/读取超时秒数（默认：30）")
    parser.add_argument(
        "--api-key-env",
        default="DASHSCOPE_API_KEY",
        help="读取密钥的环境变量名（默认：DASHSCOPE_API_KEY）",
    )
    parser.add_argument("--no-prompt", action="store_true", help="环境变量缺失时直接失败，不进行隐藏输入")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果，便于自动化采集")
    args = parser.parse_args()

    if not 1 <= args.runs <= 20:
        parser.error("--runs 必须在 1 到 20 之间")
    if not 0.1 <= args.timeout <= 300:
        parser.error("--timeout 必须在 0.1 到 300 秒之间")
    return args


def validate_base_url(raw_url: str) -> tuple[str, int, str]:
    parsed = urlsplit(raw_url.rstrip("/"))
    if parsed.scheme != "https":
        raise ValueError("base URL 必须使用 https")
    if not parsed.hostname:
        raise ValueError("base URL 缺少主机名")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL 不得包含账号、密码、查询参数或片段")
    port = parsed.port or 443
    base_path = parsed.path.rstrip("/")
    return parsed.hostname, port, f"{base_path}/chat/completions"


def load_api_key(env_name: str, *, no_prompt: bool) -> str:
    api_key = os.environ.get(env_name, "").strip()
    if not api_key and not no_prompt and sys.stdin.isatty():
        api_key = getpass.getpass("请输入百炼 API Key（输入不会回显）: ").strip()
    if not api_key:
        raise ValueError(f"未找到密钥：请设置 {env_name}，或在终端隐藏输入")
    return api_key


def redact(value: str, api_key: str) -> str:
    """Remove the complete key and a defensive prefix from provider messages."""
    redacted = value.replace(api_key, "***")
    if len(api_key) >= 12:
        redacted = redacted.replace(api_key[:12], "***")
    return redacted


def connect_tcp(addresses: list[tuple[Any, ...]], timeout: float) -> socket.socket:
    errors: list[OSError] = []
    for family, socktype, proto, _canonname, sockaddr in addresses:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            errors.append(exc)
            sock.close()
    if errors:
        raise errors[-1]
    raise OSError("DNS 查询未返回可连接地址")


def create_ssl_context() -> ssl.SSLContext:
    """Build a verified context, including common macOS system CA locations."""
    default_paths = ssl.get_default_verify_paths()
    if default_paths.cafile and Path(default_paths.cafile).is_file():
        return ssl.create_default_context()

    for candidate in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem"):
        if Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def extract_response(data: Any) -> tuple[str | None, int | None, int | None]:
    if not isinstance(data, dict):
        return None, None, None
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        answer = None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return answer, usage.get("prompt_tokens"), usage.get("completion_tokens")


def provider_error(data: Any, raw_body: str) -> tuple[str | None, str]:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or error.get("type")
            return str(code) if code else None, str(message) if message else "服务端返回错误"
        code = data.get("code")
        message = data.get("message")
        if code or message:
            return str(code) if code else None, str(message) if message else "服务端返回错误"
    return None, raw_body[:300].strip() or "服务端返回空错误响应"


def classify_status(status: int, error_code: str | None, message: str) -> tuple[str, str]:
    code = (error_code or "").lower()
    text = message.lower()
    if status in {401, 403} or any(marker in code for marker in ("auth", "key", "permission")):
        return "auth", "密钥无效、已过期，或无此区域访问权限"
    if status == 429:
        return "quota", "已鉴权，但触发限流或额度不足"
    if status in {400, 404, 422}:
        if any(marker in f"{code} {text}" for marker in ("model", "deployment", "not exist")):
            return "model", "接口可达，但该模型在此区域不可用或模型名不正确"
        return "request", "接口可达，但请求参数未被服务端接受"
    if status >= 500:
        return "provider", "百炼服务端暂时异常"
    return "http", f"HTTP {status}"


def probe_once(
    *,
    run: int,
    host: str,
    port: int,
    path: str,
    api_key: str,
    model: str,
    timeout: float,
) -> ProbeResult:
    started = time.perf_counter()
    raw_sock: socket.socket | None = None
    tls_sock: ssl.SSLSocket | None = None
    connection: http.client.HTTPSConnection | None = None
    dns_ms = tcp_ms = tls_ms = None

    try:
        phase = time.perf_counter()
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        dns_ms = (time.perf_counter() - phase) * 1000

        phase = time.perf_counter()
        raw_sock = connect_tcp(addresses, timeout)
        tcp_ms = (time.perf_counter() - phase) * 1000

        phase = time.perf_counter()
        context = create_ssl_context()
        tls_sock = context.wrap_socket(raw_sock, server_hostname=host)
        raw_sock = None
        tls_ms = (time.perf_counter() - phase) * 1000

        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                "temperature": 0,
                "max_tokens": 16,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "bailian-key-verifier/1.0",
        }

        connection = http.client.HTTPSConnection(host, port=port, timeout=timeout, context=context)
        connection.sock = tls_sock
        tls_sock = None

        request_started = time.perf_counter()
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        ttfb_ms = (time.perf_counter() - request_started) * 1000
        body_bytes = response.read(MAX_RESPONSE_BYTES + 1)
        total_ms = (time.perf_counter() - started) * 1000
        if len(body_bytes) > MAX_RESPONSE_BYTES:
            raise ValueError("响应超过 1 MiB 安全上限")

        body = body_bytes.decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        request_id = response.getheader("x-request-id") or response.getheader("x-dashscope-request-id")

        if 200 <= response.status < 300:
            answer, prompt_tokens, completion_tokens = extract_response(data)
            if answer is None:
                return ProbeResult(
                    run,
                    False,
                    response.status,
                    "response",
                    "请求成功，但响应不符合 Chat Completions JSON 格式",
                    dns_ms,
                    tcp_ms,
                    tls_ms,
                    ttfb_ms,
                    total_ms,
                    request_id,
                )
            return ProbeResult(
                run,
                True,
                response.status,
                "ok",
                "鉴权与模型调用成功",
                dns_ms,
                tcp_ms,
                tls_ms,
                ttfb_ms,
                total_ms,
                request_id,
                redact(str(answer), api_key)[:120],
                prompt_tokens,
                completion_tokens,
            )

        error_code, message = provider_error(data, body)
        message = redact(message, api_key)
        category, summary = classify_status(response.status, error_code, message)
        detail = f"{summary}；服务端信息：{message[:300]}"
        return ProbeResult(
            run,
            False,
            response.status,
            category,
            detail,
            dns_ms,
            tcp_ms,
            tls_ms,
            ttfb_ms,
            total_ms,
            request_id,
        )
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError, ValueError) as exc:
        total_ms = (time.perf_counter() - started) * 1000
        detail = redact(f"{type(exc).__name__}: {exc}", api_key)
        return ProbeResult(run, False, None, "network", detail, dns_ms, tcp_ms, tls_ms, None, total_ms)
    finally:
        if connection is not None:
            connection.close()
        if tls_sock is not None:
            tls_sock.close()
        if raw_sock is not None:
            raw_sock.close()


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def print_human(results: list[ProbeResult], *, base_url: str, model: str) -> None:
    print(f"目标: {base_url}/chat/completions")
    print(f"模型: {model} | 请求数: {len(results)}")
    print("密钥: 已安全读取（不显示、不落盘）")
    print()

    for result in results:
        status = str(result.status) if result.status is not None else "-"
        outcome = "成功" if result.ok else "失败"
        print(f"[{result.run}] {outcome} | HTTP {status} | {result.detail}")
        if result.total_ms is not None:
            print(
                "    "
                f"DNS {result.dns_ms or 0:.1f} ms | TCP {result.tcp_ms or 0:.1f} ms | "
                f"TLS {result.tls_ms or 0:.1f} ms | TTFB {result.ttfb_ms or 0:.1f} ms | "
                f"总计 {result.total_ms:.1f} ms"
            )
        if result.request_id:
            print(f"    Request ID: {result.request_id}")
        if result.answer is not None:
            usage = ""
            if result.prompt_tokens is not None or result.completion_tokens is not None:
                usage = f" | tokens: {result.prompt_tokens or 0}+{result.completion_tokens or 0}"
            print(f"    响应: {result.answer!r}{usage}")

    successful = [result for result in results if result.ok and result.total_ms is not None]
    print()
    if successful:
        totals = [result.total_ms for result in successful if result.total_ms is not None]
        ttfbs = [result.ttfb_ms for result in successful if result.ttfb_ms is not None]
        print(f"结论: 调用成功 {len(successful)}/{len(results)} 次，密钥、区域端点和模型均可用。")
        print(
            f"总延时 min/avg/p50/p95/max: {min(totals):.1f} / {statistics.fmean(totals):.1f} / "
            f"{percentile(totals, 0.50):.1f} / {percentile(totals, 0.95):.1f} / {max(totals):.1f} ms"
        )
        if ttfbs:
            print(f"TTFB avg/p95: {statistics.fmean(ttfbs):.1f} / {percentile(ttfbs, 0.95):.1f} ms")
    else:
        categories = ", ".join(sorted({result.category for result in results}))
        print(f"结论: 未调通；失败类别: {categories}。请根据上方服务端信息处理。")


def exit_code(results: list[ProbeResult]) -> int:
    if any(result.ok for result in results):
        return 0
    categories = {result.category for result in results}
    if "auth" in categories:
        return 3
    if categories & {"model", "request", "response"}:
        return 4
    if "quota" in categories:
        return 5
    return 6


def main() -> int:
    args = parse_args()
    try:
        host, port, path = validate_base_url(args.base_url)
        api_key = load_api_key(args.api_key_env, no_prompt=args.no_prompt)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    results = [
        probe_once(
            run=index,
            host=host,
            port=port,
            path=path,
            api_key=api_key,
            model=args.model,
            timeout=args.timeout,
        )
        for index in range(1, args.runs + 1)
    ]
    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print_human(results, base_url=args.base_url.rstrip("/"), model=args.model)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
