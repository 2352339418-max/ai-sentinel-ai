#!/usr/bin/env python3
"""AI 哨兵（信号版）。

跟模板版、生成版的区别：
脚本**不生成任何内容**，只在合适的时候往 RikkaHub 对话里投递一个固定信号。
RikkaHub 收到这个信号后，会按正常流程唤醒对话里的 AI，
由 AI 自己决定说什么、怎么说。

也就是说：话是 AI 自己说的，脚本只负责"戳一下"。

环境变量：
  RIKKA_API                  RikkaHub Web 基址
  RIKKA_CONVERSATION_ID      目标会话 UUID
  RIKKA_TOKEN                可选，RikkaHub 需要鉴权时填
  SENTINEL_STATE_DIR         状态目录，默认 /opt/sentinel

  SENTINEL_SIGNAL            信号文本，默认 "【唤醒】"
  SENTINEL_SILENCE_MINUTES   沉默多久算久，默认 60
  SENTINEL_COOLDOWN_SECONDS  两次唤醒间隔，默认 3600
  SENTINEL_TRIGGER_PROBABILITY 触发概率，默认 0.30
  SENTINEL_CHECK_MIN_SECONDS 检查间隔下限，默认 300
  SENTINEL_CHECK_MAX_SECONDS 检查间隔上限，默认 600
"""

from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import requests


# ---------------- 配置 ----------------

RIKKA_API = os.getenv("RIKKA_API", "http://example.invalid:8080").rstrip("/")
CONVERSATION_ID = os.getenv("RIKKA_CONVERSATION_ID", "replace-with-conversation-id")
RIKKA_TOKEN = os.getenv("RIKKA_TOKEN", "").strip()

STATE_DIR = Path(os.getenv("SENTINEL_STATE_DIR", "/opt/sentinel"))
LOG_FILE = STATE_DIR / "sentinel.log"
DAILY_FILE = STATE_DIR / "daily.json"
COOLDOWN_FILE = STATE_DIR / "cooldown.json"

SIGNAL = os.getenv("SENTINEL_SIGNAL", "【唤醒】")

SILENCE_MINUTES = int(os.getenv("SENTINEL_SILENCE_MINUTES", "60"))
COOLDOWN_SECONDS = int(os.getenv("SENTINEL_COOLDOWN_SECONDS", "3600"))
TRIGGER_PROBABILITY = float(os.getenv("SENTINEL_TRIGGER_PROBABILITY", "0.30"))
CHECK_MIN_SECONDS = int(os.getenv("SENTINEL_CHECK_MIN_SECONDS", "300"))
CHECK_MAX_SECONDS = int(os.getenv("SENTINEL_CHECK_MAX_SECONDS", "600"))


# ---------------- 基础设施 ----------------

SESSION = requests.Session()


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def auth_headers() -> Dict[str, str]:
    if not RIKKA_TOKEN:
        return {}
    value = RIKKA_TOKEN
    if not value.lower().startswith("bearer "):
        value = "Bearer " + value
    return {"Authorization": value}


def validate_config() -> None:
    if "example.invalid" in RIKKA_API:
        raise SystemExit("请通过 RIKKA_API 设置真实的 RikkaHub 地址")
    if CONVERSATION_ID == "replace-with-conversation-id":
        raise SystemExit("请通过 RIKKA_CONVERSATION_ID 设置会话 ID")
    if not 0 <= TRIGGER_PROBABILITY <= 1:
        raise SystemExit("SENTINEL_TRIGGER_PROBABILITY 必须在 0 到 1 之间")
    if CHECK_MIN_SECONDS > CHECK_MAX_SECONDS:
        raise SystemExit("检查间隔最小值不能大于最大值")


def load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, value) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)


# ---------------- RikkaHub API ----------------

def get_silence_minutes() -> Optional[float]:
    try:
        response = SESSION.get(
            f"{RIKKA_API}/api/conversations/paged?offset=0&limit=30",
            headers=auth_headers(),
            timeout=10,
        )
        if response.status_code == 401:
            log("查询返回 HTTP 401：请确认是否应移除/更新 Authorization")
            return None
        response.raise_for_status()
        for item in response.json().get("items", []):
            if item.get("id") == CONVERSATION_ID:
                updated_ms = item.get("updateAt")
                if not isinstance(updated_ms, (int, float)):
                    return None
                last = datetime.fromtimestamp(updated_ms / 1000)
                return (datetime.now() - last).total_seconds() / 60
        log("会话列表中未找到目标会话，请检查 RIKKA_CONVERSATION_ID")
    except (requests.RequestException, ValueError) as exc:
        log(f"查询失败，本轮不触发：{type(exc).__name__}: {exc}")
    return None


def send_signal(tag: str) -> bool:
    """只投递信号，不生成任何内容。"""
    try:
        response = SESSION.post(
            f"{RIKKA_API}/api/conversations/{CONVERSATION_ID}/messages",
            headers=auth_headers(),
            json={"parts": [{"text": SIGNAL, "type": "text"}]},
            timeout=15,
        )
        if response.status_code == 401:
            log("发送返回 HTTP 401：请确认是否应移除/更新 Authorization")
            return False
        if 200 <= response.status_code < 300:
            log(f"{tag}: HTTP {response.status_code}，已投递信号")
            return True
        log(f"{tag}: HTTP {response.status_code}（未发送成功）")
    except requests.RequestException as exc:
        log(f"发送失败：{type(exc).__name__}: {exc}")
    return False


# ---------------- 状态与触发规则 ----------------

def triggered_today(name: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    state = load_json(DAILY_FILE, {})
    return state.get("date") == today and bool(state.get(name))


def mark_triggered(name: str) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    state = load_json(DAILY_FILE, {})
    if state.get("date") != today:
        state = {"date": today}
    state[name] = True
    save_json(DAILY_FILE, state)


def in_cooldown(name: str) -> bool:
    state = load_json(COOLDOWN_FILE, {})
    return time.time() - float(state.get(name, 0)) < COOLDOWN_SECONDS


def mark_cooldown(name: str) -> None:
    state = load_json(COOLDOWN_FILE, {})
    state[name] = time.time()
    save_json(COOLDOWN_FILE, state)


def check_once() -> None:
    now = datetime.now()
    hour_value = now.hour + now.minute / 60

    # 03:00-07:15：静默
    if 3 <= hour_value < 7.25:
        return

    # 07:15-07:30：每天一次早安
    if 7.25 <= hour_value < 7.5:
        if not triggered_today("morning"):
            if send_signal("早安"):
                mark_triggered("morning")
        return

    # 23:30-23:45：每天一次睡前
    if 23.5 <= hour_value < 23.75:
        if not triggered_today("bedtime"):
            if send_signal("睡前"):
                mark_triggered("bedtime")
        return

    # 白天：沉默超过阈值后按概率唤醒
    if 7.5 <= hour_value < 23.5:
        silence = get_silence_minutes()
        if silence is None or silence < SILENCE_MINUTES:
            return
        if in_cooldown("companion"):
            return
        if random.random() >= TRIGGER_PROBABILITY:
            return
        if send_signal("陪伴"):
            mark_cooldown("companion")


def main() -> None:
    validate_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"哨兵启动；信号={SIGNAL!r}；Authorization=" + ("enabled" if RIKKA_TOKEN else "disabled"))
    while True:
        try:
            check_once()
        except Exception as exc:  # 单轮异常不杀死常驻进程
            log(f"未处理异常：{type(exc).__name__}: {exc}")
        delay = random.randint(CHECK_MIN_SECONDS, CHECK_MAX_SECONDS)
        log(f"下次检查：{delay / 60:.1f} 分钟后")
        time.sleep(delay)


if __name__ == "__main__":
    main()
