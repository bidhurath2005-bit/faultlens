import { describe, expect, it } from "vitest";
import { mergeSeries } from "../components/charts";
import { fmtBytes, fmtMetric, fmtMs, fmtPct, relTime } from "../lib/format";
import { layout } from "../pages/Graph";
import { buildRows } from "../pages/Traces";
import type { Span } from "../lib/types";

describe("formatters", () => {
  it("formats durations, ratios and bytes", () => {
    expect(fmtMs(0.0123)).toBe("12.3 ms");
    expect(fmtMs(2.5)).toBe("2500 ms");
    expect(fmtMs(null)).toBe("—");
    expect(fmtPct(0.0512, 1)).toBe("5.1%");
    expect(fmtBytes(110 * 1024 ** 2)).toBe("110 MB");
    expect(fmtMetric("db_pool_util", 1)).toBe("100.0%");
    expect(fmtMetric("rps", 12.34)).toBe("12.3/s");
    expect(relTime(new Date(Date.now() - 90_000).toISOString())).toBe("2m ago");
  });
});

describe("chart data", () => {
  it("merges per-series points into rows by timestamp", () => {
    const rows = mergeSeries({ a: [[1, 10], [2, 20]], b: [[2, 5], [3, 6]] });
    expect(rows).toEqual([{ t: 1, a: 10 }, { t: 2, a: 20, b: 5 }, { t: 3, b: 6 }]);
  });
});

describe("dependency graph layout", () => {
  it("layers nodes by observed call depth", () => {
    const e = (source: string, target: string) => ({ source, target, rps: 1, error_rate: 0, p95_ms: 1 });
    const pos = layout(["traffic-generator", "order-service", "payment-service"],
      [e("traffic-generator", "order-service"), e("order-service", "payment-service")]);
    expect(pos["traffic-generator"].x).toBe(0);
    expect(pos["order-service"].x).toBe(300);
    expect(pos["payment-service"].x).toBe(600);
  });
});

describe("trace waterfall", () => {
  const s = (id: string, parent: string | null, start: number): Span => ({
    trace_id: "t", span_id: id, parent_span_id: parent, service: "svc", name: id, kind: "server", start_time: "",
    start_ns: start, duration_ms: 1, status: "ok", status_message: null, attributes: {},
  });
  it("orders spans depth-first by start time; orphans become roots", () => {
    const rows = buildRows([s("c", "a", 3), s("a", null, 1), s("b", "a", 2), s("orphan", "missing", 0)]);
    expect(rows.map((r) => [r.span.span_id, r.depth])).toEqual([["orphan", 0], ["a", 0], ["b", 1], ["c", 1]]);
  });
});
