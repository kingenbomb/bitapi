// Execute production normalization without a browser or generated screenshots.
const fs = require("fs");
const vm = require("vm");
const assert = require("assert/strict");
const path = require("path");
const source = fs.readFileSync(path.join(__dirname, "../static/portal-shared.js"), "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf("  function normUsage("), source.indexOf("  const tpsOf =")), context);
const row = { input_tokens: 179, output_tokens: 19, pricing_snapshot: { total_ctx: 32179, cache_read_tokens: 32000, cache_write_tokens: 0 } };
const old = context.normUsage(row);
assert.equal(old.cacheRead, 32000);
assert.equal(old.cacheWrite, null, "old default zero must not pretend to be a reported write counter");
assert.equal(old.cache, 32000);
for (const write of [0, 100, null]) {
  const current = context.normUsage({ ...row, pricing_snapshot: { ...row.pricing_snapshot, cache_usage_version: 2, cache_write_tokens: write } });
  assert.equal(current.cacheWrite, write);
}
assert.equal(context.normUsage({ ...row, pricing_snapshot: { cache_usage_version: 2, cache_read_tokens: null, cache_write_tokens: null } }).cacheRead, null);
const split = context.normUsage({ input_tokens: 1000, output_tokens: 20,
  pricing_snapshot: { cache_usage_version: 3, mode: "token", cache_read_tokens: 4000,
    cache_write_tokens: 7000, cache_write_ttl_valid: true, cache_write_30m_tokens: 3000,
    cache_write_12h_tokens: 4000, cache_write_unclassified_tokens: 0,
    input_price: 1, output_price: 2, cache_read_price: .1, cache_write_price: 1.25,
    cache_write_30m_price: 1.25, cache_write_12h_price: 2.5 } });
assert.equal(split.cacheWrite30m, 3000);
assert.equal(split.cacheWrite12h, 4000);
assert.equal(split.cacheWriteUnclassified, 0);
assert.equal((split.i * split.px.i + split.o * split.px.o + split.cacheRead * split.px.cr +
  split.cacheWrite30m * split.px.cw30 + split.cacheWrite12h * split.px.cw12) / 1e6, .01519);
assert.equal(old.cacheWrite30m, null);
assert.equal(context.normUsage({ ...row, pricing_snapshot: { cache_write_ttl_valid: false } }).cacheWrite12h, null);
console.log("Cache reads/writes, unknown counters and frozen 30m/12h billing display passed.");
