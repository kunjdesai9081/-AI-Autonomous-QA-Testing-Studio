import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main():
    print("=" * 70)
    print("AUTOMATED REGRESSION & PERFORMANCE BENCHMARK RUNNER")
    print("=" * 70)

    start_time = time.perf_counter()
    loader = unittest.TestLoader()
    suite = loader.discover("tests", top_level_dir=PROJECT_ROOT)

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    elapsed = time.perf_counter() - start_time

    print("\n" + "=" * 70)
    print(f"SUMMARY: {result.testsRun} tests executed in {elapsed:.3f}s")
    print(f"Passed:   {result.testsRun - len(result.failures) - len(result.errors)}")
    print(f"Failed:   {len(result.failures)}")
    print(f"Errors:   {len(result.errors)}")
    print("=" * 70)

    if not result.wasSuccessful():
        sys.exit(1)
    else:
        print("ALL TESTS & BENCHMARKS PASSED CLEANLY!")
        sys.exit(0)


if __name__ == "__main__":
    main()
