"""프레젠테이션(PPT) diff + 무결성 검증.

슬라이드 정렬: 제목/본문 유사도 + slide_id 일치 가점, 순서 보존 DP → 남은 슬라이드는 '이동' 후보 매칭
도형 정렬: 같은 종류끼리 텍스트 유사도 + 위치 + shape_id/이름/플레이스홀더 일치 점수로 탐욕적 매칭
문단 정렬: 순서 보존 DP (문단 추가/삭제/수정)
표: 엑셀과 동일한 grid diff 엔진 재사용
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Optional

from ..extractors.pptx import Para, PresentationSnap, ShapeSnap, SlideSnap
from ..models import Anchor, ArtifactType, Change, ChangeType, IntegrityReport, Location
from . import normalize as N
from .align import align_sequences, text_sim
from .builder import ChangeBuilder
from .grid import diff_grids, verify_grid

SLIDE_THRESHOLD = 0.4
SLIDE_MOVE_THRESHOLD = 0.6
SHAPE_THRESHOLD = 0.35
PARA_THRESHOLD = 0.5


def _short_anchor(text: Optional[str], role: str) -> Optional[Anchor]:
    if not text:
        return None
    t = N.collapse_ws(text)
    if 2 <= len(t) <= 80 and N.parse_number(t) is None:
        return Anchor(text=t, role=role)
    return None


def _slide_sim(a: SlideSnap, b: SlideSnap) -> float:
    ts = text_sim(a.title, b.title) if (a.title or b.title) else 0.5
    ft_a, ft_b = a.full_text[:3000], b.full_text[:3000]
    cs = text_sim(ft_a, ft_b) if (ft_a or ft_b) else 1.0
    idb = 0.15 if a.slide_id == b.slide_id else 0.0
    return min(1.0, 0.35 * ts + 0.5 * cs + idb)


def _geom_sim(a: ShapeSnap, b: ShapeSnap, W: int, H: int) -> float:
    vals = (a.left, a.top, a.width, a.height, b.left, b.top, b.width, b.height)
    if any(v is None for v in vals) or not (W + H):
        return 0.5
    d = (abs(a.left - b.left) + abs(a.top - b.top) + abs(a.width - b.width) + abs(a.height - b.height)) / (W + H)
    return max(0.0, 1.0 - 2.0 * d)


def _shape_score(a: ShapeSnap, b: ShapeSnap, W: int, H: int) -> float:
    if a.kind != b.kind:
        return 0.0
    if a.kind in ("text", "table"):
        ts = text_sim(a.text, b.text) if (a.text or b.text) else 1.0
    elif a.kind == "picture":
        ts = 1.0 if a.image_sha1 == b.image_sha1 else 0.3
    elif a.kind == "chart":
        ts = 1.0 if a.chart_sig == b.chart_sig else 0.5
    else:
        ts = 0.5
    score = 0.45 * ts + 0.15 * _geom_sim(a, b, W, H)
    if a.key == b.key:
        score += 0.3
    if a.name == b.name:
        score += 0.1
    if a.placeholder and a.placeholder == b.placeholder:
        score += 0.1
    return min(1.0, score)


def _word_diff(a: str, b: str) -> dict:
    ta, tb = a.split(), b.split()
    sm = SequenceMatcher(None, ta, tb, autojunk=False)
    ins, dele = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            dele.append(" ".join(ta[i1:i2]))
        if op in ("replace", "insert"):
            ins.append(" ".join(tb[j1:j2]))
    return {"inserted": ins[:20], "deleted": dele[:20]}


class _Ctx:
    def __init__(self, base: PresentationSnap, rev: PresentationSnap, builder: ChangeBuilder):
        self.base, self.rev, self.builder = base, rev, builder
        self.W = rev.slide_width or base.slide_width
        self.H = rev.slide_height or base.slide_height

    def loc(self, *, container, path, part, rs: Optional[SlideSnap], bs: Optional[SlideSnap], shape_name=None, cell=None, row_label=None, column_label=None) -> Location:
        s = rs or bs
        return Location(
            artifact=ArtifactType.PPTX,
            container=container,
            path=path,
            part=part,
            slide_index=s.index if s else None,
            base_slide_index=bs.index if bs else None,
            slide_title=s.title if s else None,
            shape_name=shape_name,
            cell=cell,
            row_label=row_label,
            column_label=column_label,
        )


def _slide_label(s: SlideSnap) -> str:
    return f"슬라이드 {s.index} '{N.shorten(s.title, 40)}'"


def diff_paragraphs(
    ctx: _Ctx,
    base_paras: list[Para],
    rev_paras: list[Para],
    *,
    container: str,
    path_prefix: str,
    part: str,
    rs: SlideSnap,
    bs: SlideSnap,
    shape_name: Optional[str],
    anchors: list[Anchor],
    is_title: bool = False,
) -> list[tuple[int, int]]:
    al = align_sequences(
        len(base_paras),
        len(rev_paras),
        lambda i, j: text_sim(base_paras[i][0], rev_paras[j][0]),
        PARA_THRESHOLD,
        key_a=[p for p in base_paras],
        key_b=[p for p in rev_paras],
    )
    pairs: list[tuple[int, int]] = []
    removed: list[int] = []
    for i, j, s in al:
        if i is not None and j is not None:
            pairs.append((i, j))
            (bt, bl), (rt, rl) = base_paras[i], rev_paras[j]
            if bt == rt and bl == rl:
                continue
            kind = N.compare(N.canon(bt), N.canon(rt))
            fmt = kind in (None, "format")
            extra = [a for a in (_short_anchor(bt, "section" if is_title else "value"), _short_anchor(rt, "new_section" if is_title else "new_value")) if a]
            detail = {
                "container": container,
                "base_idx": i,
                "rev_idx": j,
                "before_text": bt,
                "after_text": rt,
                "before_level": bl,
                "after_level": rl,
            }
            if bt != rt:
                detail.update(_word_diff(bt, rt))
                nb, na = N.numbers_in_text(bt), N.numbers_in_text(rt)
                if nb != na:
                    detail["numbers_before"], detail["numbers_after"] = nb, na
            ctx.builder.add(
                ChangeType.TEXT_MODIFIED,
                ctx.loc(container=container, path=f"{path_prefix} > 문단 {j + 1}", part="title" if is_title else part, rs=rs, bs=bs, shape_name=shape_name),
                before=bt,
                after=rt,
                format_only=fmt,
                pairing="exact" if s >= 0.9999 else "similarity",
                pairing_score=s,
                detail=detail,
                anchors=anchors + extra,
            )
        elif j is not None:
            rt, rl = rev_paras[j]
            ctx.builder.add(
                ChangeType.TEXT_ADDED,
                ctx.loc(container=container, path=f"{path_prefix} > 문단 {j + 1}", part=part, rs=rs, bs=bs, shape_name=shape_name),
                after=rt,
                detail={"container": container, "rev_idx": j, "text": rt, "level": rl},
                anchors=anchors + [a for a in (_short_anchor(rt, "new_value"),) if a],
            )
        else:
            removed.append(i)
    for i in removed:
        bt, bl = base_paras[i]
        ctx.builder.add(
            ChangeType.TEXT_REMOVED,
            ctx.loc(container=container, path=f"{path_prefix} > (원본) 문단 {i + 1}", part=part, rs=rs, bs=bs, shape_name=shape_name),
            before=bt,
            detail={"container": container, "base_idx": i, "text": bt, "level": bl},
            anchors=anchors + [a for a in (_short_anchor(bt, "value"),) if a],
        )
    return pairs


def _table_loc_factory(ctx: _Ctx, rs: SlideSnap, bs: SlideSnap, shape: ShapeSnap, container: str):
    def make_loc(*, part, base_rc, rev_rc, row_label, col_label) -> Location:
        rc = rev_rc if rev_rc is not None else base_rc
        if rc and rc[0] is not None and rc[1] is not None:
            cell = f"R{rc[0]}C{rc[1]}"
        elif rc and rc[1] is not None:
            cell = f"{rc[1]}열"
        elif rc and rc[0] is not None:
            cell = f"{rc[0]}행"
        else:
            cell = None
        path = f"{_slide_label(rs)} > {shape.name} 표 {cell or ''} [{col_label or '-'} / {row_label or '-'}]"
        return ctx.loc(container=container, path=path, part=f"table_{part}", rs=rs, bs=bs, shape_name=shape.name, cell=cell, row_label=row_label, column_label=col_label)

    return make_loc


def diff_presentations(base: PresentationSnap, rev: PresentationSnap, builder: ChangeBuilder, *, compare_geometry: bool = True) -> tuple[dict, IntegrityReport]:
    ctx = _Ctx(base, rev, builder)
    B, R = base.slides, rev.slides
    al = align_sequences(
        len(B),
        len(R),
        lambda i, j: _slide_sim(B[i], R[j]),
        SLIDE_THRESHOLD,
        key_a=[(s.title, s.full_text, tuple(s.notes)) for s in B],
        key_b=[(s.title, s.full_text, tuple(s.notes)) for s in R],
    )
    pairs: list[tuple[int, int, float, str]] = [(i, j, s, "order") for i, j, s in al if i is not None and j is not None]
    un_b = [i for i, j, _ in al if j is None]
    un_r = [j for i, j, _ in al if i is None]
    # 순서가 바뀐 슬라이드
    cands = sorted(((_slide_sim(B[i], R[j]), i, j) for i in un_b for j in un_r), reverse=True)
    used_b, used_r = set(), set()
    for s, i, j in cands:
        if s < SLIDE_MOVE_THRESHOLD or i in used_b or j in used_r:
            continue
        used_b.add(i)
        used_r.add(j)
        pairs.append((i, j, s, "moved"))
    pairs.sort(key=lambda p: p[1])
    paired_b = {p[0] for p in pairs}
    paired_r = {p[1] for p in pairs}

    slide_align_out = []
    integrity_jobs = []  # (kind, payload)

    base_titles = [s.title for s in B]
    for i, j, s, how in pairs:
        bs, rs = B[i], R[j]
        sid_prefix = f"slide[{rs.slide_id}]"
        sec = _short_anchor(rs.title, "section")
        sec_anchors = [sec] if sec else []
        if how == "moved":
            builder.add(
                ChangeType.SLIDE_MOVED,
                ctx.loc(container=sid_prefix, path=f"{_slide_label(rs)} (원본 {bs.index}번 → {rs.index}번)", part="slide", rs=rs, bs=bs),
                before=str(bs.index),
                after=str(rs.index),
                pairing="moved",
                pairing_score=s,
                detail={"base_index": bs.index, "rev_index": rs.index},
                anchors=sec_anchors,
            )
        if bs.layout != rs.layout:
            builder.add(
                ChangeType.LAYOUT_CHANGED,
                ctx.loc(container=sid_prefix, path=_slide_label(rs), part="slide", rs=rs, bs=bs),
                before=bs.layout,
                after=rs.layout,
                format_only=True,
                pairing="similarity",
                pairing_score=s,
                detail={},
                anchors=sec_anchors,
            )
        # ---- 도형 대응
        cand = []
        for bi, a in enumerate(bs.shapes):
            for ri, b in enumerate(rs.shapes):
                sc = _shape_score(a, b, ctx.W, ctx.H)
                if sc >= SHAPE_THRESHOLD:
                    cand.append((sc, bi, ri))
        cand.sort(key=lambda x: (-x[0], x[1], x[2]))
        sp, ub, ur = [], set(), set()
        for sc, bi, ri in cand:
            if bi in ub or ri in ur:
                continue
            ub.add(bi)
            ur.add(ri)
            sp.append((bi, ri, sc))
        sp.sort(key=lambda x: x[1])
        shape_pairs_out = []
        for bi, ri, sc in sp:
            a, b = bs.shapes[bi], rs.shapes[ri]
            container = f"{sid_prefix}/shape[{b.key}]"
            prefix = f"{_slide_label(rs)} > {b.name}"
            is_title = (b.placeholder or "").upper().startswith(("TITLE", "CENTER_TITLE"))
            shape_pairs_out.append({"base": a.key, "rev": b.key, "score": round(sc, 4)})
            if b.kind == "text":
                ppairs = diff_paragraphs(
                    ctx, a.paragraphs, b.paragraphs, container=container + "/text", path_prefix=prefix, part="text",
                    rs=rs, bs=bs, shape_name=b.name, anchors=sec_anchors, is_title=is_title,
                )
                integrity_jobs.append(("para", container + "/text", a.paragraphs, b.paragraphs, ppairs))
            elif b.kind == "table" and a.table is not None and b.table is not None:
                tal = diff_grids(
                    a.table, b.table, builder, _table_loc_factory(ctx, rs, bs, b, container + "/table"),
                    context_anchors=sec_anchors, compare_styles=False,
                )
                integrity_jobs.append(("grid", a.table, b.table, tal))
            elif b.kind == "picture" and a.image_sha1 != b.image_sha1:
                builder.add(
                    ChangeType.IMAGE_REPLACED,
                    ctx.loc(container=container, path=prefix, part="shape", rs=rs, bs=bs, shape_name=b.name),
                    before=a.image_sha1,
                    after=b.image_sha1,
                    pairing="similarity",
                    pairing_score=sc,
                    detail={"container": container},
                    anchors=sec_anchors,
                )
            elif b.kind == "chart" and a.chart_sig != b.chart_sig:
                builder.add(
                    ChangeType.CHART_MODIFIED,
                    ctx.loc(container=container, path=prefix, part="shape", rs=rs, bs=bs, shape_name=b.name),
                    before=N.shorten(str(a.chart_sig), 300),
                    after=N.shorten(str(b.chart_sig), 300),
                    pairing="similarity",
                    pairing_score=sc,
                    detail={"container": container, "before_sig": a.chart_sig, "after_sig": b.chart_sig},
                    anchors=sec_anchors,
                )
            if b.kind in ("picture", "chart"):
                integrity_jobs.append(("blob", container, a, b))
            if compare_geometry and None not in (a.left, a.top, a.width, a.height, b.left, b.top, b.width, b.height):
                moved = abs(a.left - b.left) + abs(a.top - b.top) + abs(a.width - b.width) + abs(a.height - b.height)
                if ctx.W + ctx.H and moved > 0.01 * (ctx.W + ctx.H):
                    builder.add(
                        ChangeType.SHAPE_GEOMETRY_CHANGED,
                        ctx.loc(container=container, path=prefix, part="shape", rs=rs, bs=bs, shape_name=b.name),
                        before=f"({a.left},{a.top},{a.width}x{a.height})",
                        after=f"({b.left},{b.top},{b.width}x{b.height})",
                        format_only=True,
                        pairing="similarity",
                        pairing_score=sc,
                        detail={"container": container},
                        anchors=sec_anchors,
                    )
        added_shapes = [ri for ri in range(len(rs.shapes)) if ri not in ur]
        removed_shapes = [bi for bi in range(len(bs.shapes)) if bi not in ub]
        for ri in added_shapes:
            b = rs.shapes[ri]
            first = b.paragraphs[0][0] if b.paragraphs else None
            builder.add(
                ChangeType.SHAPE_ADDED,
                ctx.loc(container=f"{sid_prefix}/shape[{b.key}]", path=f"{_slide_label(rs)} > {b.name} (신규 {b.kind})", part="shape", rs=rs, bs=bs, shape_name=b.name),
                after=N.shorten(b.text, 2000) or f"[{b.kind}]",
                detail={"slide_id": rs.slide_id, "shape_key": b.key, "content": b.content()},
                anchors=sec_anchors + [a for a in (_short_anchor(first, "new_value"),) if a],
            )
        for bi in removed_shapes:
            a = bs.shapes[bi]
            first = a.paragraphs[0][0] if a.paragraphs else None
            builder.add(
                ChangeType.SHAPE_REMOVED,
                ctx.loc(container=f"{sid_prefix}/shape[{a.key}]", path=f"{_slide_label(rs)} > (원본) {a.name}", part="shape", rs=rs, bs=bs, shape_name=a.name),
                before=N.shorten(a.text, 2000) or f"[{a.kind}]",
                detail={"base_slide_id": bs.slide_id, "shape_key": a.key, "content": a.content()},
                anchors=sec_anchors + [a2 for a2 in (_short_anchor(first, "value"),) if a2],
            )
        integrity_jobs.append(("shapes", bs, rs, sp, added_shapes, removed_shapes))
        # ---- 노트
        npairs = diff_paragraphs(
            ctx, bs.notes, rs.notes, container=f"{sid_prefix}/notes", path_prefix=f"{_slide_label(rs)} > 발표자 노트",
            part="notes", rs=rs, bs=bs, shape_name=None, anchors=sec_anchors,
        )
        integrity_jobs.append(("para", f"{sid_prefix}/notes", bs.notes, rs.notes, npairs))
        slide_align_out.append({"base_index": bs.index, "rev_index": rs.index, "score": round(s, 4), "how": how, "shapes": shape_pairs_out})

    # ---- 추가/삭제 슬라이드
    rev_order = list(range(len(R)))
    for j in rev_order:
        if j in paired_r:
            continue
        rs = R[j]
        left = next((R[k].title for k in range(j - 1, -1, -1) if k in paired_r and R[k].title), None)
        right = next((R[k].title for k in range(j + 1, len(R)) if k in paired_r and R[k].title), None)
        anchors = [a for a in (_short_anchor(rs.title, "new_section"), _short_anchor(left, "neighbor_left"), _short_anchor(right, "neighbor_right")) if a]
        builder.add(
            ChangeType.SLIDE_ADDED,
            ctx.loc(container=f"slide[{rs.slide_id}]", path=f"{_slide_label(rs)} (신규)", part="slide", rs=rs, bs=None),
            after=N.shorten(rs.full_text or rs.title, 3000),
            detail={"slide_id": rs.slide_id, "rev_index": rs.index, "content": rs.content(), "sibling_titles": base_titles},
            anchors=anchors,
        )
    for i in range(len(B)):
        if i in paired_b:
            continue
        bs = B[i]
        builder.add(
            ChangeType.SLIDE_REMOVED,
            ctx.loc(container=f"slide[{bs.slide_id}]", path=f"(원본) {_slide_label(bs)} 삭제", part="slide", rs=None, bs=bs),
            before=N.shorten(bs.full_text or bs.title, 3000),
            detail={"base_slide_id": bs.slide_id, "base_index": bs.index, "content": bs.content()},
            anchors=[a for a in (_short_anchor(bs.title, "section"),) if a],
        )

    integrity = _verify(ctx, pairs, integrity_jobs)
    return {"slides": slide_align_out}, integrity


# --------------------------------------------------------------------------- 무결성 검증
def _verify_paragraphs(cid: str, base_p: list[Para], rev_p: list[Para], pairs, changes: list[Change]) -> tuple[int, list[str]]:
    checks, failures = 1, []
    rec: dict[int, Para] = {j: base_p[i] for i, j in pairs}
    removed = set()
    for ch in changes:
        d = ch.detail
        if d.get("container") != cid:
            continue
        if ch.change_type == ChangeType.TEXT_MODIFIED:
            checks += 1
            if base_p[d["base_idx"]][0] != d["before_text"]:
                failures.append(f"[{cid}] {ch.id}: 보고된 이전 문단이 원본과 다름")
            rec[d["rev_idx"]] = (d["after_text"], d["after_level"])
        elif ch.change_type == ChangeType.TEXT_ADDED:
            rec[d["rev_idx"]] = (d["text"], d["level"])
        elif ch.change_type == ChangeType.TEXT_REMOVED:
            removed.add(d["base_idx"])
            checks += 1
            if base_p[d["base_idx"]][0] != d["text"]:
                failures.append(f"[{cid}] {ch.id}: 삭제 보고 문단이 원본과 다름")
    recon = [rec.get(k) for k in range(len(rev_p))]
    if set(rec) != set(range(len(rev_p))) or recon != list(rev_p):
        failures.append(f"[{cid}] 문단 재구성 불일치")
    paired_base = {i for i, _ in pairs}
    for i in range(len(base_p)):
        if i not in paired_base and i not in removed:
            failures.append(f"[{cid}] 원본 문단 {i + 1} 이 대응도 삭제 보고도 없음")
    return checks, failures


def _verify(ctx: _Ctx, pairs, jobs) -> IntegrityReport:
    changes = ctx.builder.changes
    checks, failures = 0, []
    B, R = ctx.base.slides, ctx.rev.slides
    paired_b = {p[0] for p in pairs}
    paired_r = {p[1] for p in pairs}
    added = {c.detail["slide_id"]: c for c in changes if c.change_type == ChangeType.SLIDE_ADDED}
    removed = {c.detail["base_slide_id"]: c for c in changes if c.change_type == ChangeType.SLIDE_REMOVED}
    for j, rs in enumerate(R):
        checks += 1
        if j in paired_r:
            continue
        ch = added.get(rs.slide_id)
        if ch is None or ch.detail["content"] != rs.content():
            failures.append(f"수정본 {_slide_label(rs)} 이 대응/추가 보고와 불일치")
    for i, bs in enumerate(B):
        checks += 1
        if i in paired_b:
            continue
        ch = removed.get(bs.slide_id)
        if ch is None or ch.detail["content"] != bs.content():
            failures.append(f"원본 {_slide_label(bs)} 이 대응/삭제 보고와 불일치")

    shape_added = {(c.detail["slide_id"], c.detail["shape_key"]): c for c in changes if c.change_type == ChangeType.SHAPE_ADDED}
    shape_removed = {(c.detail["base_slide_id"], c.detail["shape_key"]): c for c in changes if c.change_type == ChangeType.SHAPE_REMOVED}
    blob_changes = {c.detail.get("container") for c in changes if c.change_type in (ChangeType.IMAGE_REPLACED, ChangeType.CHART_MODIFIED)}
    for job in jobs:
        kind = job[0]
        if kind == "para":
            _, cid, bp, rp, pp = job
            c, f = _verify_paragraphs(cid, bp, rp, pp, changes)
        elif kind == "grid":
            _, bg, rg, tal = job
            c, f = verify_grid(bg, rg, tal, changes)
        elif kind == "blob":
            _, cid, a, b = job
            c, f = 1, []
            same = (a.image_sha1 == b.image_sha1) if a.kind == "picture" else (a.chart_sig == b.chart_sig)
            if not same and cid not in blob_changes:
                f.append(f"[{cid}] 이미지/차트 변경이 보고되지 않음")
        else:  # shapes
            _, bs, rs, sp, added_idx, removed_idx = job
            c, f = 0, []
            pr = {ri for _, ri, _ in sp}
            pb = {bi for bi, _, _ in sp}
            for ri, s in enumerate(rs.shapes):
                c += 1
                if ri in pr:
                    continue
                ch = shape_added.get((rs.slide_id, s.key))
                if ch is None or ch.detail["content"] != s.content():
                    f.append(f"{_slide_label(rs)} 도형 '{s.name}' 대응/추가 보고 불일치")
            for bi, s in enumerate(bs.shapes):
                c += 1
                if bi in pb:
                    continue
                ch = shape_removed.get((bs.slide_id, s.key))
                if ch is None or ch.detail["content"] != s.content():
                    f.append(f"{_slide_label(bs)} 원본 도형 '{s.name}' 대응/삭제 보고 불일치")
        checks += c
        failures.extend(f)
    return IntegrityReport(status="PASS" if not failures else "FAIL", checks=checks, failures=failures)
