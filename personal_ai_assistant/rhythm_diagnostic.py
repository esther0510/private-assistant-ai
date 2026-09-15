"""Local, observational A/B diagnostics. No input hooks, OCR or game injection."""
from __future__ import annotations

import csv
import json
import math
import statistics as st
from datetime import datetime, timezone
from pathlib import Path


def average(values):
    return st.mean(values) if values else None


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lo = int(index)
    return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)


def correlation(xs, ys):
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    mx, my = st.mean(xs), st.mean(ys)
    return sum((x-mx)*(y-my) for x, y in zip(xs, ys)) / math.sqrt(
        sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys))


def read_samples(path):
    """Explicit per-run CSV contract; absent columns remain unavailable."""
    result = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        allowed = {"frametime_ms", "offset_ms", "keydown_timestamp_ms"}
        if not allowed.intersection(reader.fieldnames or []):
            raise ValueError("CSV 需要 frametime_ms、offset_ms 或 keydown_timestamp_ms 欄位")
        for row in reader:
            for key in allowed:
                if row.get(key, "").strip():
                    value = float(row[key])
                    if not math.isfinite(value) or (key != "offset_ms" and value < 0):
                        raise ValueError("CSV 含無效數值")
                    if key == "frametime_ms" and value == 0:
                        raise ValueError("frametime 必須大於 0")
                    result.setdefault(key, []).append(value)
    keys = result.get("keydown_timestamp_ms", [])
    if any(b < a for a, b in zip(keys, keys[1:])):
        raise ValueError("keydown timestamp 必須依時間排序")
    result["source"] = str(Path(path).name)
    return result


def validate_metrics(metrics):
    for key, value in metrics.items():
        if value is None:
            continue
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} 必須是有限數字")
        if key != "timing_bias_ms" and value < 0:
            raise ValueError(f"{key} 不可小於 0")
        if key == "accuracy" and value > 100:
            raise ValueError("accuracy 使用 0–100 百分比")
        if key in {"perfect", "great", "good", "miss", "fast", "slow", "total_notes"} and value != int(value):
            raise ValueError(f"{key} 必須是整數")
    total = metrics.get("total_notes")
    miss = metrics.get("miss")
    if total is not None and (total <= 0 or (miss is not None and miss > total)):
        raise ValueError("總判定數必須大於 0 且不小於 Miss")


def run_metrics(run):
    m = dict(run["metrics"])
    samples = run.get("samples", {})
    frames = samples.get("frametime_ms", [])
    offsets = samples.get("offset_ms", [])
    keys = samples.get("keydown_timestamp_ms", [])
    if frames:
        m["frametime_p99_ms"] = percentile(frames, .99)
        m["fps"] = 1000 / st.mean(frames)
        # Fixed per-session threshold, never infer dropped frames from timer ticks.
        threshold = run.get("spike_threshold_ms", 25)
        m["spike_rate"] = 100 * sum(x > threshold for x in frames) / len(frames)
    if offsets:
        m["timing_bias_ms"] = st.mean(offsets)
        m["timing_sd_ms"] = st.stdev(offsets) if len(offsets) > 1 else None
    if len(keys) >= 3:
        m["interval_jitter_ms"] = st.stdev([b-a for a, b in zip(keys, keys[1:])])
    if m.get("miss") is not None and m.get("total_notes"):
        m["miss_rate"] = 100 * m["miss"] / m["total_notes"]
    return m


DISPLAY_KEYS = ("display_px", "client_px", "dpi_percent", "refresh_hz", "presentation", "monitor")


def aggregate(runs):
    result = {}
    for setting in ("A", "B"):
        selected = [r for r in runs if r["setting"] == setting]
        metrics = [run_metrics(r) for r in selected]
        keys = set().union(*(m.keys() for m in metrics)) if metrics else set()
        result[setting] = {"count": len(selected), "metrics": {}}
        for key in sorted(keys):
            values = [m[key] for m in metrics if m.get(key) is not None]
            result[setting]["metrics"][key] = {"n": len(values), "mean": average(values),
                "sd": st.stdev(values) if len(values) > 1 else 0}
    return result


def analyze(session):
    runs = session["runs"]
    groups = aggregate(runs)
    evidence, warnings = [], []
    complete = all(groups[s]["count"] >= 3 for s in ("A", "B"))
    a, b = groups["A"]["metrics"], groups["B"]["metrics"]
    comparable = []
    for key in sorted(set(a) & set(b)):
        if a[key]["n"] and b[key]["n"]:
            evidence.append(f"{key}: A {a[key]['mean']:.2f} (n={a[key]['n']}), B {b[key]['mean']:.2f} (n={b[key]['n']})")
            if min(a[key]["n"], b[key]["n"]) >= 3:
                comparable.append(key)
    for setting in ("A", "B"):
        selected = [r for r in runs if r["setting"] == setting]
        if selected:
            env = selected[0].get("environment", {})
            evidence.append(f"{setting} 顯示：螢幕 {env.get('display_px', '未取得')}；遊戲 client {env.get('client_px', '未取得')}；"
                            f"{env.get('refresh_hz', '未取得')} Hz；DPI {env.get('dpi_percent', '未取得')}%；"
                            f"模式 {env.get('declared_presentation', env.get('presentation', '未取得'))}")
        signatures = {json.dumps({k: r.get("environment", {}).get(k) for k in (*DISPLAY_KEYS, "declared_presentation")}, sort_keys=True)
                      for r in runs if r["setting"] == setting}
        if len(signatures) > 1:
            warnings.append(f"{setting} 組內顯示環境有變動，請拆成一致設定重測")
    if any(r.get("environment_changed") for r in runs):
        warnings.append("有局在記錄中變更顯示設定，請重測")
    if len({r.get("chart") for r in runs}) > 1:
        warnings.append("譜面不一致")
    if not session.get("controls_confirmed"):
        warnings.append("尚未確認音訊、offset、難度、速度及輸入設備等其他條件保持一致")

    thresholds = {"accuracy": .5, "miss_rate": .5, "timing_bias_ms": 5, "timing_sd_ms": 5}
    effects = []
    uncertain = []
    for key, threshold in thresholds.items():
        if key not in comparable:
            continue
        delta = b[key]["mean"] - a[key]["mean"]
        if key == "timing_bias_ms":
            delta = abs(b[key]["mean"]) - abs(a[key]["mean"])
        noise = 2 * math.sqrt(a[key]["sd"]**2 / a[key]["n"] + b[key]["sd"]**2 / b[key]["n"])
        if noise > threshold:
            uncertain.append(key)
        if abs(delta) > max(threshold, noise):
            worse = (delta < 0) if key == "accuracy" else (abs(b[key]["mean"]) > abs(a[key]["mean"]))
            effects.append((key, "B" if worse else "A"))

    paired = [(run_metrics(r).get("spike_rate"), run_metrics(r).get("accuracy")) for r in runs]
    paired = [(x, 100-y) for x, y in paired if x is not None and y is not None]
    corr = correlation([x for x, y in paired], [y for x, y in paired])
    if corr is not None:
        evidence.append(f"每局 frametime spike rate 與 accuracy 損失相關 r={corr:.2f}；非逐按鍵時間同步證明")
    frame_support = False
    controlled = not warnings
    if "frametime_p99_ms" in comparable:
        delta = b["frametime_p99_ms"]["mean"] - a["frametime_p99_ms"]["mean"]
        evidence.append(f"B − A frametime P99：{delta:+.2f} ms")
        frame_support = abs(delta) >= 3 and any(worse == ("B" if delta > 0 else "A") for _, worse in effects)
    else:
        warnings.append("每組尚未有 3 局可比較的 frametime P99，不能排除未量測的幀時間問題。")
    formal = complete and bool(set(thresholds) & set(comparable)) and controlled
    high_sd = "timing_sd_ms" in comparable and min(a["timing_sd_ms"]["mean"], b["timing_sd_ms"]["mean"]) >= 20
    if not formal:
        verdict = "暫時趨勢：資料不足或條件未控制，尚不下正式結論"
        confidence = 0
    elif effects:
        verdict = "系統/顯示因素較可疑" if frame_support else "設定相關差異；原因仍待確認"
        confidence = min(85, 55 + (15 if frame_support else 0) + (10 if corr is not None and corr >= .7 else 0))
    elif high_sd:
        verdict = "非解析度主因（目前資料）；打點穩定度/個人 offset 較可疑"
        confidence = 65
    elif uncertain:
        verdict = "結果波動過大，尚無法區分設定影響與個人波動；請增加交錯測試局數"
        confidence = 30
    else:
        verdict = "非解析度主因（目前未見明顯設定差異）；個人因素證據仍不足"
        confidence = 50
    warnings.extend(["信心分數是啟發式證據完整度，非因果機率或統計顯著性。",
                     "解析度不等於輸入延遲；按鍵間隔 jitter 受譜面節奏影響，不能代表打點誤差。",
                     "3 局是最低門檻；建議交錯 A/B 至少各 5–10 局，避免暖身與疲勞混淆。"])
    return {"formal": formal, "verdict": verdict, "confidence": confidence, "groups": groups,
            "evidence": evidence, "warnings": warnings, "spike_correlation": corr}


def report_text(session):
    report = analyze(session)
    return "\n".join(["音遊診斷 / Rhythm Game Diagnostic", report["verdict"],
        f"信心分數：{report['confidence']}/100", f"譜面：{session.get('chart', '')}",
        f"A（{session.get('settings', {}).get('A', '')}）：{report['groups']['A']['count']} 局；B（{session.get('settings', {}).get('B', '')}）：{report['groups']['B']['count']} 局",
        "證據：", *report["evidence"], "限制：", *report["warnings"],
        "自動監測不包含 FPS、GPU load、掉幀與遊戲 keydown；缺值不視為零。"])


def save_session(path, session):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(session, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def timestamp():
    return datetime.now(timezone.utc).isoformat()
