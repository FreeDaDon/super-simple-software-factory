"""Keep pytest out of the factory.

`adw_build_test.py` and `adw_plan_build_test.py` match pytest's default `*_test.py`
pattern, so a repo-root `pytest` would import them as test modules (and fail on
their dependencies). Nothing under adws/ is a test of this repo.
"""

collect_ignore_glob = ["*"]
