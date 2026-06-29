import re


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    elif digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits
