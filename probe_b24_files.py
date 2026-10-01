"""Диагностика: какой способ прикрепить файл к задаче/комментарию работает на портале.

Запуск:  python probe_b24_files.py --webhook-url "https://.../rest/1/код/"
(или env B24_WEBHOOK). Создаёт временную задачу PROBE-KANBANO-FILES, пробует
несколько способов, печатает подробные ответы. В конце задачу удаляет
(оставить: --keep).
"""
import argparse
import base64
import json
import os
import sys

from b24_client import B24Error, b24_call

B64 = base64.b64encode(b"kanbano probe file content").decode("ascii")
FILE_NAME = "kanbano_probe.txt"

PROBE_KEYS = ("file", "FILE", "attach", "ATTACH", "UF_")


def short(value, limit=700):
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def print_step(title):
    print(f"\n=== {title} ===", flush=True)


def try_call(title, webhook, method, params):
    print_step(f"{title}: {method} {short(params, 200)}")
    try:
        payload = b24_call(webhook, method, params)
        print("OK:", short(payload.get("result")))
        return payload.get("result")
    except B24Error as e:
        print(f"ОШИБКА: {e}")
        return None


def file_fields_of(task: dict) -> dict:
    out = {}
    for k, v in (task or {}).items():
        if any(p in k for p in PROBE_KEYS) and v not in (None, "", [], {}):
            out[k] = v
    return out


def task_file_state(webhook, task_id) -> dict:
    payload = b24_call(webhook, "tasks.task.get", {"taskId": str(task_id)})
    task = (payload.get("result") or {}).get("task") or {}
    return file_fields_of(task)


def comment_ids(webhook, task_id) -> set:
    payload = b24_call(webhook, "task.commentitem.getlist",
                       {"TASKID": str(task_id), "ORDER": {"ID": "ASC"}})
    result = payload.get("result", [])
    if isinstance(result, dict):
        result = result.get("comments", [])
    return {str(c.get("ID") or c.get("id")) for c in (result or [])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webhook-url", default="", help="входящий вебхук; иначе env B24_WEBHOOK")
    parser.add_argument("--task-id", default="", help="использовать существующую задачу (иначе создать и удалить)")
    parser.add_argument("--keep", action="store_true", help="не удалять созданную задачу")
    args = parser.parse_args()

    webhook = args.webhook_url or os.environ.get("B24_WEBHOOK", "")
    if not webhook:
        print("Нужен --webhook-url или env B24_WEBHOOK", file=sys.stderr)
        sys.exit(1)

    created = False
    task_id = args.task_id
    if task_id:
        print(f"Использую существующую задачу {task_id}")
    else:
        payload = try_call("Создание тестовой задачи", webhook, "tasks.task.add",
                           {"fields": {"TITLE": "PROBE-KANBANO-FILES",
                                       "DESCRIPTION": "Временная задача диагностики, можно удалить"}})
        if not payload:
            sys.exit(1)
        task = payload.get("task") or payload
        task_id = task.get("id") or task.get("ID")
        created = True
        print(f"Создана задача {task_id}")

    print_step("Поля задачи, похожие на файловые (до экспериментов)")
    print(short(task_file_state(webhook, task_id)))

    print_step("S1: tasks.task.update с FILES=[[имя, base64]]")
    r1 = try_call("S1", webhook, "tasks.task.update",
                  {"taskId": str(task_id), "fields": {"FILES": [[FILE_NAME + "1", B64]]}})
    after = task_file_state(webhook, task_id) if r1 is not None else {}
    print("Файловые поля задачи после S1:", short(after))
    s1_ok = bool(after)

    print_step("S2: disk.storage.get (ищем диск пользователя)")
    storages = try_call("S2", webhook, "disk.storage.get", {})
    root_folder = None
    if isinstance(storages, list) and storages:
        st = storages[0]
        print("Первый storage:", short(st))
        root_folder = st.get("ROOT_FOLDER_ID") or st.get("rootFolderId")
    elif isinstance(storages, dict):
        print("storage:", short(storages))
        root_folder = storages.get("ROOT_FOLDER_ID") or storages.get("rootFolderId")

    file_id = None
    if root_folder:
        print_step("S3: disk.folder.uploadfile в корень диска")
        up = try_call("S3", webhook, "disk.folder.uploadfile",
                      {"id": root_folder, "data": {"NAME": FILE_NAME}, "fileContent": B64})
        if isinstance(up, dict):
            print("Ключи ответа загрузки:", sorted(up.keys()))
            file_id = up.get("ID") or up.get("id") or up.get("FILE_ID") or up.get("OBJECT_ID")
            print(f"file_id = {file_id}")

    working = []
    if file_id:
        before_ids = comment_ids(webhook, task_id)
        variants = [
            ("C1: комментарий c UF_TASK_COMMENT_FILES=[\"n<id>\"]",
             {"POST_MESSAGE": "probe c1", "UF_TASK_COMMENT_FILES": [f"n{file_id}"]}),
            ("C2: комментарий c UF_TASK_COMMENT_FILES=[<id>]",
             {"POST_MESSAGE": "probe c2", "UF_TASK_COMMENT_FILES": [str(file_id)]}),
        ]
        for title, fields in variants:
            print_step(title)
            payload = try_call(title, webhook, "task.commentitem.add",
                               {"TASKID": str(task_id), "FIELDS": fields})
            if payload is None:
                continue
            after = comment_ids(webhook, task_id)
            new_id = next((i for i in after - before_ids), None)
            before_ids = after
            if not new_id:
                print("  новый комментарий не найден")
                continue
            payload = b24_call(webhook, "task.commentitem.get",
                               {"TASKID": str(task_id), "COMMENTID": new_id})
            cm = payload.get("result") or {}
            hits = file_fields_of(cm)
            print("  новый комментарий:", short(cm, 900))
            print("  файловые поля:", short(hits))
            if hits:
                working.append((title, hits))

    print("\n========== ИТОГ ==========")
    print(f"S1 (FILES в tasks.task.update): {'работает' if s1_ok else 'НЕ работает (файлы не появились)'}")
    for title, hits in working:
        print(f"РАБОЧИЙ СПОСОБ: {title} -> {short(hits)}")
    if not working and not s1_ok:
        print("Ни один способ не сработал — пришлите весь вывод целиком.")

    if created and not args.keep:
        print_step("Удаление тестовой задачи")
        try_call("Удаление", webhook, "tasks.task.delete", {"taskId": str(task_id)})


if __name__ == "__main__":
    main()
