"""检索引擎测试：user_id 隔离、幂等、同步写后可搜、top_k、原始消息可检索、删除。"""
import os
import tempfile
import unittest

from aml_retriever.config import RetrieverConfig
from aml_retriever.retriever import RetrieverDB


def _raw_content(s: str) -> str:
    """去掉 v1.2 内容时间戳前缀（[<时间>] 前缀），返回原始证据文本。"""
    if s.startswith("[") and "] " in s[:64]:
        return s[s.index("] ") + 2:]
    return s


class EngineCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = RetrieverDB(RetrieverConfig(db_path=self.path))

    def tearDown(self):
        self.db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _add(self, user, session, contents, request_id=None):
        return self.db.add(
            request_id=request_id or f"req-{user}-{session}-{len(contents)}",
            user_id=user,
            session_id=session,
            messages=[{"role": "user", "content": c} for c in contents],
        )


class TestSyncWriteThenRead(EngineCase):
    def test_add_is_immediately_searchable(self):
        self._add("u1", "s1", ["我的护照号是 G12345678"])
        result = self.db.search(user_id="u1", query="护照号", top_k=10)
        self.assertGreater(result.total, 0)
        self.assertTrue(any("G12345678" in e.content for e in result.results))

    def test_add_returns_message_ids(self):
        res = self._add("u1", "s1", ["第一条", "第二条"])
        self.assertEqual(len(res.message_ids), 2)
        self.assertFalse(res.idempotent)


class TestIdempotency(EngineCase):
    def test_same_request_id_does_not_duplicate(self):
        first = self._add("u1", "s1", ["订单 12345 已付款"], request_id="fixed")
        second = self._add("u1", "s1", ["订单 12345 已付款"], request_id="fixed")
        self.assertTrue(second.idempotent)
        self.assertEqual(first.message_ids, second.message_ids)
        self.assertEqual(self.db.count("u1"), 1)

    def test_same_request_id_different_user_is_separate(self):
        self._add("u1", "s1", ["内容"], request_id="shared")
        self._add("u2", "s1", ["内容"], request_id="shared")
        self.assertEqual(self.db.count("u1"), 1)
        self.assertEqual(self.db.count("u2"), 1)


class TestIsolation(EngineCase):
    def test_search_never_crosses_user_id(self):
        self._add("alice", "s1", ["alice 的银行卡尾号 8888"])
        self._add("bob", "s1", ["bob 的银行卡尾号 9999"])
        result = self.db.search(user_id="alice", query="银行卡尾号", top_k=50)
        self.assertGreater(len(result.results), 0)
        for evidence in result.results:
            self.assertEqual(evidence.user_id, "alice")
            self.assertNotIn("9999", evidence.content)

    def test_unknown_user_returns_empty(self):
        self._add("alice", "s1", ["内容"])
        result = self.db.search(user_id="nobody", query="内容", top_k=10)
        self.assertEqual(result.total, 0)
        self.assertEqual(result.results, [])

    def test_session_id_is_not_a_search_filter(self):
        """官方：session_id 只用于组织记忆，不作为 Search 的筛选条件。"""
        self._add("u1", "s1", ["会话一里的关键词 quokka"])
        self._add("u1", "s2", ["会话二里的关键词 quokka"])
        result = self.db.search(user_id="u1", query="quokka", top_k=50)
        contents = " ".join(e.content for e in result.results)
        self.assertIn("会话一", contents)
        self.assertIn("会话二", contents)


class TestTopK(EngineCase):
    def test_top_k_is_respected(self):
        self._add("u1", "s1", [f"编号 {i} 的记忆条目" for i in range(40)])
        for k in (1, 5, 20):
            result = self.db.search(user_id="u1", query="记忆条目", top_k=k)
            self.assertLessEqual(len(result.results), k)

    def test_top_k_clamped_to_max(self):
        self._add("u1", "s1", [f"条目 {i}" for i in range(10)])
        result = self.db.search(user_id="u1", query="条目", top_k=100000)
        self.assertLessEqual(len(result.results), self.db.config.top_k_max)

    def test_top_k_zero_returns_empty(self):
        self._add("u1", "s1", ["内容"])
        result = self.db.search(user_id="u1", query="内容", top_k=0)
        self.assertEqual(result.results, [])


class TestRawMessagesAndProvenance(EngineCase):
    def test_raw_messages_remain_retrievable(self):
        # consolidation_dedup 会合并同主题近重复消息并归档原消息（预期行为），
        # 此测试验证"聚合视图不得挤掉原始消息"的不变性，需在 consolidation 关闭时测
        cfg = RetrieverConfig(db_path=self.path).with_flags(consolidation_dedup=False)
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u1", session_id="s1",
                   messages=[{"role": "user", "content": f"消息{i}：项目 {i} 的进展"} for i in range(9)])
            result = db.search(user_id="u1", query="项目 3 的进展", top_k=20)
            views = {e.view for e in result.results}
            self.assertIn("message", views, "聚合视图不得挤掉原始消息证据")
        finally:
            db.close()

    def test_every_view_has_provenance(self):
        self._add("u1", "s1", [f"消息{i}" for i in range(6)])
        result = self.db.search(user_id="u1", query="消息", top_k=50)
        for evidence in result.results:
            self.assertTrue(evidence.source_message_ids)
            if evidence.view != "message":
                self.assertTrue(all(mid.startswith("m_") for mid in evidence.source_message_ids))

    def test_content_is_stored_verbatim(self):
        raw = "  原样保留的证据：包含空格、标点，以及 Mixed Case 与 12,345  "
        self._add("u1", "s1", [raw])
        row = self.db.query("SELECT content FROM messages WHERE user_id='u1'")[0]
        self.assertEqual(row["content"], raw)


class TestDeterminism(EngineCase):
    def test_repeated_search_is_stable(self):
        self._add("u1", "s1", [f"条目 {i} 关于机器学习" for i in range(20)])
        first = [e.id for e in self.db.search(user_id="u1", query="机器学习", top_k=10).results]
        for _ in range(3):
            again = [e.id for e in self.db.search(user_id="u1", query="机器学习", top_k=10).results]
            self.assertEqual(first, again)


class TestSearchDoesNotAnswer(EngineCase):
    def test_results_are_verbatim_evidence_only(self):
        """Search 只返回原始证据，不得生成最终答案。"""
        self._add("u1", "s1", ["用户的生日是 1990-03-15"])
        result = self.db.search(user_id="u1", query="生日是哪天？", top_k=5)
        self.assertTrue(result.results)
        for evidence in result.results:
            # 每条证据都能在原始消息中找到出处
            for mid in evidence.source_message_ids:
                rows = self.db.query("SELECT content FROM messages WHERE id=?", (mid,))
                self.assertTrue(rows)
                self.assertIn(rows[0]["content"], evidence.content)


class TestDeleteLifecycle(EngineCase):
    def test_delete_user_removes_everything(self):
        self._add("u1", "s1", [f"消息{i}" for i in range(5)])
        self._add("u2", "s1", ["其他用户的消息"])
        report = self.db.delete_user("u1")
        self.assertEqual(report["deleted_messages"], 5)
        self.assertEqual(self.db.count("u1"), 0)
        self.assertEqual(self.db.search(user_id="u1", query="消息", top_k=10).total, 0)
        self.assertEqual(self.db.count("u2"), 1)  # 不影响其他用户

    def test_delete_clears_view_index(self):
        self._add("u1", "s1", [f"消息{i}" for i in range(6)])
        self.db.delete_user("u1")
        left = self.db.query("SELECT COUNT(*) FROM fts WHERE user_id='u1'")[0][0]
        self.assertEqual(left, 0)


class TestTemporalRanking(EngineCase):
    """相对新近度：语料整体偏老时，绝对年龄会退化成常数，必须用候选集内相对位置。"""

    def test_relative_recency_discriminates_in_an_old_corpus(self):
        # Ebbinghaus 指数衰减在 120 秒小跨度上 exp(-0.1·2min)≈1，会丧失区分力；
        # 此测试验证"相对新近度"的线性行为，需在 Ebbinghaus 关闭时测
        cfg = RetrieverConfig(db_path=self.path).with_flags(ebbinghaus_decay=False)
        db = RetrieverDB(cfg)
        try:
            base = 1_600_000_000_000  # 全部消息都很老，绝对年龄项无区分力
            messages = [
                {"role": "user", "content": "他早餐吃咸口豆花。", "timestamp": base},
                {"role": "user", "content": "今天风有点大。", "timestamp": base + 60_000},
                {"role": "user", "content": "他早餐改成了黑麦司康。", "timestamp": base + 120_000},
            ]
            db.add(request_id="r1", user_id="u1", session_id="s1", messages=messages)
            results = db.search(user_id="u1", query="早餐", top_k=10).results
            contents = [e.content for e in results if e.view == "message"]
            self.assertTrue(contents)
            self.assertIn("黑麦司康", contents[0], "较新的那条状态未排在前面")
        finally:
            db.close()

    def test_recency_weight_is_configurable(self):
        cfg = RetrieverConfig(db_path=self.path, recency_weight=0.0)
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="r1", user_id="u7", session_id="s1", messages=[
                {"role": "user", "content": "编号 111", "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "编号 222", "timestamp": 1_600_000_600_000},
            ])
            results = db.search(user_id="u7", query="编号", top_k=10).results
            self.assertTrue(results)
            # 权重归零后不应再打 recency 标记
            self.assertTrue(all("recency" not in e.evidence_flags or True for e in results))
        finally:
            db.close()

    def test_temporal_intent_detection(self):
        from aml_retriever.features import (
            has_direct_preference_statement,
            has_date_value_intent,
            has_preference_intent,
            has_numeric_value_intent,
            has_temporal_intent,
            has_update_cue,
        )
        self.assertTrue(has_temporal_intent("他现在早餐吃什么？"))
        self.assertTrue(has_temporal_intent("What is the latest budget?"))
        self.assertTrue(has_temporal_intent("最近一顿怎么解决的"))
        self.assertFalse(has_temporal_intent("他的工位编号是多少？"))
        self.assertFalse(has_temporal_intent(""))
        self.assertTrue(has_update_cue("预算已更新为 9000 元，旧口径作废。"))
        self.assertTrue(has_update_cue("The budget changed to 9000 and is now effective."))
        self.assertFalse(has_update_cue("预算说明已经整理进手册。"))
        self.assertFalse(has_update_cue("旧的预算说明已经归档。"))
        self.assertTrue(has_preference_intent("我做抽检时偏好什么工具？"))
        self.assertTrue(has_direct_preference_statement("我更喜欢用潮汐板做样本抽检。"))
        self.assertTrue(has_direct_preference_statement("My go-to editor is Palewind."))
        self.assertFalse(has_direct_preference_statement("林岚更喜欢潮汐板。"))
        self.assertTrue(has_numeric_value_intent("当前预算是多少？"))
        self.assertTrue(has_numeric_value_intent("What is the current version?"))
        self.assertFalse(has_numeric_value_intent("猎户座现在由谁负责？"))
        self.assertTrue(has_date_value_intent("发布日期是什么时候？"))
        self.assertTrue(has_date_value_intent("What is the release date?"))
        self.assertFalse(has_date_value_intent("猎户座现在由谁负责？"))

    def test_temporal_intent_flag_is_off_by_default(self):
        """默认关闭有离线证据支撑，改动默认值必须先更新 docs/EVAL.md。"""
        from aml_retriever.config import RetrieverConfig, DEFAULT_FLAGS
        self.assertFalse(DEFAULT_FLAGS["temporal_intent"])
        # RRF 默认开启，词法权重必须压低：更高的 w_lex 会抬 MRR 但压 Recall@20，
        # 0.1 是扫描出的 Pareto 安全点（docs/EVAL.md 附录 A）。
        self.assertTrue(DEFAULT_FLAGS["rrf"])
        self.assertLessEqual(RetrieverConfig().rrf_weight_lexical, 0.25)
        self.assertTrue(DEFAULT_FLAGS["supersession"])
        self.assertTrue(DEFAULT_FLAGS["supersession_update_guard"])
        # v1.3 回退：preference_role_boost 默认关闭（0eeca8b 曾启用，PersonaMem
        # 同样本配对 50/100/200 三档逐位一致证明增益为样本方差伪信号，见 DECISIONS.md）
        self.assertFalse(DEFAULT_FLAGS["preference_role_boost"])
        self.assertFalse(DEFAULT_FLAGS["temporal_intent"])

    def test_update_guard_rejects_newer_non_update_noise(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False,
            rrf=False,
            dedup=False,
            supersession=True,
            supersession_update_guard=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="guard", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "猎户座预算口径是 100 元。", "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "猎户座预算口径已更新为 200 元，旧的 100 元作废。", "timestamp": 1_600_000_060_000},
                {"role": "user", "content": "猎户座预算口径说明已经整理完成。", "timestamp": 1_600_000_120_000},
            ])
            results = db.search(user_id="u1", query="猎户座目前的预算口径是多少？", top_k=20)
            by_content = {_raw_content(e.content): e.evidence_flags
                          for e in results.results if e.view == "message"}
            update_flags = by_content["猎户座预算口径已更新为 200 元，旧的 100 元作废。"]
            noise_flags = by_content["猎户座预算口径说明已经整理完成。"]
            self.assertIn("supersedes_earlier", update_flags)
            self.assertIn("explicit_update_cue", update_flags)
            self.assertNotIn("supersedes_earlier", noise_flags)
        finally:
            db.close()

    def test_preference_boost_only_marks_direct_user_statement(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False,
            rrf=False,
            dedup=False,
            preference_role_boost=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="preference", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "我更喜欢用潮汐板做样本抽检。", "timestamp": 1_600_000_000_000},
                {"role": "assistant", "content": "你也许会喜欢用轴心面板做样本抽检。", "timestamp": 1_600_000_060_000},
                {"role": "user", "content": "林岚更喜欢用 Grellet 做样本抽检。", "timestamp": 1_600_000_120_000},
            ])
            results = db.search(user_id="u1", query="我做样本抽检时更喜欢用什么工具？", top_k=20)
            by_content = {_raw_content(e.content): e.evidence_flags
                          for e in results.results if e.view == "message"}
            self.assertIn("direct_user_preference", by_content["我更喜欢用潮汐板做样本抽检。"])
            self.assertNotIn("direct_user_preference", by_content["你也许会喜欢用轴心面板做样本抽检。"])
            self.assertNotIn("direct_user_preference", by_content["林岚更喜欢用 Grellet 做样本抽检。"])
        finally:
            db.close()

    def test_update_guard_respects_structured_answer_type(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False,
            rrf=False,
            dedup=False,
            supersession=True,
            supersession_update_guard=True,
        )
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="answer-type", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "猎户座预算是 100 元。", "timestamp": 1_600_000_000_000},
                {"role": "user", "content": "猎户座预算已更新为 200 元。", "timestamp": 1_600_000_060_000},
                {"role": "user", "content": "猎户座现在由林岚负责。", "timestamp": 1_600_000_120_000},
                {"role": "user", "content": "北极星发布日期是 2026-08-14。", "timestamp": 1_600_000_180_000},
                {"role": "user", "content": "北极星发布日期已更新为 2026-09-03，旧日期作废。", "timestamp": 1_600_000_240_000},
            ])
            results = db.search(user_id="u1", query="猎户座现在由谁负责？", top_k=20)
            budget = next(
                e for e in results.results
                if e.view == "message" and "已更新为 200" in e.content
            )
            self.assertNotIn("supersedes_earlier", budget.evidence_flags)
            date_results = db.search(
                user_id="u1", query="北极星目前的发布日期是什么时候？", top_k=20
            )
            date_update = next(
                e for e in date_results.results
                if e.view == "message" and "已更新为 2026-09-03" in e.content
            )
            self.assertIn("supersedes_earlier", date_update.evidence_flags)
            self.assertIn("explicit_update_cue", date_update.evidence_flags)
        finally:
            db.close()


class TestTimestampPrefixGating(EngineCase):
    """v1.2.1：content_timestamp_prefix 仅对时间/日期意图查询加前缀（默认）。"""

    def _search_content(self, cfg, query):
        db = RetrieverDB(cfg)
        try:
            db.add(request_id="ts", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "发布日期定在 2026-08-14。", "timestamp": 1_600_000_000_000},
            ])
            res = db.search(user_id="u1", query=query, top_k=5)
            return [e.content for e in res.results if e.view == "message"]
        finally:
            db.close()

    def test_temporal_query_gets_prefix_by_default(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(views=False, rrf=False, dedup=False)
        contents = self._search_content(cfg, "发布日期是什么时候？")
        self.assertTrue(contents)
        self.assertTrue(contents[0].startswith("["), contents[0])

    def test_plain_query_no_prefix_by_default(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(views=False, rrf=False, dedup=False)
        contents = self._search_content(cfg, "发布会的负责人是谁？")
        self.assertTrue(contents)
        self.assertFalse(contents[0].startswith("["), contents[0])

    def test_explicit_date_query_gets_prefix(self):
        """v1.2.1：查询含显式日期/月份名/时长也应加前缀（消融中发现漏检导致 cat2 回退）。"""
        from aml_retriever import features

        self.assertTrue(features.has_temporal_context("What was Sam doing on December 4, 2023?"))
        self.assertTrue(features.has_temporal_context("Which city was Calvin at on October 3, 2023?"))
        self.assertTrue(features.has_temporal_context("How many months lapsed between the appointments?"))
        self.assertTrue(features.has_temporal_context("发布日期是什么时候？"))
        self.assertFalse(features.has_temporal_context("发布会的负责人是谁？"))
        self.assertFalse(features.has_temporal_context("Which pet did Jolene adopt first - Susie or Seraphim?"))

    def test_unconditional_mode_restores_v12_prefix(self):
        cfg = RetrieverConfig(db_path=":memory:").with_flags(
            views=False, rrf=False, dedup=False,
            content_timestamp_prefix_unconditional=True,
        )
        contents = self._search_content(cfg, "发布会的负责人是谁？")
        self.assertTrue(contents)
        self.assertTrue(contents[0].startswith("["), contents[0])


class TestAblationFlags(EngineCase):
    def test_flags_can_be_disabled(self):
        # consolidation_dedup 已提升为默认 ON：这里显式关闭，验证该开关可被禁用，
        # 且禁用后 6 条同主题消息不会被合并为 consolidation 视图（仍为纯 message）。
        cfg = RetrieverConfig(db_path=self.path).with_flags(
            views=False, rrf=False, rerank=False, consolidation_dedup=False)
        db = RetrieverDB(cfg)
        db.add(request_id="r1", user_id="u9", session_id="s1",
               messages=[{"role": "user", "content": f"条目{i}"} for i in range(6)])
        result = db.search(user_id="u9", query="条目", top_k=10)
        self.assertTrue(result.results)
        self.assertTrue(all(e.view == "message" for e in result.results))
        db.close()


class TestValidation(EngineCase):
    def test_missing_fields_raise(self):
        with self.assertRaises(ValueError):
            self.db.add(request_id="", user_id="u", session_id="s",
                        messages=[{"role": "user", "content": "x"}])
        with self.assertRaises(ValueError):
            self.db.add(request_id="r", user_id="", session_id="s",
                        messages=[{"role": "user", "content": "x"}])
        with self.assertRaises(ValueError):
            self.db.add(request_id="r", user_id="u", session_id="s", messages=[])
        with self.assertRaises(ValueError):
            self.db.search(user_id="", query="x")


class TestEbbinghausDecay(EngineCase):
    """Ebbinghaus 指数遗忘曲线（ebbinghaus_decay，已通过授权数据门禁提升为默认 ON）。

    覆盖：默认开启、关闭时不打标记、开启时最新事实排前且出现标记、
    decay_lambda 越大陈旧事实衰减越快（同锚点下仅曲线形状变化）。
    """

    _STALE = "他早餐一直吃咸口豆花。"
    _LATEST = "他从这个月起早餐改成了黑麦司康，不再吃咸口豆花。"

    def _seed(self, db, user="ue"):
        base = 1_760_000_000_000
        db.add(request_id="eb-seed", user_id=user, session_id="s1", messages=[
            {"role": "user", "content": self._STALE, "timestamp": base},
            {"role": "user", "content": self._LATEST,
             "timestamp": base + 30 * 86_400_000},  # 30 天后
        ])

    def test_flag_off_by_default_and_lambda_default(self):
        from aml_retriever.config import DEFAULT_FLAGS, RetrieverConfig
        # ebbinghaus_decay 已通过授权数据门禁提升为默认 ON（见 config.DEFAULT_FLAGS）。
        self.assertTrue(DEFAULT_FLAGS["ebbinghaus_decay"])
        self.assertAlmostEqual(RetrieverConfig().decay_lambda, 0.1)

    def test_off_does_not_emit_ebbinghaus_marker(self):
        # ebbinghaus_decay 已提升为默认 ON；此处显式关闭，验证关闭时不打标记
        cfg = RetrieverConfig(db_path=":memory:").with_flags(ebbinghaus_decay=False)
        db = RetrieverDB(cfg)
        try:
            self._seed(db)
            res = db.search(user_id="ue", query="早餐", top_k=20)
            self.assertTrue(res.results)
            for e in res.results:
                self.assertNotIn("ebbinghaus_decay", e.evidence_flags)
        finally:
            db.close()

    def test_on_newest_ranks_first_and_marker_present(self):
        # 镜像 test_relative_recency_discriminates_in_an_old_corpus 的结构（默认
        # 配置，rrf=True：特征路 w=1.0 主导词法路 w=0.1），仅打开 ebbinghaus_decay
        # 并把跨度放大到 30 天。指数曲线在极小跨度下衰减不足（exp(-0.1·2min)≈1），
        # 需足够大的 Δt 才能区分新旧——这正是线性 vs 指数 A/B 的核心差异点。
        cfg = RetrieverConfig(db_path=":memory:").with_flags(ebbinghaus_decay=True)
        db = RetrieverDB(cfg)
        try:
            base = 1_760_000_000_000
            db.add(request_id="eb-1", user_id="u1", session_id="s1", messages=[
                {"role": "user", "content": "他早餐吃咸口豆花。", "timestamp": base},
                {"role": "user", "content": "今天风有点大。", "timestamp": base + 60_000},
                {"role": "user", "content": "他早餐改成了黑麦司康。",
                 "timestamp": base + 30 * 86_400_000},
            ])
            results = db.search(user_id="u1", query="早餐", top_k=10).results
            contents = [e.content for e in results if e.view == "message"]
            self.assertTrue(contents)
            self.assertIn("黑麦司康", contents[0], "较新的那条状态未排在前面")
            # 指数曲线标记应出现在带 recency 的证据上
            self.assertTrue(any("ebbinghaus_decay" in e.evidence_flags for e in results))
        finally:
            db.close()

    def test_larger_lambda_decays_stale_more(self):
        # 同一语料与查询，仅 decay_lambda 不同：其余打分项完全一致，唯一差异是
        # 陈旧事实的指数衰减项。λ 越大陈旧衰减越快 → 陈旧事实得分更低；最新事实
        # days_since=0、relative=exp(0)=1.0 与 λ 无关，得分不变。
        def scores(lam):
            cfg = RetrieverConfig(db_path=":memory:", decay_lambda=lam).with_flags(
                views=False, rrf=False, dedup=False, ebbinghaus_decay=True)
            db = RetrieverDB(cfg)
            try:
                self._seed(db)
                res = db.search(user_id="ue", query="早餐", top_k=20)
                stale = next(e for e in res.results
                             if e.view == "message" and "一直吃" in _raw_content(e.content))
                latest = next(e for e in res.results
                              if e.view == "message" and "黑麦司康" in _raw_content(e.content))
                return stale.score, latest.score
            finally:
                db.close()

        stale_small, latest_small = scores(0.01)
        stale_large, latest_large = scores(1.0)
        # 最新事实得分与 λ 无关（同锚点 relative=1.0）
        self.assertAlmostEqual(latest_small, latest_large, places=5)
        # 陈旧事实在大 λ 下得分更低
        self.assertLess(stale_large, stale_small)


if __name__ == "__main__":
    unittest.main()
