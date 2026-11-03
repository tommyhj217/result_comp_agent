"""비교쌍 하나(원본 산출물 vs 수정본)를 비교하는 진입점.

* 형식별 처리는 ``documents`` 레지스트리의 핸들러가 담당 (플러그인으로 확장 가능)
* 실패는 ``DocumentError(kind, message)`` 로 종류를 구분해 올린다 → 보고서에 '비교 실패'로 표시
* ``compare_files_isolated`` 는 별도 프로세스에서 비교하고 제한 시간을 넘기면 강제 종료(시간 초과 실패)
"""
from __future__ import annotations

import multiprocessing as mp
import threading
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from ..documents import CompareOptions, DocumentError, handler_for
from ..models import ComparisonResult
from .builder import ChangeBuilder

DEFAULT_TARGET = "result_comp_agent.diff.compare:compare_files"


def _parse_errors() -> tuple[type, ...]:
    """문서 라이브러리의 '파일을 읽을 수 없음' 계열 예외 (그 외 예외는 일반 오류로 구분)."""
    errs: list[type] = [zipfile.BadZipFile, UnicodeDecodeError, PermissionError, IsADirectoryError]
    for mod, name in (
        ("openpyxl.utils.exceptions", "InvalidFileException"),
        ("pptx.exc", "PackageNotFoundError"),
        ("docx.opc.exceptions", "PackageNotFoundError"),
        ("pypdf.errors", "PdfReadError"),
        ("lxml.etree", "XMLSyntaxError"),
    ):
        try:
            import importlib

            errs.append(getattr(importlib.import_module(mod), name))
        except Exception:
            pass
    return tuple(errs)


def detect_artifact(path: str | Path) -> str:
    return handler_for(path).artifact


def compare_files(
    baseline: str | Path,
    revised: str | Path,
    *,
    pair_id: str,
    agent_id: str,
    revised_by: Optional[str] = None,
    baseline_ref: Optional[str] = None,
    revised_ref: Optional[str] = None,
    compare_styles: bool = True,
    compare_geometry: bool = True,
    header_rows: Optional[dict[str, int]] = None,
    key_columns: Optional[dict[str, str]] = None,
) -> ComparisonResult:
    t0 = time.monotonic()
    for label, p in (("원본", baseline), ("수정본", revised)):
        if not Path(p).is_file():
            raise DocumentError("file_not_found", f"{label} 파일이 없습니다: {p}")
    hb, hr = handler_for(baseline), handler_for(revised)
    if hb.name != hr.name:
        raise DocumentError("format_mismatch", f"원본({Path(baseline).suffix})과 수정본({Path(revised).suffix}) 형식이 다릅니다")
    ok, why = hb.available()
    if not ok:
        raise DocumentError("missing_dependency", f"{hb.name} 형식 비교 불가: {why}")
    builder = ChangeBuilder(pair_id, agent_id, hb.artifact)
    opts = CompareOptions(compare_styles, compare_geometry, dict(header_rows or {}), dict(key_columns or {}))
    try:
        out = hb.compare(str(baseline), str(revised), builder, opts)
    except DocumentError:
        raise
    except _parse_errors() as e:
        raise DocumentError("parse_error", f"{hb.name} 문서를 읽지 못했습니다 ({type(e).__name__}: {e}) — 손상/암호화/형식 오류 여부를 확인하세요") from e
    except Exception as e:
        raise DocumentError("error", f"{hb.name} 비교 중 오류 ({type(e).__name__}: {e})") from e
    changes = builder.changes
    stats = {
        "total": len(changes),
        "content": sum(1 for c in changes if not c.format_only),
        "format_only": sum(1 for c in changes if c.format_only),
        "by_type": dict(Counter(c.change_type.value for c in changes)),
        "low_confidence_pairing": sum(1 for c in changes if c.pairing == "similarity" and c.pairing_score < 0.7),
        "handler": hb.name,
    }
    return ComparisonResult(
        pair_id=pair_id, agent_id=agent_id, artifact=hb.artifact,
        baseline_path=str(baseline), revised_path=str(revised),
        baseline_sha256=out.base_sha256, revised_sha256=out.rev_sha256,
        revised_by=revised_by, baseline_ref=baseline_ref, revised_ref=revised_ref,
        changes=changes, alignment=out.alignment, integrity=out.integrity, stats=stats,
        notes=out.notes, seconds=round(time.monotonic() - t0, 3),
    )


# --------------------------------------------------------------------------- 프로세스 격리 + 시간 제한
def _resolve(target: str):
    import importlib

    mod, fn = target.split(":")
    return getattr(importlib.import_module(mod), fn)


def _child(conn, target: str, plugins: list[str], kwargs: dict) -> None:  # pragma: no cover - 자식 프로세스
    try:
        if plugins:
            from ..plugins import load_plugins

            load_plugins(plugins)
        res = _resolve(target)(**kwargs)
        conn.send(("ok", res.model_dump_json()))
    except DocumentError as e:
        conn.send(("err", e.kind, str(e)))
    except BaseException as e:  # noqa: BLE001
        conn.send(("err", "error", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def _mp_context():
    methods = mp.get_all_start_methods()
    if "fork" in methods and threading.active_count() == 1:
        return mp.get_context("fork")
    return mp.get_context("spawn")


def compare_files_isolated(timeout: float, target: str = DEFAULT_TARGET, plugins: Optional[list[str]] = None, **kwargs: Any) -> ComparisonResult:
    """별도 프로세스에서 비교. ``timeout`` 초 안에 끝나지 않으면 프로세스를 종료하고 DocumentError('timeout')."""
    ctx = _mp_context()
    parent, child = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_child, args=(child, target, list(plugins or []), kwargs), daemon=True)
    t0 = time.monotonic()
    p.start()
    child.close()
    try:
        if not parent.poll(timeout):
            p.terminate()
            p.join(5)
            if p.is_alive():  # pragma: no cover
                p.kill()
                p.join(5)
            raise DocumentError("timeout", f"비교가 제한 시간 {timeout:g}초를 초과해 중단했습니다 (문서 크기·복잡도 확인 또는 diff.timeout_seconds 조정)")
        try:
            msg = parent.recv()
        except EOFError:
            p.join(5)
            raise DocumentError("crash", f"비교 프로세스가 결과 없이 종료되었습니다 (exitcode={p.exitcode}, 메모리 부족 가능성)")
    finally:
        parent.close()
    p.join(5)
    if msg[0] == "ok":
        res = ComparisonResult.model_validate_json(msg[1])
        res.seconds = round(time.monotonic() - t0, 3)
        return res
    raise DocumentError(msg[1], msg[2])
