// Run against a temporary local database. Does not touch the owner's credentials or settings.
const { chromium } = require(
  process.env.ZARYA_PLAYWRIGHT_MODULE || "playwright",
);
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const fs = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "..");
const output = path.join(root, "test-results");
const origin = "http://127.0.0.1:8791";
const password = crypto.randomBytes(24).toString("hex");
let server;
let browser;
let stderr = "";
const errors = [];

async function start(data) {
  server = spawn(
    path.join(
      root,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    ),
    [path.join(root, "tests", "telegram_fixture_server.py")],
    {
      cwd: root,
      windowsHide: true,
      env: {
        ...process.env,
        ZARYA_DATA_DIR: data,
        ZARYA_PORT: "8791",
        ZARYA_DEV_UI: "0",
      },
      stdio: ["ignore", "ignore", "pipe"],
    },
  );
  server.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  for (let i = 0; i < 80; i++) {
    if (server.exitCode !== null) throw new Error(`Server exited: ${stderr}`);
    try {
      if ((await fetch(`${origin}/healthz`)).ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Server timeout: ${stderr}`);
}
async function stop() {
  if (server && server.exitCode === null) {
    const ended = once(server, "exit");
    server.kill();
    await ended;
  }
}

async function navigate(page, name) {
  const toggle = page.locator(".mobile-nav-toggle");
  if (
    (await toggle.isVisible()) &&
    (await toggle.getAttribute("aria-expanded")) === "false"
  ) {
    await toggle.click();
  }
  await page
    .getByRole("navigation", { name: "Основная навигация" })
    .getByRole("button", { name: new RegExp("^" + name) })
    .click();
}
async function openSettingsSection(page, title) {
  const section = page.locator(".settings-section").filter({
    has: page.locator(".settings-section-title", { hasText: title }),
  });
  if ((await section.getAttribute("open")) === null)
    await section.locator("summary").first().click();
  return section;
}

(async () => {
  await fs.mkdir(output, { recursive: true });
  await fs.mkdir(path.join(root, "data"), { recursive: true });
  const data = await fs.mkdtemp(path.join(root, "data", "browser-smoke-"));
  await start(data);
  browser = await chromium.launch({
    headless: true,
    channel: process.env.ZARYA_BROWSER_CHANNEL || undefined,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(origin);
  await page.getByRole("heading", { name: "Давай познакомимся." }).waitFor();
  await page.screenshot({
    path: path.join(output, "setup.png"),
    fullPage: true,
  });
  const token = (
    await fs.readFile(path.join(data, "bootstrap-token.txt"), "utf8")
  ).trim();
  await page.getByLabel("Одноразовый ключ").fill(token);
  await page.getByLabel("Новый пароль администратора").fill(password);
  await page.getByRole("button", { name: "Создать доступ →" }).click();
  await page.getByRole("heading", { name: "Заря начинается здесь." }).waitFor();
  await page.getByText("Настройки сохранены", { exact: true }).waitFor();
  await page.locator(".portrait-panel img").evaluate((img) => img.decode());
  await page.screenshot({
    path: path.join(output, "overview.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "overview-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Telegram и доступ");
  await page.getByText("Тестовый собеседник", { exact: true }).waitFor();
  await page.getByRole("button", { name: /^Разрешить:/ }).click();
  await page.getByText("Разрешён", { exact: true }).waitFor();
  await page.screenshot({
    path: path.join(output, "telegram-access.png"),
    fullPage: true,
  });
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: /^Отозвать доступ:/ }).click();
  await page.getByText("Отозван", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Группы", exact: true }).click();
  await page.getByText("Тестовая группа друзей", { exact: true }).waitFor();
  await page.getByRole("button", { name: /^Разрешить:/ }).click();
  await page.getByText("Разрешён", { exact: true }).waitFor();
  await page.setViewportSize({ width: 360, height: 800 });
  await page.screenshot({
    path: path.join(output, "telegram-mobile.png"),
    fullPage: true,
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Настройки");
  await navigate(page, "Диалоги");
  await page.getByText("Ответ готов", { exact: true }).waitFor();
  const avatarDetailRoute = /\/api\/dialogues\/\d+$/;
  await page.route(avatarDetailRoute, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.snapshot.avatar_profile = {
      user_id: "77",
      name: "Тестовый профиль",
      available_count: 12,
      analysed_indices: [1, 2],
      has_more: true,
      list_changed: true,
      listing_truncated: false,
      state: "completed",
      error: null,
      observations: {
        descriptions: ["Красный геометрический рисунок", "Иллюстрация кота"],
        uncertainty: "",
      },
    };
    await route.fulfill({ response, json: body });
  });
  await page
    .locator(".dialog-item")
    .filter({ has: page.getByText("Ответ готов", { exact: true }) })
    .first()
    .getByRole("button", { name: "Подробности", exact: true })
    .click();
  await page
    .getByText("04 · Части ответа и доставка", { exact: true })
    .waitFor();
  await page
    .getByText("Аватарки выбранного собеседника", { exact: true })
    .click();
  await page
    .getByText("№ 1: Красный геометрический рисунок", { exact: true })
    .waitFor();
  await page.screenshot({
    path: path.join(output, "avatar-context.png"),
    fullPage: true,
  });
  await page.unroute(avatarDetailRoute);
  await page
    .getByText("Показать использованные сообщения", { exact: true })
    .click();
  const [recordedResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/dialogues/replay") &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", { name: "Проверить сохранённый ответ", exact: true })
      .click(),
  ]);
  assert.equal(recordedResponse.status(), 200);
  const recordedData = await recordedResponse.json();
  assert.equal(recordedData.mode, "recorded");
  await page.getByRole("status").filter({ hasText: "Тест №" }).waitFor();
  await page.screenshot({
    path: path.join(output, "dialogues.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "dialogues-mobile.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", {
      name: "Новый запрос к модели (платно)",
      exact: true,
    })
    .click();
  await page.getByText(/предел: 2500 токенов/).waitFor();
  const [paidResponse] = await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/dialogues/replay") &&
        r.request().method() === "POST",
    ),
    page
      .getByRole("button", { name: "Выполнить платный запрос", exact: true })
      .click(),
  ]);
  assert.equal(paidResponse.status(), 200);
  const paidData = await paidResponse.json();
  assert.equal(paidData.mode, "paid");
  assert.notEqual(paidData.id, recordedData.id);
  assert.equal(
    paidData.parts.every((p) => p.state === "test_only"),
    true,
  );
  await page.getByRole("status").filter({ hasText: "Тест №" }).waitFor();
  await page.route("**/api/dialogues/replay", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Тестовая ошибка replay" }),
    }),
  );
  await page
    .getByRole("button", { name: "Проверить сохранённый ответ", exact: true })
    .click();
  await page
    .getByRole("alert")
    .filter({ hasText: "Тестовая ошибка replay" })
    .waitFor();
  await page.waitForTimeout(5500);
  assert.equal(
    await page
      .getByRole("alert")
      .filter({ hasText: "Тестовая ошибка replay" })
      .count(),
    1,
  );
  await page.unroute("**/api/dialogues/replay");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Медиа");
  await page.getByLabel("Тип материала", { exact: true }).selectOption("photo");
  await page.getByText("Готово", { exact: true }).first().waitFor();
  await page.locator(".media-row").first().click();
  assert.equal(
    await page
      .locator(".media-entry")
      .first()
      .locator(".media-inline-detail")
      .count(),
    1,
  );
  await page.getByRole("heading", { name: "Что видно", exact: true }).waitFor();
  await page.getByText("Книжный клуб", { exact: true }).waitFor();
  await page.locator(".photo-grid img").evaluate((img) => img.decode());
  await page.screenshot({
    path: path.join(output, "photos.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "photos-mobile.png"),
    fullPage: true,
  });
  await page.getByLabel("Область", { exact: true }).selectOption("private");
  await page
    .getByText("По этим фильтрам материалов пока нет.", { exact: true })
    .waitFor();
  assert.equal(
    await page.getByRole("heading", { name: "Что видно", exact: true }).count(),
    0,
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Интернет");
  await page.getByText("Завершено", { exact: true }).first().waitFor();
  await page.locator(".photo-row").first().click();
  await page
    .getByRole("heading", { name: "Результат проверки", exact: true })
    .waitFor();
  await page
    .getByText("Проверяемое утверждение · Подтверждается", { exact: true })
    .waitFor();
  assert.equal(
    await page
      .getByRole("link", { name: "Источник 1 ↗", exact: true })
      .getAttribute("href"),
    "https://example.org/evidence",
  );
  await page.screenshot({
    path: path.join(output, "research.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page
    .locator("summary")
    .filter({ hasText: "Поисковые действия и расход" })
    .click();
  await page.screenshot({
    path: path.join(output, "research-mobile.png"),
    fullPage: true,
  });
  // UI-only YouTube evidence fixture; actual retrieval/routing is covered in Python tests.
  await page.route(/\/api\/research\/\d+$/, async (route) => {
    const response = await route.fetch();
    const detail = await response.json();
    detail.sources.push({
      id: 77,
      url: "https://www.youtube.com/watch?v=eXcLD3We2tw",
      title: "Тестовый YouTube-ролик",
      coverage: "youtube_transcript",
      text: "Описание автора и сохранённый фрагмент речи.",
      error: null,
      youtube: {
        channel: "Тестовый канал",
        duration_seconds: 3661,
        chapters: [
          { seconds: 0, title: "Вступление" },
          { seconds: 70, title: "Обсуждение" },
        ],
        transcript_status: "available",
        transcript_language: "ru",
        transcript_automatic: true,
        transcript_truncated: true,
        description_truncated: true,
        diagnostics: [{ step: "captions", code: "captions_empty" }],
      },
    });
    await route.fulfill({ response, json: detail });
  });
  await page
    .getByText("Субтитры: автоматические, ru", { exact: false })
    .waitFor();
  await page.getByText("Главы и таймкоды", { exact: true }).click();
  await page.getByText("1:10 — Обсуждение", { exact: true }).waitFor();
  await page.getByText("Диагностика загрузки YouTube", { exact: true }).click();
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "youtube-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: path.join(output, "youtube.png"),
    fullPage: true,
  });
  await page.unroute(/\/api\/research\/\d+$/);
  await page.getByLabel("Область", { exact: true }).selectOption("private");
  await page
    .getByText("Для выбранных чатов исследований пока нет.", { exact: true })
    .waitFor();
  assert.equal(
    await page
      .getByRole("heading", { name: "Результат проверки", exact: true })
      .count(),
    0,
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Медиа");
  await context.request.get(`${origin}/fixture-media`);
  for (let attempt = 0; attempt < 80; attempt++) {
    const response = await context.request.get(
      `${origin}/api/media?scope=group`,
    );
    const listing = await response.json();
    if (listing.items?.some((item) => item.state === "completed")) break;
    await page.waitForTimeout(100);
  }
  await page.getByLabel("Тип материала", { exact: true }).selectOption("video");
  await page.getByRole("button", { name: "Обновить", exact: true }).click();
  await page.getByText("Видео · № 1", { exact: true }).waitFor();
  await page.locator(".media-row").first().click();
  await page
    .getByRole("heading", { name: "Расшифровка речи", exact: true })
    .waitFor();
  await page
    .getByText("Привет, это короткий ролик из книжного клуба.", { exact: true })
    .waitFor();
  await page.getByText("Выбранные кадры · 2", { exact: true }).click();
  await page
    .locator(".photo-grid img")
    .first()
    .evaluate((img) => img.decode());
  assert.equal(await page.locator(".photo-grid img").count(), 2);
  await page
    .getByText("События по выбранным кадрам · 2", { exact: true })
    .click();
  await page.getByText("В начале видна вывеска", { exact: true }).waitFor();
  await page.getByText("В конце — вход в магазин", { exact: true }).waitFor();
  await page.waitForTimeout(3200);
  assert.equal(
    await page
      .getByText("В конце — вход в магазин", { exact: true })
      .isVisible(),
    true,
  );
  await page.screenshot({
    path: path.join(output, "media.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "media-mobile.png"),
    fullPage: true,
  });
  await page.getByLabel("Область", { exact: true }).selectOption("private");
  await page
    .getByText("По этим фильтрам материалов пока нет.", { exact: true })
    .waitFor();
  assert.equal(
    await page
      .getByRole("heading", { name: "Расшифровка речи", exact: true })
      .count(),
    0,
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Память");
  await page.getByLabel(/^Чат/).selectOption("-100");
  await page
    .getByText("Любит Python и проверять источники.", { exact: true })
    .waitFor();
  await page
    .getByRole("button", { name: "Редактировать", exact: true })
    .click();
  await page
    .getByLabel("Текст записи")
    .fill("Любит Python, Rust и проверять источники.");
  await page
    .getByRole("button", { name: "Сохранить запись", exact: true })
    .click();
  await page
    .getByText("Запись обновлена. Следующие ответы учтут изменение.", {
      exact: true,
    })
    .waitFor();
  await page
    .getByText("Загружаем профиль чата…", { exact: true })
    .waitFor({ state: "hidden" });
  await page
    .getByText("Любит Python, Rust и проверять источники.", { exact: true })
    .waitFor();
  await page.screenshot({
    path: path.join(output, "memory.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "memory-mobile.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Сводки и обработка", exact: true })
    .click();
  await page.waitForTimeout(250);
  await page.screenshot({
    path: path.join(output, "memory-summaries-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Собеседники", exact: true }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Удалить", exact: true }).click();
  await page
    .getByText("Запись удалена. Зависимый контекст очищен.", { exact: true })
    .waitFor();
  await page
    .getByText("Загружаем профиль чата…", { exact: true })
    .waitFor({ state: "hidden" });
  assert.equal(
    await page
      .getByText("Любит Python, Rust и проверять источники.", { exact: true })
      .count(),
    0,
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Настройки");
  await openSettingsSection(page, "Близкий человек");
  await page.getByLabel("Как к тебе обращаться").fill("Тестовый владелец");
  await navigate(page, "Обзор");
  await navigate(page, "Настройки");
  const ownerSection = await openSettingsSection(page, "Близкий человек");
  assert.equal(
    await page.getByLabel("Как к тебе обращаться").inputValue(),
    "Тестовый владелец",
  );
  await page.getByLabel("Ник в Telegram").fill("not a valid username");
  await ownerSection.locator("summary").first().click();
  let invalidPuts = 0;
  const countPut = (request) => {
    if (request.url().endsWith("/api/settings") && request.method() === "PUT")
      invalidPuts++;
  };
  page.on("request", countPut);
  await page.getByRole("button", { name: "Сохранить настройки" }).click();
  await page.getByLabel("Ник в Telegram").waitFor({ state: "visible" });
  await page.waitForFunction(() =>
    document.activeElement?.matches("input:invalid"),
  );
  assert.equal(
    await page
      .getByLabel("Ник в Telegram")
      .evaluate((el) => el === document.activeElement),
    true,
  );
  assert.equal(invalidPuts, 0);
  await page.getByLabel("Ник в Telegram").fill("");
  const mediaSection = await openSettingsSection(page, "Голос и видео");
  await mediaSection.getByText("Лимиты обработки", { exact: true }).click();
  await page.getByLabel("Максимум кадров").fill("0");
  await mediaSection.getByText("Лимиты обработки", { exact: true }).click();
  await mediaSection.locator("summary").first().click();
  await page.getByRole("button", { name: "Сохранить настройки" }).click();
  await page.getByLabel("Максимум кадров").waitFor({ state: "visible" });
  await page.waitForFunction(() =>
    document.activeElement?.matches("input:invalid"),
  );
  assert.equal(
    await page
      .getByLabel("Максимум кадров")
      .evaluate((el) => el === document.activeElement),
    true,
  );
  assert.equal(invalidPuts, 0);
  page.off("request", countPut);
  await page.getByLabel("Максимум кадров").fill("24");
  assert.equal(
    await page.getByLabel("Глубина обработки кадров").inputValue(),
    "medium",
  );
  await page.getByLabel("Глубина обработки кадров").selectOption("high");
  let releaseSave;
  const saving = new Promise((resolve) => {
    releaseSave = resolve;
  });
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() === "PUT") await saving;
    await route.continue();
  });
  await page.getByRole("button", { name: "Сохранить настройки" }).click();
  assert.equal(
    await page.getByLabel("Как к тебе обращаться").isDisabled(),
    true,
  );
  releaseSave();
  await page.getByRole("status").waitFor();
  await page.unroute("**/api/settings");
  await page.reload();
  await navigate(page, "Настройки");
  assert.equal(
    await page.getByLabel("Как к тебе обращаться").inputValue(),
    "Тестовый владелец",
  );
  assert.equal(
    await page.getByLabel("Глубина обработки кадров").inputValue(),
    "high",
  );
  const other = await context.newPage();
  await other.goto(origin);
  await navigate(other, "Настройки");
  await openSettingsSection(other, "Близкий человек");
  await openSettingsSection(page, "Близкий человек");
  await other.getByLabel("Как к тебе обращаться").fill("Вторая вкладка");
  await page.getByLabel("Как к тебе обращаться").fill("После сохранения");
  await page.getByRole("button", { name: "Сохранить настройки" }).click();
  await page.getByRole("status").waitFor();
  await other.getByRole("button", { name: "Сохранить настройки" }).click();
  await other
    .getByRole("alert")
    .filter({ hasText: "другой вкладке" })
    .waitFor();
  await other.close();
  await page.screenshot({
    path: path.join(output, "settings.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  const mobileMenu = page.locator(".mobile-nav-toggle");
  assert.equal(await page.locator("#main-navigation").isVisible(), false);
  await mobileMenu.focus();
  await page.keyboard.press("Enter");
  assert.equal(await page.locator("#main-navigation").isVisible(), true);
  await page.keyboard.press("Escape");
  assert.equal(
    await mobileMenu.evaluate((el) => el === document.activeElement),
    true,
  );
  assert.equal(await mobileMenu.getAttribute("aria-expanded"), "false");
  await navigate(page, "Обзор");
  await page.waitForFunction(() => document.activeElement?.tagName === "H1");
  assert.equal(
    await page.locator("h1").evaluate((el) => el === document.activeElement),
    true,
  );
  await navigate(page, "Настройки");
  await page.screenshot({
    path: path.join(output, "mobile.png"),
    fullPage: true,
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate(page, "Поведение");
  await page.getByLabel("Настроение и план действий").selectOption("on");
  await page
    .getByRole("button", { name: "Сохранить поведение", exact: true })
    .click();
  await page
    .getByText("Настройки поведения сохранены", { exact: true })
    .waitFor();
  await fetch(`${origin}/fixture-behavior?kind=addressed`);
  await page.getByText("Тёплая", { exact: true }).waitFor();
  await page.getByLabel(/^Чат/).selectOption("-100");
  await page.getByLabel("Режим инициативы").selectOption("shadow");
  await page.getByLabel("Шанс инициативы, %").fill("100");
  await page
    .getByRole("button", { name: "Сохранить инициативу", exact: true })
    .click();
  await fetch(`${origin}/fixture-behavior?kind=ambient`);
  await page.getByText("Заря предложила ответить", { exact: true }).waitFor();
  await page.getByText("Заря предложила ответить", { exact: true }).click();
  await page
    .getByText("О, вот это сюжетный поворот", { exact: true })
    .waitFor();
  await page.getByLabel("Шанс инициативы, %").fill("5");
  await page.waitForTimeout(5200);
  assert.equal(await page.getByLabel("Шанс инициативы, %").inputValue(), "5");
  assert.equal(
    await page.evaluate(() => {
      const e = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(e);
      return e.defaultPrevented;
    }),
    true,
  );
  await page
    .getByRole("button", { name: "Сохранить инициативу", exact: true })
    .click();
  await page
    .getByText("Настройка инициативы сохранена", { exact: true })
    .waitFor();
  await page
    .getByRole("button", {
      name: "Остановить ответы и реакции во всех чатах",
      exact: true,
    })
    .waitFor({ state: "visible" });
  await page.waitForTimeout(200);
  await page.screenshot({
    path: path.join(output, "behavior.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "behavior-mobile.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", {
      name: "Сбросить настроение: Тестовый собеседник, чат -100, топик 0",
      exact: true,
    })
    .click();
  await page
    .getByText("Настроение этого разговора сброшено", { exact: true })
    .waitFor();
  await page.getByLabel("Выразительность").selectOption("expressive");
  let samePageDialogs = 0;
  const samePageDialog = async (dialog) => {
    samePageDialogs++;
    await dialog.accept();
  };
  page.on("dialog", samePageDialog);
  await navigate(page, "Поведение");
  page.off("dialog", samePageDialog);
  assert.equal(samePageDialogs, 0);
  let guardedTransition = false;
  page.once("dialog", async (dialog) => {
    guardedTransition = true;
    await dialog.dismiss();
  });
  await navigate(page, "Пилот");
  assert.equal(guardedTransition, true);
  assert.equal(
    await page.getByLabel("Выразительность").inputValue(),
    "expressive",
  );
  await page.keyboard.press("Escape");

  await page
    .getByRole("button", {
      name: "Остановить ответы и реакции во всех чатах",
      exact: true,
    })
    .click();
  await page
    .getByText("Новые ответы и реакции остановлены", { exact: true })
    .waitFor();
  await page
    .getByRole("button", { name: "Сохранить поведение", exact: true })
    .click();
  await page.getByRole("alert").filter({ hasText: "другой вкладке" }).waitFor();
  assert.equal(
    await page.getByLabel("Выразительность").inputValue(),
    "expressive",
  );
  await page
    .getByRole("button", { name: "Загрузить актуальные значения", exact: true })
    .click();
  await page.getByLabel("Область").selectOption("private");
  await page.getByLabel(/^Чат/).selectOption("42");
  assert.equal(
    await page.getByLabel("Режим инициативы", { exact: true }).count(),
    0,
  );
  await navigate(page, "Пилот");
  await page
    .getByRole("heading", { name: "Расходы по направлениям", exact: true })
    .waitFor();
  await page.getByLabel("Период", { exact: true }).selectOption("30");
  await page
    .getByRole("heading", { name: "Очереди сейчас", exact: true })
    .waitFor();
  await page.getByRole("button", { name: "По моделям", exact: true }).click();
  await page
    .getByRole("button", { name: "Оценить: Характер и краткость", exact: true })
    .click();
  await page
    .getByLabel("Результат проверки", { exact: true })
    .selectOption("passed");
  await page
    .getByLabel("Заметка к проверке", { exact: true })
    .fill("Синтетическая браузерная проверка, не оценка живого поведения");
  page.once("dialog", (dialog) => dialog.dismiss());
  await page.getByRole("button", { name: "Обновить", exact: true }).click();
  assert.equal(
    await page.getByLabel("Заметка к проверке", { exact: true }).inputValue(),
    "Синтетическая браузерная проверка, не оценка живого поведения",
  );
  await page
    .getByRole("button", { name: "Сохранить оценку", exact: true })
    .click();
  await page.getByText("Оценка сохранена", { exact: true }).waitFor();
  await page
    .getByText("Характер и краткость · Пройдено", { exact: true })
    .waitFor();
  await page
    .getByRole("button", { name: "Настроить порог", exact: true })
    .click();
  await page.getByLabel("Порог за 30 дней, USD", { exact: true }).fill("5");
  await page
    .getByRole("button", { name: "Сохранить порог", exact: true })
    .click();
  await page.getByText("Порог сохранён", { exact: true }).waitFor();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: path.join(output, "pilot.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: path.join(output, "pilot-mobile.png"),
    fullPage: true,
  });
  await page.getByLabel("Область", { exact: true }).selectOption("private");
  await page
    .getByRole("heading", { name: "Журнал вызовов", exact: true })
    .waitFor();
  await stop();
  await start(data);
  await page.reload();
  await page.getByRole("heading", { name: "С возвращением." }).waitFor();
  await page.screenshot({
    path: path.join(output, "login-mobile.png"),
    fullPage: true,
  });
  await page.getByLabel("Пароль", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Войти →" }).click();
  await navigate(page, "Настройки");
  assert.equal(
    await page.getByLabel("Как к тебе обращаться").inputValue(),
    "После сохранения",
  );
  await page.getByRole("button", { name: "Выйти ↗" }).click();
  await page.getByRole("heading", { name: "С возвращением." }).waitFor();
  assert.deepEqual(errors, []);
  console.log(
    "Browser smoke passed: setup, access, dialogue/replay, settings/conflict, photos, research, media, memory, behavior/mood/reset/reaction/shadow/chance/polling/draft/stop/conflict/mobile360, pilot/filters/review/draft/threshold/mobile360, restart/login/logout. Telegram and OpenAI adapters are synthetic; zero external calls.",
  );
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (browser) await browser.close();
    await stop();
  });
