"""Run the real sibling component CLI with the contract test's source paths."""
import json
import runpy
import sys

paths, module, *arguments = sys.argv[1:]
sys.path[:0] = json.loads(paths)
sys.argv = [module, *arguments]
runpy.run_module(module, run_name="__main__")
