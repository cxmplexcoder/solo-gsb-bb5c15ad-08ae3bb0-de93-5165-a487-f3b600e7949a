"""邮件原始头部解析包。"""

from .batch import MIN_BATCH_EMAILS, batch_delay_stats, split_headers_batch
from .compare import compare_headers
from .parser import analyze_headers, parse_header_blocks

__all__ = [
    "MIN_BATCH_EMAILS",
    "analyze_headers",
    "batch_delay_stats",
    "compare_headers",
    "parse_header_blocks",
    "split_headers_batch",
]
