"""Минимальный клиент входящих вебхуков Bitrix24 (только stdlib).

Вебхук — секрет: URL никогда не печатается и не попадает в сообщения об ошибках.
"""
import json
import time
import urllib.error
import urllib.request


class B24Error(RuntimeError):
    pass


def b24_call(webhook_url: str, method: str, params: dict | None = None, timeout: int = 90) -> dict:
    url = webhook_url.rstrip("/") + "/" + method
    body = json.dumps(params or {}).encode("utf-8")
    last_err = None
    for attempt in range(1, 6):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                raw = e.read()
                parsed = json.loads(raw.decode("utf-8", "replace"))
                if isinstance(parsed, dict) and parsed.get("error"):
                    detail = f"{parsed.get('error')}: {parsed.get('error_description') or ''}".strip()
                elif raw:
                    detail = raw.decode("utf-8", "replace")[:300].strip()
            except Exception:
                pass
            if e.code >= 500 and attempt < 5:
                time.sleep(2 * attempt)
                continue
            suffix = f" — {detail}" if detail else ""
            raise B24Error(f"Bitrix24 {method}: HTTP {e.code}{suffix}") from None
        except urllib.error.URLError as e:
            last_err = e.reason
            if attempt < 5:
                time.sleep(2 * attempt)
                continue
            raise B24Error(f"Bitrix24 {method}: сеть недоступна ({last_err})") from None
        if isinstance(payload, dict) and payload.get("error"):
            err = str(payload.get("error"))
            if err == "QUERY_LIMIT_EXCEEDED" and attempt < 5:
                time.sleep(5)
                continue
            raise B24Error(
                f"Bitrix24 {method}: {err} ({payload.get('error_description') or ''})".strip()
            )
        return payload
    raise B24Error(f"Bitrix24 {method}: не удалось выполнить запрос")


def b24_list(webhook_url: str, method: str, list_key: str | None, params: dict | None = None) -> list:
    """Пагинация через start; list_key=None — результат сразу список (например user.get)."""
    items = []
    start = None
    while True:
        p = dict(params or {})
        if start is not None:
            p["start"] = start
        payload = b24_call(webhook_url, method, p)
        result = payload.get("result", {})
        if list_key is None:
            batch = result if isinstance(result, list) else []
        else:
            batch = result.get(list_key, []) if isinstance(result, dict) else []
        items.extend(batch or [])
        nxt = payload.get("next")
        if nxt is None:
            return items
        start = nxt


def fetch_projects(webhook_url: str) -> list[dict]:
    """sonet_group.get: группы (проекты), видные пользователю вебхука, [{'ID': ..., 'NAME': ...}, ...]."""
    return b24_list(webhook_url, "sonet_group.get", None)


def fetch_users(webhook_url: str) -> list[dict]:
    """user.get: все пользователи портала."""
    return b24_list(webhook_url, "user.get", None)


def norm_name(value: str) -> str:
    return (value or "").strip().casefold()


def norm_person(value: str) -> str:
    """Нормализация имени человека: пробелы схлопываются, регистр не важен."""
    return " ".join((value or "").split()).casefold()
