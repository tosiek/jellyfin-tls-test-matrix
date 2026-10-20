#!/usr/bin/env python3
"""Converts the raw output of `adb shell am instrument -r -w` into a JUnit XML file.

Usage: instrument2junit.py <instrument.txt> <TEST-results.xml> <suite name>
"""
import sys
import xml.etree.ElementTree as ET

source, target, suite_name = sys.argv[1], sys.argv[2], sys.argv[3]

tests = []  # (class, name, status, stack)
current = {}
crashed = None
for line in open(source, errors="replace").read().splitlines():
	if line.startswith("INSTRUMENTATION_STATUS: "):
		key, _, value = line[len("INSTRUMENTATION_STATUS: "):].partition("=")
		current[key] = value
		current["_last"] = key
	elif line.startswith("INSTRUMENTATION_STATUS_CODE: "):
		code = int(line.split(": ", 1)[1])
		if code != 1:  # 1 = test started
			tests.append((current.get("class", "?"), current.get("test", "?"), code, current.get("stack", "")))
		current = {}
	elif line.startswith("INSTRUMENTATION_RESULT: shortMsg=") or "Process crashed" in line:
		crashed = line
	elif current.get("_last") == "stack":
		# Stack traces span multiple lines
		current["stack"] = current.get("stack", "") + "\n" + line

failures = sum(1 for t in tests if t[2] in (-1, -2))
skipped = sum(1 for t in tests if t[2] in (-3, -4))
root = ET.Element("testsuite", name=suite_name, tests=str(len(tests)), failures=str(failures), errors="0",
	skipped=str(skipped))
for cls, name, code, stack in tests:
	case = ET.SubElement(root, "testcase", classname=cls, name=name)
	if code in (-1, -2):
		failure = ET.SubElement(case, "failure", message=stack.strip().split("\n")[0][:300])
		failure.text = stack.strip()
	elif code in (-3, -4):
		ET.SubElement(case, "skipped")
if crashed:
	ET.SubElement(root, "system-err").text = crashed
ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
print(f"{len(tests)} tests, {failures} failed" + (f", {crashed}" if crashed else ""))
