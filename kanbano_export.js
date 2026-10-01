(async () => {
  "use strict";

  const CONFIG = {
    boardIds: null,
    includeArchivedBoards: false,
    includeArchivedTasks: false,
    delayMs: 150,
    filename: "kanbano_export.json",
    download: true
  };

  const API_BASE = "https://api.kanbano.ru";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const toId = (v) => {
    const raw = v !== null && typeof v === "object" ? v.id : v;
    const n = Number(raw);
    return Number.isFinite(n) ? n : raw ?? null;
  };

  const token = localStorage.getItem("access_token");
  if (!token) {
    console.error(
      "[kanbano-export] access_token не найден в localStorage. Откройте https://app.kanbano.ru, войдите в аккаунт и запустите скрипт заново."
    );
    return;
  }

  async function api(path, params = {}, attempt = 1) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const url = API_BASE + path + (qs.toString() ? "?" + qs.toString() : "");
    const res = await fetch(url, {
      headers: { Authorization: "Bearer " + token, Accept: "application/json" }
    });
    if (res.status === 401) {
      throw new Error(
        "[kanbano-export] 401: токен истёк. Обновите страницу (F5) и запустите скрипт заново."
      );
    }
    if (res.status === 429 && attempt <= 5) {
      console.warn(`[kanbano-export] 429 на ${path}, повтор ${attempt}/5...`);
      await sleep(1500 * attempt);
      return api(path, params, attempt + 1);
    }
    if (!res.ok) {
      throw new Error(`[kanbano-export] ${res.status} ${res.statusText} на GET ${path}`);
    }
    return res.json();
  }

  const statusLabel = (s) => (s === 1 ? "completed" : "in_work");
  const byOrder = (a, b) => Number(a.order ?? 0) - Number(b.order ?? 0);

  window.__kanbanoExportTasks = async function __kanbanoExportTasks(overrides = {}) {
    window.__kanbanoExportDone = false;
    const cfg = { ...CONFIG, ...overrides };
    const out = { exportedAt: new Date().toISOString(), source: "kanbano", boards: [] };

    let boards = [];
    if (Array.isArray(cfg.boardIds) && cfg.boardIds.length > 0) {
      for (const id of cfg.boardIds) {
        boards.push(await api(`/boards/${id}`));
        await sleep(cfg.delayMs);
      }
    } else {
      boards = await api("/boards");
      if (cfg.includeArchivedBoards) {
        const archived = await api("/boards", { archived: "true" });
        const seen = new Set(boards.map((b) => b.id));
        for (const b of archived) if (!seen.has(b.id)) boards.push(b);
      }
    }
    console.log(`[kanbano-export] Досок к выгрузке: ${boards.length}`);

    for (let bi = 0; bi < boards.length; bi++) {
      const board = boards[bi];
      console.log(`[kanbano-export] (${bi + 1}/${boards.length}) Доска #${board.id} «${board.name}»`);

      const members = await api(`/boards/${board.id}/members`);
      await sleep(cfg.delayMs);

      let tags = [];
      try {
        tags = await api(`/boards/${board.id}/tags`);
      } catch (e) {
        console.warn(e.message);
      }
      await sleep(cfg.delayMs);

      let tasks = await api(`/tasks/by-board/${board.id}`);
      await sleep(cfg.delayMs);

      if (cfg.includeArchivedTasks) {
        try {
          const archived = await api(`/tasks/by-board/${board.id}`, { archived: "true" });
          const seen = new Set(tasks.map((t) => t.id));
          for (const t of archived) if (!seen.has(t.id)) tasks.push(t);
        } catch (e) {
          console.warn(e.message);
        }
        await sleep(cfg.delayMs);
      }

      const columns = [...(board.columns || [])].sort(byOrder);
      const columnName = new Map(columns.map((c) => [c.id, c.name]));
      const columnPos = new Map(columns.map((c, i) => [c.id, i]));

      // Комментарии задачи = /tasks/{id}/messages. События (история) не включаются.
      // Файлы не скачиваются здесь: вложения остаются метаданными (name + link),
      // скачивание делает обёртка на Python вне браузера (нет ограничений CORS).
      const fetchComments = async (taskId, hasMessages) => {
        if (hasMessages === false) return [];
        let messages;
        try {
          messages = await api(`/tasks/${taskId}/messages`);
        } catch (e) {
          console.warn(e.message);
          return [];
        }
        await sleep(cfg.delayMs);
        // Поля сообщения, которые мы разбираем отдельно; остальное попадает в extra
        // (на случай, если поле автора называется иначе, чем ожидается).
        const KNOWN = new Set(["id", "text", "files", "createdAt", "updatedAt",
                               "deleted", "isPinned", "pinned", "messageReplyId", "taskId"]);
        const comments = [];
        for (const m of Array.isArray(messages) ? messages : []) {
          if (!m || m.deleted) continue;
          const extra = {};
          for (const [k, v] of Object.entries(m)) {
            if (!KNOWN.has(k) && typeof v !== "object") extra[k] = v;
          }
          const authorId = toId(m.creatorId ?? m.senderId ?? m.authorId ?? m.userId) ?? null;
          if (authorId === null) {
            const extraKeys = Object.keys(extra).join(", ") || "нет";
            console.warn(`[kanbano-export]   комментарий #${m.id ?? "?"} задачи #${taskId}: ` +
                         `поле автора не найдено (extra: ${extraKeys})`);
          }
          comments.push({
            id: m.id ?? null,
            taskId,
            authorId,
            createdAt: m.createdAt ?? null,
            text: m.text ?? "",
            attachments: (m.files ?? []).filter(Boolean).map((f) => ({
              id: f.id ?? null,
              name: f.fileName ?? f.name ?? "",
              link: f.link ?? ""
            })),
            extra
          });
        }
        return comments;
      };

      const rawHasMessages = new Map(tasks.map((t) => [t.id, t.hasMessages]));
      const mappedTasks = tasks
        .map((t) => {
          const checklistItems = [...(t.checklistItems || [])].sort(byOrder);
          return {
            id: t.id,
            name: t.name,
            description: t.description ?? "",
            columnId: t.columnId,
            columnName: columnName.get(t.columnId) ?? null,
            order: Number(t.order ?? 0),
            status: t.status,
            statusLabel: statusLabel(t.status),
            isArchived: Boolean(t.isArchived),
            createdAt: t.createdAt ?? null,
            updatedAt: t.updatedAt ?? null,
            dateBegin: t.dateBegin ?? null,
            dateEnd: t.dateEnd ?? null,
            creatorId: toId(t.creatorId),
            performers: (t.performers ?? []).map(toId),
            members: (t.members ?? []).map(toId),
            tags: (t.tags ?? []).map(toId),
            comments: [],
            attachments: [],
            subtasks: checklistItems.map((c) => ({ name: c.name, isChecked: Boolean(c.isChecked) })),
            checklistItems
          };
        })
        .sort((a, b) => {
          const ca = columnPos.get(a.columnId) ?? 1e9;
          const cb = columnPos.get(b.columnId) ?? 1e9;
          return ca - cb || a.order - b.order;
        });

      let commentsTotal = 0;
      let filesTotal = 0;
      for (let ti = 0; ti < mappedTasks.length; ti++) {
        const task = mappedTasks[ti];
        if ((ti + 1) % 25 === 0 || ti + 1 === mappedTasks.length) {
          console.log(`[kanbano-export]   комментарии/файлы: ${ti + 1}/${mappedTasks.length} задач...`);
        }
        const comments = await fetchComments(task.id, rawHasMessages.get(task.id));
        task.comments = comments;
        task.attachments = comments.flatMap((c) =>
          c.attachments.map((a) => ({ ...a, messageId: c.id }))
        );
        commentsTotal += comments.length;
        filesTotal += task.attachments.length;
      }

      console.log(
        `[kanbano-export]   задач: ${mappedTasks.length}, колонок: ${columns.length}, участников: ${members.length}, комментариев: ${commentsTotal}, файлов: ${filesTotal}`
      );

      out.boards.push({
        id: board.id,
        name: board.name,
        isArchived: Boolean(board.isArchived),
        columns: columns.map((c) => ({ id: c.id, name: c.name, order: c.order })),
        members,
        tags,
        tasks: mappedTasks
      });

      await sleep(cfg.delayMs);
    }

    const total = out.boards.reduce((n, b) => n + b.tasks.length, 0);
    console.log(`[kanbano-export] Готово. Всего задач: ${total}.`);
    // Результат остаётся в браузере: обёртка на Python забирает его по частям,
    // иначе evaluate виснет на передаче одного большого JSON с base64-файлами.
    window.__kanbanoExportResult = out;
    window.__kanbanoExportDone = true;
    return out;
  };

  if (!window.__KANBANO_EXPORT_NO_AUTORUN) {
    try {
      const data = await window.__kanbanoExportTasks();
      window.__kanbanoExportResult = data;
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = CONFIG.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 10000);
      console.log(
        `[kanbano-export] Файл ${CONFIG.filename} скачан. Результат также в window.__kanbanoExportResult.`
      );
    } catch (e) {
      console.error(e);
    }
  }
})();
