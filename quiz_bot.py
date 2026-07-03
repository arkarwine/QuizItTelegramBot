from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import string
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeAlias, TypedDict, cast

import httpx
from telegram import (
    Bot,
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    CallbackQueryHandler,
    TypeHandler,
    filters,
)


LOGGER = logging.getLogger("cloze_quiz_bot")
UNIT_FILE = re.compile(r"^unit-(\d+)-.+\.md$", re.IGNORECASE)
SOURCE_WORD = re.compile(r"\b[A-Za-z]+\b")
MAX_QUESTIONS = 30
DEFAULT_QUESTIONS = 10
ALLOWED_UPDATES = ["message", "callback_query"]
PROMPT_FILE = Path(__file__).with_name("prompt_template.txt")
TelegramApplication: TypeAlias = Application[Any, Any, Any, Any, Any, Any]
HIGHLIGHTED_STOP_WORDS = {
    "a",
    "an",
    "and",
    "all",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "between",
    "but",
    "by",
    "can",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "his",
    "in",
    "is",
    "it",
    "its",
    "not",
    "of",
    "on",
    "or",
    "our",
    "she",
    "that",
    "the",
    "their",
    "them",
    "they",
    "this",
    "to",
    "was",
    "we",
    "were",
    "with",
    "you",
    "your",
}


class BotError(RuntimeError):
    pass


class QuestionGenerator(Protocol):
    async def generate(self, source: Source, count: int) -> list[Question]: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Question:
    prompt: str
    answer: str


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    text: str


class UserSettings(TypedDict):
    unit: str
    count: int


@dataclass(slots=True)
class Session:
    questions: list[Question]
    position: int = 0
    correct: int = 0


class UnitCatalog:
    def __init__(self, units: dict[int, str]) -> None:
        if not units:
            raise ValueError("No unit files found.")
        self.units = dict(sorted(units.items()))

    @classmethod
    def load(cls, directory: Path) -> UnitCatalog:
        if not directory.is_dir():
            raise ValueError(f"Unit directory does not exist: {directory}")
        units: dict[int, str] = {}
        for path in directory.iterdir():
            match = UNIT_FILE.match(path.name)
            if match:
                units[int(match.group(1))] = path.read_text(encoding="utf-8")
        return cls(units)

    def select(self, value: str) -> Source:
        value = value.strip().lower()
        if not value or value == "all":
            text = "\n\n".join(
                f"UNIT {number}\n{body}" for number, body in self.units.items()
            )
            return Source("All units", text)

        if not value.isdigit() or int(value) not in self.units:
            available = ", ".join(map(str, self.units))
            raise ValueError(f"Choose all or a unit from: {available}")
        number = int(value)
        return Source(f"Unit {number}", self.units[number])


def extract_highlighted_words(text: str) -> set[str]:
    """Extract useful words from Markdown bold text and KEY VOCABULARY sections."""
    body_without_headings = re.sub(r"(?m)^[ \t]*#{1,6}.*$", "", text)
    highlighted_parts = [
        part
        for part in re.findall(r"\*\*(.+?)\*\*", body_without_headings, flags=re.DOTALL)
        if not re.match(
            r"(?is)^[ \t]*(?:UNIT\s+\d+|READ\b|PART\s+[IVX]+|==>)",
            part,
        )
    ]
    key_sections = re.findall(
        r"(?ims)^[ \t]*##\s+KEY\s+VOCABULARY\b(.*?)"
        r"(?=^[ \t]*(?:#{1,6}\s|UNIT\s+\d+\s*$)|\Z)",
        text,
    )
    words = SOURCE_WORD.findall("\n".join([*highlighted_parts, *key_sections]))
    return {
        word.casefold()
        for word in words
        if word.casefold() not in HIGHLIGHTED_STOP_WORDS and len(word) >= 2
    }


class HighlightUsageStore:
    """Persist counters only for highlighted vocabulary words."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS highlighted_usage (
                scope TEXT NOT NULL,
                word TEXT NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0 CHECK (use_count >= 0),
                PRIMARY KEY (scope, word)
            )
            """
        )
        self.connection.commit()

    def reserve(self, scope: str, candidates: set[str], count: int) -> list[str]:
        words = sorted(candidates)
        if not words or count < 1:
            return []
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO highlighted_usage(scope, word) VALUES (?, ?)",
                ((scope, word) for word in words),
            )
            rows = self.connection.execute(
                "SELECT word, use_count FROM highlighted_usage WHERE scope = ?",
                (scope,),
            ).fetchall()
            counts = {str(word): int(use_count) for word, use_count in rows}
            random.shuffle(words)
            words.sort(key=lambda word: counts.get(word, 0))
            selected = words[: min(count, len(words))]
            self.connection.executemany(
                """
                UPDATE highlighted_usage
                SET use_count = use_count + 1
                WHERE scope = ? AND word = ?
                """,
                ((scope, word) for word in selected),
            )
        return selected

    def reconcile(self, scope: str, reserved: list[str], used: set[str]) -> None:
        unused = [word for word in reserved if word not in used]
        if not unused:
            return
        with self.connection:
            self.connection.executemany(
                """
                UPDATE highlighted_usage
                SET use_count = CASE
                    WHEN use_count > 0 THEN use_count - 1
                    ELSE 0
                END
                WHERE scope = ? AND word = ?
                """,
                ((scope, word) for word in unused),
            )

    def counts(self, scope: str, candidates: set[str]) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT word, use_count FROM highlighted_usage WHERE scope = ?",
            (scope,),
        ).fetchall()
        counts = {str(word): int(use_count) for word, use_count in rows}
        return {word: counts.get(word, 0) for word in candidates}

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True, slots=True)
class PlayerStats:
    user_id: int
    display_name: str
    quizzes_completed: int
    questions_answered: int
    correct_answers: int
    best_score: int

    @property
    def accuracy(self) -> int:
        if not self.questions_answered:
            return 0
        return round(self.correct_answers * 100 / self.questions_answered)

    @property
    def points(self) -> int:
        return self.correct_answers * 10


@dataclass(frozen=True, slots=True)
class AdminStats:
    known_users: int
    subscribed_users: int
    total_players: int
    quizzes_completed: int
    questions_answered: int
    correct_answers: int
    active_today: int
    broadcasts_sent: int

    @property
    def accuracy(self) -> int:
        if not self.questions_answered:
            return 0
        return round(self.correct_answers * 100 / self.questions_answered)


class StatsStore:
    """Persist aggregate quiz results for dashboards and leaderboards."""

    def __init__(self, path: Path | str) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                quizzes_completed INTEGER NOT NULL DEFAULT 0,
                questions_answered INTEGER NOT NULL DEFAULT 0,
                correct_answers INTEGER NOT NULL DEFAULT 0,
                best_score INTEGER NOT NULL DEFAULT 0,
                last_played TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                username TEXT,
                is_subscribed INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                total_recipients INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            );
            INSERT OR IGNORE INTO bot_users (user_id, display_name)
            SELECT user_id, display_name FROM player_stats;
            """
        )
        self.connection.commit()

    def register_user(
        self, user_id: int, display_name: str, username: str | None
    ) -> None:
        if user_id <= 0:
            return
        safe_name = display_name.strip()[:80] or "Player"
        safe_username = username.strip()[:64] if username else None
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO bot_users (user_id, display_name, username)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    username = COALESCE(excluded.username, bot_users.username),
                    last_seen = CURRENT_TIMESTAMP
                """,
                (user_id, safe_name, safe_username),
            )

    def set_subscription(self, user_id: int, subscribed: bool) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE bot_users SET is_subscribed = ? WHERE user_id = ?",
                (1 if subscribed else 0, user_id),
            )

    def broadcast_recipients(self) -> list[int]:
        rows = self.connection.execute(
            "SELECT user_id FROM bot_users WHERE is_subscribed = 1 ORDER BY user_id"
        ).fetchall()
        return [int(row[0]) for row in rows]

    def create_broadcast(self, admin_id: int, total_recipients: int) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO broadcasts (admin_id, total_recipients)
                VALUES (?, ?)
                """,
                (admin_id, total_recipients),
            )
        if cursor.lastrowid is None:
            raise RuntimeError("Could not create broadcast record.")
        return int(cursor.lastrowid)

    def finish_broadcast(self, broadcast_id: int, delivered: int, failed: int) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE broadcasts
                SET delivered = ?, failed = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (delivered, failed, broadcast_id),
            )

    def record_quiz(
        self,
        user_id: int,
        display_name: str,
        answered: int,
        correct: int,
        score: int,
    ) -> None:
        if user_id <= 0:
            return
        self.register_user(user_id, display_name, None)
        safe_name = display_name.strip()[:80] or "Player"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO player_stats (
                    user_id, display_name, quizzes_completed,
                    questions_answered, correct_answers, best_score
                ) VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    quizzes_completed = quizzes_completed + 1,
                    questions_answered = questions_answered + excluded.questions_answered,
                    correct_answers = correct_answers + excluded.correct_answers,
                    best_score = MAX(best_score, excluded.best_score),
                    last_played = CURRENT_TIMESTAMP
                """,
                (user_id, safe_name, max(0, answered), max(0, correct), score),
            )

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> PlayerStats:
        return PlayerStats(
            user_id=int(row[0]),
            display_name=str(row[1]),
            quizzes_completed=int(row[2]),
            questions_answered=int(row[3]),
            correct_answers=int(row[4]),
            best_score=int(row[5]),
        )

    def profile(self, user_id: int) -> PlayerStats | None:
        row = self.connection.execute(
            """
            SELECT user_id, display_name, quizzes_completed,
                   questions_answered, correct_answers, best_score
            FROM player_stats WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        return self._from_row(cast(tuple[Any, ...], row)) if row else None

    def leaderboard(self, limit: int = 10) -> list[PlayerStats]:
        rows = self.connection.execute(
            """
            SELECT user_id, display_name, quizzes_completed,
                   questions_answered, correct_answers, best_score
            FROM player_stats
            ORDER BY correct_answers DESC,
                     CASE WHEN questions_answered = 0 THEN 0.0
                          ELSE CAST(correct_answers AS REAL) / questions_answered END DESC,
                     best_score DESC,
                     quizzes_completed DESC,
                     user_id ASC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        return [self._from_row(cast(tuple[Any, ...], row)) for row in rows]

    def rank(self, user_id: int) -> int | None:
        rows = self.leaderboard(1_000_000)
        for index, player in enumerate(rows, start=1):
            if player.user_id == user_id:
                return index
        return None

    def admin_summary(self) -> AdminStats:
        player_row = self.connection.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(quizzes_completed), 0),
                   COALESCE(SUM(questions_answered), 0),
                   COALESCE(SUM(correct_answers), 0),
                   COALESCE(SUM(CASE WHEN date(last_played) = date('now')
                                     THEN 1 ELSE 0 END), 0)
            FROM player_stats
            """
        ).fetchone() or (0, 0, 0, 0, 0)
        user_row = self.connection.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(is_subscribed), 0)
            FROM bot_users
            """
        ).fetchone() or (0, 0)
        broadcast_row = self.connection.execute(
            "SELECT COUNT(*) FROM broadcasts WHERE completed_at IS NOT NULL"
        ).fetchone() or (0,)
        return AdminStats(
            known_users=int(user_row[0]),
            subscribed_users=int(user_row[1]),
            total_players=int(player_row[0]),
            quizzes_completed=int(player_row[1]),
            questions_answered=int(player_row[2]),
            correct_answers=int(player_row[3]),
            active_today=int(player_row[4]),
            broadcasts_sent=int(broadcast_row[0]),
        )

    def close(self) -> None:
        self.connection.close()


def parse_quiz_args(args: list[str], default_count: int) -> tuple[str, int]:
    if len(args) > 2:
        raise ValueError("Usage: /quiz [unit|all] [number of questions]")
    unit = args[0].lower() if args else "all"
    count = default_count
    if len(args) == 2:
        try:
            count = int(args[1])
        except ValueError as error:
            raise ValueError("The question count must be a number.") from error
    if not 1 <= count <= MAX_QUESTIONS:
        raise ValueError(f"The question count must be between 1 and {MAX_QUESTIONS}.")
    return unit, count


def buffered_question_count(requested: int) -> int:
    """Add a 25% generation buffer, with at least two spare questions."""
    return requested + max(2, (requested + 3) // 4)


def clean_fragment(fragment: str, answer: str) -> str:
    fragment = re.sub(
        r"(?i)\[(?:blank|answer|missing word)\]|"
        r"\((?:blank|answer|missing word)\)|"
        r"<(?:blank|answer|missing word)>",
        "",
        fragment,
    )
    fragment = re.sub(r"\b[A-Za-z]\s*_+", "", fragment)
    fragment = re.sub(r"_+", "", fragment)
    fragment = re.sub(rf"\b{re.escape(answer)}\b", "", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"\s+", " ", fragment).strip()
    return re.sub(r"\s+([.,;:?!])", r"\1", fragment)


def build_question(before: str, answer: str, after: str) -> Question:
    before = clean_fragment(before, answer)
    after = clean_fragment(after, answer)
    initial = answer[0].lower()
    before = re.sub(
        rf"\b{re.escape(initial)}$", "", before, flags=re.IGNORECASE
    ).rstrip()
    left = before + (" " if before else "")
    right = ("" if not after or after[0] in ".,;:?!" else " ") + after
    return Question(left + initial + "________" + right, answer)


def question_identity(question: Question) -> tuple[str, str]:
    """Return canonical template and completed-sentence keys for deduplication."""
    blank = re.compile(r"\b[A-Za-z]_{8}\b")
    template = blank.sub(" blanktoken ", question.prompt, count=1)
    completed = blank.sub(f" {question.answer} ", question.prompt, count=1)

    def canonical(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

    return canonical(template), canonical(completed)


class OpenRouterGenerator:
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(
        self,
        api_key: str,
        model: str,
        usage_store: HighlightUsageStore,
        site_url: str = "",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.site_url = site_url
        self.usage_store = usage_store
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(90.0))

    async def generate(self, source: Source, count: int) -> list[Question]:
        template = PROMPT_FILE.read_text(encoding="utf-8")
        generation_count = buffered_question_count(count)
        highlighted_words = extract_highlighted_words(source.text)
        highlighted_count = min(
            len(highlighted_words), max(1, round(generation_count * 0.7))
        )
        scope = source.name.casefold()
        highlighted_targets = self.usage_store.reserve(
            scope, highlighted_words, highlighted_count
        )
        free_count = generation_count - len(highlighted_targets)
        schema = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": generation_count,
                    "maxItems": generation_count,
                    "items": {
                        "type": "object",
                        "properties": {
                            "before": {
                                "type": "string",
                                "description": "Text ending exactly where the answer belongs; never a complete sentence.",
                            },
                            "answer": {
                                "type": "string",
                                "description": "One alphabetic word found in the source text.",
                            },
                            "after": {
                                "type": "string",
                                "minLength": 1,
                                "description": "All remaining sentence text after the answer, including final punctuation.",
                            },
                        },
                        "required": ["before", "answer", "after"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        }
        prompt = template.format(
            count=generation_count,
            highlighted_targets=json.dumps(highlighted_targets, ensure_ascii=False),
            highlighted_count=len(highlighted_targets),
            free_count=free_count,
            source_text=source.text,
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "Create clear ESL cloze-test sentences and follow the JSON schema.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.6,
            "max_tokens": 12000,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "cloze_quiz", "strict": True, "schema": schema},
            },
        }
        last_error: BotError | None = None
        candidates: list[Question] = []
        seen_answers: set[str] = set()
        seen_templates: set[str] = set()
        seen_sentences: set[str] = set()
        try:
            for attempt in range(2):
                try:
                    items = await self._complete(payload)
                    source_words = {
                        word.casefold() for word in SOURCE_WORD.findall(source.text)
                    }
                    questions = self._parse_questions(
                        items,
                        generation_count,
                        source_words,
                        highlighted_words,
                        set(highlighted_targets),
                    )
                    for question in questions:
                        answer_key = question.answer.casefold()
                        template_key, sentence_key = question_identity(question)
                        if (
                            answer_key in seen_answers
                            or template_key in seen_templates
                            or sentence_key in seen_sentences
                        ):
                            continue
                        candidates.append(question)
                        seen_answers.add(answer_key)
                        seen_templates.add(template_key)
                        seen_sentences.add(sentence_key)
                    if len(candidates) >= count:
                        selected = self._select_questions(
                            candidates, count, set(highlighted_targets)
                        )
                        self._reconcile_highlighted(
                            scope, highlighted_targets, selected
                        )
                        return selected
                    last_error = BotError(
                        f"Only {len(candidates)} valid questions were generated."
                    )
                    if attempt == 0:
                        LOGGER.warning("%s Retrying for replacements.", last_error)
                        payload["temperature"] = 0.5
                except BotError as error:
                    last_error = error
                    if attempt == 0:
                        LOGGER.warning(
                            "OpenRouter generation failed; retrying once: %s", error
                        )
                        payload["temperature"] = 0.5
        except BaseException:
            self.usage_store.reconcile(scope, highlighted_targets, set())
            raise
        if candidates:
            selected = self._select_questions(
                candidates, count, set(highlighted_targets)
            )
            self._reconcile_highlighted(scope, highlighted_targets, selected)
            LOGGER.warning(
                "Returning %d valid questions after both generation attempts.",
                len(selected),
            )
            return selected
        self.usage_store.reconcile(scope, highlighted_targets, set())
        raise last_error or BotError("OpenRouter generation failed.")

    @staticmethod
    def _select_questions(
        questions: list[Question], count: int, highlighted_targets: set[str]
    ) -> list[Question]:
        highlighted = [
            question
            for question in questions
            if question.answer.casefold() in highlighted_targets
        ]
        general = [
            question
            for question in questions
            if question.answer.casefold() not in highlighted_targets
        ]
        random.shuffle(highlighted)
        random.shuffle(general)

        highlighted_goal = min(len(highlighted), max(1, round(count * 0.7)))
        selected = highlighted[:highlighted_goal]
        selected.extend(general[: count - len(selected)])
        if len(selected) < count:
            selected.extend(highlighted[highlighted_goal:])
        random.shuffle(selected)
        return selected[:count]

    def _reconcile_highlighted(
        self, scope: str, reserved: list[str], questions: list[Question]
    ) -> None:
        used = {
            question.answer.casefold()
            for question in questions
            if question.answer.casefold() in reserved
        }
        self.usage_store.reconcile(scope, reserved, used)

    async def _complete(self, payload: dict[str, Any]) -> object:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Cloze Quiz Bot",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        try:
            response = await self.client.post(self.URL, json=payload, headers=headers)
            response.raise_for_status()
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise BotError("OpenRouter returned non-text structured output.")
            result = json.loads(content)
            return result["questions"]
        except httpx.HTTPStatusError as error:
            raise BotError(
                f"OpenRouter HTTP {error.response.status_code}: {error.response.text}"
            ) from error
        except (
            httpx.RequestError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
            TypeError,
        ) as error:
            raise BotError(
                f"OpenRouter returned an invalid response: {error}"
            ) from error

    async def aclose(self) -> None:
        try:
            await self.client.aclose()
        finally:
            self.usage_store.close()

    @staticmethod
    def _parse_questions(
        items: Any,
        count: int,
        source_words: set[str] | None = None,
        highlighted_words: set[str] | None = None,
        selected_highlighted: set[str] | None = None,
    ) -> list[Question]:
        if not isinstance(items, list):
            raise BotError("OpenRouter did not return a question list.")

        questions: list[Question] = []
        seen_answers: set[str] = set()
        seen_templates: set[str] = set()
        seen_sentences: set[str] = set()
        for index, item in enumerate(items, start=1):
            try:
                if not isinstance(item, dict):
                    raise BotError("invalid question object")
                before = item.get("before")
                answer = item.get("answer")
                after = item.get("after")
                if (
                    not isinstance(before, str)
                    or not isinstance(answer, str)
                    or not isinstance(after, str)
                ):
                    raise BotError("incomplete question")
                if not after.strip():
                    raise BotError("missing sentence text after the answer")
                if before.rstrip().endswith((".", "!", "?")):
                    raise BotError("blank would appear outside a complete sentence")
                answer = answer.strip()
                if not answer.isalpha():
                    raise BotError("answer is not one alphabetic word")
                answer_key = answer.casefold()
                if source_words is not None and answer_key not in source_words:
                    raise BotError("answer is not present in the selected source")
                if (
                    highlighted_words is not None
                    and selected_highlighted is not None
                    and answer_key in highlighted_words
                    and answer_key not in selected_highlighted
                ):
                    raise BotError("highlighted answer was not selected for this test")
                if answer_key in seen_answers:
                    raise BotError("duplicate answer")
                question = build_question(before, answer, after)
                template_key, sentence_key = question_identity(question)
                if template_key in seen_templates:
                    raise BotError("duplicate question template")
                if sentence_key in seen_sentences:
                    raise BotError("duplicate completed sentence")
                questions.append(question)
                seen_answers.add(answer_key)
                seen_templates.add(template_key)
                seen_sentences.add(sentence_key)
            except BotError as error:
                LOGGER.warning("Skipping generated question %d: %s", index, error)

        if not questions:
            raise BotError("OpenRouter returned no usable questions.")
        if len(questions) < count:
            LOGGER.warning(
                "Using %d valid questions out of %d requested.", len(questions), count
            )
        return questions


def format_full_test(source: Source, questions: list[Question]) -> str:
    lines = [source.name, ""]
    lines.extend(
        f"{index}. {question.prompt}" for index, question in enumerate(questions, 1)
    )
    lines.extend(["", "Keys:", ""])
    lines.extend(
        f"{index}. {question.answer}" for index, question in enumerate(questions, 1)
    )
    return "\n".join(lines)


class QuizBot:
    QUICK = "⚡ Quick Quiz"
    CUSTOM = "🧠 Custom Quiz"
    FULL_TEST = "📄 Full Test + Keys"
    LEADERBOARD = "🏆 Leaderboard"
    STATS = "📊 My Stats"
    SETTINGS = "⚙️ Settings"
    HELP = "ℹ️ Help"
    HINT = "💡 Hint"
    SKIP = "⏭ Skip"
    END = "🛑 End Quiz"

    MAIN_KEYBOARD = ReplyKeyboardMarkup(
        [
            [QUICK],
            [CUSTOM, FULL_TEST],
            [LEADERBOARD, STATS],
            [SETTINGS, HELP],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an option…",
    )
    QUIZ_KEYBOARD = ReplyKeyboardMarkup(
        [[HINT, SKIP], [END]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Type the missing word…",
    )

    def __init__(
        self,
        catalog: UnitCatalog,
        generator: QuestionGenerator,
        default_count: int = DEFAULT_QUESTIONS,
        stats_store: StatsStore | None = None,
        admin_user_ids: set[int] | None = None,
    ) -> None:
        self.catalog = catalog
        self.generator = generator
        self.default_count = default_count
        self.stats_store = stats_store or StatsStore(":memory:")
        self.admin_user_ids = admin_user_ids or set()
        self.busy_users: set[int] = set()
        self.sent_deliveries: dict[int, tuple[int, int]] = {}

    @staticmethod
    def user_id(update: Update) -> int:
        return update.effective_user.id if update.effective_user else 0

    @staticmethod
    def user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object]:
        if context.user_data is None:
            raise RuntimeError("Telegram user data is unavailable.")
        return cast(dict[str, object], context.user_data)

    @staticmethod
    async def safe_edit(
        query: CallbackQuery,
        text: str,
        *,
        parse_mode: str = ParseMode.HTML,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            await query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except BadRequest as error:
            if "message is not modified" in str(error).casefold():
                LOGGER.debug("Ignoring unchanged Telegram message edit.")
                return
            raise

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message:
            return
        first_name = (
            html.escape(update.effective_user.first_name)
            if update.effective_user
            else "there"
        )
        await message.reply_text(
            f"👋 <b>Welcome, {first_name}!</b>\n\n"
            "🧩 <b>Cloze Master</b> turns your textbook units into fresh vocabulary "
            "challenges powered by AI.\n\n"
            "⚡ <b>Quick Quiz</b> starts immediately with your saved preferences.\n"
            "🧠 <b>Custom Quiz</b> lets you choose a unit and length.\n"
            "📄 <b>Full Test + Keys</b> creates a printable test file.\n\n"
            "🏆 <b>Leaderboard</b> shows the top learners.\n"
            "📊 <b>My Stats</b> tracks your progress.\n\n"
            "👇 Choose an option below—no commands to remember.",
            parse_mode=ParseMode.HTML,
            reply_markup=self.MAIN_KEYBOARD,
        )

    async def shutdown(self, application: TelegramApplication) -> None:
        try:
            await self.generator.aclose()
        finally:
            self.stats_store.close()

    def settings(self, context: ContextTypes.DEFAULT_TYPE) -> UserSettings:
        user_data = self.user_data(context)
        stored = user_data.get("settings")
        if isinstance(stored, dict):
            unit = stored.get("unit")
            count = stored.get("count")
            if isinstance(unit, str) and isinstance(count, int):
                return cast(UserSettings, stored)
        settings = UserSettings(unit="all", count=self.default_count)
        user_data["settings"] = settings
        return settings

    @staticmethod
    def unit_label(unit: str) -> str:
        return "🌍 All Units" if unit == "all" else f"📘 Unit {unit}"

    def unit_keyboard(self, flow: str) -> InlineKeyboardMarkup:
        def callback_for(unit: str) -> str:
            return f"fullunit:{unit}" if flow == "full" else f"unit:{flow}:{unit}"

        rows = [
            [InlineKeyboardButton("🌍 All Units", callback_data=callback_for("all"))]
        ]
        numbers = list(self.catalog.units)
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        f"Unit {number}", callback_data=callback_for(str(number))
                    )
                    for number in numbers[index : index + 3]
                ]
                for index in range(0, len(numbers), 3)
            ]
        )
        rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def count_keyboard(flow: str, unit: str) -> InlineKeyboardMarkup:
        counts = [5, 10, 15, 20, 25, 30]
        prefix = (
            "fullrun" if flow == "full" else "quizrun" if flow == "quiz" else "run:set"
        )
        rows = [
            [
                InlineKeyboardButton(
                    f"{count} questions", callback_data=f"{prefix}:{unit}:{count}"
                )
                for count in counts[index : index + 2]
            ]
            for index in range(0, len(counts), 2)
        ]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"menu:{flow}")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def settings_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📚 Default Unit", callback_data="settings:unit"
                    ),
                    InlineKeyboardButton(
                        "🔢 Quiz Length", callback_data="settings:count"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "♻️ Reset Defaults", callback_data="settings:reset"
                    )
                ],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
            ]
        )

    @staticmethod
    def settings_count_keyboard() -> InlineKeyboardMarkup:
        counts = [5, 10, 15, 20, 25, 30]
        rows = [
            [
                InlineKeyboardButton(
                    f"{count} questions", callback_data=f"setcount:{count}"
                )
                for count in counts[index : index + 2]
            ]
            for index in range(0, len(counts), 2)
        ]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="settings:back")])
        return InlineKeyboardMarkup(rows)

    async def show_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, edit: bool = False
    ) -> None:
        settings = self.settings(context)
        text = (
            "⚙️ <b>Your Quiz Settings</b>\n\n"
            f"📚 Source: <b>{html.escape(self.unit_label(str(settings['unit'])))}</b>\n"
            f"🔢 Questions: <b>{settings['count']}</b>\n\n"
            "These preferences are used by ⚡ Quick Quiz and saved automatically."
        )
        query = update.callback_query
        if edit and query:
            await self.safe_edit(
                query,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=self.settings_keyboard(),
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=self.settings_keyboard()
            )

    async def show_help(self, update: Update) -> None:
        text = (
            "ℹ️ <b>How to Play</b>\n\n"
            "1️⃣ Choose <b>Quick Quiz</b> or build a <b>Custom Quiz</b>.\n"
            "2️⃣ Read the sentence and type the complete missing word.\n"
            "3️⃣ Use 💡 <b>Hint</b> if you are stuck, or ⏭ <b>Skip</b>.\n"
            "4️⃣ Receive instant feedback and a final score.\n\n"
            "📄 <b>Full Test + Keys</b> downloads a complete numbered test that you can "
            "print, share, or study offline.\n\n"
            "💡 Your answers are not case-sensitive."
        )
        query = update.callback_query
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")]]
        )
        if query:
            await self.safe_edit(
                query, text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )

    @staticmethod
    def stats_keyboard(view: str) -> InlineKeyboardMarkup:
        other_label = "📊 My Stats" if view == "leaderboard" else "🏆 Leaderboard"
        other_data = "stats:personal" if view == "leaderboard" else "stats:leaderboard"
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"stats:{view}"),
                    InlineKeyboardButton(other_label, callback_data=other_data),
                ],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
            ]
        )

    async def show_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user_id = self.user_id(update)
        stats = self.stats_store.profile(user_id)
        refreshed = datetime.now().astimezone().strftime("%H:%M:%S")
        if stats is None:
            text = (
                "📊 <b>My Stats</b>\n\n"
                "No quiz results yet. Complete an interactive quiz to start tracking "
                "your progress.\n\n"
                f"🕒 Updated: {refreshed}"
            )
        else:
            rank = self.stats_store.rank(user_id)
            rank_text = f"#{rank}" if rank is not None else "—"
            text = (
                "📊 <b>My Stats</b>\n\n"
                f"🏅 Global rank: <b>{rank_text}</b>\n"
                f"⭐ Points: <b>{stats.points}</b>\n"
                f"🎮 Quizzes completed: <b>{stats.quizzes_completed}</b>\n"
                f"📝 Questions answered: <b>{stats.questions_answered}</b>\n"
                f"✅ Correct answers: <b>{stats.correct_answers}</b>\n"
                f"🎯 Overall accuracy: <b>{stats.accuracy}%</b>\n"
                f"🏆 Best score: <b>{stats.best_score}%</b>\n\n"
                f"🕒 Updated: {refreshed}"
            )
        keyboard = self.stats_keyboard("personal")
        if update.callback_query:
            await self.safe_edit(
                update.callback_query,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )

    def is_admin(self, update: Update) -> bool:
        return self.user_id(update) in self.admin_user_ids

    async def track_user(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user = update.effective_user
        if user:
            self.stats_store.register_user(user.id, user.full_name, user.username)

    async def subscribe(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user = update.effective_user
        if not user or not update.effective_message:
            return
        self.stats_store.register_user(user.id, user.full_name, user.username)
        self.stats_store.set_subscription(user.id, True)
        await update.effective_message.reply_text(
            "🔔 <b>Broadcasts enabled.</b> You will receive future announcements.",
            parse_mode=ParseMode.HTML,
            reply_markup=self.MAIN_KEYBOARD,
        )

    async def unsubscribe(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        user = update.effective_user
        if not user or not update.effective_message:
            return
        self.stats_store.register_user(user.id, user.full_name, user.username)
        self.stats_store.set_subscription(user.id, False)
        await update.effective_message.reply_text(
            "🔕 <b>Broadcasts disabled.</b> Use /subscribe whenever you want to rejoin.",
            parse_mode=ParseMode.HTML,
            reply_markup=self.MAIN_KEYBOARD,
        )

    @staticmethod
    def broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="broadcast:cancel")]]
        )

    async def start_broadcast(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.is_admin(update):
            await self.show_admin_dashboard(update, context)
            return
        user_data = self.user_data(context)
        user_data["broadcast_state"] = "awaiting_message"
        user_data.pop("broadcast_draft", None)
        text = (
            "📣 <b>New Broadcast</b>\n\n"
            "Send the message to broadcast next. Text formatting, photos, videos, "
            "documents, stickers, and other copyable Telegram messages are supported.\n\n"
            "You will see a preview before anything is sent."
        )
        keyboard = self.broadcast_cancel_keyboard()
        if update.callback_query:
            await self.safe_edit(update.callback_query, text, reply_markup=keyboard)
        elif update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )

    async def cancel_broadcast(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        user_data = self.user_data(context)
        user_data.pop("broadcast_state", None)
        user_data.pop("broadcast_draft", None)
        text = "❌ <b>Broadcast cancelled.</b>"
        if update.callback_query:
            await self.safe_edit(update.callback_query, text)
        elif update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=self.MAIN_KEYBOARD
            )

    async def capture_broadcast_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        user_data = self.user_data(context)
        if user_data.get("broadcast_state") != "awaiting_message":
            return False
        if not self.is_admin(update):
            user_data.pop("broadcast_state", None)
            return False
        message = update.effective_message
        if not message:
            return True

        user_data["broadcast_state"] = "preview"
        user_data["broadcast_draft"] = {
            "chat_id": message.chat_id,
            "message_id": message.message_id,
        }
        await context.bot.copy_message(
            chat_id=message.chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        recipients = len(self.stats_store.broadcast_recipients())
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"📨 Send to {recipients}", callback_data="broadcast:send"
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="broadcast:cancel")],
            ]
        )
        await message.reply_text(
            "👆 <b>Broadcast Preview</b>\n\n"
            f"Recipients: <b>{recipients}</b>\n"
            "Confirm only after checking the copied message above.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return True

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if await self.capture_broadcast_message(update, context):
            return
        await self.answer(update, context)

    @staticmethod
    def retry_delay(error: RetryAfter) -> float:
        retry_after = error.retry_after
        if isinstance(retry_after, timedelta):
            return retry_after.total_seconds() + 0.2
        return float(retry_after) + 0.2

    async def send_broadcast(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.is_admin(update):
            await self.show_admin_dashboard(update, context)
            return
        query = update.callback_query
        if not query:
            return
        user_data = self.user_data(context)
        draft = user_data.get("broadcast_draft")
        if not isinstance(draft, dict):
            await self.safe_edit(query, "⌛ This broadcast draft has expired.")
            return
        source_chat_id = draft.get("chat_id")
        source_message_id = draft.get("message_id")
        if not isinstance(source_chat_id, int) or not isinstance(
            source_message_id, int
        ):
            await self.safe_edit(query, "⚠️ This broadcast draft is invalid.")
            return

        user_data.pop("broadcast_state", None)
        user_data.pop("broadcast_draft", None)
        recipients = self.stats_store.broadcast_recipients()
        broadcast_id = self.stats_store.create_broadcast(
            self.user_id(update), len(recipients)
        )
        await self.safe_edit(
            query,
            f"📡 <b>Broadcasting…</b>\n\nDelivered: <b>0/{len(recipients)}</b>",
        )
        context.application.create_task(
            self._deliver_broadcast(
                query,
                context.bot,
                broadcast_id,
                recipients,
                source_chat_id,
                source_message_id,
            ),
            update=update,
            name=f"broadcast-{broadcast_id}",
        )

    async def _deliver_broadcast(
        self,
        query: CallbackQuery,
        bot: Bot,
        broadcast_id: int,
        recipients: list[int],
        source_chat_id: int,
        source_message_id: int,
    ) -> None:
        delivered = 0
        failed = 0

        for processed, recipient_id in enumerate(recipients, start=1):
            try:
                try:
                    await bot.copy_message(
                        chat_id=recipient_id,
                        from_chat_id=source_chat_id,
                        message_id=source_message_id,
                    )
                except RetryAfter as error:
                    await asyncio.sleep(self.retry_delay(error))
                    await bot.copy_message(
                        chat_id=recipient_id,
                        from_chat_id=source_chat_id,
                        message_id=source_message_id,
                    )
                delivered += 1
            except (Forbidden, BadRequest):
                failed += 1
                self.stats_store.set_subscription(recipient_id, False)
            except TelegramError as error:
                failed += 1
                LOGGER.warning(
                    "Broadcast delivery to user %d failed: %s", recipient_id, error
                )

            if processed % 25 == 0 and processed < len(recipients):
                await self.safe_edit(
                    query,
                    "📡 <b>Broadcasting…</b>\n\n"
                    f"Processed: <b>{processed}/{len(recipients)}</b>\n"
                    f"✅ Delivered: <b>{delivered}</b>\n"
                    f"⚠️ Failed: <b>{failed}</b>",
                )
            await asyncio.sleep(0.05)

        self.stats_store.finish_broadcast(broadcast_id, delivered, failed)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📣 New Broadcast", callback_data="broadcast:start"
                    ),
                    InlineKeyboardButton(
                        "🔐 Dashboard", callback_data="admin:dashboard"
                    ),
                ],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
            ]
        )
        await self.safe_edit(
            query,
            "✅ <b>Broadcast complete.</b>\n\n"
            f"👥 Recipients: <b>{len(recipients)}</b>\n"
            f"✅ Delivered: <b>{delivered}</b>\n"
            f"⚠️ Failed: <b>{failed}</b>",
            reply_markup=keyboard,
        )

    async def show_admin_dashboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self.is_admin(update):
            text = (
                "🔒 <b>Admin access required.</b>\n\n"
                f"Your Telegram user ID is <code>{self.user_id(update)}</code>. "
                "The bot owner must add it to <code>ADMIN_USER_IDS</code>."
            )
            if update.callback_query:
                await self.safe_edit(update.callback_query, text)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=self.MAIN_KEYBOARD,
                )
            return

        summary = self.stats_store.admin_summary()
        leaders = self.stats_store.leaderboard(1)
        top_player = (
            f"{html.escape(leaders[0].display_name)} ({leaders[0].points} pts)"
            if leaders
            else "—"
        )
        refreshed = datetime.now().astimezone().strftime("%H:%M:%S")
        text = (
            "🔐 <b>Admin Dashboard</b>\n\n"
            f"👥 Known users: <b>{summary.known_users}</b>\n"
            f"📣 Broadcast recipients: <b>{summary.subscribed_users}</b>\n"
            f"🏅 Ranked players: <b>{summary.total_players}</b>\n"
            f"🟢 Active today: <b>{summary.active_today}</b>\n"
            f"🎮 Completed quizzes: <b>{summary.quizzes_completed}</b>\n"
            f"📝 Questions answered: <b>{summary.questions_answered}</b>\n"
            f"✅ Correct answers: <b>{summary.correct_answers}</b>\n"
            f"🎯 Global accuracy: <b>{summary.accuracy}%</b>\n"
            f"🏆 Top player: <b>{top_player}</b>\n\n"
            f"📨 Broadcasts sent: <b>{summary.broadcasts_sent}</b>\n"
            f"⚙️ In-progress requests: <b>{len(self.busy_users)}</b>\n"
            f"🕒 Updated: {refreshed}"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data="admin:dashboard"),
                    InlineKeyboardButton(
                        "🏆 Leaderboard", callback_data="stats:leaderboard"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📣 New Broadcast", callback_data="broadcast:start"
                    )
                ],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
            ]
        )
        if update.callback_query:
            await self.safe_edit(
                update.callback_query,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )

    async def show_leaderboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        leaders = self.stats_store.leaderboard(10)
        refreshed = datetime.now().astimezone().strftime("%H:%M:%S")
        if not leaders:
            body = "No ranked players yet. Complete a quiz to claim first place!"
        else:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lines = []
            for rank, player in enumerate(leaders, start=1):
                badge = medals.get(rank, f"{rank}.")
                lines.append(
                    f"{badge} <b>{html.escape(player.display_name)}</b> — "
                    f"{player.points} pts · {player.accuracy}%"
                )
            body = "\n".join(lines)

        own_rank = self.stats_store.rank(self.user_id(update))
        own_rank_text = f"#{own_rank}" if own_rank is not None else "Unranked"
        text = (
            "🏆 <b>Global Leaderboard</b>\n\n"
            f"{body}\n\n"
            f"📍 Your rank: <b>{own_rank_text}</b>\n"
            "⭐ 10 points per correct answer\n\n"
            f"🕒 Updated: {refreshed}"
        )
        keyboard = self.stats_keyboard("leaderboard")
        if update.callback_query:
            await self.safe_edit(
                update.callback_query,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )

    async def menu_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if not message or not message.text:
            return
        if message.text == self.QUICK:
            settings = self.settings(context)
            await self.generate_quiz(
                update, context, str(settings["unit"]), int(settings["count"])
            )
        elif message.text == self.CUSTOM:
            await message.reply_text(
                "📚 <b>Choose your source</b>\n\nWhich unit should this quiz cover?",
                parse_mode=ParseMode.HTML,
                reply_markup=self.unit_keyboard("quiz"),
            )
        elif message.text == self.FULL_TEST:
            await message.reply_text(
                "📄 <b>Create a Full Test</b>\n\nChoose the source material:",
                parse_mode=ParseMode.HTML,
                reply_markup=self.unit_keyboard("full"),
            )
        elif message.text == self.LEADERBOARD:
            await self.show_leaderboard(update, context)
        elif message.text == self.STATS:
            await self.show_stats(update, context)
        elif message.text == self.SETTINGS:
            await self.show_settings(update, context)
        elif message.text == self.HELP:
            await self.show_help(update)

    async def callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        try:
            await query.answer()
        except TelegramError as error:
            LOGGER.warning("Could not acknowledge callback query: %s", error)
        data = query.data

        if data == "menu:home":
            await self.safe_edit(
                query,
                "🏠 <b>Main Menu</b>\n\nChoose an activity using the keyboard below.",
                parse_mode=ParseMode.HTML,
            )
            return
        if data == "stats:leaderboard":
            await self.show_leaderboard(update, context)
            return
        if data in {"stats:personal", "stats:dashboard"}:
            # stats:dashboard supports buttons created by the previous version.
            await self.show_stats(update, context)
            return
        if data == "admin:dashboard":
            await self.show_admin_dashboard(update, context)
            return
        if data == "broadcast:start":
            await self.start_broadcast(update, context)
            return
        if data == "broadcast:cancel":
            await self.cancel_broadcast(update, context)
            return
        if data == "broadcast:send":
            await self.send_broadcast(update, context)
            return
        if data in {"menu:quiz", "menu:full"}:
            flow = data.split(":", 1)[1]
            title = "🧠 <b>Custom Quiz</b>" if flow == "quiz" else "📄 <b>Full Test</b>"
            await self.safe_edit(
                query,
                title + "\n\nChoose the source material:",
                parse_mode=ParseMode.HTML,
                reply_markup=self.unit_keyboard(flow),
            )
            return
        if data.startswith("unit:"):
            _, flow, unit = data.split(":", 2)
            if flow == "set":
                self.settings(context)["unit"] = unit
                await self.show_settings(update, context, edit=True)
            else:
                await self.safe_edit(
                    query,
                    f"🔢 <b>Choose quiz length</b>\n\nSource: {html.escape(self.unit_label(unit))}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=self.count_keyboard(flow, unit),
                )
            return
        if data.startswith("fullunit:"):
            unit = data.split(":", 1)[1]
            await self.safe_edit(
                query,
                f"🔢 <b>Choose test length</b>\n\nSource: {html.escape(self.unit_label(unit))}",
                parse_mode=ParseMode.HTML,
                reply_markup=self.count_keyboard("full", unit),
            )
            return
        if data.startswith("quizrun:"):
            _, unit, raw_count = data.split(":", 2)
            count = int(raw_count)
            await self.generate_quiz(update, context, unit, count, edit_status=True)
            return
        if data.startswith("fullrun:"):
            _, unit, raw_count = data.split(":", 2)
            count = int(raw_count)
            await self.generate_fulltest(update, context, unit, count, edit_status=True)
            return
        if data.startswith("run:"):
            # Compatibility with inline buttons created by earlier bot versions.
            _, flow, unit, raw_count = data.split(":", 3)
            count = int(raw_count)
            if flow == "quiz":
                await self.generate_quiz(update, context, unit, count, edit_status=True)
            elif flow == "full":
                await self.generate_fulltest(
                    update, context, unit, count, edit_status=True
                )
            return
        if data == "settings:unit":
            await self.safe_edit(
                query,
                "📚 <b>Choose your default source</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=self.unit_keyboard("set"),
            )
            return
        if data == "settings:count":
            await self.safe_edit(
                query,
                "🔢 <b>Choose your default quiz length</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=self.settings_count_keyboard(),
            )
            return
        if data.startswith("setcount:"):
            count = int(data.rsplit(":", 1)[1])
            self.settings(context)["count"] = count
            await self.show_settings(update, context, edit=True)
            return
        if data == "settings:reset":
            self.user_data(context)["settings"] = UserSettings(
                unit="all", count=self.default_count
            )
            await self.show_settings(update, context, edit=True)
            return
        if data == "settings:back":
            await self.show_settings(update, context, edit=True)
            return

        if query:
            await self.safe_edit(
                query,
                "⌛ That menu has expired. Please use the current main menu below.",
            )

    async def quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            unit, count = parse_quiz_args(context.args or [], self.default_count)
        except ValueError as error:
            if update.effective_message:
                await update.effective_message.reply_text(str(error))
            return
        await self.generate_quiz(update, context, unit, count)

    async def fulltest(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        try:
            unit, count = parse_quiz_args(context.args or [], self.default_count)
        except ValueError as error:
            if update.effective_message:
                await update.effective_message.reply_text(
                    str(error).replace("/quiz", "/fulltest")
                )
            return
        await self.generate_fulltest(update, context, unit, count)

    async def generate_quiz(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        unit: str,
        count: int,
        *,
        edit_status: bool = False,
    ) -> None:
        user_id = self.user_id(update)
        if user_id in self.busy_users:
            text = "⏳ Please wait for the current request."
            if update.callback_query:
                await self.safe_edit(update.callback_query, text)
            elif update.effective_chat:
                await context.bot.send_message(update.effective_chat.id, text)
            return
        source = self.catalog.select(unit)
        self.busy_users.add(user_id)
        status = (
            "✨ <b>Building your quiz…</b>\n\n"
            f"📚 {html.escape(source.name)}\n"
            f"🔢 {count} questions\n\n"
            "This usually takes a few seconds."
        )
        if edit_status and update.callback_query:
            await self.safe_edit(
                update.callback_query, status, parse_mode=ParseMode.HTML
            )
        elif update.effective_message:
            await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
        try:
            questions = await self.generator.generate(source, count)
        except Exception as error:
            LOGGER.error("Quiz generation failed: %s", error)
            error_text = (
                "⚠️ <b>I couldn't build that quiz.</b>\n\nPlease try again in a moment."
            )
            if update.callback_query:
                await self.safe_edit(update.callback_query, error_text)
            elif update.effective_chat:
                await context.bot.send_message(
                    update.effective_chat.id,
                    error_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=self.MAIN_KEYBOARD,
                )
            return
        finally:
            self.busy_users.discard(user_id)

        random.shuffle(questions)
        self.user_data(context)["session"] = Session(questions)
        if update.effective_chat:
            if update.callback_query:
                await self.safe_edit(
                    update.callback_query,
                    "✅ <b>Quiz generated.</b> Your first question is below.",
                )
            await self.send_question(
                update.effective_chat.id,
                context,
                "🎉 <b>Your quiz is ready!</b> Good luck! 🍀",
            )

    async def generate_fulltest(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        unit: str,
        count: int,
        *,
        edit_status: bool = False,
    ) -> None:
        user_id = self.user_id(update)
        if user_id in self.busy_users:
            text = "⏳ Your previous request is still being prepared. Please wait."
            if update.callback_query:
                await self.safe_edit(update.callback_query, text)
            elif update.effective_chat:
                await context.bot.send_message(
                    update.effective_chat.id,
                    text,
                )
            return
        source = self.catalog.select(unit)
        self.busy_users.add(user_id)
        status = (
            "📝 <b>Preparing your full test…</b>\n\n"
            f"📚 {html.escape(source.name)}\n🔢 {count} questions + answer key"
        )
        if edit_status and update.callback_query:
            await self.safe_edit(
                update.callback_query, status, parse_mode=ParseMode.HTML
            )
        elif update.effective_message:
            await update.effective_message.reply_text(status, parse_mode=ParseMode.HTML)
        try:
            questions = await self.generator.generate(source, count)
        except Exception as error:
            LOGGER.error("Full test generation failed: %s", error)
            error_text = "⚠️ Test generation failed. Please try again."
            if update.callback_query:
                await self.safe_edit(update.callback_query, error_text)
            elif update.effective_chat:
                await context.bot.send_message(
                    update.effective_chat.id,
                    error_text,
                    reply_markup=self.MAIN_KEYBOARD,
                )
            return
        finally:
            self.busy_users.discard(user_id)

        random.shuffle(questions)
        content = format_full_test(source, questions).encode("utf-8")
        filename = f"cloze_test_{unit}_{len(questions)}_questions.txt"
        if update.effective_chat:
            await context.bot.send_document(
                update.effective_chat.id,
                document=InputFile(content, filename=filename),
                caption=(
                    "✅ <b>Your full test is ready!</b>\n\n"
                    f"📚 {html.escape(source.name)}\n"
                    f"🧩 {len(questions)} questions\n"
                    "🔑 Answer key included"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=self.MAIN_KEYBOARD,
            )

    async def answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if not message or not message.text:
            return
        player_id = self.user_id(update)
        player_name = (
            update.effective_user.full_name if update.effective_user else "Player"
        )
        if message.text in {
            self.QUICK,
            self.CUSTOM,
            self.FULL_TEST,
            self.LEADERBOARD,
            self.STATS,
            self.SETTINGS,
            self.HELP,
        }:
            await self.menu_text(update, context)
            return
        session = self.user_data(context).get("session")
        if not isinstance(session, Session):
            await message.reply_text(
                "🤔 I didn't recognize that choice. Please use the menu below.",
                reply_markup=self.MAIN_KEYBOARD,
            )
            return

        if message.text == self.HINT:
            question = session.questions[session.position]
            answer = question.answer
            await message.reply_text(
                f"💡 <b>Hint</b>\n\nThe word has <b>{len(answer)} letters</b> "
                f"and ends with <b>“{html.escape(answer[-1])}”</b>.",
                parse_mode=ParseMode.HTML,
                reply_markup=self.QUIZ_KEYBOARD,
            )
            return
        if message.text == self.SKIP:
            question = session.questions[session.position]
            session.position += 1
            if session.position == len(session.questions):
                await self.finish_quiz(
                    message,
                    context,
                    session,
                    f"⏭ Skipped. Answer: {question.answer}",
                    player_id,
                    player_name,
                )
            else:
                await self.send_question(
                    message.chat_id,
                    context,
                    f"⏭ Skipped. Answer: <b>{html.escape(question.answer)}</b>",
                )
            return
        if message.text == self.END:
            await self.finish_quiz(
                message,
                context,
                session,
                "🛑 Quiz ended early.",
                player_id,
                player_name,
            )
            return

        question = session.questions[session.position]
        correct = normalize(message.text) == normalize(question.answer)
        if correct:
            session.correct += 1
        session.position += 1
        feedback = (
            "✅ <b>Correct!</b> Excellent work."
            if correct
            else f"❌ <b>Not quite.</b> The answer is <b>{html.escape(question.answer)}</b>."
        )

        if session.position == len(session.questions):
            await self.finish_quiz(
                message,
                context,
                session,
                feedback,
                player_id,
                player_name,
            )
        else:
            await self.send_question(message.chat_id, context, feedback)

    async def send_question(
        self,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        intro: str = "",
    ) -> None:
        session = self.user_data(context).get("session")
        if not isinstance(session, Session):
            return
        delivery = (id(session), session.position)
        if self.sent_deliveries.get(chat_id) == delivery:
            LOGGER.warning(
                "Ignoring duplicate delivery request for question position %d.",
                session.position,
            )
            return
        question = session.questions[session.position]
        total = len(session.questions)
        completed = session.position
        bar_count = round(completed * 10 / total)
        progress = "🟩" * bar_count + "⬜" * (10 - bar_count)
        intro_text = f"{intro}\n\n" if intro else ""
        self.sent_deliveries[chat_id] = delivery
        try:
            await context.bot.send_message(
                chat_id,
                f"{intro_text}🧩 <b>Question {session.position + 1} of {total}</b>\n"
                f"{progress}\n\n"
                f"<blockquote>{html.escape(question.prompt)}</blockquote>\n"
                "✍️ Type the complete missing word:",
                parse_mode=ParseMode.HTML,
                reply_markup=self.QUIZ_KEYBOARD,
            )
        except BaseException:
            if self.sent_deliveries.get(chat_id) == delivery:
                self.sent_deliveries.pop(chat_id, None)
            raise

    async def finish_quiz(
        self,
        message: Message,
        context: ContextTypes.DEFAULT_TYPE,
        session: Session,
        feedback: str,
        player_id: int,
        player_name: str,
    ) -> None:
        total = len(session.questions)
        answered = session.position
        percentage = round(session.correct * 100 / total)
        self.stats_store.record_quiz(
            player_id,
            player_name,
            answered,
            session.correct,
            percentage,
        )
        if percentage >= 80:
            verdict = "🏆 Outstanding!"
        elif percentage >= 60:
            verdict = "👏 Good job!"
        else:
            verdict = "🌱 Keep practising!"
        await message.reply_text(
            f"{feedback}\n\n"
            "🎊 <b>Quiz Summary</b>\n\n"
            f"✅ Correct: <b>{session.correct}</b>\n"
            f"📝 Completed: <b>{answered}/{total}</b>\n"
            f"📊 Score: <b>{percentage}%</b>\n\n"
            f"{verdict}\n\nChoose another activity below 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=self.MAIN_KEYBOARD,
        )
        self.sent_deliveries.pop(message.chat_id, None)
        self.user_data(context).pop("session", None)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    return value.strip(string.punctuation + "‘’“”").strip()


def parse_admin_user_ids(value: str) -> set[int]:
    if not value.strip():
        return set()
    ids: set[int] = set()
    for item in re.split(r"[,\s]+", value.strip()):
        if not item.isdigit() or int(item) <= 0:
            raise ValueError("ADMIN_USER_IDS must contain positive numeric IDs.")
        ids.add(int(item))
    return ids


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


async def set_commands(application: TelegramApplication) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open the main menu 🏠"),
            BotCommand("quiz", "Start a custom quiz 🧠"),
            BotCommand("fulltest", "Create a test with keys 📄"),
            BotCommand("leaderboard", "View the top learners 🏆"),
            BotCommand("stats", "View your personal progress 📊"),
            BotCommand("unsubscribe", "Disable broadcast announcements 🔕"),
            BotCommand("subscribe", "Enable broadcast announcements 🔔"),
        ]
    )
    await application.bot.set_my_short_description(
        "AI-powered cloze quizzes from your textbook units 🧩"
    )
    await application.bot.set_my_description(
        "Master vocabulary with fresh AI-generated cloze quizzes. Choose a unit, set "
        "your quiz length, get instant feedback, and download complete tests with keys."
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Unhandled Telegram error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            text = (
                "⚠️ Something went wrong while handling that action. Please try again."
            )
            if update.callback_query:
                await QuizBot.safe_edit(update.callback_query, text)
            else:
                await context.bot.send_message(
                    update.effective_chat.id,
                    text,
                    reply_markup=QuizBot.MAIN_KEYBOARD,
                )
        except TelegramError:
            LOGGER.exception("Could not send error feedback to the user")


def build_application(
    telegram_token: str, bot: QuizBot, persistence_path: Path
) -> TelegramApplication:
    persistence = PicklePersistence(filepath=persistence_path)
    application = (
        Application.builder()
        .token(telegram_token)
        .persistence(persistence)
        .post_init(set_commands)
        .post_shutdown(bot.shutdown)
        .build()
    )
    application.add_handler(TypeHandler(Update, bot.track_user), group=-1)
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("menu", bot.start))
    application.add_handler(CommandHandler("quiz", bot.quiz))
    application.add_handler(CommandHandler("fulltest", bot.fulltest))
    application.add_handler(CommandHandler("leaderboard", bot.show_leaderboard))
    application.add_handler(CommandHandler("stats", bot.show_stats))
    application.add_handler(CommandHandler("dashboard", bot.show_admin_dashboard))
    application.add_handler(CommandHandler("broadcast", bot.start_broadcast))
    application.add_handler(CommandHandler("cancel", bot.cancel_broadcast))
    application.add_handler(CommandHandler("subscribe", bot.subscribe))
    application.add_handler(CommandHandler("unsubscribe", bot.unsubscribe))
    application.add_handler(CallbackQueryHandler(bot.callback))
    application.add_handler(MessageHandler(~filters.COMMAND, bot.handle_message))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    base = Path(__file__).resolve().parent
    load_env(base / ".env")
    telegram_token = os.environ.get("BOT_TOKEN", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if (
        not telegram_token
        or "replace_with" in telegram_token
        or not openrouter_key
        or "replace_with" in openrouter_key
    ):
        raise SystemExit("BOT_TOKEN and OPENROUTER_API_KEY are required in .env")

    try:
        default_count = int(os.environ.get("QUESTION_COUNT", str(DEFAULT_QUESTIONS)))
    except ValueError as error:
        raise SystemExit("QUESTION_COUNT must be a number") from error
    if not 1 <= default_count <= MAX_QUESTIONS:
        raise SystemExit(f"QUESTION_COUNT must be between 1 and {MAX_QUESTIONS}")

    catalog = UnitCatalog.load(Path(os.environ.get("UNITS_DIR", base / "units")))
    model = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    site_url = os.environ.get("OPENROUTER_SITE_URL", "").strip()
    usage_store = HighlightUsageStore(
        Path(os.environ.get("HIGHLIGHTED_USAGE_DB", base / "highlighted_usage.sqlite3"))
    )
    stats_store = StatsStore(
        Path(os.environ.get("QUIZ_STATS_DB", base / "quiz_stats.sqlite3"))
    )
    try:
        admin_user_ids = parse_admin_user_ids(os.environ.get("ADMIN_USER_IDS", ""))
    except ValueError as error:
        raise SystemExit(str(error)) from error
    bot = QuizBot(
        catalog,
        OpenRouterGenerator(openrouter_key, model, usage_store, site_url),
        default_count,
        stats_store,
        admin_user_ids,
    )

    application = build_application(telegram_token, bot, base / "bot_state.pkl")
    application.run_polling(allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
