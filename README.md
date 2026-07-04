# Cloze Master Telegram Bot

> **🎓 Myanmar Grade 12 Matriculation Examination Preparation 🇲🇲**

An AI-powered, button-first Telegram learning app built specifically for Grade 12 matriculation exam practice, using `python-telegram-bot` and OpenRouter's `google/gemini-2.5-flash` model.

Telegram and OpenRouter use fully asynchronous HTTP clients. Runtime SQLite operations and prompt-file reads are dispatched through asynchronous worker threads with per-database locks, so network, database, and file access do not block the bot event loop.

The Telegram application processes up to 16 updates concurrently, so one user waiting for AI quiz generation does not freeze commands, menus, or quizzes for other users.

## User experience

Users start with a persistent native Telegram menu—no commands to learn:

- ⚡ **Quick Quiz** — starts immediately with saved defaults
- 🧠 **Custom Quiz** — inline unit and question-count selectors
- 📄 **Full Test + Keys** — downloads a printable test file
- 🏆 **Leaderboard** — ranks learners by correct answers and accuracy
- 📊 **My Stats** — shows personal rank, points, accuracy, and best score
- ⚙️ **Settings** — saves the default unit and quiz length
- ℹ️ **Help** — visual onboarding and instructions

During a quiz, Telegram keyboard controls provide hints, skipping, and early exit. Questions include progress indicators, immediate feedback, and a final score summary.

If OpenRouter returns a mixture of valid and invalid question items, the bot quietly omits the invalid items and continues with the usable questions.

Each request asks the AI for 50% extra questions, rounded upward. Only the requested number of valid questions is delivered. If the first response still does not contain enough valid items, a second response supplements the valid questions already collected.

Default settings work immediately:

- Source: all units
- Questions: 10
- Maximum: 30

## Deployment setup

The operator only needs the two unavoidable service credentials:

```powershell
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Add `BOT_TOKEN` and `OPENROUTER_API_KEY` to `.env`, then run:

```powershell
py quiz_bot.py
```

User preferences are persisted automatically in `bot_state.pkl`. Unit Markdown files are loaded from `units/`.

The app rotates through the least-used bold and KEY VOCABULARY words for each source selection, while the AI continues to choose non-highlighted words freely. Only highlighted-word counters are stored in `highlighted_usage.sqlite3`; questions and user answers are not stored.

Interactive quiz results are aggregated in `quiz_stats.sqlite3` for the leaderboard, personal stats, and admin dashboard. The database stores Telegram user IDs, display names, and score totals; individual answers and question text are not stored.

Set `ADMIN_USER_IDS` in `.env` to one or more comma-separated Telegram numeric user IDs. Authorized users can open `/dashboard` for global usage totals; everyone else is denied access.

Admins can use `/broadcast` to compose and preview an announcement before confirming delivery. Broadcasts preserve Telegram formatting and supported media, show delivery progress, retry rate limits, deactivate blocked recipients, and store aggregate delivery history. Users can opt out with `/unsubscribe` and rejoin with `/subscribe`. Broadcast message content is not stored.

The generation instructions and example style are maintained separately in `prompt_template.txt`, so prompt revisions do not require editing application code.

## Optional command fallbacks

- `/start` — open the native main menu
- `/quiz 4 15` — Unit 4 interactive quiz with 15 questions
- `/fulltest all 10` — complete test file with keys
- `/leaderboard` — global top learners
- `/stats` — personal progress statistics
- `/dashboard` — admin-only system dashboard
- `/broadcast` — admin-only broadcast composer
- `/subscribe` — enable announcements
- `/unsubscribe` — disable announcements

## Tests

```powershell
py -m pip install -r requirements-dev.txt
py -m unittest -v
py -m mypy .
py -m ruff check .
```
