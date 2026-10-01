import argparse
import csv
import html
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from b24_client import B24Error, fetch_projects, norm_name

COLUMNS = [
    "Название",
    "Описание",
    "Это важная задача",
    "Исполнитель",
    "Постановщик",
    "Соисполнители",
    "Наблюдатели",
    "Крайний срок",
    "Начать задачу с",
    "Завершить задачу",
    "Разрешить исполнителю менять сроки задачи",
    "Пропустить выходные и праздничные дни",
    "Проконтролировать задачу после завершения",
    "Сроки определяются сроками подзадач",
    "Автоматически завершать задачу при завершении подзадач",
    "Проект",
    "Задано время на завершение задачи",
    "Время на выполнение задачи в секундах",
    "Чеклист",
    "Теги",
]

BLOCK_TAGS_RE = re.compile(
    r"</?(?:address|article|aside|blockquote|div|dl|fieldset|figcaption|figure|footer|form"
    r"|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|tbody|thead|tfoot|tr|td|th|ul)\b[^>]*>",
    re.I,
)
ANY_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    if not raw or ("<" not in raw and "&" not in raw):
        return (raw or "").strip()
    s = re.sub(r"\r\n?", "\n", raw)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = BLOCK_TAGS_RE.sub("\n", s)
    s = ANY_TAG_RE.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\f\v]*\n[ \t\f\v]*", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def parse_dt(value, to_local: bool = True):
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if to_local and dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt


def fmt_dt(value, keep_utc: bool) -> str:
    dt = parse_dt(value, to_local=not keep_utc)
    if dt is None:
        return ""
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def user_label(user, fmt: str) -> str:
    if not user:
        return ""
    name = (user.get("name") or "").strip()
    email = (user.get("email") or "").strip()
    if fmt == "email":
        return email or name
    if fmt == "name-email":
        if name and email:
            return f"{name} ({email})"
        return name or email
    return name or email


def users_line(users, fmt: str, exclude_ids=()) -> str:
    exclude = set(exclude_ids)
    seen = set()
    parts = []
    for u in users or []:
        if not u or u.get("id") in exclude:
            continue
        label = user_label(u, fmt)
        key = label or u.get("id")
        if not label or key in seen:
            continue
        seen.add(key)
        parts.append(label)
    return ",".join(parts)


def checklist_line(task, checklist_name: str, mark_done: bool) -> str:
    items = task.get("subtasks")
    if items is None:
        raw = task.get("checklistItems") or []
        items = [{"name": c.get("name"), "isChecked": bool(c.get("isChecked"))} for c in raw]
    parts = [f"[**]{checklist_name}"]
    for item in items or []:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        if mark_done and item.get("isChecked"):
            name = "[x] " + name
        parts.append(f"[*]{name}")
    return "".join(parts) if len(parts) > 1 else ""


def build_lookup(items) -> dict:
    lookup = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("id") is not None:
            try:
                lookup[int(it["id"])] = it
            except (TypeError, ValueError):
                lookup[it["id"]] = it
    return lookup


def ref_id(value):
    """Числовой ID из ссылки: 12, "12" или {"id": 12} (устойчивость к старым JSON)."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_user(value, users_by_id: dict):
    uid = ref_id(value)
    if uid is None:
        return None
    return users_by_id.get(uid) or {"id": uid, "name": "", "email": ""}


def resolve_tag(value, tags_by_id: dict):
    tid = ref_id(value)
    if tid is None:
        return None
    return tags_by_id.get(tid) or {"id": tid, "name": ""}


def load_project_names(args) -> list:
    """Имена проектов Bitrix24: --projects-file, вебхук (с кэшем) или кэш."""
    if args.projects_file:
        path = Path(args.projects_file)
        if not path.exists():
            print(f"Файл проектов не найден: {path}", file=sys.stderr)
            sys.exit(1)
        names = [ln.strip() for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
        print(f"Проекты (офлайн-файл {path}): {len(names)} шт.")
        return names

    webhook = args.webhook_url or os.environ.get("B24_WEBHOOK", "")
    if webhook:
        try:
            projects = fetch_projects(webhook)
            names = sorted({((p.get("NAME") or p.get("name") or "").strip()) for p in projects} - {""})
            Path(args.projects_cache).write_text(
                json.dumps(names, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"Проекты из Bitrix24: {len(names)} шт. (кэш: {args.projects_cache})")
            return names
        except B24Error as e:
            print(f"Внимание: вебхук не сработал, {e}")

    cache = Path(args.projects_cache)
    if cache.exists():
        try:
            names = json.loads(cache.read_text(encoding="utf-8"))
            print(f"Проекты из кэша {cache}: {len(names)} шт.")
            return [str(n) for n in names if str(n).strip()]
        except (json.JSONDecodeError, OSError) as e:
            print(f"Внимание: кэш проектов повреждён ({e})")
    return []


def tags_line(tags, import_tag: str = "") -> str:
    names = []
    seen = set()

    def push(name: str) -> None:
        key = norm_name(name)
        if not key or key in seen:
            return
        seen.add(key)
        names.append(name.strip())

    if (import_tag or "").strip():
        push(import_tag)
    for t in tags or []:
        if isinstance(t, dict):
            name = (t.get("name") or "").strip()
        elif t is None:
            continue
        else:
            name = str(t).strip()
        push(name)
    return "".join(f"[*]{n}" for n in names)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Конвертация kanbano_export.json в CSV для импорта задач в Bitrix24"
    )
    parser.add_argument("--input", "-i", default="kanbano_export.json", help="входной JSON")
    parser.add_argument("--output", "-o", default="tasks_import.csv", help="выходной CSV")
    parser.add_argument("--boards", default="", help="фильтр по id досок через запятую")
    parser.add_argument("--user-format", choices=["name", "email", "name-email"], default="name",
                        help="как выводить пользователей (по умолчанию name)")
    parser.add_argument("--members-as", choices=["observers", "coexecutors"], default="observers",
                        help="куда определять участников задачи, не являющихся исполнителями")
    parser.add_argument("--project-from", choices=["tag", "board", "column", "none"], default="tag",
                        help="что писать в «Проект»: tag = первый тег задачи, совпавший с проектом Bitrix24 (по умолчанию)")
    parser.add_argument("--webhook-url", default="",
                        help="входящий вебхук Bitrix24 для загрузки проектов (sonet_group.get); иначе env B24_WEBHOOK")
    parser.add_argument("--projects-cache", default="bitrix_projects_cache.json",
                        help="файл кэша списка проектов (по умолчанию bitrix_projects_cache.json)")
    parser.add_argument("--projects-file", default="",
                        help="текстовый файл со списком проектов (имя на строку), офлайн-режим без вебхука")
    parser.add_argument("--import-tag", default="Импорт",
                        help="метка, добавляемая в «Теги» каждой задачи первой и исключаемая из подбора проекта")
    parser.add_argument("--checklist-name", default="Чек-лист", help="имя группы чек-листа в Bitrix24")
    parser.add_argument("--mark-done", action="store_true", help="помечать выполненные пункты чек-листа префиксом [x]")
    parser.add_argument("--keep-html", action="store_true", help="не превращать HTML-описание в текст")
    parser.add_argument("--include-archived-tasks", action="store_true")
    parser.add_argument("--keep-utc", action="store_true", help="не переводить даты в локальный часовой пояс")
    parser.add_argument("--encoding", default="utf-8-sig", help="кодировка CSV (по умолчанию utf-8-sig, можно cp1251)")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"Файл не найден: {in_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(in_path.read_text(encoding="utf-8-sig"))
    boards = data.get("boards", data if isinstance(data, list) else [])
    board_filter = {int(x) for x in args.boards.split(",") if x.strip()}

    rows = []
    skipped = 0
    unresolved_users = set()
    unresolved_tags = set()
    uncovered_tags = Counter()
    empty_project_tasks = []

    project_from = args.project_from
    import_key = norm_name(args.import_tag)
    project_names = load_project_names(args) if project_from == "tag" else []
    projects_by_key = {}
    for n in project_names:
        projects_by_key.setdefault(norm_name(n), n)
    if project_from == "tag" and not projects_by_key:
        print("Внимание: список проектов пуст (нужен --webhook-url/B24_WEBHOOK, --projects-file или кэш "
              f"{args.projects_cache}); колонка «Проект» останется пустой")

    for board in boards:
        if board_filter and board.get("id") not in board_filter:
            continue

        users_by_id = build_lookup(board.get("members"))
        tags_by_id = build_lookup(board.get("tags"))

        for task in board.get("tasks", []):
            if task.get("isArchived") and not args.include_archived_tasks:
                skipped += 1
                continue

            performers = [
                u for u in (resolve_user(v, users_by_id) for v in task.get("performers") or []) if u
            ]
            members = [
                u for u in (resolve_user(v, users_by_id) for v in task.get("members") or []) if u
            ]
            tags = [t for t in (resolve_tag(v, tags_by_id) for v in task.get("tags") or []) if t]
            creator = resolve_user(task.get("creatorId"), users_by_id) \
                or resolve_user(task.get("creator"), users_by_id)

            for u in performers + members + ([creator] if creator else []):
                if isinstance(u, dict) and not (u.get("name") or u.get("email")):
                    unresolved_users.add(u.get("id"))
            for t in tags:
                if isinstance(t, dict) and not (t.get("name") or "").strip():
                    unresolved_tags.add(t.get("id"))

            performer_ids = [u.get("id") for u in performers]
            extra_members = [u for u in members if u.get("id") not in performer_ids]

            main_performer = user_label(performers[0], args.user_format) if performers else ""
            coexecutors = [u for u in performers[1:]]
            if args.members_as == "coexecutors":
                coexecutors += extra_members
            observers = extra_members if args.members_as == "observers" else []

            project = ""
            if project_from == "tag":
                tag_names = []
                for t in tags:
                    tn = (t.get("name") or "").strip()
                    if tn and norm_name(tn) not in tag_names:
                        tag_names.append(tn)
                matched = False
                for tn in tag_names:
                    key = norm_name(tn)
                    if key == import_key:
                        continue
                    if key in projects_by_key:
                        project = projects_by_key[key]
                        matched = True
                        break
                for tn in tag_names:
                    key = norm_name(tn)
                    if key == import_key:
                        continue
                    if key not in projects_by_key:
                        uncovered_tags[tn] += 1
                if not matched:
                    empty_project_tasks.append((task.get("id"), task.get("name") or ""))
            elif project_from == "board":
                project = board.get("name") or ""
            elif project_from == "column":
                project = task.get("columnName") or ""

            description = task.get("description") or ""
            if not args.keep_html:
                description = html_to_text(description)

            rows.append({
                "Название": task.get("name") or "",
                "Описание": description,
                "Это важная задача": "0",
                "Исполнитель": main_performer,
                "Постановщик": user_label(creator, args.user_format),
                "Соисполнители": users_line(coexecutors, args.user_format),
                "Наблюдатели": users_line(observers, args.user_format),
                "Крайний срок": fmt_dt(task.get("dateEnd"), args.keep_utc),
                "Начать задачу с": fmt_dt(task.get("dateBegin"), args.keep_utc),
                "Завершить задачу": "",
                "Разрешить исполнителю менять сроки задачи": "0",
                "Пропустить выходные и праздничные дни": "0",
                "Проконтролировать задачу после завершения": "0",
                "Сроки определяются сроками подзадач": "0",
                "Автоматически завершать задачу при завершении подзадач": "0",
                "Проект": project,
                "Задано время на завершение задачи": "0",
                "Время на выполнение задачи в секундах": "0",
                "Чеклист": checklist_line(task, args.checklist_name, args.mark_done),
                "Теги": tags_line(tags, args.import_tag),
            })

    out_path = Path(args.output)
    with out_path.open("w", encoding=args.encoding, newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow([row[c] for c in COLUMNS])

    print(f"Записано задач: {len(rows)} -> {out_path.resolve()}")
    if skipped:
        print(f"Пропущено архивных задач: {skipped} (включить: --include-archived-tasks)")
    if unresolved_users:
        print(f"Внимание: не найдены в members доски пользователи с id: {sorted(unresolved_users)} (ячейки останутся пустыми)")
    if unresolved_tags:
        print(f"Внимание: не найдены в tags доски теги с id: {sorted(unresolved_tags)} (ячейки останутся пустыми)")
    if project_from == "tag" and uncovered_tags:
        print(f"Теги без совпадения с проектами Bitrix24 (тег -> задач): {len(uncovered_tags)}")
        for name, cnt in uncovered_tags.most_common():
            print(f"  «{name}» ({cnt})")
    if project_from == "tag" and empty_project_tasks:
        print(f"Задач с пустым «Проект»: {len(empty_project_tasks)}")
        for tid, name in empty_project_tasks[:20]:
            print(f"  #{tid} «{name}»")
        if len(empty_project_tasks) > 20:
            print(f"  ... и ещё {len(empty_project_tasks) - 20}")


if __name__ == "__main__":
    main()
