import asyncio
import copy
import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from telegram import Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import CallbackQueryHandler, ContextTypes, TypeHandler

from quiz_bot import (
    MAX_QUESTIONS,
    MAX_CONCURRENT_UPDATES,
    ALLOWED_UPDATES,
    BotError,
    HighlightUsageStore,
    HybridCachedGenerator,
    OpenRouterGenerator,
    QuizBot,
    Question,
    QuestionCacheStore,
    Session,
    Source,
    StatsStore,
    UnitCatalog,
    buffered_question_count,
    build_question,
    build_application,
    difficulty_counts,
    extract_highlighted_words,
    format_full_test,
    normalize,
    parse_admin_user_ids,
    parse_quiz_args,
    select_difficulty_questions,
)


BASE = Path(__file__).resolve().parent


def persistence_app(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(
        update_persistence=AsyncMock(),
        mark_data_for_update_persistence=Mock(),
        **values,
    )


class StubGenerator:
    async def generate(self, source: Source, count: int) -> list[Question]:
        return [Question("It is v________.", "valid") for _ in range(count)]

    async def aclose(self) -> None:
        return None


class Tests(unittest.TestCase):
    def test_legacy_persisted_objects_survive_deepcopy(self) -> None:
        question = Question("It is v________.", "valid")
        object.__delattr__(question, "difficulty")
        copied_question = copy.deepcopy(question)
        self.assertEqual("medium", copied_question.difficulty)

        session = Session([question])
        del session.source
        copied_session = copy.deepcopy(session)
        self.assertIsNone(copied_session.source)
        self.assertEqual("medium", copied_session.questions[0].difficulty)

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
            easy_count=4,
            medium_count=3,
            hard_count=3,
            source_text="SOURCE CONTENT",
        )
        self.assertIn("create exactly 10", prompt.lower())
        self.assertIn("4 easy, 3 medium, and 3 hard", prompt)
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
        async def scenario() -> None:
            words = {"alpha", "bravo", "charlie", "delta"}
            with TemporaryDirectory() as directory:
                path = Path(directory) / "highlighted.sqlite3"
                store = HighlightUsageStore(path)
                first = await store.reserve("unit 1", words, 2)
                second = await store.reserve("unit 1", words, 2)
                self.assertEqual(set(), set(first) & set(second))
                self.assertEqual(
                    {1}, set((await store.counts("unit 1", words)).values())
                )
                self.assertEqual(
                    {0}, set((await store.counts("unit 2", words)).values())
                )
                await store.close()

        asyncio.run(scenario())

    def test_unused_highlighted_reservation_is_rolled_back(self) -> None:
        async def scenario() -> None:
            words = {"alpha", "bravo"}
            with TemporaryDirectory() as directory:
                store = HighlightUsageStore(Path(directory) / "highlighted.sqlite3")
                reserved = await store.reserve("unit 1", words, 2)
                await store.reconcile("unit 1", reserved, {reserved[0]})
                counts = await store.counts("unit 1", words)
                self.assertEqual(1, counts[reserved[0]])
                self.assertEqual(0, counts[reserved[1]])
                await store.close()

        asyncio.run(scenario())

    def test_normalize(self) -> None:
        self.assertEqual("external", normalize(" EXTERNAL! "))

    def test_polling_subscribes_to_inline_button_updates(self) -> None:
        self.assertIn("message", ALLOWED_UPDATES)
        self.assertIn("callback_query", ALLOWED_UPDATES)

    def test_openrouter_network_path_is_async(self) -> None:
        self.assertTrue(inspect.iscoroutinefunction(OpenRouterGenerator.generate))
        self.assertTrue(inspect.iscoroutinefunction(OpenRouterGenerator._complete))
        self.assertTrue(inspect.iscoroutinefunction(HighlightUsageStore.reserve))
        self.assertTrue(inspect.iscoroutinefunction(HighlightUsageStore.reconcile))
        self.assertTrue(inspect.iscoroutinefunction(StatsStore.record_quiz))
        self.assertTrue(inspect.iscoroutinefunction(StatsStore.leaderboard))
        self.assertTrue(inspect.iscoroutinefunction(StatsStore.broadcast_recipients))

    def test_cache_serves_new_users_and_refills_after_exhaustion(self) -> None:
        class CountingGenerator:
            def __init__(self) -> None:
                self.calls = 0

            async def generate(self, source: Source, count: int) -> list[Question]:
                del source
                self.calls += 1
                words = [
                    "alpha",
                    "bravo",
                    "charlie",
                    "delta",
                    "echo",
                    "foxtrot",
                    "golf",
                    "hotel",
                ]
                start = (self.calls - 1) * count
                questions: list[Question] = []
                index = start
                for difficulty, amount in difficulty_counts(count).items():
                    for _ in range(amount):
                        answer = words[index]
                        questions.append(
                            Question(
                                f"Context {index} uses {answer[0]}________.",
                                answer,
                                difficulty,
                            )
                        )
                        index += 1
                return questions

            async def aclose(self) -> None:
                return None

        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                upstream = CountingGenerator()
                generator = HybridCachedGenerator(
                    upstream,
                    QuestionCacheStore(Path(directory) / "questions.sqlite3"),
                )
                source = Source("Unit 1", "alpha bravo charlie delta")
                first = await generator.generate_for_user(source, 2, 101)
                self.assertEqual(
                    [],
                    await generator.cache_store.unseen_candidates(source, 202, 10),
                )
                for question in first:
                    await generator.record_answered(source, 101, question)
                self.assertEqual(
                    2,
                    len(await generator.cache_store.unseen_candidates(source, 202, 10)),
                )
                (
                    total,
                    answer_records,
                    by_source,
                    unseen,
                ) = await generator.cache_store.stats([source], 202)
                self.assertEqual(2, total)
                self.assertEqual(2, answer_records)
                self.assertEqual(2, sum(by_source["Unit 1"].values()))
                self.assertEqual(2, sum(unseen["Unit 1"].values()))
                second_user = await generator.generate_for_user(source, 2, 202)
                repeat_user = await generator.generate_for_user(source, 2, 101)

                self.assertEqual(2, upstream.calls)
                self.assertEqual(
                    {question.prompt for question in first},
                    {question.prompt for question in second_user},
                )
                self.assertTrue(
                    {question.prompt for question in first}.isdisjoint(
                        question.prompt for question in repeat_user
                    )
                )
                await generator.aclose()

        asyncio.run(scenario())

    def test_difficulty_allocation_is_forty_thirty_thirty(self) -> None:
        self.assertEqual({"easy": 4, "medium": 3, "hard": 3}, difficulty_counts(10))
        self.assertEqual(30, sum(difficulty_counts(30).values()))
        candidates = [
            Question(
                f"Context {index} uses w________.",
                f"word{index}",
                difficulty,
            )
            for index, difficulty in enumerate(
                ["easy"] * 8 + ["medium"] * 8 + ["hard"] * 8
            )
        ]
        selected = select_difficulty_questions(candidates, 10)
        self.assertEqual(
            {"easy": 4, "medium": 3, "hard": 3},
            {
                difficulty: sum(
                    question.difficulty == difficulty for question in selected
                )
                for difficulty in ("easy", "medium", "hard")
            },
        )

    def test_partial_cache_is_preferred_over_fresh_questions(self) -> None:
        class FreshGenerator:
            async def generate(self, source: Source, count: int) -> list[Question]:
                del source, count
                return [
                    Question("A fresh context needs f________.", "fresh", "easy"),
                    Question(
                        "A moderate context needs m________.", "moderate", "medium"
                    ),
                ]

            async def aclose(self) -> None:
                return None

        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                store = QuestionCacheStore(Path(directory) / "questions.sqlite3")
                generator = HybridCachedGenerator(FreshGenerator(), store)
                source = Source("Unit 1", "cached fresh moderate")
                cached = Question("A cached context needs c________.", "cached", "easy")
                await store.store(source, [cached])

                selected = await generator.generate_for_user(source, 2, 202)

                self.assertIn(cached, selected)
                self.assertEqual(
                    {"easy", "medium"},
                    {question.difficulty for question in selected},
                )
                await generator.aclose()

        asyncio.run(scenario())

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
        self.assertIn("🛑 Stop", labels)
        self.assertNotIn("⚙️ Settings", labels)

    def test_stats_store_persists_profiles_and_orders_leaderboard(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                path = Path(directory) / "stats.sqlite3"
                store = StatsStore(path)
                await store.record_quiz(1, "Accurate", 8, 8, 100)
                await store.record_quiz(2, "Steady", 10, 8, 80)
                await store.record_quiz(3, "Leader", 12, 9, 75)
                await store.record_quiz(1, "Accurate", 10, 5, 50)

                leaders = await store.leaderboard()
                self.assertEqual([1, 3, 2], [player.user_id for player in leaders])
                profile = await store.profile(1)
                self.assertIsNotNone(profile)
                assert profile is not None
                self.assertEqual(2, profile.quizzes_completed)
                self.assertEqual(13, profile.correct_answers)
                self.assertEqual(100, profile.best_score)
                self.assertEqual(130, profile.points)
                self.assertEqual(1, await store.rank(1))
                summary = await store.admin_summary()
                self.assertEqual(3, summary.known_users)
                self.assertEqual(3, summary.subscribed_users)
                self.assertEqual(3, summary.total_players)
                self.assertEqual(4, summary.quizzes_completed)
                self.assertEqual(40, summary.questions_answered)
                self.assertEqual(30, summary.correct_answers)
                self.assertEqual(75, summary.accuracy)
                self.assertEqual(0, summary.broadcasts_sent)
                await store.close()

                reopened = StatsStore(path)
                reopened_profile = await reopened.profile(1)
                self.assertIsNotNone(reopened_profile)
                assert reopened_profile is not None
                self.assertEqual(13, reopened_profile.correct_answers)
                await reopened.close()

        asyncio.run(scenario())

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

    def test_quick_quiz_selects_one_random_unit(self) -> None:
        bot = QuizBot(UnitCatalog({1: "one", 4: "four"}), StubGenerator())
        with patch("quiz_bot.random.choice", return_value=4) as choose:
            self.assertEqual("4", bot.random_unit())
        choose.assert_called_once_with((1, 4))

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
        self.assertEqual(
            MAX_CONCURRENT_UPDATES,
            application.update_processor.max_concurrent_updates,
        )

    def test_broadcast_registry_honours_subscriptions(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                store = StatsStore(Path(directory) / "stats.sqlite3")
                await store.register_user(1, "One", "one")
                await store.register_user(2, "Two", None)
                self.assertEqual([1, 2], await store.broadcast_recipients())
                await store.set_subscription(2, False)
                self.assertEqual([1], await store.broadcast_recipients())
                await store.register_user(2, "Two Updated", "two")
                self.assertEqual([1], await store.broadcast_recipients())
                await store.set_subscription(2, True)
                self.assertEqual([1, 2], await store.broadcast_recipients())
                await store.close()

        asyncio.run(scenario())

    def test_interrupted_broadcast_retains_only_pending_recipients(self) -> None:
        async def scenario() -> None:
            with TemporaryDirectory() as directory:
                path = Path(directory) / "stats.sqlite3"
                store = StatsStore(path)
                broadcast_id = await store.create_broadcast(42, 42, 900, [1, 2])
                await store.mark_broadcast_delivery(broadcast_id, 1, "delivered")
                await store.close()

                reopened = StatsStore(path)
                self.assertEqual(
                    [(broadcast_id, 42, 900)],
                    await reopened.pending_broadcasts(),
                )
                self.assertEqual(
                    [2],
                    await reopened.pending_broadcast_recipients(broadcast_id),
                )
                await reopened.mark_broadcast_delivery(broadcast_id, 2, "failed")
                await reopened.finish_broadcast(broadcast_id)
                self.assertEqual(
                    (2, 1, 1), await reopened.broadcast_counts(broadcast_id)
                )
                await reopened.close()

        asyncio.run(scenario())

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
                return [
                    {
                        "before": "It is",
                        "answer": "valid",
                        "after": ".",
                        "difficulty": "easy",
                    }
                ]

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
                        "difficulty": (
                            "easy" if index <= 4 else "medium" if index <= 7 else "hard"
                        ),
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
    async def test_full_test_caches_every_generated_question(self) -> None:
        class FullTestGenerator(StubGenerator):
            def __init__(self) -> None:
                self.cached: list[Question] = []

            async def generate(self, source: Source, count: int) -> list[Question]:
                del source
                return [
                    Question(
                        f"Full test context {index} uses w________.",
                        f"word{index}",
                        "easy" if index == 0 else "medium",
                    )
                    for index in range(count)
                ]

            async def cache_questions(
                self, source: Source, questions: list[Question]
            ) -> None:
                del source
                self.cached.extend(questions)

        generator = FullTestGenerator()
        bot = QuizBot(UnitCatalog({1: "source"}), generator)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42),
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
            callback_query=None,
        )
        context = SimpleNamespace(
            user_data={},
            bot=AsyncMock(),
            application=persistence_app(),
        )

        await bot.generate_fulltest(
            cast(Update, update),
            cast(ContextTypes.DEFAULT_TYPE, context),
            "1",
            2,
        )

        self.assertEqual(2, len(generator.cached))
        self.assertEqual(1, context.bot.send_document.await_count)

    async def test_only_submitted_answers_are_recorded_in_cache(self) -> None:
        class RecordingGenerator(StubGenerator):
            def __init__(self) -> None:
                self.recorded: list[Question] = []

            async def record_answered(
                self, source: Source, user_id: int, question: Question
            ) -> None:
                del source, user_id
                self.recorded.append(question)

        generator = RecordingGenerator()
        bot = QuizBot(UnitCatalog({1: "alpha bravo"}), generator)
        source = Source("Unit 1", "alpha bravo")
        questions = [
            Question("It is a________.", "alpha", "easy"),
            Question("It is b________.", "bravo", "medium"),
        ]
        context = SimpleNamespace(
            user_data={"session": Session(questions, source=source)},
            bot=AsyncMock(),
            application=persistence_app(),
        )
        answer_message = SimpleNamespace(
            text="alpha", chat_id=42, reply_text=AsyncMock()
        )
        answer_update = SimpleNamespace(
            effective_message=answer_message,
            effective_user=SimpleNamespace(id=42, full_name="Learner"),
        )

        await bot.answer(
            cast(Update, answer_update), cast(ContextTypes.DEFAULT_TYPE, context)
        )
        self.assertEqual([questions[0]], generator.recorded)

        skip_message = SimpleNamespace(
            text=bot.SKIP, chat_id=42, reply_text=AsyncMock()
        )
        skip_update = SimpleNamespace(
            effective_message=skip_message,
            effective_user=SimpleNamespace(id=42, full_name="Learner"),
        )
        await bot.answer(
            cast(Update, skip_update), cast(ContextTypes.DEFAULT_TYPE, context)
        )
        self.assertEqual([questions[0]], generator.recorded)

    async def test_stop_cancels_in_flight_generation_and_releases_user(self) -> None:
        class WaitingGenerator:
            def __init__(self) -> None:
                self.started = asyncio.Event()

            async def generate(self, source: Source, count: int) -> list[Question]:
                del source, count
                self.started.set()
                await asyncio.Event().wait()
                return []

            async def aclose(self) -> None:
                return None

        generator = WaitingGenerator()
        bot = QuizBot(UnitCatalog({1: "source"}), generator)
        generation_message = SimpleNamespace(reply_text=AsyncMock())
        generation_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42),
            effective_message=generation_message,
            callback_query=None,
        )
        application = persistence_app()
        context = SimpleNamespace(
            user_data={},
            bot=AsyncMock(),
            application=application,
        )
        task = asyncio.create_task(
            bot.generate_quiz(
                cast(Update, generation_update),
                cast(ContextTypes.DEFAULT_TYPE, context),
                "1",
                10,
            )
        )
        await generator.started.wait()

        stop_message = SimpleNamespace(text="stop", reply_text=AsyncMock())
        stop_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42),
            effective_message=stop_message,
            callback_query=None,
        )
        await bot.handle_message(
            cast(Update, stop_update), cast(ContextTypes.DEFAULT_TYPE, context)
        )

        self.assertTrue(task.cancelled())
        self.assertNotIn(42, bot.busy_users)
        self.assertNotIn(42, bot.generation_tasks)
        self.assertNotIn("pending_generation", context.user_data)
        self.assertEqual(1, stop_message.reply_text.await_count)
        self.assertIn("Stopped", stop_message.reply_text.await_args.args[0])

    async def test_quick_quiz_uses_ten_questions_from_one_random_unit(self) -> None:
        bot = QuizBot(UnitCatalog({1: "one", 4: "four"}), StubGenerator())
        message = SimpleNamespace(text=bot.QUICK)
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(user_data={})

        with (
            patch.object(bot, "random_unit", return_value="4"),
            patch.object(bot, "generate_quiz", new_callable=AsyncMock) as generate,
        ):
            await bot.menu_text(
                cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
            )

        generate.assert_awaited_once_with(update, context, "4", 10)

    async def test_pending_quiz_generation_resumes_after_restart(self) -> None:
        bot = QuizBot(UnitCatalog({4: "source"}), StubGenerator())
        user_data: dict[int, dict[str, object]] = {
            42: {
                "pending_generation": {
                    "flow": "quiz",
                    "unit": "4",
                    "count": 2,
                    "chat_id": 42,
                }
            }
        }
        application = SimpleNamespace(
            user_data=user_data,
            bot=AsyncMock(),
            mark_data_for_update_persistence=Mock(),
            update_persistence=AsyncMock(),
        )

        await bot.resume_generation(
            cast(Any, application), 42, cast(Any, user_data[42]["pending_generation"])
        )

        self.assertNotIn("pending_generation", user_data[42])
        self.assertIsInstance(user_data[42].get("session"), Session)
        self.assertEqual(42, user_data[42].get("session_chat_id"))
        self.assertGreaterEqual(application.bot.send_message.await_count, 2)
        application.mark_data_for_update_persistence.assert_called()

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
            context = SimpleNamespace(
                user_data={"session": session},
                bot=AsyncMock(),
                application=persistence_app(),
            )

            await bot.finish_quiz(
                cast(Any, message),
                cast(ContextTypes.DEFAULT_TYPE, context),
                session,
                "Finished",
                42,
                "Ada Learner",
            )

            profile = await store.profile(42)
            self.assertIsNotNone(profile)
            assert profile is not None
            self.assertEqual(1, profile.quizzes_completed)
            self.assertEqual(2, profile.questions_answered)
            self.assertEqual(1, profile.correct_answers)
            self.assertEqual(50, profile.best_score)
            await store.close()

    async def test_personal_stats_and_leaderboard_render_native_controls(self) -> None:
        with TemporaryDirectory() as directory:
            store = StatsStore(Path(directory) / "stats.sqlite3")
            await store.record_quiz(42, "Ada & Bob", 10, 8, 80)
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
            await store.close()

    async def test_admin_dashboard_rejects_users_not_in_allowlist(self) -> None:
        bot = QuizBot(UnitCatalog({1: "source"}), StubGenerator(), admin_user_ids={42})
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            effective_message=message,
            callback_query=None,
        )
        context = SimpleNamespace(
            user_data={},
            bot=AsyncMock(),
            application=persistence_app(),
        )

        await bot.show_admin_dashboard(
            cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
        )

        text = message.reply_text.await_args.args[0]
        self.assertIn("Admin access required", text)

    async def test_admin_dashboard_shows_global_summary_to_admin(self) -> None:
        with TemporaryDirectory() as directory:
            store = StatsStore(Path(directory) / "stats.sqlite3")
            await store.record_quiz(42, "Admin", 10, 9, 90)
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
            await store.close()

    async def test_admin_can_preview_a_rich_broadcast_message(self) -> None:
        bot = QuizBot(UnitCatalog({1: "source"}), StubGenerator(), admin_user_ids={42})
        prompt_message = SimpleNamespace(reply_text=AsyncMock())
        start_update = SimpleNamespace(
            effective_user=SimpleNamespace(id=42),
            effective_message=prompt_message,
            callback_query=None,
        )
        context = SimpleNamespace(
            user_data={},
            bot=AsyncMock(),
            application=persistence_app(),
        )
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
            await store.register_user(1, "Reachable", None)
            await store.register_user(2, "Blocked", None)
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
                application=persistence_app(create_task=create_task),
            )

            await bot.send_broadcast(
                cast(Update, update), cast(ContextTypes.DEFAULT_TYPE, context)
            )
            self.assertEqual(1, len(background_tasks))
            await background_tasks[0]

            self.assertEqual([1], await store.broadcast_recipients())
            summary = await store.admin_summary()
            self.assertEqual(1, summary.broadcasts_sent)
            final_text = query.edit_message_text.await_args_list[-1].args[0]
            self.assertIn("Delivered: <b>1</b>", final_text)
            self.assertIn("Failed: <b>1</b>", final_text)
            self.assertNotIn("broadcast_draft", context.user_data)
            await store.close()

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
        context = SimpleNamespace(
            user_data={"session": session},
            bot=AsyncMock(),
            application=persistence_app(),
        )

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
        context = SimpleNamespace(
            user_data={},
            bot=AsyncMock(),
            application=persistence_app(),
        )

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
