/* Copyright 2026 Mozilla Foundation
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

// Knobs of the walltime variance experiment, set through `.variance/env`.
// With none of them set, the benchmarks run exactly as they do upstream.

function parseFlag(name) {
  const value = process.env[name];
  switch (value) {
    case undefined:
    case "":
    case "0":
    case "false":
      return false;
    case "1":
    case "true":
      return true;
    default:
      throw new Error(`${name} must be 0/1/true/false, got \`${value}\``);
  }
}

function parsePositiveInteger(name) {
  const value = process.env[name];
  if (value === undefined || value === "") {
    return undefined;
  }
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1) {
    throw new Error(`${name} must be a positive integer, got \`${value}\``);
  }
  return number;
}

// Run `global.gc()` after every complete benchmark, outside of its measurement.
// Needs `--expose-gc` (see `vitest.config.mjs`).
const gcBetweenBenchmarks = parseFlag("VARIANCE_GC_BETWEEN_BENCHMARKS");

// Per `bench().run()` minimums (tinybench: both must be reached), replacing
// tinybench's defaults of 1000 ms / 64 measured rounds and 250 ms / 16
// warmup iterations. With forks, they apply to each fork.
const benchRunOptions = {};
const benchTimeMs = parsePositiveInteger("VARIANCE_BENCH_TIME_MS");
if (benchTimeMs !== undefined) {
  benchRunOptions.time = benchTimeMs;
}
const benchIterations = parsePositiveInteger("VARIANCE_BENCH_ITERATIONS");
if (benchIterations !== undefined) {
  benchRunOptions.iterations = benchIterations;
}
const benchWarmupTimeMs = parsePositiveInteger("VARIANCE_BENCH_WARMUP_TIME_MS");
if (benchWarmupTimeMs !== undefined) {
  benchRunOptions.warmupTime = benchWarmupTimeMs;
}
const benchWarmupIterations = parsePositiveInteger(
  "VARIANCE_BENCH_WARMUP_ITERATIONS"
);
if (benchWarmupIterations !== undefined) {
  benchRunOptions.warmupIterations = benchWarmupIterations;
}

export { benchRunOptions, gcBetweenBenchmarks };
