"""Определение публичного IP сервера.

Alfabit-чекаут требует обязательное антифрод-поле `payer_ip` — IP плательщика.
У Telegram-бота его нет (Telegram не отдаёт IP пользователя), поддержка alfabit
ответила: слать IP сервера, на котором крутится бот. Значение можно задать
жёстко через ALFABIT_PAYER_IP, иначе определяем автоматически при старте.
"""
from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)

_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)
_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def detect_public_ip() -> str | None:
    """Публичный IP сервера. None — если ни один сервис не ответил."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        for url in _ENDPOINTS:
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    ip = (await resp.text()).strip()
            except Exception as exc:
                logger.warning("public IP: %s не ответил (%s)", url, exc)
                continue
            if ip:
                logger.info("public IP определён через %s: %s", url, ip)
                return ip
    logger.error("public IP определить не удалось — задайте ALFABIT_PAYER_IP вручную")
    return None
