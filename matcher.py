from __future__ import annotations

import random
import re
from dataclasses import dataclass

try:
    from .database import MemeRecord
except ImportError:
    from database import MemeRecord


@dataclass
class MatchResult:
    record: MemeRecord
    score: float
    reasons: list[str]


class KeywordMatcher:
    """MVP 关键词匹配器；后续可在这个类旁边扩展语义向量匹配。"""

    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold

    def match(self, text: str, records: list[MemeRecord], *, limit: int = 8) -> list[MatchResult]:
        normalized = normalize_text(text)
        if not normalized:
            return []

        results: list[MatchResult] = []
        for record in records:
            score, reasons = self._score_record(normalized, record)
            if score >= self.threshold:
                results.append(MatchResult(record=record, score=score, reasons=reasons))
        results.sort(key=lambda item: item.score, reverse=True)
        return results[:limit]

    def choose(self, text: str, records: list[MemeRecord]) -> MatchResult | None:
        candidates = self.match(text, records)
        if not candidates:
            return None
        top_score = candidates[0].score
        top_candidates = [item for item in candidates if item.score >= top_score * 0.75]
        return random.choice(top_candidates)

    def _score_record(self, text: str, record: MemeRecord) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []

        for tag in record.tags:
            term = normalize_text(tag)
            if term and term in text:
                score += 3.0
                reasons.append(tag)

        for emotion in record.emotion:
            term = normalize_text(emotion)
            if term and term in text:
                score += 2.0
                reasons.append(emotion)

        description = normalize_text(record.description)
        for token in split_tokens(text):
            if len(token) >= 2 and token in description:
                score += 1.0
                reasons.append(token)

        # 中文短句经常无空格，反向包含可以捕捉“离谱”“绷不住”等短关键词。
        for token in split_tokens(description):
            if len(token) >= 2 and token in text:
                score += 0.6

        return score, list(dict.fromkeys(reasons))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def split_tokens(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_]{2,}", text.lower())
