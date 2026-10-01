import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

APP_URL = "https://app.kanbano.ru/"
SCRIPT_PATH = Path(__file__).with_name("kanbano_export.js")
FILES_DIR_DEFAULT = "kanbano_files"
BOARD_FILE_BASE = "https://api.kanbano.ru/files/board-files/"

_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str) -> str:
    name = _UNSAFE_NAME_RE.sub("_", str(name or "")).strip(" .")
    return name or "file"


def safe_dirname(v: str) -> str:
    v = _UNSAFE_NAME_RE.sub("_", str(v or "")).strip(" .")
    return v or "0"


async def wait_for_token(page, timeout_s: int, headed: bool) -> str:
    print(f"Ожидаю входа в Kanbano ({timeout_s} с). Откройте окно браузера и войдите, если ещё не вошли.", flush=True)
    waited = 0
    while waited < timeout_s:
        token = await page.evaluate("localStorage.getItem('access_token')")
        if token:
            return token
        await asyncio.sleep(2)
        waited += 2
        if waited % 20 == 0:
            print(f"Токена пока нет... {waited}s" + ("" if headed else " (запустите с --headed, чтобы войти вручную)"), flush=True)
    raise RuntimeError("access_token не появился в localStorage за отведённое время")


def wire_console(page) -> None:
    """Печатает прогресс [kanbano-export] из консоли браузера в терминал.

    Playwright дублирует console-события из асинхронного evaluate — соседние
    повторы одного сообщения схлопываются.
    """
    last = {"text": None}

    def on_console(msg) -> None:
        try:
            text = msg.text
        except Exception:
            return
        if not isinstance(text, str) or not text.startswith("[kanbano-export]"):
            return
        if text == last["text"]:
            return
        last["text"] = text
        print(text, flush=True)

    page.on("console", on_console)


async def run_export(page, overrides: dict) -> None:
    """Запускает экспорт в браузере; результат остаётся в window.__kanbanoExportResult.

    Раз в 30 с печатает сердцебиение, пока идёт выгрузка.
    """
    stop = asyncio.Event()
    started = time.monotonic()

    async def heartbeat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                elapsed = int(time.monotonic() - started)
                print(f"...выгрузка ещё идёт ({elapsed} с), прогресс — в строках [kanbano-export] выше", flush=True)

    beat = asyncio.create_task(heartbeat())
    try:
        expr = (
            "window.__kanbanoExportTasks(" + json.dumps(overrides, ensure_ascii=False) + ")"
            ".then(r => { window.__kanbanoExportResult = r; window.__kanbanoExportDone = true; return true; })"
        )
        await page.evaluate(expr)
    finally:
        stop.set()
        await beat


async def pull_result(page) -> dict:
    """Забирает результат из браузера по частям (доски/задачи), без одного гигантского JSON."""
    exported_at = await page.evaluate("window.__kanbanoExportResult.exportedAt")
    source = await page.evaluate("window.__kanbanoExportResult.source")
    n_boards = await page.evaluate("window.__kanbanoExportResult.boards.length")
    boards = []
    for bi in range(n_boards):
        board = json.loads(await page.evaluate(
            "(bi) => JSON.stringify({...window.__kanbanoExportResult.boards[bi], tasks: []})", bi
        ))
        tcount = await page.evaluate(f"window.__kanbanoExportResult.boards[{bi}].tasks.length")
        tasks = []
        for ti in range(tcount):
            tasks.append(json.loads(await page.evaluate(
                "([bi, ti]) => JSON.stringify(window.__kanbanoExportResult.boards[bi].tasks[ti])", [bi, ti]
            )))
            if (ti + 1) % 25 == 0 or ti + 1 == tcount:
                print(f"[kanbano-export] передача данных: доска {bi + 1}/{n_boards}, задач {ti + 1}/{tcount}", flush=True)
        board["tasks"] = tasks
        boards.append(board)
    return {"exportedAt": exported_at, "source": source, "boards": boards}


async def run_export_then_pull(page, overrides: dict) -> dict:
    await run_export(page, overrides)
    return await pull_result(page)


async def download_attachments(context, token: str, data: dict, files_dir: Path, delay_ms: int) -> int:
    """Скачивает вложения через HTTP-клиент Playwright (вне браузера, без CORS)
    в files_dir/<taskId>/<имя> и проставляет att["file"]."""
    headers = {"Authorization": "Bearer " + token}
    count = 0
    for board in data.get("boards", []):
        for task in board.get("tasks", []):
            atts = [a for a in (task.get("attachments") or []) if a.get("link")]
            if not atts:
                continue
            task_dir = files_dir / safe_dirname(str(task.get("id")))
            names = [safe_filename(a.get("name")) for a in atts]
            dup_names = {n for n, c in Counter(names).items() if c > 1}
            used: dict = {}
            for att, name in zip(atts, names):
                link = att.get("link") or ""
                url = link if re.match(r"^https?://", link, re.I) else BOARD_FILE_BASE + link
                if name in dup_names:
                    used[name] = used.get(name, 0) + 1
                    stem, suffix = Path(name).stem, Path(name).suffix
                    fname = name if used[name] == 1 else f"{stem} ({used[name] - 1}){suffix}"
                else:
                    fname = name
                target = task_dir / fname
                try:
                    resp = await context.request.get(url, headers=headers)
                    if not resp.ok:
                        raise RuntimeError(f"HTTP {resp.status} {resp.status_text}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(await resp.body())
                except Exception as e:
                    print(f"[kanbano-export]   файл «{att.get('name')}» задачи #{task.get('id')} не скачан: {e}", flush=True)
                    continue
                rel = f"{target.parent.name}/{target.name}"
                att["file"] = rel
                count += 1
                print(f"[kanbano-export] файл сохранён ({count}): {rel}", flush=True)
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000)

    # зеркалим пути в те же вложения внутри комментариев (это отдельные копии в JSON)
    for board in data.get("boards", []):
        for task in board.get("tasks", []):
            by_id = {a.get("id"): a.get("file") for a in (task.get("attachments") or []) if a.get("file")}
            if not by_id:
                continue
            for c in task.get("comments") or []:
                for att in c.get("attachments") or []:
                    if att.get("id") in by_id:
                        att["file"] = by_id[att.get("id")]
    return count


async def main() -> None:
    from playwright.async_api import async_playwright

    parser = argparse.ArgumentParser(description="Экспорт задач Kanbano в JSON через headless Chrome")
    parser.add_argument("--out", default="kanbano_export.json", help="имя итогового JSON-файла")
    parser.add_argument("--user-data-dir", default="kanbano_chrome_profile", help="каталог профиля Chrome (там хранится логин)")
    parser.add_argument("--board-ids", default="", help="через запятую id досок; пусто = все доски")
    parser.add_argument("--include-archived-boards", action="store_true")
    parser.add_argument("--include-archived-tasks", action="store_true")
    parser.add_argument("--delay-ms", type=int, default=150)
    parser.add_argument("--download-files", action="store_true",
                        help=f"скачивать вложения комментариев в {FILES_DIR_DEFAULT}/<taskId>/ (иначе в JSON только метаданные)")
    parser.add_argument("--files-dir", default=FILES_DIR_DEFAULT, help="каталог для скачанных файлов")
    parser.add_argument("--headed", action="store_true", help="показать окно браузера (нужно для первого входа)")
    parser.add_argument("--login-timeout", type=int, default=300, help="сколько секунд ждать входа вручную")
    args = parser.parse_args()

    board_ids = [int(x) for x in args.board_ids.split(",") if x.strip()]

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.user_data_dir).resolve()),
            headless=not args.headed,
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        wire_console(page)
        await page.goto(APP_URL, wait_until="domcontentloaded")

        token = await page.evaluate("localStorage.getItem('access_token')")
        if not token:
            await wait_for_token(page, args.login_timeout, args.headed)
        print("Токен найден. Запускаю выгрузку — это может занять несколько минут.", flush=True)

        await page.evaluate("window.__KANBANO_EXPORT_NO_AUTORUN = true")
        await page.add_script_tag(content=SCRIPT_PATH.read_text(encoding="utf-8"))

        overrides = {
            "boardIds": board_ids or None,
            "includeArchivedBoards": args.include_archived_boards,
            "includeArchivedTasks": args.include_archived_tasks,
            "delayMs": args.delay_ms,
            "download": False,
        }
        data = await run_export_then_pull(page, overrides)

        if args.download_files:
            saved = await download_attachments(
                context, token, data, Path(args.files_dir).resolve(), args.delay_ms
            )
            print(f"Файлов сохранено: {saved} -> {Path(args.files_dir).resolve()}", flush=True)
        await context.close()

    out_path = Path(args.out).resolve()
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(b.get("tasks", [])) for b in data.get("boards", []))
    print(f"Готово: {out_path} (досок: {len(data.get('boards', []))}, задач: {total})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(1)
