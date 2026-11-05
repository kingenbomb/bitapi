// Exercise the production chart's scales, gaps and model selection without network calls.
const fs = require("fs"), vm = require("vm"), path = require("path"), assert = require("assert/strict");
const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(path.join(root, "static/portal-shared.js"), "utf8");
const start = source.indexOf("  const ModelCachePanel = {");
const end = source.indexOf("\n  };", start) + "\n  };".length;
const mounted = [];
let resize;
const context = { console, ref: value => ({ value }), computed: getter => ({ get value() { return getter(); } }),
  watch() {}, onMounted(fn) { mounted.push(fn); }, onUnmounted() {}, h() {},
  ResizeObserver: class { constructor(callback) { resize = callback; } observe() {} disconnect() {} },
  nf: n => String(n), kf: n => String(n), fmt: (n, digits) => n.toFixed(digits),
  usd: n => "$" + n.toFixed(4), modelChip() {}, api() { throw Error("Unexpected network call"); } };
vm.createContext(context);
vm.runInContext(source.slice(start, end) + "\nthis.panel = ModelCachePanel;", context);
const view = context.panel.setup({ scope: "site", defaultRange: "week" });
const day = { date: "2026-11-04", requests: 1, input_tokens: 83280, output_tokens: 2580,
  cache_write_tokens: 0, cache_read_tokens: 119300, total_input_tokens: 202580, cache_tokens: 119300,
  cache_write_rows: 1, cache_read_rows: 1, cache_rows: 1, actual_cost: .0019, cost: .192, cache_rate: 99 };
const gap = { ...day, date: "2026-11-05", requests: 0 };
const partial = { ...day, date: "2026-11-06", cache_write_tokens: null, cache_write_rows: 0 };
view.trend.value = { series: [day, gap, partial], summary: { requests: 2 } };
let plot = view.plot.value;
assert.equal(plot.lines.length, 5);
assert.equal(new Set(plot.lines.map(line => line.color)).size, 5);
assert.equal(plot.lines.find(line => line.key.endsWith(":cache_write_tokens")).points.length, 1,
  "missing writes must leave a gap, while an explicit zero remains a point");
assert.equal(plot.lines.find(line => line.key.endsWith(":input_tokens")).paths.length, 2,
  "days without requests must break the line");
const read = plot.lines.find(line => line.key.endsWith(":cache_read_tokens")).points[0];
const hit = plot.lines.find(line => line.key.endsWith(":cache_hit_rate")).points[0];
assert.equal(read.y, 220 - 119300 / 120000 * 198);
assert.equal(hit.y, 220 - 119300 / 202580 * 198,
  "cache hits use the percentage axis and the reported read counter");
assert.equal(view.metricText(day, view.metricDefs[4]), "58.9%");
assert.equal(view.metricText(partial, view.metricDefs[2]), "未提供");
assert.equal(view.metricText({ ...day, cache_tokens: null }, view.metricDefs[4]), "未提供");
assert.equal(view.metricText({ ...day, total_input_tokens: 100 }, view.metricDefs[4]), "未提供");
assert.equal(view.money(null), "未提供");
assert.equal(view.money(0), "$0.0000");
view.toggleMetric("cache_read_tokens");
assert.equal(view.plot.value.lines.length, 4);
view.toggleMetric("cache_read_tokens");
view.trend.value = { ...view.trend.value, model_series: [
  { model: "model-a", series: [day, gap, partial], summary: { requests: 2 } },
  { model: "model-b", series: [day, gap, partial], summary: { requests: 2 } } ] };
plot = view.plot.value;
assert.equal(plot.lines.length, 10);
assert.notEqual(plot.lines[0].dash, plot.lines[5].dash);
view.trendRoot.value = {};
for (const mount of mounted) if (mount !== view.load) mount();
resize([{ contentRect: { width: 420 } }]);
plot = view.plot.value;
assert.equal(plot.width, 420);
assert.equal(plot.right, 356);
assert(plot.items.every(point => point.x >= plot.left && point.x <= plot.right));
assert.equal(view.metricText(day, view.metricDefs[4]), "58.9%", "resizing must not change the data");
// Compile the actual Vue template to catch invalid directives and nesting.
const compiler = vm.createContext({ console });
vm.runInContext(fs.readFileSync(path.join(root, "static/vue.global.prod.js"), "utf8"), compiler);
compiler.Vue.compile(context.panel.template, { decodeEntities: text => text, onError(error) { throw error; } });
console.log("Five metrics, independent axes, missing data, model comparison and Vue template passed.");
