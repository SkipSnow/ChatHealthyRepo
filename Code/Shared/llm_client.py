# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Unified LLM client — one interface, any frontier model.
#
# Routes by model name:
#   claude-*  → Anthropic SDK
#   everything else → OpenAI SDK (OpenAI, Google, Groq, DeepSeek all support this format)
#
# The caller passes model name from prompts.json. No vendor lock-in.
# Two dependencies: anthropic, openai. No frameworks.
#
# Usage:
#   from llm_client import chat, get_active_model
#   response = chat(model="gemini-2.5-flash", messages=[...], tools=[...])
#   response = chat(model="claude-sonnet-4-6", messages=[...], tools=[...])

import json
import os
from pathlib import Path
from typing import Any, Optional
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

# Vendor base URLs for OpenAI-compatible APIs
_VENDOR_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com",
}

# Model prefix → API key env var
_VENDOR_KEYS = {
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "gpt": "OPENAI_API_KEY",
    "o1": "OPENAI_API_KEY",
    "o3": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}



def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


def _anthropic_to_openai_tools(tools: list) -> list:
    """Convert Anthropic tool format to OpenAI format."""
    converted = []
    for t in tools:
        if "type" in t and t.get("type") == "function":
            converted.append(t)  # Already OpenAI format
        elif "name" in t and "input_schema" in t:
            # Anthropic format → OpenAI format
            converted.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            })
        else:
            converted.append(t)  # Pass through unknown format
    return converted


def _openai_to_anthropic_tools(tools: list) -> list:
    """Convert OpenAI tool format to Anthropic format."""
    converted = []
    for t in tools:
        if "name" in t and "input_schema" in t:
            converted.append(t)  # Already Anthropic format
        elif t.get("type") == "function" and "function" in t:
            # OpenAI format → Anthropic format
            func = t["function"]
            converted.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {}),
            })
        else:
            converted.append(t)  # Pass through
    return converted


def _get_vendor(model: str) -> str:
    """Determine vendor from model name."""
    m = model.lower()
    if m.startswith("claude"):
        return "claude"
    if m.startswith("gemini"):
        return "gemini"
    if m.startswith("groq") or m.startswith("llama"):
        return "groq"
    if m.startswith("deepseek"):
        return "deepseek"
    # Default to OpenAI (gpt-*, o1-*, o3-*, etc.)
    return "gpt"


def _get_api_key(vendor: str) -> str:
    """Get API key for vendor from environment."""
    env_var = _VENDOR_KEYS.get(vendor, "OPENAI_API_KEY")
    key = os.environ.get(env_var, "")
    if not key:
        raise ChatHealthyException(
            mode="value_error",
            component="llm_client",
            message=f"API key not found: {env_var}")
    return key


def _anthropic_call(model: str, messages: list, tools: Optional[list] = None,
                    system: Optional[str] = None, max_tokens: int = 4096,
                    response_format: Optional[dict] = None, **kwargs) -> dict:
    """Call Anthropic Claude API."""
    from anthropic import Anthropic
    client = Anthropic(api_key=_get_api_key("claude"))

    call_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        call_kwargs["system"] = system
    if tools:
        call_kwargs["tools"] = _openai_to_anthropic_tools(tools)

    response = client.messages.create(**call_kwargs)

    # Normalize response to common format
    content = ""
    tool_calls = []
    for block in response.content:
        if hasattr(block, "text"):
            content += block.text
        elif hasattr(block, "type") and block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "function": {
                    "name": block.name,
                    "arguments": json.dumps(block.input) if isinstance(block.input, dict) else block.input,
                },
            })

    return {
        "content": content,
        "tool_calls": tool_calls if tool_calls else None,
        "model": model,
        "vendor": "anthropic",
        "stop_reason": response.stop_reason,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
        "_raw": response,
    }


def _openai_call(model: str, messages: list, tools: Optional[list] = None,
                 system: Optional[str] = None, max_tokens: int = 4096,
                 response_format: Optional[dict] = None, **kwargs) -> dict:
    """Call OpenAI-compatible API (OpenAI, Google, Groq, DeepSeek)."""
    from openai import OpenAI

    vendor = _get_vendor(model)
    api_key = _get_api_key(vendor)
    base_url = _VENDOR_URLS.get(vendor)

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    client = OpenAI(**client_kwargs)

    # Prepend system message if provided
    full_messages = messages
    if system:
        full_messages = [{"role": "system", "content": system}] + messages

    call_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": full_messages,
    }
    if tools:
        call_kwargs["tools"] = _anthropic_to_openai_tools(tools)
    if response_format:
        call_kwargs["response_format"] = response_format

    response = client.chat.completions.create(**call_kwargs)

    choice = response.choices[0]
    content = choice.message.content or ""
    tool_calls = None
    if choice.message.tool_calls:
        tool_calls = [{
            "id": tc.id,
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            },
        } for tc in choice.message.tool_calls]

    return {
        "content": content,
        "tool_calls": tool_calls,
        "model": model,
        "vendor": vendor,
        "stop_reason": choice.finish_reason,
        "usage": {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0,
        },
        "_raw": response,
    }


def chat(model: str, messages: list, tools: Optional[list] = None,
         system: Optional[str] = None, max_tokens: int = 4096,
         response_format: Optional[dict] = None, **kwargs) -> dict:
    """
    Call any frontier model. One interface.

    Args:
        model: Model name (e.g. "claude-sonnet-4-6", "gemini-2.5-flash", "gpt-4.1-mini")
        messages: List of {"role": "user"|"assistant", "content": "..."}
        tools: Optional tool definitions (OpenAI format for non-Anthropic, Anthropic format for Claude)
        system: Optional system prompt (separate from messages for Anthropic compatibility)
        max_tokens: Max response tokens
        response_format: Optional {"type": "json_object"} for structured output

    Returns:
        Normalized dict: {content, tool_calls, model, vendor, stop_reason, usage, _raw}
    """
    vendor = _get_vendor(model)
    if vendor == "claude":
        return _anthropic_call(model, messages, tools, system, max_tokens, response_format, **kwargs)
    else:
        return _openai_call(model, messages, tools, system, max_tokens, response_format, **kwargs)
