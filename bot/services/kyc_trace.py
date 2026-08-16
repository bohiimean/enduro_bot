"""След KYC в логах.

До этого по логам нельзя было ни найти конкретного пользователя, ни понять, что
именно ответил Alfabit: из всего ответа в лог попадало одно поле `processing`.
Разбор двух живых случаев (16.08.2026) пришлось делать запросами к API вручную —
в логах о них не было ни строчки.

Телефон пишем маскированным: код страны и последние четыре цифры. Этого хватает,
чтобы сопоставить строку лога с обращением клиента, и мало, чтобы лог стал
базой номеров. Логи уезжают в json-file драйвер Docker и живут на диске — то
самое, от чего 08.08 отказались, храня телефоны файлом.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("kyc")


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return "***"
    return f"+{digits[0]}***{digits[-4:]}"


def log_payer(event: str, *, user_id: int | None, phone: str, payer: dict) -> None:
    """Полное состояние заявки на каждом ответе Alfabit.

    `name` — только признак наличия ФИО, не само ФИО: пустое поле после
    распознавания означает, что главную страницу паспорта прочитать не удалось,
    и это единственный признак проблемы с качеством снимка, который сервис
    отдаёт.
    """
    logger.info(
        "%s: user=%s phone=%s status=%s processing=%s manual_review=%s name=%s",
        event,
        user_id,
        mask_phone(phone),
        payer.get("kyc_status"),
        payer.get("processing"),
        payer.get("manual_review"),
        "yes" if payer.get("expected_payer_name") else "no",
    )


def log_upload(
    event: str, *, user_id: int | None, phone: str, doc_type: str, result: dict
) -> None:
    """Ответ на загрузку документа. `low_confidence` живёт только здесь — в
    GET /payers его нет, так что другого шанса увидеть «распозналось плохо» у
    бота не будет."""
    logger.info(
        "%s: user=%s phone=%s doc=%s processing=%s low_confidence=%s status=%s",
        event,
        user_id,
        mask_phone(phone),
        doc_type,
        result.get("processing"),
        result.get("low_confidence"),
        result.get("kyc_status"),
    )
