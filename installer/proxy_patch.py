#!/usr/bin/env python3
import argparse
import difflib
import shutil
import sys
import time
from pathlib import Path


def brace_delta(line):
    return line.count("{") - line.count("}")


def split_top_level_blocks(text):
    lines = text.splitlines(keepends=True)
    blocks = []
    start = None
    depth = 0
    for index, line in enumerate(lines):
        if start is None and "{" in line:
            start = index
        depth += brace_delta(line)
        if start is not None and depth == 0:
            blocks.append((start, index + 1, "".join(lines[start : index + 1])))
            start = None
    return lines, blocks


def caddy_header_domains(header):
    raw = header.split("{", 1)[0].strip()
    domains = []
    for part in raw.replace(",", " ").split():
        if part:
            domains.append(part)
    return domains


def caddy_target_block(text, domain=None, sub2api_upstream=None):
    lines, blocks = split_top_level_blocks(text)
    for start, end, block in blocks:
        header = lines[start]
        if domain and domain in caddy_header_domains(header):
            return start, end
    if sub2api_upstream:
        needle = sub2api_upstream.replace("http://", "").replace("https://", "")
        for start, end, block in blocks:
            if needle in block and "reverse_proxy" in block:
                return start, end
    if len(blocks) == 1:
        return blocks[0][0], blocks[0][1]
    raise ValueError("could not identify Caddy site block; pass --domain")


def patch_caddy(text, domain=None, path="/usage", upstream="127.0.0.1:8091", sub2api_upstream=None):
    path = normalize_path(path)
    if f"handle_path {path}/*" in text or f"handle {path}/*" in text:
        return text
    lines = text.splitlines(keepends=True)
    start, _ = caddy_target_block(text, domain=domain, sub2api_upstream=sub2api_upstream)
    indent = leading_whitespace(lines[start]) + "\t"
    snippet = [
        f"{indent}redir {path} {path}/\n",
        f"{indent}handle {path}/* {{\n",
        f"{indent}\treverse_proxy {upstream}\n",
        f"{indent}}}\n",
        "\n",
    ]
    return "".join(lines[: start + 1] + snippet + lines[start + 1 :])


def find_nginx_blocks(lines, block_name):
    blocks = []
    stack = []
    depth = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{block_name} ") and "{" in stripped:
            stack.append((block_name, index, depth))
        depth += brace_delta(line)
        while stack and depth == stack[-1][2]:
            _, start, _ = stack.pop()
            blocks.append((start, index + 1, "".join(lines[start : index + 1])))
    return blocks


def nginx_server_names(block):
    names = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("server_name "):
            value = stripped[len("server_name ") :].rstrip(";")
            names.extend(value.split())
    return names


def nginx_target_block(text, domain=None, sub2api_upstream=None):
    lines = text.splitlines(keepends=True)
    blocks = find_nginx_blocks(lines, "server")
    for start, end, block in blocks:
        if domain and domain in nginx_server_names(block):
            return start, end
    if sub2api_upstream:
        for start, end, block in blocks:
            if sub2api_upstream in block:
                return start, end
    if len(blocks) == 1:
        return blocks[0][0], blocks[0][1]
    raise ValueError("could not identify Nginx server block; pass --domain")


def patch_nginx(text, domain=None, path="/usage", upstream="http://127.0.0.1:8091", sub2api_upstream=None):
    path = normalize_path(path)
    if f"location {path}/" in text or f"location = {path}" in text:
        return text
    lines = text.splitlines(keepends=True)
    _, end = nginx_target_block(text, domain=domain, sub2api_upstream=sub2api_upstream)
    closing_index = end - 1
    indent = leading_whitespace(lines[closing_index]) + "    "
    snippet = [
        f"{indent}location = {path} {{ return 301 {path}/; }}\n",
        f"{indent}location {path}/ {{\n",
        f"{indent}    proxy_set_header Host $host;\n",
        f"{indent}    proxy_set_header X-Forwarded-Proto $scheme;\n",
        f"{indent}    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n",
        f"{indent}    proxy_pass {upstream};\n",
        f"{indent}}}\n",
        "\n",
    ]
    return "".join(lines[:closing_index] + snippet + lines[closing_index:])


def normalize_path(path):
    path = "/" + (path or "/usage").strip("/")
    return "" if path == "/" else path


def leading_whitespace(line):
    return line[: len(line) - len(line.lstrip())]


def patch_text(kind, text, domain, path, upstream, sub2api_upstream):
    if kind == "caddy":
        caddy_upstream = upstream.replace("http://", "").replace("https://", "")
        return patch_caddy(text, domain=domain, path=path, upstream=caddy_upstream, sub2api_upstream=sub2api_upstream)
    if kind == "nginx":
        nginx_upstream = upstream if upstream.startswith(("http://", "https://")) else f"http://{upstream}"
        return patch_nginx(text, domain=domain, path=path, upstream=nginx_upstream, sub2api_upstream=sub2api_upstream)
    raise ValueError(f"unsupported proxy kind: {kind}")


def backup_file(path):
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.before-sub2api-usage-{stamp}")
    shutil.copy2(path, backup)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description="Patch Caddy or Nginx config for Sub2API usage dashboard.")
    parser.add_argument("--kind", choices=["caddy", "nginx"], required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--domain", default="")
    parser.add_argument("--path", default="/usage")
    parser.add_argument("--upstream", default="http://127.0.0.1:8091")
    parser.add_argument("--sub2api-upstream", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    config = Path(args.config)
    original = config.read_text(encoding="utf-8")
    patched = patch_text(args.kind, original, args.domain or None, args.path, args.upstream, args.sub2api_upstream or None)

    if patched == original:
        print("config already contains requested route")
        return 0

    diff = "".join(difflib.unified_diff(original.splitlines(True), patched.splitlines(True), fromfile=str(config), tofile=f"{config} patched"))
    if args.dry_run or not args.write:
        sys.stdout.write(diff)
        return 0

    backup = backup_file(config)
    config.write_text(patched, encoding="utf-8")
    print(f"patched {config}; backup saved to {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
