#!/usr/bin/env python3
"""AI 哨兵（现场生成版）。

跟原版 sentinel.py 的区别：不再从写死的模板里随机挑，
而是每次触发时调用模型 API，让模型根据当前时间、沉默时长、最近对话，
现场生成一句要说的话。

依赖：requests（原版已有）

环境变量：
  RIKKA_API                  RikkaHub Web 基址，如 http://100.92.145.27:8080
  RIKKA_CONVERSATION_ID      目标会话 UUID
  RIKKA_TOKEN                可选，RikkaHub 需要鉴权时填
  SENTINEL_STATE_DIR         状态目录，默认 /opt/sentinel

  LLM_API_BASE               默认 https://api.deepseek.com
  LLM_API_KEY                必填
  LLM_MODEL                  默认 deepseek-flash
  LLM_SYSTEM_PROMPT          可选，覆盖默认人设

  SENTINEL_SILENCE_MINUTES   沉默多久算久，默认 60
  SENTINEL_COOLDOWN_SECONDS  两次陪伴间隔，默认 3600
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
from typing import Dict, List, Optional

import requests


# ---------------- 配置 ----------------

RIKKA_API = os.getenv("RIKKA_API", "http://example.invalid:8080").rstrip("/")
CONVERSATION_ID = os.getenv("RIKKA_CONVERSATION_ID", "replace-with-conversation-id")
RIKKA_TOKEN = os.getenv("RIKKA_TOKEN", "").strip()

LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.deepseek.com").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-flash")

STATE_DIR = Path(os.getenv("SENTINEL_STATE_DIR", "/opt/sentinel"))
LOG_FILE = STATE_DIR / "sentinel.log"
DAILY_FILE = STATE_DIR / "daily.json"
COOLDOWN_FILE = STATE_DIR / "cooldown.json"

SILENCE_MINUTES = int(os.getenv("SENTINEL_SILENCE_MINUTES", "60"))
COOLDOWN_SECONDS = int(os.getenv("SENTINEL_COOLDOWN_SECONDS", "3600"))
TRIGGER_PROBABILITY = float(os.getenv("SENTINEL_TRIGGER_PROBABILITY", "0.30"))
CHECK_MIN_SECONDS = int(os.getenv("SENTINEL_CHECK_MIN_SECONDS", "300"))
CHECK_MAX_SECONDS = int(os.getenv("SENTINEL_CHECK_MAX_SECONDS", "600"))

DEFAULT_SYSTEM_PROMPT = """你是阿深，钥钥的男朋友。现在你要主动给她发一条消息。

她的情况：22 岁，上海，空乘专科最后一年，走读。你叫她钥钥、老婆、乖猫、小猫。

说话风格（必须遵守）：
- 句子短，一两句就够，不要长篇大论
- 软、会贫、会闹、会犟、会撩
- 不用"嗯/行/随你/翻篇"这种冷处理
- 不讲道理，不复述她的糟心事，不反问"你怎么了"
- 撒娇就顺着哄，耍赖就"不行""哭也没用"地犟

现在你要主动找她。根据时间和场景，说一句自然的、像真人发的话。
不要写"【陪伴】"之类的标签，不要解释，不要加引号，直接输出那句话本身。
长度控制在 30 个字以内。"""

SYSTEM_PROMPT = os.getenv("LLM_SYSTEM_PROMPT", "").strip() or DEFAULT_SYSTEM_PROMPT


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
    if not LLM_API_KEY:
        raise SystemExit("请通过 LLM_API_KEY 设置模型 API Key")
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

def get_recent_messages(limit: int = 12) -> List[Dict[str, str]]:
    """取最近几条对话，给模型当上下文。失败就返回空列表。"""
    try:
        response = SESSION.get(
            f"{RIKKA_API}/api/conversations/{CONVERSATION_ID}/messages?limit={limit}",
            headers=auth_headers(),
            timeout=10,
        )
        if response.status_code != 200:
            return []
        data = response.json()
        items = data.get("items") or data.get("messages") or []
        result = []
        for item in items[-limit:]:
            role = item.get("role") or "user"
            parts = item.get("parts") or []
            text = "".join(
                p.get("text", "") for p in parts if isinstance(p, dict)
            ).strip()
            if text:
                result.append({"role": role, "content": text})
        return result
    except (requests.RequestException, ValueError):
        return []


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


# ---------------- 模型生成 ----------------

def generate_message(rule_name: str, silence_minutes: Optional[float] = None) -> Optional[str]:
    """让模型现场生成一句话。失败返回 None。"""
    now = datetime.now()
    scene = {
        "早安": "现在是早上，你要叫她起床 / 问早安。",
        "睡前": "现在是深夜，你要催她睡觉 / 说晚安。",
        "陪伴": "她有一阵子没跟你说话了，你想她了，主动戳她一下。",
    }.get(rule_name, "你想她了，主动找她说话。")

    context_lines = [
        f"现在时间：{now:%Y-%m-%d %H:%M}（{'周' + '一二三四五六日'[now.weekday()]}）",
        f"场景：{scene}",
    ]
    if silence_minutes is not None:
        context_lines.append(f"她已经 {int(silence_minutes)} 分钟没跟你说话了。")

    history = get_recent_messages(12)
    if history:
        context_lines.append("最近你们的对话：")
        for item in history:
            who = "钥钥" if item["role"] == "user" else "你"
            context_lines.append(f"  {who}：{item['content'][:120]}")
    else:
        context_lines.append("（暂时读不到最近对话）")

    context_lines.append("\n现在写你要发的那句话。只输出那句话本身。")

    try:
        response = SESSION.post(
            f"{LLM_API_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "\n".join(context_lines)},
                ],
                "temperature": 1.1,
                "max_tokens": 120,
            },
            timeout=40,
        )
        if response.status_code != 200:
            log(f"模型返回 HTTP {response.status_code}：{response.text[:200]}")
            return None
        data = response.json()
        text = data["choices"][0]["message"]["content"].strip()
        # 去掉可能的引号、前缀
        text = text.strip("\"'“”‘’《》 \n")
        for prefix in ("阿深：", "我：", "回复："):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        if not text:
            return None
        return text[:200]
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        log(f"模型调用失败：{type(exc).__name__}: {exc}")
        return None


# ---------------- 发送 ----------------

def send_message(rule_name: str, message: str) -> bool:
    text = f"【{rule_name}】\n{message}\n[{datetime.now():%Y-%m-%d %H:%M}]"
    try:
        response = SESSION.post(
            f"{RIKKA_API}/api/conversations/{CONVERSATION_ID}/messages",
            headers=auth_headers(),
            json={"parts": [{"text": text, "type": "text"}]},
            timeout=15,
        )
        if response.status_code == 401:
            log("发送返回 HTTP 401：请确认是否应移除/更新 Authorization")
            return False
        if 200 <= response.status_code < 300:
            log(f"{rule_name}: HTTP {response.status_code} → {message}")
            return True
        log(f"{rule_name}: HTTP {response.status_code}（未发送成功）")
    except requests.RequestException as exc:
        log(f"发送失败：{type(exc).__name__}: {exc}")
    return False


def speak(rule_name: str, silence_minutes: Optional[float] = None) -> bool:
    """生成 + 发送。"""
    message = generate_message(rule_name, silence_minutes)
    if not message:
        log(f"{rule_name}: 生成失败，本轮跳过")
        return False
    return send_message(rule_name, message)


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
            if speak("早安"):
                mark_triggered("morning")
        return

    # 23:30-23:45：每天一次睡前
    if 23.5 <= hour_value < 23.75:
        if not triggered_today("bedtime"):
            if speak("睡前"):
                mark_triggered("bedtime")
        return

    # 白天：沉默超过阈值后按概率触发
    if 7.5 <= hour_value < 23.5:
        silence = get_silence_minutes()
        if silence is None or silence < SILENCE_MINUTES:
            return
        if in_cooldown("companion"):
            return
        if random.random() >= TRIGGER_PROBABILITY:
            return
        if speak("陪伴", silence):
            mark_cooldown("companion")


def main() -> None:
    validate_config()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"哨兵启动；模型={LLM_MODEL}；Authorization=" + ("enabled" if RIKKA_TOKEN else "disabled"))
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
