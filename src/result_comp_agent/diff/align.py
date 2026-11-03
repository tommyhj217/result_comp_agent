"""순서 보존 정렬(alignment) 유틸리티.

1단계: 정확히 같은 요소(해시 키)를 ``SequenceMatcher`` 로 앵커링 (오탐 없음, 점수 1.0)
2단계: 앵커 사이의 구간만 Needleman-Wunsch 동적계획법으로 유사도 합이 최대가 되도록 짝지음
       (임계값 미만은 짝짓지 않음 → 추가/삭제로 보고)
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Callable, Hashable, Optional, Sequence

from .normalize import collapse_ws

AlignPair = tuple[Optional[int], Optional[int], float]

_MAX_TEXT = 2000


def text_sim(a: Optional[str], b: Optional[str]) -> float:
    a = collapse_ws(a or "")
    b = collapse_ws(b or "")
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:_MAX_TEXT], b[:_MAX_TEXT], autojunk=False).ratio()


def quick_text_sim(a: Optional[str], b: Optional[str]) -> float:
    """상한 추정(quick_ratio) 기반의 빠른 유사도. 대량 DP 에서 사용."""
    a = a or ""
    b = b or ""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:_MAX_TEXT], b[:_MAX_TEXT], autojunk=False).quick_ratio()


def jaccard(a: Counter, b: Counter) -> float:
    if not a and not b:
        return 1.0
    inter = sum((a & b).values())
    union = sum((a | b).values())
    return inter / union if union else 0.0


def align_sequences(
    n_a: int,
    n_b: int,
    sim: Callable[[int, int], float],
    threshold: float,
    key_a: Optional[Sequence[Hashable]] = None,
    key_b: Optional[Sequence[Hashable]] = None,
    max_dp_cells: int = 400_000,
) -> list[AlignPair]:
    """순서를 보존하며 a[0..n_a) 와 b[0..n_b) 를 정렬한다.

    반환: (i, j, score) 목록. 짝이 없으면 (i, None, 0.0) / (None, j, 0.0).
    """
    out: list[AlignPair] = []
    if key_a is not None and key_b is not None:
        sm = SequenceMatcher(None, list(key_a), list(key_b), autojunk=False)
        blocks = sm.get_matching_blocks()
    else:
        blocks = []
    ai = bj = 0
    for blk in blocks:
        a0, b0, size = blk.a, blk.b, blk.size
        out.extend(_dp(range(ai, a0), range(bj, b0), sim, threshold, max_dp_cells))
        for k in range(size):
            out.append((a0 + k, b0 + k, 1.0))
        ai, bj = a0 + size, b0 + size
    out.extend(_dp(range(ai, n_a), range(bj, n_b), sim, threshold, max_dp_cells))
    return out


def _dp(ra: range, rb: range, sim, threshold: float, max_cells: int) -> list[AlignPair]:
    A, B = list(ra), list(rb)
    n, m = len(A), len(B)
    if n == 0:
        return [(None, j, 0.0) for j in B]
    if m == 0:
        return [(i, None, 0.0) for i in A]
    if n * m > max_cells:
        # 너무 큰 구간: 위치 기반 대응 (안전한 폴백, 임계값 적용)
        res: list[AlignPair] = []
        for k in range(max(n, m)):
            i = A[k] if k < n else None
            j = B[k] if k < m else None
            if i is not None and j is not None:
                s = sim(i, j)
                if s >= threshold:
                    res.append((i, j, s))
                else:
                    res.append((i, None, 0.0))
                    res.append((None, j, 0.0))
            elif i is not None:
                res.append((i, None, 0.0))
            else:
                res.append((None, j, 0.0))
        return res

    S = [[0.0] * (m + 1) for _ in range(n + 1)]
    M = [[0.0] * m for _ in range(n)]
    for i in range(1, n + 1):
        Si, Sp, Mi = S[i], S[i - 1], M[i - 1]
        for j in range(1, m + 1):
            s = sim(A[i - 1], B[j - 1])
            Mi[j - 1] = s
            best = Sp[j] if Sp[j] >= Si[j - 1] else Si[j - 1]
            if s >= threshold and Sp[j - 1] + s > best:
                best = Sp[j - 1] + s
            Si[j] = best
    # traceback (대각선 우선)
    i, j = n, m
    rev: list[AlignPair] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            s = M[i - 1][j - 1]
            if s >= threshold and abs(S[i][j] - (S[i - 1][j - 1] + s)) < 1e-9:
                rev.append((A[i - 1], B[j - 1], s))
                i -= 1
                j -= 1
                continue
        if i > 0 and (j == 0 or abs(S[i][j] - S[i - 1][j]) < 1e-9):
            rev.append((A[i - 1], None, 0.0))
            i -= 1
        else:
            rev.append((None, B[j - 1], 0.0))
            j -= 1
    rev.reverse()
    return rev


def longest_increasing_subsequence(seq: Sequence[int]) -> set[int]:
    """seq 의 LIS 에 속하는 인덱스 집합 (순서 이동 판별용)."""
    import bisect

    if not seq:
        return set()
    tails: list[int] = []
    tails_idx: list[int] = []
    prev = [-1] * len(seq)
    for idx, v in enumerate(seq):
        pos = bisect.bisect_left(tails, v)
        if pos == len(tails):
            tails.append(v)
            tails_idx.append(idx)
        else:
            tails[pos] = v
            tails_idx[pos] = idx
        prev[idx] = tails_idx[pos - 1] if pos > 0 else -1
    res = set()
    k = tails_idx[-1]
    while k != -1:
        res.add(k)
        k = prev[k]
    return res
