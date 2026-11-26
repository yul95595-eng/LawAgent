"""轻量指标注册表 —— Prometheus 文本格式，零第三方依赖。

为什么不直接上 prometheus-client：
- 少一个依赖，`pip install -r requirements.txt` 之后零配置就能跑；
- 需要的只有 counter / histogram / gauge 三种最基础的类型，自己实现
  80 行就能覆盖，且 Prometheus 抓取格式是稳定的公开协议（text/plain v0.0.4）。

口径约定：
- 指标名统一 `lawagent_` 前缀，单位进名字（`_seconds` / `_total`）；
- 标签值只放**有限枚举**（method / status / agent / node / intent），
  绝不放 user_id、thread_id 这种高基数维度 —— 否则 Prometheus 直接被打爆；
- HTTP 路由标签用 FastAPI 的 route.path（`/api/v1/reviews/{rid}/export`），
  不是真实 URL，否则每个 review_id 都会生成一条时序。
"""
from __future__ import annotations

import threading

DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

_lock = threading.Lock()
# key = (name, labels_key) → {"type", "help", "value"/"buckets"/"sum"/"count"}
_series: dict[tuple[str, tuple], dict] = {}


def _lk(labels: dict | None) -> tuple:
    return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


def _fmt_labels(labels: tuple) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_esc(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _get(name: str, labels: dict | None, type_: str, help_text: str) -> dict:
    key = (name, _lk(labels))
    s = _series.get(key)
    if s is None:
        s = {"type": type_, "help": help_text, "labels": _lk(labels)}
        if type_ == "histogram":
            s.update({"buckets": list(DEFAULT_BUCKETS), "counts": [0] * len(DEFAULT_BUCKETS),
                      "sum": 0.0, "count": 0})
        else:
            s["value"] = 0.0
        _series[key] = s
    return s


def inc(name: str, labels: dict | None = None, value: float = 1.0, help_text: str = "") -> None:
    with _lock:
        _get(name, labels, "counter", help_text)["value"] += value


def set_gauge(name: str, value: float, labels: dict | None = None, help_text: str = "") -> None:
    with _lock:
        _get(name, labels, "gauge", help_text)["value"] = float(value)


def add_gauge(name: str, delta: float, labels: dict | None = None, help_text: str = "") -> None:
    with _lock:
        _get(name, labels, "gauge", help_text)["value"] += float(delta)


def observe(name: str, value: float, labels: dict | None = None, help_text: str = "") -> None:
    with _lock:
        s = _get(name, labels, "histogram", help_text)
        v = float(value)
        s["sum"] += v
        s["count"] += 1
        for i, b in enumerate(s["buckets"]):
            if v <= b:
                s["counts"][i] += 1


def render() -> str:
    """输出 Prometheus 抓取格式（按指标名分组，同组共用一个 HELP/TYPE）。"""
    with _lock:
        items = [(k[0], k[1], dict(v)) for k, v in _series.items()]
    grouped: dict[str, list[tuple[tuple, dict]]] = {}
    for name, labels, s in items:
        grouped.setdefault(name, []).append((labels, s))

    lines: list[str] = []
    for name in sorted(grouped):
        series = sorted(grouped[name], key=lambda x: x[0])
        type_ = series[0][1]["type"]
        help_text = series[0][1]["help"] or name
        lines.append(f"# HELP {name} {_esc(help_text)}")
        lines.append(f"# TYPE {name} {type_}")
        for labels, s in series:
            if type_ == "histogram":
                for b, c in zip(s["buckets"], s["counts"]):
                    lb = tuple(sorted(labels + (("le", str(b)),)))
                    lines.append(f"{name}_bucket{_fmt_labels(lb)} {c}")
                lb = tuple(sorted(labels + (("le", "+Inf"),)))
                lines.append(f"{name}_bucket{_fmt_labels(lb)} {s['count']}")
                lines.append(f"{name}_sum{_fmt_labels(labels)} {s['sum']:.6f}")
                lines.append(f"{name}_count{_fmt_labels(labels)} {s['count']}")
            else:
                lines.append(f"{name}{_fmt_labels(labels)} {s['value']:g}")
    return "\n".join(lines) + ("\n" if lines else "")


def reset() -> None:
    """测试隔离用。"""
    with _lock:
        _series.clear()
