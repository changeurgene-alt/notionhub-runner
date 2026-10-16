"""Run the pinned public Podcast2Notion source without its deletion routine."""
import importlib.util
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from notion_client import Client
from notion_client.errors import APIResponseError

_last_notion_request = 0.0
_original_notion_request = Client.request
_original_http_request = requests.sessions.Session.request

def mask(value):
    if isinstance(value, str) and value:
        escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::add-mask::{escaped}", flush=True)

def delay_from(headers, default=5):
    try:
        return max(2, min(300, float(headers.get("Retry-After", default))))
    except (TypeError, ValueError):
        return default

def paced_notion_request(self, *args, **kwargs):
    global _last_notion_request
    method = kwargs.get("method", args[1] if len(args) > 1 else "")
    body = kwargs.get("body") or {}
    if str(method).upper() == "DELETE" or body.get("archived") or body.get("in_trash"):
        raise RuntimeError("保护机制已阻止删除或归档操作")
    for attempt in range(10):
        time.sleep(max(0, 0.6 - (time.monotonic() - _last_notion_request)))
        _last_notion_request = time.monotonic()
        try:
            return _original_notion_request(self, *args, **kwargs)
        except APIResponseError as error:
            if error.status != 429:
                raise
            wait = delay_from(error.headers)
            print(f"Notion 限流，等待 {wait:g} 秒后重试", flush=True)
            time.sleep(wait)
    raise RuntimeError("Notion 持续限流，已停止；已有记录保留")

def bounded_http_request(self, method, url, **kwargs):
    kwargs.setdefault("timeout", (10, 40))
    for attempt in range(5):
        try:
            response = _original_http_request(self, method, url, **kwargs)
        except requests.RequestException:
            raise RuntimeError("小宇宙网络连接失败，请稍后重试") from None
        if response.status_code == 429:
            time.sleep(delay_from(response.headers))
            continue
        if not response.ok:
            endpoint = urlsplit(url).path
            raise RuntimeError(f"小宇宙接口 {endpoint} 返回 HTTP {response.status_code}") from None
        for header in ("x-jike-access-token", "x-jike-refresh-token"):
            mask(response.headers.get(header))
        if urlsplit(url).path.endswith("app_auth_tokens.refresh"):
            try:
                data = response.json()
                mask(data.get("x-jike-access-token"))
                mask(data.get("x-jike-refresh-token"))
            except (ValueError, AttributeError):
                raise RuntimeError("小宇宙登录响应格式异常") from None
        return response
    raise RuntimeError("小宇宙持续限流，已停止；已有记录保留")

def main():
    for key in ("NOTION_TOKEN", "REFRESH_TOKEN", "NOTION_PAGE"):
        if not os.environ.get(key):
            raise RuntimeError(f"缺少配置：{key}")
    mask(os.environ["NOTION_TOKEN"])
    mask(os.environ["REFRESH_TOKEN"])
    Client.request = paced_notion_request
    requests.sessions.Session.request = bounded_http_request
    source = Path(os.environ.get("PODCAST_SOURCE_DIR", ".podcast-source")) / "scripts"
    sys.path.insert(0, str(source.resolve()))
    spec = importlib.util.spec_from_file_location("podcast_upstream", source / "podcast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Keep private listening titles out of this public repository's action logs.
    module.print = lambda *args, **kwargs: None
    helper = module.NotionHelper()
    expected = {
        "podcast_database_id": "5140193e8d68834390bf01ec3552c0e0",
        "episode_database_id": "7580193e8d6882f78ac281dd6ba8c834",
        "author_database_id": "3e80193e8d6883889d65815b948fcd43",
        "all_database_id": "2c30193e8d688359982481552a429d57",
    }
    for field, wanted in expected.items():
        found = (getattr(helper, field, None) or "").replace("-", "")
        if found != wanted:
            raise RuntimeError(f"播客模板校验失败：{field}。已停止，未开始同步。")
    module.notion_helper = helper
    module.refresh_token()
    if not module.headers.get("x-jike-access-token"):
        raise RuntimeError("小宇宙登录未返回有效凭证，请更新 Refresh Token")
    podcasts = module.insert_podcast()
    episodes = module.get_history()
    module.insert_episode(episodes, podcasts)
    # Deliberately never call upstream delete(): preserve unheard episodes.
    print(f"同步完成：{len(podcasts)} 个播客，处理 {len(episodes)} 条历史单集；未执行删除。", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(f"## 小宇宙独立同步\n\n同步完成：{len(podcasts)} 个播客，处理 {len(episodes)} 条历史单集。\n\n未执行删除或归档。\n")

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not include response bodies or tracebacks, which may contain credentials.
        print(f"同步未完成：{type(error).__name__}: {str(error)[:350]}", file=sys.stderr)
        sys.exit(1)
