"""Досылка комментариев и файлов из kanbano_export.json в Bitrix24 после CSV-импорта.

Задачи сопоставляются по названию внутри проекта (тег задачи -> проект Bitrix24).
Вебхук — секрет: берётся из --webhook-url или env B24_WEBHOOK и никогда не печатается.
По умолчанию работает в режиме --dry-run; реальные изменения — только с --apply.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from b24_client import B24Error, b24_call, b24_list, fetch_projects, fetch_users, norm_name, norm_person
from kanbano_export import safe_filename
from kanbano_json_to_csv import html_to_text, parse_dt

FILES_COMMENT_TEXT = "Файлы из Kanbano"
AMBIGUOUS = object()


def first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def dt_key(value: str):
    dt = parse_dt(value)
    if dt is None:
        return datetime.max
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def api_sleep(delay_s: float) -> None:
    time.sleep(delay_s)


def load_projects_by_key(webhook: str) -> dict:
    """Нормализованное имя проекта (группы) -> {'id': groupId, 'name': имя}. Источник: sonet_group.get."""
    by_key = {}
    for p in fetch_projects(webhook):
        name = (first(p, "name", "NAME") or "").strip()
        gid = to_int(first(p, "id", "ID"))
        key = norm_name(name)
        if key and gid is not None and key not in by_key:
            by_key[key] = {"id": str(gid), "name": name}
    print(f"[followup] проекты Bitrix24: {len(by_key)} шт.", flush=True)
    return by_key


def build_title_map(webhook: str, group_id: str, delay_s: float, name: str = "") -> dict:
    """Нормализованное название задачи -> bitrix taskId; дубли -> AMBIGUOUS."""
    label = f"проект «{name}»" if name else f"группа {group_id}"
    print(f"[followup] загружаю задачи: {label}…", flush=True)
    tasks = b24_list(
        webhook,
        "tasks.task.list",
        "tasks",
        # у старого tasks.task.list ключ фильтра — GROUP_ID; groupId он молча игнорирует
        params={"filter": {"GROUP_ID": group_id}, "select": ["ID", "TITLE", "groupId"]},
    )
    api_sleep(delay_s)
    # страховка: если сервер проигнорировал фильтр группы, отбрасываем чужие задачи
    in_group = []
    for t in tasks:
        t_group = first(t, "groupId", "GROUP_ID")
        if t_group is None or str(t_group) == str(group_id):
            in_group.append(t)
    if len(in_group) != len(tasks):
        print(f"[followup] внимание: сервер вернул {len(tasks)} задач без учёта группы; "
              f"по группе {group_id} осталось {len(in_group)}", flush=True)
    tasks = in_group
    titles = {}
    for t in tasks:
        tid = str(first(t, "id", "ID") or "").strip()
        title = (first(t, "title", "TITLE") or "").strip()
        if not tid or not title:
            continue
        key = norm_name(title)
        titles[key] = AMBIGUOUS if key in titles else tid
    print(f"[followup] задачи {label}: {len(titles)} шт.", flush=True)
    return titles


def build_user_maps(webhook: str, delay_s: float) -> tuple:
    """user.get -> (имя -> bitrix id, email -> bitrix id, id -> имя).

    Основное сопоставление — по имени (Имя Фамилия; регистр и пробелы не важны),
    email — запасной вариант.
    """
    print("[followup] загружаю пользователей Bitrix24…", flush=True)
    by_name, by_email, names = {}, {}, {}
    for u in fetch_users(webhook):
        uid = to_int(first(u, "ID", "id"))
        if uid is None:
            continue
        uname = " ".join(x for x in [first(u, "NAME", "name") or "",
                                     first(u, "LAST_NAME", "last_name") or ""] if x).strip()
        names[uid] = uname or str(uid)
        key = norm_person(uname)
        if key and key not in by_name:
            by_name[key] = uid
        email = (first(u, "EMAIL", "email") or "").strip().casefold()
        if email and email not in by_email:
            by_email[email] = uid
    api_sleep(delay_s)
    print(f"[followup] пользователи Bitrix24: {len(names)} шт.", flush=True)
    return by_name, by_email, names


def get_existing_comments(webhook: str, bitrix_task_id: str, delay_s: float) -> list:
    payload = b24_call(
        webhook,
        "task.commentitem.getlist",
        {"TASKID": bitrix_task_id, "ORDER": {"ID": "ASC"}},
    )
    api_sleep(delay_s)
    result = payload.get("result", [])
    if isinstance(result, dict):
        result = result.get("comments", [])
    return result if isinstance(result, list) else []


def add_comment(webhook: str, bitrix_task_id: str, text: str, author_id, delay_s: float):
    """Комментарий без файлов: современные порталы не принимают FILES в task.commentitem.add."""
    fields = {"POST_MESSAGE": text}
    if author_id:
        fields["AUTHOR_ID"] = int(author_id)
    b24_call(webhook, "task.commentitem.add", {"TASKID": str(bitrix_task_id), "FIELDS": fields})
    api_sleep(delay_s)


def get_disk_root_folder(webhook: str, delay_s: float) -> str:
    """Корневая папка Диска пользователя вебхука (куда заливаются файлы перед прикреплением).

    disk.storage.get требует id хранилища, поэтому сначала disk.storage.getList:
    выбираем личное хранилище пользователя из URL вебхука (ENTITY_TYPE=user),
    иначе первое попавшееся с корневой папкой.
    """
    webhook_user = None
    m = re.search(r"/rest/(?:api/)?(\d+)/", webhook)
    if m:
        webhook_user = m.group(1)
    try:
        payload = b24_call(webhook, "disk.storage.getList", {})
    except B24Error as e:
        if "ERROR_METHOD_NOT_FOUND" not in str(e):
            raise
        payload = b24_call(webhook, "disk.storage.get", {})
    result = payload.get("result") or []
    if isinstance(result, dict):
        result = result.get("storages") or []
    api_sleep(delay_s)

    personal = None
    first_any = None
    for st in result:
        if not isinstance(st, dict):
            continue
        root = first(st, "ROOT_OBJECT_ID", "ROOT_FOLDER_ID", "rootObjectId", "rootFolderId")
        if not root:
            continue
        if first_any is None:
            first_any = str(root)
        et = (first(st, "ENTITY_TYPE", "entityType") or "").strip().casefold()
        eid = str(first(st, "ENTITY_ID", "entityId") or "")
        if et == "user" and webhook_user and eid == webhook_user and personal is None:
            personal = str(root)
    chosen = personal or first_any
    if not chosen:
        raise B24Error("disk.storage.getList: не найдено хранилище с корневой папкой")
    return str(chosen)


def upload_disk_file(webhook: str, folder_id: str, name: str, b64: str, delay_s: float,
                     disk_name: str = "") -> int:
    """Загрузка на Диск. Имена в одной папке должны быть уникальны: при коллизии
    (DISK_OBJ_22000 «Файл с таким именем уже есть») подбираем суффикс (1), (2), …"""
    disk_name = disk_name or name
    params = {"id": str(folder_id),
              "data": {"NAME": disk_name},
              "fileContent": [name, b64]}
    try:
        payload = b24_call(webhook, "disk.folder.uploadfile", params)
    except B24Error as e:
        collision = "уже есть" in str(e) or "DISK_OBJ_" in str(e)
        if not collision:
            raise
        # в корне Диска копятся файлы прошлых прогонов — перебираем суффиксы,
        # пока не найдём свободное имя (коллизий конечное число)
        stem, suffix = os.path.splitext(disk_name)
        last = e
        n = 1
        while n <= 200:
            params["data"] = {"NAME": f"{stem} ({n}){suffix}"}
            try:
                payload = b24_call(webhook, "disk.folder.uploadfile", params)
                break
            except B24Error as e2:
                last = e2
                if "уже есть" not in str(e2) and "DISK_OBJ_" not in str(e2):
                    raise
                n += 1
        else:
            raise last
    result = payload.get("result") or {}
    fid = to_int(first(result, "ID", "id"))
    api_sleep(delay_s)
    if fid is None:
        raise B24Error("disk.folder.uploadfile: в ответе нет ID файла")
    return fid


def attach_task_files(webhook: str, bitrix_task_id: str, files: list, folder_id: str,
                      delay_s: float, files_done: dict, used_disk_names: set) -> tuple:
    """Загружает файлы на Диск (уникальные имена) и прикрепляет к задаче
    через tasks.task.file.attach (фолбэк: UF_TASK_WEBDAV_FILES).

    files_done: {ключ: disk id} — файлы, уже загруженные и прикреплённые ранее (маркер).
    Возвращает (прикреплено сейчас, пропущено по маркеру, не загрузилось).
    """
    to_upload = []
    skipped_marker = 0
    for pos, (name, b64) in enumerate(files):
        key = f"{pos}:{norm_name(name)}"
        if key in files_done:
            skipped_marker += 1
            continue
        stem, suffix = os.path.splitext(name)
        disk_name = name
        n = 0
        while norm_name(disk_name) in used_disk_names:
            n += 1
            disk_name = f"{stem} ({n}){suffix}"
        used_disk_names.add(norm_name(disk_name))
        to_upload.append((key, name, b64, disk_name))

    uploaded = []  # (key, disk_id)
    failed = 0
    for key, name, b64, disk_name in to_upload:
        try:
            fid = upload_disk_file(webhook, folder_id, name, b64, delay_s, disk_name)
        except B24Error as e:
            failed += 1
            print(f"  файл «{name}» не загружен на Диск: {e}", file=sys.stderr)
            continue
        uploaded.append((key, fid))
        api_sleep(delay_s)

    if uploaded:
        try:
            b24_call(webhook, "tasks.task.file.attach",
                     {"taskId": int(bitrix_task_id), "fileIds": [fid for _, fid in uploaded]})
        except B24Error as e:
            if "ERROR_METHOD_NOT_FOUND" not in str(e):
                raise
            # старые порталы: те же ID Диска через пользовательское поле задачи
            b24_call(webhook, "tasks.task.update",
                     {"taskId": str(bitrix_task_id),
                      "fields": {"UF_TASK_WEBDAV_FILES": [f"n{fid}" for _, fid in uploaded]}})
        api_sleep(delay_s)
        for key, fid in uploaded:
            files_done[key] = fid
    return len(uploaded), skipped_marker, failed


def find_local_file(files_dir: Path, task_id, att: dict):
    """Локальный файл вложения: атрибут file из экспорта или files_dir/<taskId>/<имя>."""
    recorded = att.get("file")
    if recorded:
        p = Path(recorded)
        if p.is_file():
            return p
    p = files_dir / str(task_id) / safe_filename(att.get("name"))
    return p if p.is_file() else None


def att_payload(att: dict, files_dir: Path, task_id):
    """(name, base64) вложения либо None, если данных нет ни в JSON, ни на диске."""
    data = att.get("data")
    if data:
        return safe_filename(att.get("name")), data
    local = find_local_file(files_dir, task_id, att)
    if local:
        return safe_filename(att.get("name")), base64.b64encode(local.read_bytes()).decode("ascii")
    return None


_AUTHOR_KEY_RE = re.compile(r"author|creat|sender|user|member|from", re.I)


def resolve_comment_author_id(comment: dict):
    """ID автора комментария из экспорта: authorId, иначе похожее поле из extra.

    (экспорт кладёт неизвестные поля сообщения в extra, если поле автора
    называется не так, как ожидается)
    """
    candidates = [comment.get("authorId")]
    for k, v in sorted((comment.get("extra") or {}).items()):
        if _AUTHOR_KEY_RE.search(k) and isinstance(v, (int, str)) and str(v).strip().lstrip("-").isdigit():
            candidates.append(v)
    for v in candidates:
        n = to_int(v)
        if n is not None:
            return n
    return None


def collect_steps(task, comments, attachments, files_dir: Path, author_for, max_file_bytes: int, report_files: list):
    """Список шагов отправки: [{'cid', 'text', 'author', 'files': [(name, b64)]}, ...]."""
    label = f"#{task.get('id')} «{task.get('name') or ''}»"

    def take_file(att, text):
        payload = att_payload(att, files_dir, task.get("id"))
        if payload is None:
            link = att.get("link") or ""
            report_files.append(f"{label}: «{att.get('name')}» — файла нет, вставлена ссылка")
            if link:
                text = (text + ("\n" if text else "") + f"{att.get('name')} — {link}").strip()
            return None, text
        name, b64 = payload
        if len(b64) * 3 // 4 > max_file_bytes:
            report_files.append(f"{label}: «{name}» больше {max_file_bytes // (1024 * 1024)} МБ — пропущен")
            return None, text
        return (name, b64), text

    steps = []
    ordered = sorted(comments, key=lambda c: (dt_key(c.get("createdAt")), str(c.get("id") or "")))
    for c in ordered:
        text = html_to_text(c.get("text") or "")
        files = []
        for att in c.get("attachments") or []:
            item, text = take_file(att, text)
            if item:
                files.append(item)
        if files:
            # FILES в комментарии не поддерживаются порталом — файлы вкладываются в задачу,
            # а в текст комментария идёт список имён
            text = text + "\n[Файлы: " + ", ".join(n for n, _ in files) + "]"
        if not text and not files:
            continue
        steps.append({"cid": c.get("id"), "text": text,
                      "author": author_for(resolve_comment_author_id(c)), "files": files})

    message_ids = {c.get("id") for c in ordered}
    standalone = []
    for att in attachments:
        if att.get("messageId") in message_ids:
            continue
        item, _ = take_file(att, "")
        if item:
            standalone.append(item)
    if standalone:
        standalone_text = FILES_COMMENT_TEXT + "\n[Файлы: " + ", ".join(n for n, _ in standalone) + "]"
        steps.append({"cid": None, "text": standalone_text, "author": None, "files": standalone})
    return steps


def get_comment_author(webhook: str, bitrix_task_id: str, comment_id, delay_s: float):
    payload = b24_call(webhook, "task.commentitem.get",
                       {"TASKID": str(bitrix_task_id), "COMMENTID": str(comment_id)})
    result = payload.get("result") or {}
    api_sleep(delay_s)
    return to_int(first(result, "AUTHOR_ID", "author_id"))


def run_fix_authors(args, webhook: str, data, done: dict, done_path: Path) -> None:
    """Смена авторов уже отправленных комментариев на оригинальных (сопоставление по имени, email — запасной).

    Сначала пробует task.commentitem.update с AUTHOR_ID (сохраняет даты);
    если портал игнорирует смену автора — пересоздаёт комментарий (delete + add).
    """
    delay_s = args.delay_ms / 1000
    boards = data.get("boards", data if isinstance(data, list) else [])
    board_filter = {int(x) for x in args.boards.split(",") if x.strip()}
    max_file_bytes = args.max_file_mb * 1024 * 1024
    files_dir = Path(args.files_dir)

    try:
        by_name, by_email, user_names = build_user_maps(webhook, delay_s)
    except B24Error as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    details = []
    error_reasons = Counter()
    changed = recreated = already = no_author = mismatches = failed = 0
    tasks_skipped_marker = 0
    no_match_users = set()
    use_update = None  # None = ещё не проверено
    applied = 0

    for board in boards:
        if board_filter and board.get("id") not in board_filter:
            continue
        users_by_id = {}
        for m in board.get("members") or []:
            mid = to_int(first(m, "id", "ID"))
            if mid is not None:
                users_by_id[mid] = m

        for task in board.get("tasks", []):
            label = f"#{task.get('id')} «{task.get('name') or ''}»"
            state = done.get(str(task.get("id"))) or {}
            target = state.get("bitrixTaskId")
            if not target:
                continue
            if state.get("authorsFixed"):
                details.append(f"{label}: авторы уже исправлены ранее (маркер), пропуск")
                tasks_skipped_marker += 1
                continue
            comments = task.get("comments") or []
            attachments = task.get("attachments") or []
            if not comments and not attachments:
                continue

            def author_for(uid):
                member = users_by_id.get(uid) if uid is not None else None
                key = norm_person((member or {}).get("name") or "")
                if key and key in by_name:
                    return by_name[key]
                email = ((member or {}).get("email") or "").strip().casefold()
                if email and email in by_email:
                    return by_email[email]
                if uid is not None:
                    no_match_users.add(uid)
                return None

            steps = collect_steps(task, comments, attachments, files_dir, author_for,
                                  max_file_bytes, [])
            portal = get_existing_comments(webhook, target, delay_s)
            portal.sort(key=lambda c: to_int(first(c, "ID", "id")) or 0)
            if len(portal) != len(steps):
                mismatches += 1
                details.append(f"{label}: на портале {len(portal)} комментариев, в экспорте шагов {len(steps)} — пропуск")
                continue

            task_changed = task_recreated = 0
            for pc, step in zip(portal, steps):
                pid = str(first(pc, "ID", "id"))
                current = to_int(first(pc, "AUTHOR_ID", "author_id"))
                desired = step["author"]
                if desired is None:
                    no_author += 1
                    continue
                if current == desired:
                    already += 1
                    continue
                ptext = norm_name(first(pc, "POST_MESSAGE", "post_message") or "")
                stext = norm_name(step["text"] or FILES_COMMENT_TEXT)
                if stext[:40] and stext[:40] not in ptext:
                    mismatches += 1
                    details.append(f"{label}: комментарий {pid} не совпал по тексту — пропуск")
                    continue
                if not args.apply:
                    details.append(f"{label}: {pid}: автор {user_names.get(current, current)} -> "
                                   f"{user_names.get(desired, desired)}")
                    continue

                if use_update is None:
                    try:
                        b24_call(webhook, "task.commentitem.update",
                                 {"TASKID": str(target), "COMMENTID": pid,
                                  "FIELDS": {"AUTHOR_ID": int(desired)}})
                        api_sleep(delay_s)
                        use_update = get_comment_author(webhook, target, pid, delay_s) == desired
                        if not use_update:
                            print("Портал игнорирует AUTHOR_ID при update — комментарии будут "
                                  "пересозданы (даты станут текущими)", flush=True)
                    except B24Error as e:
                        use_update = False
                        print(f"Update автора не поддерживается ({e}) — комментарии будут "
                              f"пересозданы (даты станут текущими)", flush=True)

                if use_update:
                    try:
                        b24_call(webhook, "task.commentitem.update",
                                 {"TASKID": str(target), "COMMENTID": pid,
                                  "FIELDS": {"AUTHOR_ID": int(desired)}})
                        changed += 1
                        task_changed += 1
                    except B24Error as e:
                        failed += 1
                        error_reasons[f"{type(e).__name__}: {e}"] += 1
                        print(f"Ошибка смены автора {label} (комментарий {pid}): {e}", file=sys.stderr)
                        continue
                else:
                    try:
                        b24_call(webhook, "task.commentitem.delete",
                                 {"TASKID": str(target), "COMMENTID": pid})
                        add_comment(webhook, target, step["text"], desired, delay_s)
                        recreated += 1
                        task_recreated += 1
                    except B24Error as e:
                        failed += 1
                        error_reasons[f"{type(e).__name__}: {e}"] += 1
                        print(f"Ошибка пересоздания {label} (комментарий {pid}): {e}", file=sys.stderr)
                        continue
                details.append(f"{label}: {pid}: автор -> {user_names.get(desired, desired)}"
                               + (" (update)" if use_update else " (пересоздан)"))

            if args.apply:
                state["authorsFixed"] = failed == 0
                done_path.write_text(
                    json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                if task_changed or task_recreated:
                    applied += 1
                    print(f"Авторы: {label} -> задача {target} (изменено {task_changed}, "
                          f"пересоздано {task_recreated})", flush=True)

    print()
    for line in details:
        print(line)
    print()
    print(f"Комментариев: изменено {changed}, пересоздано {recreated}, уже верно {already}, "
          f"без автора в Kanbano/email {no_author}, пропущено по несоответствию {mismatches}, ошибок {failed}")
    print(f"Задач пропущено по маркеру (авторы уже исправлены): {tasks_skipped_marker}")
    if no_match_users:
        print(f"Пользователи Kanbano без совпадения по имени и email (оставлены как есть): {sorted(no_match_users)}")
    if error_reasons:
        print("Причины ошибок:")
        for reason, cnt in error_reasons.most_common():
            print(f"  {cnt}× {reason}")
    if not args.apply:
        print("Режим dry-run: ничего не изменено. Для применения запустите с --apply")


def run_check_authors(args, webhook: str, data) -> None:
    """Сопоставление авторов комментариев Kanbano с пользователями Bitrix24 (только чтение).

    Основной способ — имя участника Kanbano против Имя+Фамилия пользователя Bitrix24
    (регистр и пробелы не важны), запасной — совпадение email.
    """
    delay_s = args.delay_ms / 1000
    boards = data.get("boards", data if isinstance(data, list) else [])
    board_filter = {int(x) for x in args.boards.split(",") if x.strip()}
    try:
        by_name, by_email, bitrix_names = build_user_maps(webhook, delay_s)
    except B24Error as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    stats = {}  # kanbano uid -> {"name": имя, "email": email, "bitrix": (uid, имя) | None, "comments": n}
    no_author = 0
    sample_extra = None
    for board in boards:
        if board_filter and board.get("id") not in board_filter:
            continue
        users_by_id = {}
        for m in board.get("members") or []:
            mid = to_int(first(m, "id", "ID"))
            if mid is not None:
                users_by_id[mid] = m
        for task in board.get("tasks", []):
            for c in task.get("comments") or []:
                uid = resolve_comment_author_id(c)
                if uid is None:
                    no_author += 1
                    if sample_extra is None:
                        sample_extra = c.get("extra") or {}
                    continue
                member = users_by_id.get(uid) or {}
                st = stats.setdefault(uid, {
                    "name": (member.get("name") or "").strip() or f"#{uid}",
                    "email": (member.get("email") or "").strip(),
                    "bitrix": None, "comments": 0})
                st["comments"] += 1
                if st["bitrix"] is None:
                    key = norm_person(member.get("name") or "")
                    if key and key in by_name:
                        st["bitrix"] = (by_name[key], bitrix_names.get(by_name[key], str(by_name[key])))
                    else:
                        email = (member.get("email") or "").strip().casefold()
                        if email and email in by_email:
                            st["bitrix"] = (by_email[email],
                                            bitrix_names.get(by_email[email], str(by_email[email])))

    print()
    print("Сопоставление авторов комментариев (Kanbano -> Bitrix24):")
    total = matched = unmatched = 0
    for uid, st in sorted(stats.items(), key=lambda kv: -kv[1]["comments"]):
        total += st["comments"]
        if st["bitrix"]:
            matched += st["comments"]
            print(f"  {st['name']} (Kanbano #{uid}) -> {st['bitrix'][1]} (Bitrix #{st['bitrix'][0]}): "
                  f"{st['comments']} комм.")
        else:
            unmatched += st["comments"]
            print(f"  {st['name']} (Kanbano #{uid}) -> НЕ НАЙДЕН по имени и email: {st['comments']} комм. "
                  f"(комментарии уйдут от владельца вебхука)")
    print()
    print(f"Всего комментариев: {total + no_author}; сопоставлено: {matched}; "
          f"не сопоставлено: {unmatched}; без автора в экспорте: {no_author}")
    if no_author:
        print(f"Поля неизвестного комментария-примера (extra): {json.dumps(sample_extra, ensure_ascii=False)}")
        print("Пришлите эту строку — добавлю правильное поле автора в экспортёр.")
    if unmatched:
        print("Чтобы комментарии ушли от правильных авторов, приведите имена участников Kanbano "
              "к именам пользователей Bitrix24 (Имя Фамилия; регистр и лишние пробелы не важны) "
              "или совпадающим email — затем запустите --fix-authors.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Досылка комментариев и файлов Kanbano в Bitrix24 (после импорта задач через CSV)"
    )
    parser.add_argument("--input", "-i", default="kanbano_export.json", help="входной JSON экспорта")
    parser.add_argument("--files-dir", default="kanbano_files", help="каталог со скачанными файлами")
    parser.add_argument("--webhook-url", default="",
                        help="входящий вебхук Bitrix24; иначе env B24_WEBHOOK")
    parser.add_argument("--boards", default="", help="фильтр по id досок Kanbano через запятую")
    parser.add_argument("--include-archived-tasks", action="store_true")
    parser.add_argument("--import-tag", default="Импорт",
                        help="метка, исключаемая из подбора проекта (как в конвертере)")
    parser.add_argument("--delay-ms", type=int, default=500,
                        help="пауза между запросами к Bitrix24, мс (по умолчанию 500)")
    parser.add_argument("--done-file", default="bitrix_followup_done.json",
                        help="файл-маркер успешно отправленных задач")
    parser.add_argument("--max-file-mb", type=int, default=25,
                        help="пропускать файлы больше этого размера, МБ")
    parser.add_argument("--no-task-files", action="store_true",
                        help="не вкладывать файлы в задачу через tasks.task.update "
                             "(в комментариях останется только список имён)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false",
                      help="только показать план действий (по умолчанию)")
    mode.add_argument("--apply", dest="apply", action="store_true",
                      help="реально создать комментарии и файлы")
    parser.set_defaults(apply=False)
    parser.add_argument("--attach-files-only", action="store_true",
                        help="только прикрепить файлы к задачам, комментарии не трогать "
                             "(для случая, когда комментарии уже отправлены, а файлы — нет); "
                             "файлы с именами, уже присутствующими на задаче, пропускаются")
    parser.add_argument("--fix-authors", action="store_true",
                        help="сменить авторов уже отправленных комментариев на оригинальных "
                             "(сопоставление по имени, email — запасной вариант; "
                             "при необходимости комментарии пересоздаются)")
    parser.add_argument("--check-authors", action="store_true",
                        help="показать сопоставление авторов комментариев Kanbano -> Bitrix24 "
                             "(только чтение, без изменений)")
    args = parser.parse_args()

    webhook = args.webhook_url or os.environ.get("B24_WEBHOOK", "")
    if not webhook:
        print("Не задан вебхук Bitrix24: используйте --webhook-url или переменную окружения B24_WEBHOOK",
              file=sys.stderr)
        sys.exit(1)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Файл не найден: {in_path}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(in_path.read_text(encoding="utf-8-sig"))
    boards = data.get("boards", data if isinstance(data, list) else [])
    board_filter = {int(x) for x in args.boards.split(",") if x.strip()}
    files_dir = Path(args.files_dir)
    delay_s = args.delay_ms / 1000
    max_file_bytes = args.max_file_mb * 1024 * 1024

    done_path = Path(args.done_file)
    done = {}
    if done_path.exists():
        try:
            done = json.loads(done_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"Внимание: маркер-файл повреждён, начинаю с нуля ({e})")

    if args.fix_authors:
        if args.attach_files_only:
            print("Флаги --fix-authors и --attach-files-only взаимоисключающие", file=sys.stderr)
            sys.exit(1)
        run_fix_authors(args, webhook, data, done, done_path)
        return

    if args.check_authors:
        run_check_authors(args, webhook, data)
        return

    try:
        projects_by_key = load_projects_by_key(webhook)
    except B24Error as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
    if not projects_by_key:
        print("Список проектов Bitrix24 пуст — сопоставление задач невозможно", file=sys.stderr)
        sys.exit(1)

    import_key = norm_name(args.import_tag)
    title_maps = {}
    user_maps = None
    disk_root = None
    used_disk_names = set()

    report_missing_project = []
    report_unmatched = []
    report_ambiguous = []
    report_users = set()
    report_files = []
    error_reasons = Counter()
    plan = []
    matched = 0
    applied = 0
    errors = 0

    # предпрогон: сколько задач предстоит обработать (для прогресса)
    tasks_total = 0
    for board in boards:
        if board_filter and board.get("id") not in board_filter:
            continue
        for task in board.get("tasks", []):
            if task.get("isArchived") and not args.include_archived_tasks:
                continue
            if task.get("comments") or task.get("attachments"):
                tasks_total += 1
    print(f"[followup] задач с комментариями/файлами к обработке: {tasks_total}", flush=True)
    tasks_done = 0

    for board in boards:
        if board_filter and board.get("id") not in board_filter:
            continue
        users_by_id = {}
        for m in board.get("members") or []:
            mid = to_int(first(m, "id", "ID"))
            if mid is not None:
                users_by_id[mid] = m
        tags_by_id = {}
        for t in board.get("tags") or []:
            tid = to_int(first(t, "id", "ID"))
            if tid is not None:
                tags_by_id[tid] = t

        for task in board.get("tasks", []):
            if task.get("isArchived") and not args.include_archived_tasks:
                continue
            comments = task.get("comments") or []
            attachments = task.get("attachments") or []
            if not comments and not attachments:
                continue
            label = f"#{task.get('id')} «{task.get('name') or ''}»"
            tasks_done += 1
            if tasks_done % 10 == 0 or tasks_done == tasks_total:
                print(f"[followup] обработано задач: {tasks_done}/{tasks_total}", flush=True)

            prev = done.get(str(task.get("id")))
            if (not args.attach_files_only and isinstance(prev, dict)
                    and prev.get("bitrixTaskId") and prev.get("complete", True)):
                plan.append(f"{label}: уже отправлено ранее (маркер), пропуск")
                continue

            project = None
            for v in task.get("tags") or []:
                tv = to_int(v.get("id") if isinstance(v, dict) else v)
                if tv is None:
                    continue
                tag_name = ((tags_by_id.get(tv) or {}).get("name") or "").strip()
                key = norm_name(tag_name)
                if not key or key == import_key:
                    continue
                if key in projects_by_key:
                    project = projects_by_key[key]
                    break
            if project is None:
                report_missing_project.append(label)
                continue
            if project["name"] not in title_maps:
                try:
                    title_maps[project["name"]] = build_title_map(webhook, project["id"], delay_s,
                                                                  project["name"])
                except B24Error as e:
                    print(f"Ошибка: {e}", file=sys.stderr)
                    sys.exit(1)
            titles = title_maps[project["name"]]

            tkey = norm_name(task.get("name") or "")
            target = titles.get(tkey)
            if target is None:
                report_unmatched.append(f"{label} (проект «{project['name']}»)")
                continue
            if target is AMBIGUOUS:
                report_ambiguous.append(f"{label} (проект «{project['name']}»)")
                continue
            titles[tkey] = AMBIGUOUS

            if user_maps is None and not args.attach_files_only:
                try:
                    user_maps = build_user_maps(webhook, delay_s)
                except B24Error as e:
                    print(f"Ошибка: {e}", file=sys.stderr)
                    sys.exit(1)

            if args.attach_files_only:
                def author_for(uid):
                    return None
            else:
                by_name, by_email, _ = user_maps

                def author_for(uid):
                    member = users_by_id.get(uid) if uid is not None else None
                    key = norm_person((member or {}).get("name") or "")
                    if key and key in by_name:
                        return by_name[key]
                    email = ((member or {}).get("email") or "").strip().casefold()
                    if email and email in by_email:
                        return by_email[email]
                    if uid is not None:
                        report_users.add(uid)
                    return None

            steps = collect_steps(task, comments, attachments, files_dir, author_for,
                                  max_file_bytes, report_files)
            matched += 1

            if args.attach_files_only:
                # комментарии не трогаем: только прикрепляем файлы к задаче
                task_files = [f for s in steps for f in s["files"]]
                if not task_files:
                    plan.append(f"{label} -> задача {target} (проект «{project['name']}»): файлов нет")
                    continue
                plan.append(f"{label} -> задача {target} (проект «{project['name']}»): вложить файлов {len(task_files)}")
                if not args.apply:
                    continue
                state = done.setdefault(str(task.get("id")), {})
                state_files = state.setdefault("filesDone", {})
                state.update({
                    "bitrixTaskId": target,
                    "boardId": board.get("id"),
                    "files": len(task_files),
                })
                try:
                    if disk_root is None:
                        disk_root = get_disk_root_folder(webhook, delay_s)
                    attached, marker_skipped, up_failed = attach_task_files(
                        webhook, target, task_files, disk_root, delay_s, state_files, used_disk_names)
                    state["taskFilesSent"] = up_failed == 0
                    state["complete"] = up_failed == 0
                    state["at"] = datetime.now(timezone.utc).isoformat()
                    done_path.write_text(
                        json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    applied += 1
                    if up_failed:
                        report_files.append(
                            f"{label}: не загружено на Диск {up_failed} из {len(task_files)} файлов "
                            f"(остались локально в {files_dir}\\{task.get('id')}\\)"
                        )
                    print(f"Файлы: {label} -> задача {target} "
                          f"(прикреплено {attached}, уже загружено ранее {marker_skipped}, "
                          f"не загрузилось {up_failed})")
                except (B24Error, ValueError) as e:
                    errors += 1
                    error_reasons[f"{type(e).__name__}: {e}"] += 1
                    print(f"Ошибка при вложении файлов в задачу {label}: {e}", file=sys.stderr)
                continue

            if not steps:
                plan.append(f"{label} -> задача {target} (проект «{project['name']}»): нечего отправлять")
                continue

            plan.append(f"{label} -> задача {target} (проект «{project['name']}»):")
            for s in steps:
                author = f"автор {s['author']}" if s["author"] else "автор = владелец вебхука"
                nfiles = f", файлов {len(s['files'])}" if s["files"] else ""
                preview = (s["text"] or FILES_COMMENT_TEXT)[:60].replace("\n", " ")
                plan.append(f"    комментарий ({author}{nfiles}): {preview}")

            if not args.apply:
                continue

            state = done.setdefault(str(task.get("id")), {})
            state.update({
                "bitrixTaskId": target,
                "boardId": board.get("id"),
                "comments": len(steps),
                "files": sum(len(s["files"]) for s in steps),
            })
            sent = skipped = failed = 0

            try:
                existing = get_existing_comments(webhook, target, delay_s)
            except B24Error as e:
                errors += 1
                error_reasons[f"{type(e).__name__}: {e}"] += 1
                print(f"Ошибка при чтении комментариев {label}: {e}", file=sys.stderr)
                continue
            seen_comments = set()
            for c in existing:
                aid = to_int(first(c, "AUTHOR_ID", "author_id")) or 0
                seen_comments.add((aid, norm_name(first(c, "POST_MESSAGE", "post_message") or "")))

            sent_ids = state.setdefault("sentComments", {})
            sent = skipped = failed = 0
            for s in steps:
                step_id = str(s["cid"]) if s["cid"] is not None else "files"
                if step_id in sent_ids:
                    continue
                if (s["author"] or 0, norm_name(s["text"])) in seen_comments:
                    sent_ids[step_id] = "on-portal"
                    skipped += 1
                else:
                    preview = (s["text"] or FILES_COMMENT_TEXT)[:40].replace("\n", " ")
                    try:
                        add_comment(webhook, target, s["text"], s["author"], delay_s)
                    except (B24Error, ValueError) as e:
                        failed += 1
                        errors += 1
                        reason = f"{type(e).__name__}: {e}"
                        error_reasons[reason] += 1
                        print(f"Ошибка при отправке {label} (комментарий «{preview}…», файлов {len(s['files'])}): {e}",
                              file=sys.stderr)
                        continue
                    sent_ids[step_id] = 1
                    sent += 1
                state["sent"] = sent
                state["at"] = datetime.now(timezone.utc).isoformat()
                done_path.write_text(
                    json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            # файлы не принимаются в комментариях — загружаем на Диск и прикрепляем к задаче
            task_files = [f for s in steps for f in s["files"]]
            if task_files and not args.no_task_files and not state.get("taskFilesSent"):
                state_files = state.setdefault("filesDone", {})
                try:
                    if disk_root is None:
                        disk_root = get_disk_root_folder(webhook, delay_s)
                    attached, marker_skipped, up_failed = attach_task_files(
                        webhook, target, task_files, disk_root, delay_s, state_files, used_disk_names)
                    state["taskFilesSent"] = up_failed == 0
                    if up_failed:
                        state["taskFilesSkipped"] = up_failed
                        report_files.append(
                            f"{label}: не загружено на Диск {up_failed} из {len(task_files)} файлов "
                            f"(остались локально в {files_dir}\\{task.get('id')}\\)"
                        )
                except (B24Error, ValueError) as e:
                    failed += 1
                    errors += 1
                    reason = f"{type(e).__name__}: {e}"
                    error_reasons[reason] += 1
                    print(f"Ошибка при вложении {len(task_files)} файлов в задачу {label}: {e}", file=sys.stderr)
                    print(f"  файлы остались локально: {files_dir}\\{task.get('id')}\\", file=sys.stderr)
            state["complete"] = failed == 0
            done_path.write_text(
                json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if failed:
                print(f"Частично: {label} -> задача {target} "
                      f"(комментариев отправлено {sent}, ошибок {failed}, уже на портале {skipped}); "
                      f"перезапуск --apply повторит только несохранённое")
            elif sent and skipped:
                print(f"Обработано: {label} -> задача {target} (отправлено {sent}, уже на портале {skipped})")
            elif sent:
                print(f"Отправлено: {label} -> задача {target} (комментариев: {sent})")
            else:
                print(f"Пропущено (комментарии уже есть на портале): {label} -> задача {target}")
            if not failed:
                applied += 1

    print()
    for line in plan:
        print(line, flush=True)
    print()
    print(f"Задач с комментариями/файлами: {matched}, к отправке: "
          f"{sum(1 for p in plan if '-> задача' in p and 'нечего отправлять' not in p)}, "
          f"применено: {applied}, ошибок: {errors}")
    if not args.apply:
        print("Режим dry-run: ничего не изменено. Для реальной отправки запустите с --apply")
    for title, items in [
        ("Задач без проекта (тег не совпал)", report_missing_project),
        ("Ненайденные по названию задачи", report_unmatched),
        ("Неоднозначные (дубли названия в проекте)", report_ambiguous),
    ]:
        if items:
            print(f"{title}: {len(items)}")
            for x in items[:20]:
                print(f"  {x}")
            if len(items) > 20:
                print(f"  ... и ещё {len(items) - 20}")
    if report_users:
        print(f"Пользователи без совпадения по имени и email (комментарий от владельца вебхука): {sorted(report_users)}")
    if error_reasons:
        print("Причины ошибок (сколько раз / сообщение):")
        for reason, cnt in error_reasons.most_common():
            print(f"  {cnt}× {reason}")
    if report_files:
        print(f"Проблемы с файлами: {len(report_files)}")
        for x in report_files[:20]:
            print(f"  {x}")
        if len(report_files) > 20:
            print(f"  ... и ещё {len(report_files) - 20}")


if __name__ == "__main__":
    main()
