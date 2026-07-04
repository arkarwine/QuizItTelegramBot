import asyncio
import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from telegram import Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import CallbackQueryHandler, ContextTypes, TypeHandler

from quiz_bot import (
    MAX_QUESTIONS,
    ALLOWED_UPDATES,
    BotError,
    HighlightUsageStore,
    OpenRouterGenerator,
    QuizBot,
    Question,
    Session,
    Source,
    StatsStore,
    UnitCatalog,
    buffered_question_count,
    build_question,
    build_application,
    extract_highlighted_words,
    format_full_test,
    normalize,
    parse_admin_user_ids,
    parse_quiz_args,
)


BASE = Path(__file__).resolve().parent


class StubGenerator:
    async def generate(self, source: Source, count: int) -> list[Question]:
        return [Question("It is v________.", "valid") for _ in range(count)]

    async def aclose(self) -> None:
        return None


class Tests(unittest.TestCase):
    def test_builds_exact_inline_cloze_format(self) -> None:
        question = build_question(
            "The photographer saved the images on an", "external", "hard drive."
        )
        self.assertEqual(
            "The photographer saved the images on an e________ hard drive.",
            question.prompt,
        )

    def test_model_added_outside_blank_is_removed(self) -> None:
        question = build_question(
            "The photographer saved the images on an e ________",
            "external",
            "________ hard drive.",
        )
        self.assertEqual(
            "The photographer saved the images on an e________ hard drive.",
            question.prompt,
        )
        self.assertEqual(8, question.prompt.count("_"))

    def test_default_is_all_units_and_ten_questions(self) -> None:
        self.assertEqual(("all", 10), parse_quiz_args([], 10))

    def test_unit_and_count_are_configurable(self) -> None:
        self.assertEqual(("4", 15), parse_quiz_args(["4", "15"], 10))
        self.assertEqual(("all", 20), parse_quiz_args(["all", "20"], 10))

    def test_question_count_is_limited_to_thirty(self) -> None:
        self.assertEqual(("all", MAX_QUESTIONS), parse_quiz_args(["all", "30"], 10))
        with self.assertRaises(ValueError):
            parse_quiz_args(["all", "31"], 10)

    def test_generation_count_always_includes_spares(self) -> None:
        self.assertEqual(2, buffered_question_count(1))
        self.assertEqual(15, buffered_question_count(10))
        self.assertEqual(45, buffered_question_count(30))

    def test_full_test_contains_questions_and_keys(self) -> None:
        text = format_full_test(
            Source("Unit 4", "source"),
            [Question("The drive is e________.", "external")],
        )
        self.assertIn("1. The drive is e________.", text)
        self.assertIn("Keys:", text)
        self.assertIn("1. external", text)

    def test_units_are_loaded(self) -> None:
        catalog = UnitCatalog.load(BASE / "units")
        self.assertEqual(list(range(1, 13)), list(catalog.units))

    def test_prompt_template_formats_source_and_count(self) -> None:
        template = (BASE / "prompt_template.txt").read_text(encoding="utf-8")
        prompt = template.format(
            count=10,
            highlighted_targets='["resilient", "integrity"]',
            highlighted_count=2,
            free_count=8,
            source_text="SOURCE CONTENT",
        )
        self.assertIn("create exactly 10", prompt.lower())
        self.assertIn("40 easy, 40 medium, and 30 hard", prompt)
        self.assertIn('HIGHLIGHTED TARGETS: ["resilient", "integrity"]', prompt)
        self.assertIn("remaining 8 items", prompt)
        self.assertIn("letters only", prompt)
        self.assertIn("exact whole word", prompt)
        self.assertIn("Make every sentence and cloze pattern distinct", prompt)
        self.assertIn("Before returning, verify every item", prompt)
        self.assertIn("SOURCE CONTENT", prompt)

    def test_extracts_only_bold_and_key_vocabulary_words(self) -> None:
        text = """
        ## **UNIT 1 HELEN KELLER**
        Ordinary context includes balance.
        A **resilient** person can **cope with** stress.
        ## KEY VOCABULARY
        empathy, deliberate
        UNIT 2
        ordinary words after the section
        """
        self.assertEqual(
            {"resilient", "cope", "empathy", "deliberate"},
            extract_highlighted_words(text),
        )

    def test_highlighted_usage_rotates_least_used_words_per_scope(self) -> None:
        words = {"alpha", "bravo", "charlie", "delta"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "highlighted.sqlite3"
            store = HighlightUsageStore(path)
            first = store.reserve("unit 1", words, 2)
            second = store.reserve("unit 1", words, 2)
            self.assertEqual(set(), set(first) & set(second))
            self.assertEqual({1}, set(store.counts("unit 1", words).values()))
            self.assertEqual({0}, set(store.counts("unit 2", words).values()))
            store.close()

    def test_unused_highlighted_reservation_is_rolled_back(self) -> None:
        words = {"alpha", "bravo"}
        with TemporaryDirectory() as directory:
            store = HighlightUsageStore(Path(directory) / "highlighted.sqlite3")
            reserved = store.reserve("unit 1", words, 2)
            store.reconcile("unit 1", reserved, {reserved[0]})
            counts = store.counts("unit 1", words)
            self.assertEqual(1, counts[reserved[0]])
            self.assertEqual(0, counts[reserved[1]])
            store.close()

    def test_normalize(self) -> None:
        self.assertEqual("external", normalize(" EXTERNAL! "))

    def test_polling_subscribes_to_inline_button_updates(self) -> None:
        self.assertIn("message", ALLOWED_UPDATES)
        self.assertIn("callback_query", ALLOWED_UPDATES)

    def test_openrouter_network_path_is_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(OpenRouterGenerator.generate))
        self.assertTrue(inspect.iscoroutinefunction(OpenRouterGenerator._complete))

    def test_response_schema_avoids_unsupported_unique_items(self) -> None:
        source = (BASE / "quiz_bot.py").read_text(encoding="utf-8")
        self.assertNotIn('"uniqueItems"', source)

    def test_main_menu_exposes_native_button_flows(self) -> None:
        labels = [
            button.text for row in QuizBot.MAIN_KEYBOARD.keyboard for button in row
        ]
        self.assertIn("⚡ Quick Quiz", labels)
        self.assertIn("🧠 Custom Quiz", labels)
        self.assertIn("📄 Full Test + Keys", labels)
        self.assertIn("🏆 Leaderboard", labels)
        self.assertIn("📊 My Stats", labels)
        self.assertIn("⚙️ Settings", labels)

    def test_stats_store_persists_profiles_and_orders_leaderboard(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "stats.sqlite3"
            store = StatsStore(path)
            store.record_quiz(1, "Accurate", 8, 8, 100)
            store.record_quiz(2, "Steady", 10, 8, 80)
            store.record_quiz(3, "Leader", 12, 9, 75)
            store.record_quiz(1, "Accurate", 10, 5, 50)

            leaders = store.leaderboard()
            self.assertEqual([1, 3, 2], [player.user_id for player in leaders])
            profile = store.profile(1)
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(2, profile.quizzes_completed)
            self.assertEqual(13, profile.correct_answers)
            self.assertEqual(100, profile.best_score)
            self.assertEqual(130, profile.points)
            self.assertEqual(1, store.rank(1))
            summary = store.admin_summary()
            self.assertEqual(3, summary.known_users)
            self.assertEqual(3, summary.subscribed_users)
            self.assertEqual(3, summary.total_players)
            self.assertEqual(4, summary.quizzes_completed)
            self.assertEqual(40, summary.questions_answered)
            self.assertEqual(30, summary.correct_answers)
            self.assertEqual(75, summary.accuracy)
            self.assertEqual(0, summary.broadcasts_sent)
            store.close()

            reopened = StatsStore(path)
            self.assertEqual(13, reopened.profile(1).correct_answers)  # type: ignore[union-attr]
            reopened.close()

    def test_admin_user_ids_accept_commas_and_spaces(self) -> None:
        self.assertEqual({123, 456, 789}, parse_admin_user_ids("123, 456 789"))
        self.assertEqual(set(), parse_admin_user_ids(""))
        with self.assertRaises(ValueError):
            parse_admin_user_ids("123, invalid")

    def test_unit_picker_contains_all_units_and_individual_units(self) -> None:
        bot = QuizBot(UnitCatalog({1: "one", 2: "two"}), StubGenerator())
        callbacks = [
            button.callback_data
            for row in bot.unit_keyboard("quiz").inline_keyboard
            for button in row
        ]
        self.assertIn("unit:quiz:all", callbacks)
        self.assertIn("unit:quiz:1", callbacks)

    def test_count_picker_reaches_maximum(self) -> None:
        callbacks = [
            button.callback_data
            for row in QuizBot.count_keyboard("quiz", "all").inline_keyboard
            for button in row
        ]
        self.assertIn("quizrun:all:30", callbacks)

    def test_full_test_has_dedicated_unit_and_count_routes(self) -> None:
        bot = QuizBot(UnitCatalog({1: "one", 4: "four"}), StubGenerator())
        unit_callbacks = [
            button.callback_data
            for row in bot.unit_keyboard("full").inline_keyboard
            for button in row
        ]
        count_callbacks = [
            button.callback_data
            for row in bot.count_keyboard("full", "4").inline_keyboard
            for button in row
        ]
        self.assertIn("fullunit:4", unit_callbacks)
        self.assertIn("fullrun:4:10", count_callbacks)

    def test_busy_state_is_transient_not_persisted_user_data(self) -> None:
        bot = QuizBot(UnitCatalog({1: "one"}), StubGenerator())
        self.assertEqual(set(), bot.busy_users)

    def test_settings_count_picker_uses_direct_callbacks(self) -> None:
        callbacks = [
            button.callback_data
            for row in QuizBot.settings_count_keyboard().inline_keyboard
            for button in row
        ]
        self.assertIn("setcount:10", callbacks)
        self.assertIn("setcount:30", callbacks)

    def test_application_registers_callback_query_handler(self) -> None:
        bot = QuizBot(UnitCatalog({1: "one"}), StubGenerator())
        with TemporaryDirectory() as directory:
            application = build_application(
                "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                bot,
                Path(directory) / "state.pkl",
            )
        handlers = [
            handler for group in application.handlers.values() for handler in group
        ]
        self.assertTrue(
            any(isinstance(handler, CallbackQueryHandler) for handler in handlers)
        )
        self.assertTrue(any(isinstance(handler, TypeHandler) for handler in handlers))

    def test_broadcast_registry_honours_subscriptions(self) -> None:
        with TemporaryDirectory() as directory:
            store = StatsStore(Path(directory) / "stats.sqlite3")
            store.register_user(1, "One", "one")
            store.register_user(2, "Two", None)
            self.assertEqual([1, 2], store.broadcast_recipients())
            store.set_subscription(2, False)
            self.assertEqual([1], store.broadcast_recipients())
            store.register_user(2, "Two Updated", "two")
            self.assertEqual([1], store.broadcast_recipients())
            store.set_subscription(2, True)
            self.assertEqual([1, 2], store.broadcast_recipients())
            store.close()

    def test_duplicate_generated_answers_are_skipped(self) -> None:
        items = [
            {"before": "It is", "answer": "valid", "after": "."},
            {"before": "This is also", "answer": "valid", "after": "."},
        ]
        questions = OpenRouterGenerator._parse_questions(items, 2)
        self.assertEqual(1, len(questions))
        self.assertEqual("valid", questions[0].answer)

    def test_duplicate_cloze_templates_are_skipped(self) -> None:
        items = [
            {"before": "The answer is", "answer": "alpha", "after": "."},
            {"before": "The answer is", "answer": "bravo", "after": "."},
        ]
        questions = OpenRouterGenerator._parse_questions(items, 2, {"alpha", "bravo"})
        self.assertEqual(["alpha"], [question.answer for question in questions])

    def test_same_completed_sentence_with_different_blanks_is_skipped(self) -> None:
        items = [
            {"before": "The", "answer": "quick", "after": "fox jumped."},
            {"before": "The quick", "answer": "fox", "after": "jumped."},
        ]
        questions = OpenRouterGenerator._parse_questions(items, 2, {"quick", "fox"})
        self.assertEqual(["quick"], [question.answer for question in questions])

    def test_answer_must_exist_in_selected_source(self) -> None:
        items = [{"before": "It is", "answer": "invented", "after": "."}]
        with self.assertRaises(BotError):
            OpenRouterGenerator._parse_questions(items, 1, {"external", "valid"})

    def test_invalid_items_are_omitted_while_valid_items_are_kept(self) -> None:
        items = [
            {"before": "It is", "answer": "valid", "after": "."},
            {"before": "Missing answer field", "after": "."},
            "not an object",
        ]
        questions = OpenRouterGenerator._parse_questions(items, 3, {"valid"})
        self.assertEqual(["valid"], [question.answer for question in questions])

    def test_unselected_highlighted_answer_is_skipped(self) -> None:
        items = [
            {"before": "She stayed", "answer": "resilient", "after": "."},
            {"before": "He acted with", "answer": "integrity", "after": "."},
            {"before": "The plan was", "answer": "practical", "after": "."},
        ]
        questions = OpenRouterGenerator._parse_questions(
            items,
            3,
            {"resilient", "integrity", "practical"},
            {"resilient", "integrity"},
            {"integrity"},
        )
        self.assertEqual(
            ["integrity", "practical"],
            [question.answer for question in questions],
        )

    def test_blank_appended_outside_complete_sentence_is_skipped(self) -> None:
        items = [
            {
                "before": "Eating food from polluted rivers can expose people to substances.",
                "answer": "contaminated",
                "after": "",
            },
            {
                "before": "People may consume",
                "answer": "contaminated",
                "after": "substances from polluted rivers.",
            },
        ]
        questions = OpenRouterGenerator._parse_questions(items, 2, {"contaminated"})
        self.assertEqual(1, len(questions))
        self.assertEqual(
            "People may consume c________ substances from polluted rivers.",
            questions[0].prompt,
        )

    def test_generation_retries_once_after_invalid_response(self) -> None:
        class FlakyGenerator(OpenRouterGenerator):
            def __init__(self, usage_store: HighlightUsageStore) -> None:
                super().__init__("key", "model", usage_store)
                self.calls = 0

            async def _complete(self, payload: dict[str, Any]) -> object:
                self.calls += 1
                if self.calls == 1:
                    raise BotError("malformed JSON")
                return [{"before": "It is", "answer": "valid", "after": "."}]

        with TemporaryDirectory() as directory:
            generator = FlakyGenerator(
                HighlightUsageStore(Path(directory) / "highlighted.sqlite3")
            )
            try:
                questions = asyncio.run(
                    generator.generate(Source("Unit 1", "valid"), 1)
                )
            finally:
                asyncio.run(generator.aclose())
            self.assertEqual(2, generator.calls)
            self.assertEqual("It is v________.", questions[0].prompt)

    def test_extra_generated_items_replace_invalid_questions(self) -> None:
        answers = [
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
            "foxtrot",
            "golf",
            "hotel",
            "india",
            "juliet",
        ]

        class BufferedGenerator(OpenRouterGenerator):
            generated_count = 0

            async def _complete(self, payload: dict[str, Any]) -> object:
                schema = payload["response_format"]["json_schema"]["schema"]
                self.generated_count = int(
                    schema["properties"]["questions"]["maxItems"]
                )
                valid = [
                    {
                        "before": f"Situation number {index} requires",
                        "answer": answer,
                        "after": ".",
                    }
                    for index, answer in enumerate(answers, start=1)
                ]
                invalid = [
                    {"before": "This is", "answer": "invented", "after": "."}
                    for _ in range(5)
                ]
                return [*valid, *invalid]

        with TemporaryDirectory() as directory:
            generator = BufferedGenerator(
                "key",
                "model",
                HighlightUsageStore(Path(directory) / "highlighted.sqlite3"),
            )
            try:
                questions = asyncio.run(
                    generator.generate(Source("Unit 1", " ".join(answers)), 10)
                )
            finally:
                asyncio.run(generator.aclose())
            self.assertEqual(15, generator.generated_count)
            self.assertEqual(10, len(questions))


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_message_edit_is_ignored(self) -> None:
        query = SimpleNamespace(
            edit_message_text=AsyncMock(
                side_effect=BadRequest("Message is not modified: content is identical")
            )
        )

        await QuizBot.safe_edit(cast(Any, query), "Same content")

        self.assertEqual(1, query.edit_message_text.await_count)

    async def test_other_bad_request_from_message_edit_is_not_hidden(self) -> None:
        query = SimpleNamespace(
            edit_message_text=AsyncMock(
                side_effect=BadRequest("Message cannot be edited")
            )
        )

        with self.assertRaises(BadRequest):
            await QuizBot.safe_edit(cast(Any, query), "New content")

    async def test_finishing_quiz_updates_player_dashboard_stats(self) -> None:
        with TemporaryDirectory() as directory:
            store = StatsStore(Path(directory) / "stats.sqlite3")
            bot = QuizBot(
                UnitCatalog({1: "source"}),
                StubGenerator(),
                stats_store=store,
            )
            session = Session(
                [
                    Question("It is a________.", "alpha"),
                    Question("It is b________.", "bravo"),
                ],
                position=2,
                correct=1,
            )
            message = SimpleNamespace(chat_id=10, reply_text=AsyncMock())
            context = SimpleNamespace(user_data={"session": session}, bot=AsyncMock())

            await bot.finish_quiz(
                cast(Any, message),
                cast(ContextTypes.DEFAULT_TYPE, context),
                session,
                "Finished",
                42,
                "Ada Learner",
            )

            profile = store.profile(42)
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(1, profile.quizzes_completed)
            self.assertEqual(2, profile.questions_answered)
            self.assertEqual(1, profile.correct_answers)
            self.assertEqual(50, profile.best_score)
            store.close()

    async def test_personal_stats_and_leaderboard_render_native_controls(self) -> None:
        with TemporaryDirectory() as directory:
            store = StatsStore(Path(directory) / "stats.sqlite3")
            store.record_quiz(42, "Ada & Bob", 10, 8, 80)
            bot = QuizBot(
                UnitCatalog({1: "source"}),
                StubGenerator(),
                stats_store=store,
            )
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=42),
                effective_message=message,
                callback_query=None,
            )
            context = SimpleNamespace(user_data={}, bot=AsyncMock())

            await bot.show_stats(
                cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
            )
            await bot.show_leaderboard(
                cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
            )

            self.assertEqual(2, message.reply_text.await_count)
            stats_text = message.reply_text.await_args_list[0].args[0]
            leaderboard_text = message.reply_text.await_args_list[1].args[0]
            self.assertIn("My Stats", stats_text)
            self.assertIn("Overall accuracy", stats_text)
            self.assertIn("Ada &amp; Bob", leaderboard_text)
            for call in message.reply_text.await_args_list:
                self.assertIsNotNone(call.kwargs["reply_markup"])
            store.close()

    async def test_admin_dashboard_rejects_users_not_in_allowlist(self) -> None:
        bot = QuizBot(UnitCatalog({1: "source"}), StubGenerator(), admin_user_ids={42})
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            effective_message=message,
            callback_query=None,
        )
        context = SimpleNamespace(user_data={}, bot=AsyncMock())

        await bot.show_admin_dashboard(
            cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
        )

        text = message.reply_text.await_args.args[0]
        self.assertIn("Admin access required", text)

    async def test_admin_dashboard_shows_global_summary_to_admin(self) -> None:
        with TemporaryDirectory() as directory:
            store = StatsStore(Path(directory) / "stats.sqlite3")
            store.record_quiz(42, "Admin", 10, 9, 90)
            bot = QuizBot(
                UnitCatalog({1: "source"}),
                StubGenerator(),
                stats_store=store,
                admin_user_ids={42},
            )
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=42),
                effective_message=message,
                callback_query=None,
            )
            context = SimpleNamespace(user_data={}, bot=AsyncMock())

            await bot.show_admin_dashboard(
                cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
            )

            text = message.reply_text.await_args.args[0]
            self.assertIn("Admin Dashboard", text)
            self.assertIn("Known users: <b>1</b>", text)
            self.assertIn("Ranked players: <b>1</b>", text)
            self.assertIn("Global accuracy: <b>90%</b>", text)
            store.close()

    async def test_admin_can_preview_a_rich_broadcast_message(self) -> None:
        bot = QuizBot(UnitCatalog({1: "source"}), StubGenerator(), admin_user_ids={42})
        prompt_message = SimpleNamespace(reply_text=AsyncMock())
        start_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_message=prompt_message,
            callback_query=None,
        )
        context = SimpleNamespace(user_data={}, bot=AsyncMock())
        await bot.start_broadcast(
            cast(Update, start_update), cast(ContextTypes.DEFAULT_TYPE, context)
        )

        rich_message = SimpleNamespace(
            chat_id=42,
            message_id=900,
            text=None,
            reply_text=AsyncMock(),
        )
        rich_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_message=rich_message,
        )
        captured = await bot.capture_broadcast_message(
            cast(Update, rich_update), cast(ContextTypes.DEFAULT_TYPE, context)
        )

        self.assertTrue(captured)
        self.assertEqual(
            {"chat_id": 42, "message_id": 900}, context.user_data["broadcast_draft"]
        )
        self.assertEqual(1, context.bot.copy_message.await_count)
        self.assertEqual(1, rich_message.reply_text.await_count)

    async def test_broadcast_tracks_success_and_deactivates_blocked_users(self) -> None:
        with TemporaryDirectory() as directory:
            store = StatsStore(Path(directory) / "stats.sqlite3")
            store.register_user(1, "Reachable", None)
            store.register_user(2, "Blocked", None)
            bot = QuizBot(
                UnitCatalog({1: "source"}),
                StubGenerator(),
                stats_store=store,
                admin_user_ids={42},
            )

            async def copy_message(**kwargs: Any) -> object:
                if kwargs["chat_id"] == 2:
                    raise Forbidden("bot was blocked")
                return SimpleNamespace(message_id=100)

            query = SimpleNamespace(edit_message_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=42),
                effective_message=SimpleNamespace(reply_text=AsyncMock()),
                callback_query=query,
            )
            background_tasks: list[Any] = []

            def create_task(coroutine: Any, **kwargs: Any) -> None:
                del kwargs
                background_tasks.append(coroutine)

            context = SimpleNamespace(
                user_data={
                    "broadcast_state": "preview",
                    "broadcast_draft": {"chat_id": 42, "message_id": 900},
                },
                bot=SimpleNamespace(copy_message=AsyncMock(side_effect=copy_message)),
                application=SimpleNamespace(create_task=create_task),
            )

            await bot.send_broadcast(
                cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
            )
            self.assertEqual(1, len(background_tasks))
            await background_tasks[0]

            self.assertEqual([1], store.broadcast_recipients())
            summary = store.admin_summary()
            self.assertEqual(1, summary.broadcasts_sent)
            final_text = query.edit_message_text.await_args_list[-1].args[0]
            self.assertIn("Delivered: <b>1</b>", final_text)
            self.assertIn("Failed: <b>1</b>", final_text)
            self.assertNotIn("broadcast_draft", context.user_data)
            store.close()

    async def test_same_question_position_is_sent_only_once(self) -> None:
        bot = QuizBot(UnitCatalog({1: "source"}), StubGenerator())
        context = SimpleNamespace(
            user_data={"session": Session([Question("It is v________.", "valid")])},
            bot=AsyncMock(),
        )

        await bot.send_question(10, cast(ContextTypes.DEFAULT_TYPE, context))
        await bot.send_question(10, cast(ContextTypes.DEFAULT_TYPE, context))

        self.assertEqual(1, context.bot.send_message.await_count)

    async def test_feedback_is_combined_with_next_question(self) -> None:
        bot = QuizBot(UnitCatalog({1: "source"}), StubGenerator())
        session = Session(
            [
                Question("It is a________.", "alpha"),
                Question("It is b________.", "bravo"),
            ]
        )
        message = SimpleNamespace(text="alpha", chat_id=10, reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42, full_name="Learner"),
        )
        context = SimpleNamespace(user_data={"session": session}, bot=AsyncMock())

        await bot.answer(cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context))

        self.assertEqual(0, message.reply_text.await_count)
        self.assertEqual(1, context.bot.send_message.await_count)
        sent_text = context.bot.send_message.await_args.args[1]
        self.assertIn("Correct", sent_text)
        self.assertIn("Question 2 of 2", sent_text)

    async def test_legacy_full_test_callback_still_runs(self) -> None:
        class Generator:
            async def generate(self, source: Source, count: int) -> list[Question]:
                return [Question("It is v________.", "valid") for _ in range(count)]

            async def aclose(self) -> None:
                return None

        bot = QuizBot(UnitCatalog({4: "source"}), Generator())
        query = SimpleNamespace(
            data="run:full:4:5",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=10),
            effective_user=SimpleNamespace(id=20),
        )
        context = SimpleNamespace(user_data={}, bot=AsyncMock())

        await bot.callback(
            cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
        )

        self.assertEqual(1, context.bot.send_document.await_count)

    async def test_unknown_callback_returns_user_to_menu(self) -> None:
        bot = QuizBot(UnitCatalog({1: "source"}), StubGenerator())
        query = SimpleNamespace(
            data="obsolete:action",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=10),
            effective_user=SimpleNamespace(id=20),
        )
        context = SimpleNamespace(user_data={}, bot=AsyncMock())

        await bot.callback(
            cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
        )

        self.assertEqual(0, context.bot.send_message.await_count)
        self.assertEqual(1, query.edit_message_text.await_count)


if __name__ == "__main__":
    unittest.main()
