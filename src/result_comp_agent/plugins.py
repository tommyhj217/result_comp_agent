"""플러그인 로더 — 사용자가 코딩한 문서 핸들러 / GPT 도구를 실행 시 등록한다.

지정 방법 (모두 가능, 중복은 1회만 로드)
  * 설정 파일   plugins: [my_company.rca_plugins, ./plugins/hwp_handler.py]
  * 환경 변수   RCA_PLUGINS=my_company.rca_plugins,./plugins/hwp_handler.py
  * 패키지 entry point 그룹 'result_comp_agent.plugins'
플러그인 모듈은 import 시점에 ``register_handler(...)`` / ``@register_tool(...)`` 로 등록한다.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional

_LOADED: dict[str, str] = {}


def _import_path(path: Path):
    name = f"rca_plugin_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"모듈 스펙을 만들 수 없음: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_plugins(specs: Optional[list[str]] = None, base_dir: Optional[str] = None, include_env: bool = True, include_entry_points: bool = True) -> tuple[list[str], list[str]]:
    """반환: (로드된 플러그인 목록, 오류 메시지 목록)"""
    items = list(specs or [])
    if include_env and os.environ.get("RCA_PLUGINS"):
        items += [x.strip() for x in os.environ["RCA_PLUGINS"].split(",") if x.strip()]
    loaded, errors = [], []
    for spec in items:
        key = spec
        try:
            if spec.endswith(".py") or "/" in spec or os.sep in spec:
                p = Path(spec).expanduser()
                if not p.is_absolute() and base_dir:
                    p = Path(base_dir) / p
                key = str(p.resolve())
                if key in _LOADED:
                    loaded.append(spec)
                    continue
                _import_path(p)
            else:
                if key in _LOADED:
                    loaded.append(spec)
                    continue
                importlib.import_module(spec)
            _LOADED[key] = spec
            loaded.append(spec)
        except Exception as e:
            errors.append(f"플러그인 '{spec}' 로드 실패: {type(e).__name__}: {e}")
    if include_entry_points:
        try:
            from importlib.metadata import entry_points

            for ep in entry_points(group="result_comp_agent.plugins"):
                key = f"ep:{ep.value}"
                if key in _LOADED:
                    continue
                try:
                    ep.load()
                    _LOADED[key] = ep.name
                    loaded.append(f"{ep.name} (entry point)")
                except Exception as e:
                    errors.append(f"entry point '{ep.name}' 로드 실패: {e}")
        except Exception:  # pragma: no cover
            pass
    return loaded, errors
