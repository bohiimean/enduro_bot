"""Клиент Alfabit integration API (подпись HMAC-SHA256).

Документация: https://alfabit.org/ru/api-docs (SPA — разделы Converter / Checkout).

Каждый запрос подписывается тремя заголовками:
    X-API-Key:       публичный ключ pk_live_...
    X-API-Signature: HMAC_SHA256(secret_key, message)
    X-API-Timestamp: unix-время в секундах (живёт 5 минут)
где message = "{timestamp}{METHOD}{path}{body}".

OPEN QUESTIONS (проверить на реальных ключах — в доках не однозначно):
- Что входит в {path} для GET с query-параметрами: только путь или путь + "?...".
  Сейчас подписываем полный path вместе с query (см. _signed_path). Если alfabit
  вернёт INVALID_SIGNATURE на converter-курс — попробовать подпись без query.
- Как подписывается multipart-загрузка документов (тело не JSON). Сейчас для
  multipart подписываем с пустым body — тоже под вопросом.
- payer_ip: у бота нет реального IP плательщика (Telegram его не отдаёт).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

_API_PREFIX = "/api"
_TIMEOUT = aiohttp.ClientTimeout(total=20)


class AlfabitError(RuntimeError):
    """Ошибка ответа Alfabit API (success=false или HTTP != 2xx)."""

    def __init__(self, code: str, message: str, status: int | None = None):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


class AlfabitClient:
    def __init__(self, api_key: str, secret_key: str, base_url: str = "https://alfabit.org"):
        self._api_key = api_key
        self._secret_key = secret_key.encode()
        self._base_url = base_url.rstrip("/")

    def _sign(self, method: str, signed_path: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        message = f"{timestamp}{method.upper()}{signed_path}{body}"
        signature = hmac.new(self._secret_key, message.encode(), hashlib.sha256).hexdigest()
        return {
            "X-API-Key": self._api_key,
            "X-API-Signature": signature,
            "X-API-Timestamp": timestamp,
        }

    @staticmethod
    def _signed_path(path: str, params: dict[str, Any] | None) -> str:
        """Путь для подписи. Query включаем в подпись (см. OPEN QUESTIONS)."""
        if not params:
            return path
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{path}?{query}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        path = _API_PREFIX + path if not path.startswith(_API_PREFIX) else path
        body = json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
        headers = self._sign(method, self._signed_path(path, params), body)
        headers["Content-Type"] = "application/json"

        url = self._base_url + path
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.request(
                method,
                url,
                params=params,
                data=body or None,
                headers=headers,
            ) as resp:
                payload = await resp.json(content_type=None)
                return self._unwrap(payload, resp.status)

    @staticmethod
    def _unwrap(payload: Any, status: int) -> Any:
        if isinstance(payload, dict) and payload.get("success") is False:
            err = payload.get("error") or {}
            raise AlfabitError(err.get("code", "UNKNOWN"), err.get("message", ""), status)
        if status >= 400:
            raise AlfabitError("HTTP_ERROR", f"status {status}", status)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # ---- Курс (Converter, «обменник») -------------------------------------

    async def converter_fiat_rate(
        self, crypto: str = "USDT", fiat: str = "RUB", direction: str = "sell"
    ) -> Decimal:
        """GET /api/v1/integration/converter/fiat/rate — курс крипто/фиат с их спредом."""
        data = await self._request(
            "GET",
            "/v1/integration/converter/fiat/rate",
            params={"crypto": crypto, "fiat": fiat, "direction": direction},
        )
        return Decimal(str(data["rate"]))

    # ---- Checkout (RUB / СБП + KYC) ----------------------------------------

    async def create_checkout_payment(
        self,
        *,
        amount: str,
        payer_phone: str,
        payer_ip: str,
        external_payment_id: str,
        payer_user_agent: str | None = None,
        payer_device_id: str | None = None,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/integration/checkout/payments — создать RUB-платёж.

        Возможные ответы (в data):
          status=kyc_required  → нужна верификация, поле upload_documents_endpoint
          status=created       → payment_uid готов, дальше get_checkout_payment(uid)
        """
        body: dict[str, Any] = {
            "amount": amount,
            "payer_phone": payer_phone,
            "payer_ip": payer_ip,
            "external_payment_id": external_payment_id,
        }
        if payer_user_agent:
            body["payer_user_agent"] = payer_user_agent
        if payer_device_id:
            body["payer_device_id"] = payer_device_id
        if channel:
            body["channel"] = channel
        return await self._request("POST", "/v1/integration/checkout/payments", json_body=body)

    async def get_checkout_payment(self, uid: str) -> dict[str, Any]:
        """GET /api/v1/integration/checkout/payments/{uid} — статус + qr_url/qr_payload."""
        return await self._request("GET", f"/v1/integration/checkout/payments/{uid}")

    async def get_payer(self, phone: str) -> dict[str, Any]:
        """GET /api/v1/integration/checkout/payers/{phone} — статус KYC плательщика."""
        return await self._request("GET", f"/v1/integration/checkout/payers/{phone}")

    async def upload_payer_document(
        self, phone: str, doc_type: str, file_bytes: bytes, filename: str
    ) -> dict[str, Any]:
        """POST /api/v1/integration/checkout/payers/{phone}/documents (multipart).

        ВНИМАНИЕ: подпись multipart-запроса не подтверждена (см. OPEN QUESTIONS).
        Подписываем с пустым body — при INVALID_SIGNATURE пересмотреть.
        """
        path = _API_PREFIX + f"/v1/integration/checkout/payers/{phone}/documents"
        headers = self._sign("POST", path, "")
        form = aiohttp.FormData()
        form.add_field("doc_type", doc_type)
        form.add_field("file", file_bytes, filename=filename, content_type="application/octet-stream")

        url = self._base_url + path
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, data=form, headers=headers) as resp:
                payload = await resp.json(content_type=None)
                return self._unwrap(payload, resp.status)

    async def checkout_channels(self) -> Any:
        """GET /api/v1/integration/checkout/channels — доступные СБП-каналы приёма."""
        return await self._request("GET", "/v1/integration/checkout/channels")
