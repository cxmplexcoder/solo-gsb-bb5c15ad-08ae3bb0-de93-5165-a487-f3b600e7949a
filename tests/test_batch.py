import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mailtrace.parsing import (  # noqa: E402
    batch_delay_stats,
    split_headers_batch,
)
from mailtrace.parsing.sample import SAMPLE_BATCH_HEADERS  # noqa: E402

try:  # Flask 相关测试在未安装 Flask 时跳过（解析核心仅需标准库）
    from app import create_app
except ImportError:  # pragma: no cover
    create_app = None

DATE = "Wed, 24 Sep 2026 %s +0000"


def _received(by, date, frm="peer.example", ip=None, no_by=False):
    """构造单条 Received（书写顺序：越靠后的行越早投递）。"""
    if no_by:
        return f"Received: (internal queue scan); {date}\n"
    parts = [f"Received: from {frm}"]
    if ip:
        parts.append(f" ({frm} [{ip}])")
    parts.append(f"\n\tby {by} with ESMTP")
    if date:
        parts.append(f"; {date}")
    parts.append("\n")
    return "".join(parts)


def _path(edge_t, mid_t, final_t, *, edge_ip="192.0.2.1",
          mid_ip="198.51.100.1", final_ip="203.0.113.1"):
    """三跳链路 edge -> mid -> final 的完整头部（时间为 (hh, mm, ss)）。"""
    def ts(t):
        return DATE % f"{t[0]:02d}:{t[1]:02d}:{t[2]:02d}"

    return (
        _received("final.example", ts(final_t), frm="mid.example", ip=final_ip)
        + _received("mid.example", ts(mid_t), frm="edge.example", ip=mid_ip)
        + _received("edge.example", ts(edge_t), frm="sender", ip=edge_ip)
    )


def _seg(stats, upstream, downstream):
    for seg in stats["segments"]:
        if seg["from_label"].startswith(upstream) and seg["to_label"].startswith(downstream):
            return seg
    raise AssertionError(f"segment {upstream} -> {downstream} not found")


class SplitTests(unittest.TestCase):
    def test_dash_and_hash_separators(self):
        text = (
            "Received: by a.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
            "----- 下一封 -----\n"
            "Received: by b.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
            "#####\n"
            "Received: by c.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
        )
        pieces = split_headers_batch(text)
        self.assertEqual(len(pieces), 3)
        self.assertIn("a.example", pieces[0])
        self.assertIn("b.example", pieces[1])
        self.assertIn("c.example", pieces[2])

    def test_leading_trailing_and_blank_pieces_ignored(self):
        text = (
            "=====\n"
            "Received: by a.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
            "\n"
            "~~~~~\n"
            "\n"
        )
        pieces = split_headers_batch(text)
        self.assertEqual(len(pieces), 1)

    def test_header_line_with_colon_is_not_a_separator(self):
        # 含冒号的行即使以 --- 开头也不能当成分隔符（真实头部字段）
        text = (
            "Received: by a.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
            "X-Custom: -----not-a-separator-----\n"
            "Received: by b.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
        )
        pieces = split_headers_batch(text)
        self.assertEqual(len(pieces), 1)

    def test_explicit_separator(self):
        text = (
            "Received: by a.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
            "NEXT MAIL\n"
            "Received: by b.example; Wed, 24 Sep 2026 02:00:00 +0000\n"
        )
        self.assertEqual(len(split_headers_batch(text)), 1)
        pieces = split_headers_batch(text, separator="NEXT MAIL")
        self.assertEqual(len(pieces), 2)

    def test_crlf_normalized(self):
        text = (
            "Received: by a.example; Wed, 24 Sep 2026 02:00:00 +0000\r\n"
            "---\r\n"
            "Received: by b.example; Wed, 24 Sep 2026 02:00:00 +0000\r\n"
        )
        self.assertEqual(len(split_headers_batch(text)), 2)


class BatchStatsTests(unittest.TestCase):
    def test_basic_aggregation_median_max_and_order(self):
        m1 = _path((2, 0, 0), (2, 0, 10), (2, 0, 40))   # edge->mid 10, mid->final 30
        m2 = _path((3, 0, 0), (3, 0, 50), (3, 1, 0))    # 50, 10
        m3 = _path((4, 0, 0), (4, 0, 30), (4, 0, 36))   # 30, 6
        stats = batch_delay_stats([m1, m2, m3])
        edge_mid = _seg(stats, "edge.example", "mid.example")
        mid_final = _seg(stats, "mid.example", "final.example")
        self.assertEqual(edge_mid["sample_count"], 3)
        self.assertEqual(edge_mid["median_seconds"], 30)
        self.assertEqual(edge_mid["max_seconds"], 50)
        self.assertEqual(mid_final["sample_count"], 3)
        self.assertEqual(mid_final["median_seconds"], 10)
        self.assertEqual(mid_final["max_seconds"], 30)
        # 从慢到快：edge->mid（中位 30）排在 mid->final（中位 10）前
        self.assertEqual(stats["segments"][0]["from_label"], edge_mid["from_label"])
        self.assertEqual(stats["sample_count"], 6)

    def test_segment_identity_requires_full_known_field_combo(self):
        # 两封邮件 mid 主机名相同但对端 IP 不同 -> 同一 by 标签也不合并
        m1 = _path((2, 0, 0), (2, 0, 10), (2, 0, 20), mid_ip="198.51.100.1")
        m2 = _path((3, 0, 0), (3, 0, 10), (3, 0, 20), mid_ip="198.51.100.9")
        stats = batch_delay_stats([m1, m2])
        mid_final = [s for s in stats["segments"]
                     if s["to_label"].startswith("final.example")]
        # edge->mid / mid->final 各因 IP 差异拆成两种键
        self.assertEqual(len(mid_final), 2)
        for seg in mid_final:
            self.assertEqual(seg["sample_count"], 1)

    def test_identity_less_segments_not_merged_and_counted(self):
        # 在 mid 位置插入一个无 by / 无 IP 的内部处理节点：
        # 其两侧的相邻区段都不能合并到普通链路统计中。
        def path_with_internal(t_edge, t_scan, t_final):
            return (
                _received("final.example", DATE % t_final, frm="scanner", ip="203.0.113.1")
                + _received(None, DATE % t_scan, no_by=True)
                + _received("edge.example", DATE % t_edge, frm="sender", ip="192.0.2.1")
            )

        m1 = path_with_internal("02:00:00", "02:00:05", "02:00:20")
        m2 = path_with_internal("03:00:00", "03:00:05", "03:00:20")
        stats = batch_delay_stats([m1, m2])
        # 有身份的端点 final / edge 各自只与无身份节点相邻 -> 无任何可合并区段
        self.assertEqual(stats["segment_count"], 0)
        self.assertEqual(stats["excluded_missing_identity"], 4)
        self.assertEqual(stats["sample_count"], 0)

    def test_backflow_and_invalid_time_excluded(self):
        # 邮件 1：mid 时间倒灌（早于 edge）
        backflow = _path((2, 0, 30), (2, 0, 10), (2, 0, 40))
        # 邮件 2：mid 时间无效
        invalid = (
            _received("final.example", DATE % "02:00:40", frm="mid.example", ip="203.0.113.1")
            + _received("mid.example", "not-a-date", frm="edge.example", ip="198.51.100.1")
            + _received("edge.example", DATE % "02:00:00", frm="sender", ip="192.0.2.1")
        )
        # 邮件 3：正常
        normal = _path((4, 0, 0), (4, 0, 20), (4, 0, 50))
        stats = batch_delay_stats([backflow, invalid, normal])
        self.assertEqual(stats["excluded_backflow"], 1)
        # mid 无时间 -> edge->mid 无效；final 的相邻耗时同样不可计算（不拼接）
        self.assertEqual(stats["excluded_invalid_time"], 2)
        # 倒灌邮件的 edge->mid 被排除，但其 mid->final 仍为有效样本；
        # 无效邮件不贡献样本，正常邮件贡献两个 -> 共 3 个样本
        self.assertEqual(stats["sample_count"], 3)
        edge_mid = _seg(stats, "edge.example", "mid.example")
        mid_final = _seg(stats, "mid.example", "final.example")
        self.assertEqual(edge_mid["sample_count"], 1)
        self.assertEqual(mid_final["sample_count"], 2)

    def test_duplicate_same_key_segment_in_one_email_keeps_max(self):
        # 一封真实重试/环路邮件，投递顺序（发件 -> 收件）：
        #   smtp01(00:00) -> out(00:01:30)   同键出现第 1 次：90s
        #   -> smtp01(00:01) [倒灌]
        #   -> out(00:02)                    同键出现第 2 次：60s
        #   -> mid(00:03) -> final(00:03:30)
        # smtp01 -> out 同键两次：只保留最大有效非负耗时 90s，
        # 另一个样本被丢弃（duplicate_samples_dropped = 1）。
        mail = (
            _received("final.example", DATE % "00:03:30",
                      frm="mid.example", ip="203.0.113.1")
            + _received("mid.example", DATE % "00:03:00",
                      frm="out.example", ip="198.51.100.1")
            + _received("out.example", DATE % "00:02:00",
                        frm="smtp01.example", ip="10.10.0.5")
            + "Received: from out.example\n"
            f"\tby smtp01.example with ESMTP; {DATE % '00:01:00'}\n"
            + _received("out.example", DATE % "00:01:30",
                        frm="smtp01.example", ip="10.10.0.5")
            + _received("smtp01.example", DATE % "00:00:00",
                        frm="sender")
        )
        stats = batch_delay_stats([mail])
        seg = _seg(stats, "smtp01.example", "out.example")
        self.assertEqual(seg["sample_count"], 1)
        self.assertEqual(seg["max_seconds"], 90)
        self.assertEqual(stats["duplicate_samples_dropped"], 1)
        # 倒灌的 out -> smtp01 被排除；其余区段正常
        self.assertEqual(stats["excluded_backflow"], 1)

    def test_invalid_emails_are_reported_but_do_not_abort_batch(self):
        good = _path((2, 0, 0), (2, 0, 10), (2, 0, 20))
        stats = batch_delay_stats([good, "Subject: no received\n", "  "])
        self.assertEqual(stats["email_count"], 3)
        self.assertEqual(stats["valid_email_count"], 1)
        self.assertEqual(stats["error_count"], 2)
        self.assertEqual([e["index"] for e in stats["errors"]], [1, 2])

    def test_all_emails_invalid_returns_error(self):
        stats = batch_delay_stats(["Subject: x\n", ""])
        self.assertIn("error", stats)
        self.assertEqual(stats["valid_email_count"], 0)
        self.assertEqual(stats["segments"], [])

    def test_first_hop_has_no_segment_sample(self):
        # 单封两跳：第一跳 edge 没有上游，只有 edge->mid 一个区段
        mail = (
            _received("mid.example", DATE % "02:00:10", frm="edge.example", ip="198.51.100.1")
            + _received("edge.example", DATE % "02:00:00", frm="sender", ip="192.0.2.1")
        )
        stats = batch_delay_stats([mail])
        self.assertEqual(stats["segment_count"], 1)
        self.assertEqual(stats["sample_count"], 1)

    def test_sample_batch(self):
        pieces = split_headers_batch(SAMPLE_BATCH_HEADERS)
        self.assertEqual(len(pieces), 3)
        stats = batch_delay_stats(pieces)
        self.assertEqual(stats["valid_email_count"], 3)
        self.assertEqual(stats["excluded_backflow"], 1)
        self.assertGreaterEqual(stats["excluded_missing_identity"], 2)
        self.assertGreaterEqual(stats["duplicate_samples_dropped"], 1)
        medians = [s["median_seconds"] for s in stats["segments"]]
        self.assertEqual(medians, sorted(medians, reverse=True))


@unittest.skipIf(create_app is None, "未安装 Flask，跳过 API 测试")
class BatchApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()
        self.m1 = _path((2, 0, 0), (2, 0, 10), (2, 0, 40))
        self.m2 = _path((3, 0, 0), (3, 0, 50), (3, 1, 0))

    def test_api_batch_with_headers_array(self):
        resp = self.client.post(
            "/api/batch-delays", json={"headers": [self.m1, self.m2]}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["valid_email_count"], 2)
        self.assertEqual(data["segment_count"], 2)

    def test_api_batch_rejects_non_array(self):
        resp = self.client.post("/api/batch-delays", json={"headers": "nope"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("数组", resp.get_json()["error"])

    def test_api_batch_rejects_fewer_than_two(self):
        resp = self.client.post(
            "/api/batch-delays", json={"headers": [self.m1]}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("2 份", resp.get_json()["error"])

    def test_api_batch_via_headers_text(self):
        text = self.m1 + "\n-----\n" + self.m2
        resp = self.client.post(
            "/api/batch-delays", json={"headers_text": text}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["valid_email_count"], 2)

    def test_api_batch_without_separator_returns_400(self):
        resp = self.client.post(
            "/api/batch-delays", json={"headers_text": self.m1}
        )
        self.assertEqual(resp.status_code, 400)

    def test_form_page_renders_batch_result(self):
        text = SAMPLE_BATCH_HEADERS
        resp = self.client.post(
            "/batch-delays", data={"headers_batch": text}
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("多邮件慢段归因", html)
        self.assertIn("中位耗时", html)
        self.assertIn("时间倒灌", html)
        # 原有功能仍在页面上
        self.assertIn("分析投递路径（A）", html)
        self.assertIn("对照 A / B 路径", html)


if __name__ == "__main__":
    unittest.main()
