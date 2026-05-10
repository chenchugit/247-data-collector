from pathlib import Path
import subprocess


TEST_FILES = [
    "tests/test_db_smoke.py",
    "tests/test_discovery_smoke.py",
    "tests/test_fetch_smoke.py",
    "tests/test_extract_smoke.py",
    "tests/test_versioning_smoke.py",
    "tests/test_ui_smoke.py",
    "tests/test_analysis_smoke.py",
    "tests/test_runtime_smoke.py",
]


def main() -> int:
    project_dir = Path(__file__).resolve().parent.parent

    for test_file in TEST_FILES:
        print(f"[regression] running {test_file}")
        result = subprocess.run(
            ["uv", "run", "--with", "pytest", "pytest", test_file],
            cwd=project_dir,
            check=False,
        )
        if result.returncode != 0:
            print(f"[regression] failed: {test_file}")
            return result.returncode

    print("[regression] all smoke checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
