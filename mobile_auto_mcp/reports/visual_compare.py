"""Built-in, explainable screenshot comparison for completed mobile cases."""

from __future__ import annotations

from collections import OrderedDict
from itertools import combinations
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

from mobile_auto_mcp.state.storage import LocalStore


_PLATFORM_LABELS = {"android": "Android", "ios": "iOS", "harmony": "HarmonyOS"}


def compare_case_runs(
    runs: list[dict[str, Any]],
    *,
    expected_targets: list[str] | None = None,
) -> dict[str, Any]:
    """Compare screenshots as a non-final precheck that always requires review."""
    expected = list(
        dict.fromkeys(
            expected_targets or [str(run.get("target") or "") for run in runs]
        )
    )
    by_target = {str(run.get("target") or ""): run for run in runs}
    missing = [
        target for target in expected if not _valid_evidence_run(by_target.get(target))
    ]
    loaded: dict[str, Image.Image] = {}
    image_errors: dict[str, str] = {}
    for target in expected:
        run = by_target.get(target) or {}
        screenshot = str(run.get("screenshot") or "")
        if target in missing:
            continue
        try:
            with Image.open(screenshot) as source:
                loaded[target] = ImageOps.exif_transpose(source).convert("RGB")
        except (OSError, UnidentifiedImageError) as exc:
            image_errors[target] = str(exc) or exc.__class__.__name__
            missing.append(target)
    missing = list(dict.fromkeys(missing))

    pairwise = [
        _compare_pair(left, right, loaded[left], loaded[right])
        for left, right in combinations(loaded, 2)
    ]
    if missing or len(loaded) < 2:
        precheck_status = "needs_check"
        precheck_reason = "insufficient_evidence"
        labels = "、".join(_label(target) for target in missing) or "至少两端"
        conclusion = (
            f"证据不足：{labels} 缺少可用截图或执行门禁未通过，不能给出跨端一致性结论。"
        )
    elif any(item["status"] == "different" for item in pairwise):
        precheck_status = "different"
        precheck_reason = "pixel_difference_detected"
        different = "、".join(
            f"{_label(item['left'])}-{_label(item['right'])}"
            for item in pairwise
            if item["status"] == "different"
        )
        conclusion = (
            f"传统视觉预检发现显著差异：{different}；该结果不是业务失败结论，"
            "必须由 VLM 或人工语义复核。"
        )
    elif pairwise and all(item["status"] == "similar" for item in pairwise):
        precheck_status = "similar"
        precheck_reason = "no_significant_pixel_difference"
        conclusion = (
            "传统视觉预检未发现显著差异；该结果不是业务通过结论，"
            "仍需 VLM 或人工语义复核。"
        )
    else:
        precheck_status = "needs_check"
        precheck_reason = "metric_gray_zone"
        uncertain = "、".join(
            f"{_label(item['left'])}-{_label(item['right'])}"
            for item in pairwise
            if item["status"] == "needs_check"
        )
        conclusion = f"视觉指标处于灰区：{uncertain}；需要 VLM 或人工进行语义裁决。"
    return {
        "engine": "pillow_visual_v1",
        # 像素和哈希指标只做候选筛查，绝不能把自动预检提升为 passed/failed 最终结论。
        "status": precheck_status,
        "precheck_status": precheck_status,
        "precheck_reason": precheck_reason,
        "final_decision": False,
        "case_conclusion": conclusion,
        "expected_targets": expected,
        "compared_targets": list(loaded),
        "missing_targets": missing,
        "image_errors": image_errors,
        "pairwise": pairwise,
        "thresholds": {
            "similar": {
                "mean_absolute_error_max": 0.08,
                "pixel_difference_ratio_max": 0.18,
                "dhash_distance_max": 12,
            },
            "different": {
                "mean_absolute_error_min": 0.35,
                "pixel_difference_ratio_min": 0.70,
                "dhash_distance_min": 20,
            },
        },
    }


def apply_session_visual_comparison(
    store: LocalStore,
    session_id: str,
    *,
    expected_targets: list[str] | None = None,
) -> dict[str, Any]:
    """Persist built-in metrics as review candidates without making final decisions."""
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for run in reversed(store.list_runs(session_id)):
        grouped.setdefault(str(run.get("rule_id") or run.get("id") or ""), []).append(
            run
        )
    cases: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for rule_id, case_runs in grouped.items():
        comparison = {
            "rule_id": rule_id,
            **compare_case_runs(case_runs, expected_targets=expected_targets),
        }
        cases.append(comparison)
        for run in case_runs:
            if run.get("status") != "pending_review":
                continue
            target = str(run.get("target") or "")
            review = {
                **comparison,
                "target": target,
                "reviewer": "builtin_visual_precheck",
                "manual": False,
                "requires_semantic_review": True,
            }
            # 预检写入独立证据字段，保持 pending_review 状态由后续 VLM/人工最终裁决。
            updated.append(store.update_run_visual_precheck(str(run["id"]), review))
    return {
        "session_id": session_id,
        "engine": "pillow_visual_v1",
        "case_count": len(cases),
        "updated_count": len(updated),
        "cases": cases,
    }


def _valid_evidence_run(run: dict[str, Any] | None) -> bool:
    """Accept only mutable-response evidence anchored to a verified target page."""
    if not run or run.get("status") != "pending_review" or not run.get("screenshot"):
        return False
    gate = run.get("execution_gate") or {}
    page_anchor = gate.get("page_anchor") or {}
    anchor_proven = (
        bool(page_anchor.get("ok"))
        and bool(page_anchor.get("verified"))
        and not bool(page_anchor.get("skipped"))
    )
    return (
        bool(gate.get("change_applied"))
        and anchor_proven
        and Path(str(run["screenshot"])).is_file()
    )


def _compare_pair(
    left: str, right: str, left_image: Image.Image, right_image: Image.Image
) -> dict[str, Any]:
    """Measure normalized full-screen pixel and perceptual-hash differences."""
    width = max(32, min(left_image.width, right_image.width, 360))
    height = max(32, min(left_image.height, right_image.height, 720))
    size = (width, height)
    left_normalized = ImageOps.fit(
        left_image, size, method=Image.Resampling.LANCZOS
    ).convert("L")
    right_normalized = ImageOps.fit(
        right_image, size, method=Image.Resampling.LANCZOS
    ).convert("L")
    difference = ImageChops.difference(left_normalized, right_normalized)
    histogram = difference.histogram()
    pixels = width * height
    mean_absolute_error = sum(
        value * count for value, count in enumerate(histogram)
    ) / (pixels * 255)
    pixel_difference_ratio = (
        sum(count for value, count in enumerate(histogram) if value > 24) / pixels
    )
    hash_distance = (_dhash(left_normalized) ^ _dhash(right_normalized)).bit_count()
    if (
        mean_absolute_error <= 0.08
        and pixel_difference_ratio <= 0.18
        and hash_distance <= 12
    ):
        status = "similar"
    elif mean_absolute_error >= 0.35 or (
        pixel_difference_ratio >= 0.70 and hash_distance >= 20
    ):
        status = "different"
    else:
        status = "needs_check"
    return {
        "left": left,
        "right": right,
        "status": status,
        "normalized_size": [width, height],
        "mean_absolute_error": round(mean_absolute_error, 6),
        "pixel_difference_ratio": round(pixel_difference_ratio, 6),
        "dhash_distance": hash_distance,
    }


def _dhash(image: Image.Image) -> int:
    """Create a compact horizontal-gradient hash for coarse visual comparison."""
    sample = image.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(sample.getdata())
    result = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            result = (result << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return result


def _label(target: str) -> str:
    """Return the report-facing platform label for a target identifier."""
    return _PLATFORM_LABELS.get(target, target or "未知端")
