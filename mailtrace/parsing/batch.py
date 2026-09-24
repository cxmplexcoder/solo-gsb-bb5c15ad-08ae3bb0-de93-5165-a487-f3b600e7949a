"""多封邮件的慢段归因（批量统计相邻节点耗时）。

设计要点：
- 直接复用 :func:`mailtrace.parsing.parser.analyze_headers` 的解析结果
  （节点身份、相邻节点耗时），不重复实现 Received 解析。
- 只统计各邮件**投递顺序中实际相邻的两个节点**构成的区段，绝不跨节点
  拼接耗时。
- 节点身份沿用 :mod:`mailtrace.parsing.compare` 的定义：规范化 ``by``
  主机名（小写、去尾点）与对端 IP 的**完整已知字段组合** ``(by, ip)``；
  两者皆缺的节点没有身份。区段身份为
  ``(上游身份, 下游身份)``，仅当两端身份的已知字段组合完全一致时才
  合并；任一端缺少身份的区段不参与合并，单独计入排除数。
- 同一封邮件中同键区段重复出现（例如真实转发环路）时，仅取其中**最大
  的有效非负耗时**作为该邮件在这一区段上的唯一样本。
- 耗时无效（本节点或上游时间缺失/无法解析）或时间倒灌（下游早于上游，
  阈值与单条分析的时钟偏移告警一致）的区段被排除并分别计数。
"""

from __future__ import annotations

import re
from statistics import median

from mailtrace.parsing.compare import _identity
from mailtrace.parsing.parser import (
    SKEW_TOLERANCE_SECONDS,
    _humanize_delay,
    analyze_headers,
)

# 明确分隔符行：可选空白后至少 3 个分隔字符，整行不含冒号
# （真正的头部字段必含冒号，因此不会误伤；中文冒号同样排除）。
_SEPARATOR_LINE_RE = re.compile(r"^[\t ]*[-=#~*_]{3,}[^:：\n]*$")

MIN_BATCH_EMAILS = 2


def split_headers_batch(text: str, separator: str | None = None) -> list[str]:
    """把一次粘贴的多封邮件原始头部按明确分隔符切分。

    ``separator`` 为 None 时，识别整行只含 ``-`` ``=`` ``#`` ``~``
    ``*`` ``_``（至少 3 个，可带空白与说明文字、不含冒号）的分隔行，
    例如 ``----- 邮件 1 -----``、``###``、``========``。

    传入自定义 ``separator`` 时，以整行去空白后**精确等于**该字符串的
    行作为分隔。空白分片自动丢弃。
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")

    explicit = separator.strip() if isinstance(separator, str) else ""

    def is_separator(line: str) -> bool:
        if explicit:
            return line.strip() == explicit
        return bool(_SEPARATOR_LINE_RE.match(line))

    pieces: list[str] = []
    current: list[str] = []

    def flush() -> None:
        piece = "\n".join(current).strip()
        if piece:
            pieces.append(piece)

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if is_separator(line):
            flush()
            current = []
        else:
            current.append(line)
    flush()
    return pieces


def _endpoint_label(identity: tuple[str | None, str | None]) -> str:
    """区段端点的可读标签：``by (ip)``，缺一项时显示另一项。"""
    by, ip = identity
    if by and ip:
        return f"{by} ({ip})"
    return by or ip or "(未知节点)"


def _effective_delay(delay: float) -> float:
    """秒级取整可能产生落在容差内的微小负值，按 0 秒样本处理。"""
    return 0.0 if delay < 0 else delay


def batch_delay_stats(headers_list: list[str]) -> dict:
    """对多封邮件头部做相邻区段耗时聚合。

    返回每类区段的样本数、中位耗时、最大耗时（按中位耗时从慢到快排序），
    以及因无效时间 / 时间倒灌 / 缺少节点身份被排除的区段数量等汇总信息；
    单封邮件解析失败时记入 ``errors`` 并跳过，不影响其余邮件。
    """
    if not isinstance(headers_list, (list, tuple)):
        raise TypeError("headers_list 必须是字符串列表")

    errors: list[dict] = []
    # 每封邮件一个 dict：区段键 -> 该邮件在这一区段上的最大有效耗时
    email_samples: list[dict[tuple, float]] = []

    excluded_invalid_time = 0
    excluded_backflow = 0
    excluded_missing_identity = 0
    duplicate_samples_dropped = 0
    candidate_count = 0

    for index, raw in enumerate(headers_list):
        if not isinstance(raw, str):
            errors.append({"index": index, "error": "该条目不是字符串，已跳过"})
            continue
        if not raw.strip():
            errors.append({"index": index, "error": "头部内容为空，已跳过"})
            continue

        result = analyze_headers(raw)
        if result["hop_count"] == 0:
            errors.append(
                {"index": index, "error": "未找到 Received 字段，已跳过"}
            )
            continue

        path = result["path"]
        samples: dict[tuple, float] = {}
        for i in range(1, len(path)):
            candidate_count += 1
            upstream, downstream = path[i - 1], path[i]
            id_up = _identity(upstream)
            id_down = _identity(downstream)

            # 任一端缺少身份（by 与对端 IP 全缺）：区段无法稳定归并。
            if id_up is None or id_down is None:
                excluded_missing_identity += 1
                continue

            delay = downstream.get("delay_seconds")
            if delay is None:
                # 本节点或上游时间缺失/无法解析，不跨节点拼接。
                excluded_invalid_time += 1
                continue
            if delay < -SKEW_TOLERANCE_SECONDS:
                # 下游时间早于上游：时间倒灌（时钟偏移/伪造）。
                excluded_backflow += 1
                continue

            key = (id_up, id_down)
            value = _effective_delay(float(delay))
            if key in samples:
                # 同邮件同键区段只保留最大有效非负耗时。
                duplicate_samples_dropped += 1
                if value > samples[key]:
                    samples[key] = value
            else:
                samples[key] = value

        email_samples.append(samples)

    valid_email_count = len(email_samples)
    if valid_email_count == 0:
        return {
            "error": "没有可用于统计的邮件：至少需要一份包含 Received 字段的头部",
            "email_count": len(headers_list),
            "valid_email_count": 0,
            "errors": errors,
            "segments": [],
        }

    # 区段键 -> 各邮件样本
    buckets: dict[tuple, list[float]] = {}
    for samples in email_samples:
        for key, value in samples.items():
            buckets.setdefault(key, []).append(value)

    segments: list[dict] = []
    for (id_up, id_down), values in buckets.items():
        ordered = sorted(values)
        med = float(median(ordered))
        segments.append(
            {
                "from_node": {"by_host": id_up[0], "ip": id_up[1]},
                "to_node": {"by_host": id_down[0], "ip": id_down[1]},
                "from_label": _endpoint_label(id_up),
                "to_label": _endpoint_label(id_down),
                "segment_label": f"{_endpoint_label(id_up)} → {_endpoint_label(id_down)}",
                "sample_count": len(values),
                "samples_seconds": ordered,
                "median_seconds": med,
                "median_human": _humanize_delay(med),
                "max_seconds": ordered[-1],
                "max_human": _humanize_delay(ordered[-1]),
            }
        )

    # 从慢到快：中位耗时降序，并列时最大耗时降序，再按区段名稳定排序。
    segments.sort(
        key=lambda s: (-s["median_seconds"], -s["max_seconds"], s["segment_label"])
    )

    return {
        "email_count": len(headers_list),
        "valid_email_count": valid_email_count,
        "error_count": len(errors),
        "errors": errors,
        "segment_count": len(segments),
        "sample_count": sum(s["sample_count"] for s in segments),
        "candidate_count": candidate_count,
        "excluded_invalid_time": excluded_invalid_time,
        "excluded_backflow": excluded_backflow,
        "excluded_missing_identity": excluded_missing_identity,
        "duplicate_samples_dropped": duplicate_samples_dropped,
        "segments": segments,
    }
