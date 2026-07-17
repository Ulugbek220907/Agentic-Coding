"""
Standalone entry point run as a SEPARATE PROCESS (never imported directly by
the main app) to actually execute one skill function call. This is the
isolation boundary described in core/skills.py: if a skill's code hangs,
crashes, or segfaults, it takes down this short-lived subprocess, not the
main PyQt6 application.

Usage: python skill_runner.py <skill_dir> <entry_file> <func_name> <args_json_path> <output_json_path>

Contract:
- The skill function is called as func(**kwargs) where kwargs come from
  args_json_path.
- Its return value must be JSON-serializable. To surface a generated file
  (e.g. a chart PNG) to the app, return a dict containing an "output_file"
  key with a path (relative to the project root the function was given, or
  absolute).
- Any exception is caught and reported back as {"ok": false, "error": ...}
  rather than crashing with a traceback the app has to parse.
"""
import sys
import json
import importlib.util


def main():
    if len(sys.argv) != 6:
        print(json.dumps({"ok": False, "error": "skill_runner.py called with wrong number of arguments"}))
        sys.exit(1)

    skill_dir, entry_file, func_name, args_json_path, output_json_path = sys.argv[1:6]

    result = {"ok": False, "error": "unknown error"}
    try:
        sys.path.insert(0, skill_dir)

        spec = importlib.util.spec_from_file_location("_skill_entry", f"{skill_dir}/{entry_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, func_name):
            result = {"ok": False, "error": f"'{func_name}' not found in {entry_file}"}
        else:
            func = getattr(module, func_name)
            with open(args_json_path, "r", encoding="utf-8") as f:
                kwargs = json.load(f)
            output = func(**kwargs)
            # Must be JSON-serializable -- if the skill returns something
            # exotic (a numpy array, a matplotlib Figure, etc.) that's a
            # skill-authoring bug, not something we try to guess how to
            # serialize.
            json.dumps(output)
            result = {"ok": True, "result": output}
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(result, f)


if __name__ == "__main__":
    main()