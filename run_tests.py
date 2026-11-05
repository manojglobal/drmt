#!/usr/bin/env python3
"""
run_tests.py — Automated Unit Test Runner for DRMT V2.
Discovers and executes all unit tests across modules, displaying a structured summary table.
"""

import os
import sys
import time
import unittest
from typing import Dict, List, Any

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

TEST_SUITES = [
    ("Models & Schemas", "tests.test_models"),
    ("Database & CRUD", "tests.test_database"),
    ("Position Normalization", "tests.test_position_manager"),
    ("Broker Adapters", "tests.test_position_adapters"),
    ("Failed Auth & Zero-Pos Safety", "tests.test_failed_auth_adapters"),
    ("Chronos & Blocklist", "tests.test_chronos_blocklist"),
    ("Credentials & Dynamic Loader", "tests.test_credentials_loader"),
    ("Execution & Payloads", "tests.test_execution_payloads"),
    ("API Routes & Auth", "tests.test_api_routes"),
    ("Health & Utilities", "tests.test_health_utils"),
]


def run_all_tests():
    print("\n" + "=" * 78)
    print(" 🧪 DRMT V2 AUTOMATED UNIT TEST SUITE")
    print("=" * 78 + "\n")

    loader = unittest.TestLoader()
    suite_results: List[Dict[str, Any]] = []
    total_start = time.monotonic()
    all_passed = True

    for name, module_path in TEST_SUITES:
        print(f"▶ Running {name} ({module_path})...", end=" ", flush=True)
        try:
            suite = loader.loadTestsFromName(module_path)
            runner = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
            t_start = time.monotonic()
            result = runner.run(suite)
            duration = time.monotonic() - t_start

            passed_count = result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped)
            failed_count = len(result.failures) + len(result.errors)
            skipped_count = len(result.skipped)

            if failed_count > 0:
                all_passed = False
                status_str = f"❌ FAILED ({failed_count} failures)"
            else:
                status_str = f"✅ PASSED ({passed_count}/{result.testsRun})"

            print(f"{status_str} in {duration:.2f}s")

            suite_results.append({
                "name": name,
                "module": module_path,
                "total": result.testsRun,
                "passed": passed_count,
                "failed": failed_count,
                "skipped": skipped_count,
                "duration": duration,
                "success": result.wasSuccessful(),
                "failures_details": result.failures + result.errors,
            })
        except Exception as e:
            all_passed = False
            print(f"❌ LOAD ERROR: {e}")
            suite_results.append({
                "name": name,
                "module": module_path,
                "total": 0,
                "passed": 0,
                "failed": 1,
                "skipped": 0,
                "duration": 0.0,
                "success": False,
                "failures_details": [(module_path, str(e))],
            })

    total_duration = time.monotonic() - total_start

    # Summary Table
    print("\n" + "=" * 78)
    print(" 📊 TEST EXECUTION SUMMARY")
    print("=" * 78)
    print(f"{'Test Suite':<30} | {'Total':<6} | {'Passed':<6} | {'Failed':<6} | {'Time (s)':<8} | {'Status'}")
    print("-" * 78)

    grand_total = sum(r["total"] for r in suite_results)
    grand_passed = sum(r["passed"] for r in suite_results)
    grand_failed = sum(r["failed"] for r in suite_results)

    for r in suite_results:
        status = "PASSED ✅" if r["success"] else "FAILED ❌"
        print(f"{r['name']:<30} | {r['total']:<6} | {r['passed']:<6} | {r['failed']:<6} | {r['duration']:<8.2f} | {status}")

    print("-" * 78)
    overall_status = "ALL TESTS PASSED ✅" if all_passed else "SOME TESTS FAILED ❌"
    print(f"{'TOTALS':<30} | {grand_total:<6} | {grand_passed:<6} | {grand_failed:<6} | {total_duration:<8.2f} | {overall_status}")
    print("=" * 78 + "\n")

    if not all_passed:
        print("❗ Details of failures:")
        for r in suite_results:
            if not r["success"]:
                print(f"\n--- {r['name']} ---")
                for item, detail in r["failures_details"]:
                    print(f"• {item}:\n{detail}\n")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    run_all_tests()
